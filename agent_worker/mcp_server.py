"""MCP front door: any MCP client (Hermes, Claude Code, Open WebUI, AI Workspace) can hand a task to a coding agent."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Annotated, Any, Callable, Literal

from mcp import types as mtypes
from mcp.server.mcpserver import Context, MCPServer
from mcp.shared.exceptions import NoBackChannelError
from mcp.server.request_state import RequestStateSecurity
from pydantic import BaseModel, ConfigDict, Field, create_model

from agent_worker import i18n
from agent_worker import tasks as st
from agent_worker.jobs import Job, JobError, JobManager, JobRequest, conversation_from_meta
from agent_worker.mcp_config import DEPTH_HEADER, parse_depth
from agent_worker.settings import TOOL_PROFILES
from agent_worker.workspaces import list_workspaces

logger = logging.getLogger(__name__)

INSTRUCTIONS = (
    "Agent Worker runs coding tasks on a coding agent (Cursor or Claude Code) in a workspace folder on the "
    "worker's machine. Use agent_run for work that needs files, a shell, a repository or several steps. "
    "Send _meta.progressToken with agent_run and the call stays open until the job ends, with "
    "notifications/progress on every step and as keepalive (reset your timeout on progress). Without it, "
    "long tasks return a job_id while still running: poll it with agent_job, stop it with agent_cancel. "
    "agent_jobs lists what runs or waits (also jobs whose job_id you never got). "
    "Statuses follow MCP Tasks: queued, working, input_required, completed, failed, cancelled. "
    "Status input_required means a remote command waits for the user: show it to them and let them decide. "
    "Cancelling an agent_run call (notifications/cancelled) cancels its job unless it was started with "
    "background=true. "
    "auth_status tells which engines are signed in; signing in is up to the user (UI or HTTP API)."
)



# ---- Output schema of agent_run / agent_job / agent_approve (outputSchema, structuredContent) ------------

JobStatus = Literal[
    # MCP Task vocabulary (JOB_STATUS_NAMES=mcp, the default)
    "queued", "working", "input_required", "completed", "failed", "cancelled",
    # names before 0.5.0 (JOB_STATUS_NAMES=legacy)
    "running", "needs_approval", "succeeded",
    # no job: the request was refused, or the job_id is not known
    "rejected", "unknown",
]
ERROR_STATUSES = frozenset({"failed", "cancelled", "rejected", "unknown"})


class _Open(BaseModel):
    # Later versions may add fields; clients should keep what they do not know.
    model_config = ConfigDict(extra="allow")


class TaskView(_Open):
    """The job as an MCP Task object (tasks/get shape)."""

    taskId: str
    status: Literal["working", "input_required", "completed", "failed", "cancelled"]
    statusMessage: str | None = None
    createdAt: str | None = None
    lastUpdatedAt: str | None = None
    ttl: int | None = None
    pollInterval: int | None = Field(default=None, description="Suggested polling interval (ms) while running.")


class PendingApproval(_Open):
    """A remote command that waits for the user's decision."""

    id: str
    tool: str | None = Field(default=None, description="ssh, scp, sftp or rsync")
    host: str | None = None
    user: str | None = None
    hostname: str | None = Field(default=None, description="The real host behind an ssh alias.")
    command: str | None = None


class JobResult(_Open):
    """An agent job: status, and once it ended the agent's answer (result) or what went wrong (error)."""

    status: JobStatus = Field(description="queued/working/input_required while it runs; completed, failed or "
                              "cancelled at the end; rejected/unknown when there is no job (see error).")
    job_id: str | None = Field(default=None, description="Poll it with agent_job, stop it with agent_cancel.")
    legacy_status: str | None = Field(default=None, description="The status name before 0.5.0.")
    status_message: str | None = None
    engine: str | None = None
    workspace: str | None = None
    model: str | None = None
    session: str | None = None
    conversation_id: str | None = None
    background: bool | None = None
    task: TaskView | None = None
    approval: PendingApproval | None = Field(default=None, description="Set while status is input_required.")
    hint: str | None = Field(default=None, description="What to do next.")
    result: str | None = Field(default=None, description="The agent's final answer.")
    error: str | None = None
    changes: dict[str, Any] | None = Field(default=None, description="Changed files and commits in the workspace.")
    usage: dict[str, Any] | None = None
    engines_used: list[str] | None = None
    fallback: list[dict[str, Any]] | None = Field(default=None, description="Every engine try, with why it ended.")
    runtime_seconds: int | None = None
    last_step: str | None = None
    last_event_at: str | None = None
    recent_steps: list[str] | None = None


def tool_result(data: dict[str, Any]) -> mtypes.CallToolResult:
    """A tool answer: structuredContent plus the same JSON as text; isError for a failed, cancelled, refused
    or unknown job (MCP: errors of the tool's work go in the result, not as a protocol error)."""
    return mtypes.CallToolResult(
        content=[mtypes.TextContent(type="text", text=json.dumps(data, indent=2, ensure_ascii=False))],
        structured_content=data,
        is_error=str(data.get("status") or "") in ERROR_STATUSES,
    )


JobAnswer = Annotated[mtypes.CallToolResult, JobResult]


class ApprovalAnswer(BaseModel):
    approve: bool = Field(title="Allow", description="Allow this command on that server?")
    note: str = Field(default="", title="Note", description="Optional note for the agent")


_ANSWER_MODELS: dict[str, type[BaseModel]] = {i18n.DEFAULT: ApprovalAnswer}
_STATE = "agent-worker-approval:"


def _answer_model(lang: str) -> type[BaseModel]:
    """ApprovalAnswer with the field titles and descriptions in lang (the field names stay)."""
    model = _ANSWER_MODELS.get(lang)
    if model is None:
        model = create_model(
            "ApprovalAnswer",
            __base__=BaseModel,
            approve=(bool, Field(title=i18n.t(lang, "approval.allow_title"),
                                 description=i18n.t(lang, "approval.allow_description"))),
            note=(str, Field(default="", title=i18n.t(lang, "approval.note_title"),
                             description=i18n.t(lang, "approval.note_description"))),
        )
        _ANSWER_MODELS[lang] = model
    return model


def _schema(lang: str = i18n.DEFAULT) -> dict[str, Any]:
    """The elicitation's requested schema (2026-07-28 InputRequiredResult), texts in lang."""
    return {
        "type": "object",
        "properties": {
            "approve": {"type": "boolean", "title": i18n.t(lang, "approval.allow_title"),
                        "description": i18n.t(lang, "approval.allow_description")},
            "note": {"type": "string", "title": i18n.t(lang, "approval.note_title"),
                     "description": i18n.t(lang, "approval.note_description")},
        },
        "required": ["approve"],
    }


_SCHEMA = _schema()


def _approval_message(job: Job, approval: dict[str, Any], lang: str = i18n.DEFAULT) -> str:
    target = f"{approval['user']}@{approval['host']}" if approval.get("user") else approval["host"]
    detail = approval.get("command") or i18n.t(lang, "approval.no_command")
    real = approval.get("hostname")
    return i18n.t(
        lang,
        "approval.question",
        job=job.id,
        tool=approval["tool"],
        target=target,
        real=f" ({real})" if real and real != approval["host"] else "",
        detail=detail,
    )


def _lang(ctx: Context | None) -> str:
    """The language for texts to the user: the MCP request's Accept-Language header (English without one)."""
    try:
        headers = ctx.headers if ctx is not None else None
    except Exception:
        headers = None
    return i18n.from_headers(headers)


def _answer_from(result: Any) -> tuple[bool, str] | None:
    """(allow, note) from an elicitation answer, or None when the user dismissed it."""
    action = getattr(result, "action", "")
    if action not in {"accept", "decline"}:
        return None
    if action == "decline":
        return False, "declined"
    data = getattr(result, "data", None)
    content = getattr(result, "content", None)
    if data is not None:
        return bool(getattr(data, "approve", False)), str(getattr(data, "note", "") or "")
    if isinstance(content, dict):
        return bool(content.get("approve")), str(content.get("note") or "")
    return False, ""


def _can_elicit(ctx: Context | None) -> bool:
    try:
        caps = ctx.client_capabilities if ctx is not None else None
    except Exception:
        return False
    return caps is not None and getattr(caps, "elicitation", None) is not None


def _token_ok(token: Any) -> bool:
    return isinstance(token, (str, int)) and not isinstance(token, bool)


def _progress_token(ctx: Context | None) -> Any:
    """The request's _meta.progressToken (None: the client did not ask for progress).

    mcp 2.3.0 keeps the token on the request's dispatch context (JSON-RPC transports: `_progress_token`;
    the in-process client: a progress callback) and drops it from ctx.request_context.meta on 2026-07-28,
    so this looks there first and at the request's _meta second.
    """
    try:
        rc = ctx.request_context  # type: ignore[union-attr]
    except Exception:
        return None
    try:
        dctx = rc.session._request_outbound
    except Exception:
        dctx = None
    for _ in range(4):
        if dctx is None:
            break
        token = getattr(dctx, "_progress_token", None)
        if _token_ok(token):
            return token
        if getattr(dctx, "_on_progress", None) is not None:
            return rc.request_id if _token_ok(rc.request_id) else "in-process"
        dctx = getattr(dctx, "_inner", None)
    for source in (getattr(rc, "meta", None), (getattr(rc, "params", None) or {}).get("_meta")
                   if isinstance(getattr(rc, "params", None), dict) else None):
        if isinstance(source, dict):
            for key in ("progress_token", "progressToken"):
                if _token_ok(source.get(key)):
                    return source[key]
    return None


class _Reporter:
    """notifications/progress for one call. progress must grow with every notification (spec), so it is
    the job's event count, but at least one more than the last value sent."""

    def __init__(self, ctx: Context | None) -> None:
        self.ctx = ctx
        self.lang = _lang(ctx)
        self.last = 0.0
        self.sent_text = ""

    async def send(self, job: Job, message: str, *, final: bool = False) -> None:
        if self.ctx is None:
            return
        value = float(max(len(job.events), self.last + 1))
        try:
            await self.ctx.report_progress(value, value if final else None, f"job {job.id}: {message}"[:300])
        except Exception:
            logger.debug("progress report failed", exc_info=True)
            return
        self.last = value
        self.sent_text = message


def _step_text(job: Job, lang: str = i18n.DEFAULT) -> str:
    """A short line for a progress message: the last step while working, else what the status means."""
    if job.status == st.WORKING:
        step = next(
            (i18n.event_text(e, lang) for e in reversed(job.events) if e.get("type") in {"tool", "status"}),
            "",
        )
        return step or i18n.t(lang, "progress.working")
    return job.status_message(lang=lang)


def _final_text(job: Job, lang: str = i18n.DEFAULT) -> str:
    """The last progress message: the status name (kept for clients that read it), in lang with a label,
    and for a stopped job the reason when the worker knows it."""
    status = job.status
    label = i18n.t(lang, f"status.{status}") if f"status.{status}" in i18n.MESSAGES[i18n.DEFAULT] else status
    text = status if label == status else f"{label} ({status})"
    if job.error_i18n and status != st.COMPLETED and lang != i18n.DEFAULT:
        text += ": " + i18n.render(lang, job.error_i18n)
    return text


def _depth(ctx: Context | None) -> int:
    try:
        headers = ctx.headers if ctx is not None else None
    except Exception:
        headers = None
    if not headers:
        return 0
    return parse_depth(headers.get(DEPTH_HEADER) or headers.get(DEPTH_HEADER.lower()))


def _cancel_kind(ctx: Context | None) -> str:
    """Why an MCP call was cancelled: "cancelled" (the client sent notifications/cancelled), "disconnect"
    (anything else: the connection or session went away), "unknown" (no way to tell).

    mcp 2.3.0 sets cancel_requested on the request's dispatch context for notifications/cancelled but does
    not expose it on the tool Context, so this reads it from the request's session. Without it, the call
    counts as a disconnect.
    """
    try:
        outbound = ctx.request_context.session._request_outbound  # type: ignore[union-attr]
        event = outbound.cancel_requested
    except Exception:
        return "unknown"
    return "cancelled" if event.is_set() else "disconnect"


def job_payload(
    job: Job,
    *,
    recent: int = 0,
    approval_tool: bool = False,
    poll_interval_ms: int | None = None,
    blocked_by: str | None = None,
) -> dict[str, Any]:
    message = job.status_message(blocked_by)
    out: dict[str, Any] = {
        "job_id": job.id,
        "status": job.public_status(),
        "legacy_status": st.legacy(job.status),
        "status_message": message,
        "engine": job.engine,
        "workspace": job.workspace,
        # The model the job runs on: the one asked for, else the engine's default. None: Codex picks
        # (CODEX_MODEL unset); the real one shows once the job has run.
        "model": job.model or None,
    }
    if job.session:
        out["session"] = job.session
    if job.conversation_id:
        out["conversation_id"] = job.conversation_id
    if job.background:
        out["background"] = True
    # The same job as an MCP Task object (tasks/get shape), for clients that already speak Tasks.
    out["task"] = st.task_view(job, poll_interval_ms=poll_interval_ms, status_message=message)
    if job.status == st.INPUT_REQUIRED and job.pending_approval:
        out["approval"] = job.pending_approval
        out["hint"] = (
            "The agent is paused until a human approves this remote command. Show it to the user and ask. "
            "Do not decide yourself. The user answers with POST /jobs/{id}/approve on the worker"
            + (" or you pass their answer to agent_approve" if approval_tool else "")
            + "; then call agent_job to wait for the result."
        ).replace("{id}", job.id)
        return out
    if job.done:
        if job.result:
            out["result"] = job.result
        if job.error:
            out["error"] = job.error
        if job.changes:
            out["changes"] = job.changes
        if job.usage:
            out["usage"] = job.usage
        if job.attempts:
            # Engine fallback: which engines ran it, and why each one stopped.
            out["engines_used"] = job.engines_used()
            if len(job.attempts) > 1:
                out["fallback"] = job.attempts
    else:
        out["hint"] = "Still running. Call agent_job with this job_id to wait for the result."
        out.update(job.live())
        if recent:
            steps = [
                str(e.get("text") or e.get("message") or "")
                for e in job.events
                if e.get("type") in {"tool", "status", "error"}
            ]
            out["recent_steps"] = [s for s in steps if s][-recent:]
    return out


PARALLELISM = (
    "Jobs in different workspaces run in parallel, up to MAX_CONCURRENT_JOBS at once. Jobs in the same "
    "workspace that may touch files (tools full, local, offline) run one after another; chat-only jobs "
    "(tools mcp, none) do not wait for them. Jobs with the same session always take turns."
)

_ENGINE_LABELS = {"cursor": "cursor (Cursor SDK)", "claude": "claude (Claude Code)", "codex": "codex (OpenAI Codex)"}


def _engines_note(engine) -> str:
    """Which engines this worker has, for the agent_run description. Set up at start; signing in later works too."""
    engines = getattr(engine, "engines", None) or {"cursor": engine}
    default = engine.default() if hasattr(engine, "default") else "cursor"
    names = [_ENGINE_LABELS.get(name, name) + (" (default)" if name == default else "") for name in engines]
    return "engine picks the agent: " + ", ".join(names) + ". Leave it empty for the default."


def build_mcp(manager: JobManager, auth_status: Callable[[], dict[str, Any]] | None = None) -> MCPServer:
    """auth_status: the read-only sign-in overview (without links or codes); default: built from the engine."""
    settings = manager.settings
    if auth_status is None:
        from agent_worker.auth import auth_status as overview

        def auth_status() -> dict[str, Any]:
            return overview(settings, manager.engine, reveal=False)

    from agent_worker.version import VERSION

    server = MCPServer(
        name="agent-worker",
        title="Agent Worker",
        version=VERSION,
        instructions=INSTRUCTIONS,
        # The sealed requestState of a 2026-07-28 approval question (InputRequiredResult) must outlive the
        # question: the SDK's default of 600 s is shorter than APPROVAL_TIMEOUT_SECONDS, and a user who
        # answered later got a 400 "Invalid or expired requestState".
        request_state_security=RequestStateSecurity.ephemeral(ttl=float(settings.approval_timeout + 300)),
    )
    engines_note = _engines_note(manager.engine)
    if manager.fallback_on:
        chain = " -> ".join(name for name, _ in settings.fallback_engines)
        engines_note += (
            f" Fallback chain: {chain}. On a provider failure (plan limit, 429, expired sign-in, outage) the same "
            "job goes on with the next engine below the one it started on (engines_used in the result); with "
            "no engine left it waits (queued) and resumes on its own."
        )

    async def _progress(ctx: Context | None, job: Job, reporter: _Reporter | None = None) -> None:
        # The job_id goes with every progress message: a client that gives up waiting has seen it.
        reporter = reporter or _Reporter(ctx)
        await reporter.send(job, _step_text(job, reporter.lang))

    def _apply(job: Job, decided: tuple[bool, str]) -> bool:
        try:
            manager.decide(job.id, *decided)
        except JobError:
            return False
        return True

    async def _ask_human(
        job: Job, ctx: Context | None, reporter: _Reporter | None = None
    ) -> bool | mtypes.InputRequiredResult:
        """Ask the user through the MCP client (elicitation), never the calling model.

        True once decided. Older protocol versions get a server-initiated request on the open
        stream; 2026-07-28 clients get an InputRequiredResult and retry the call with the answer.
        With a reporter (agent_run with a progressToken) the call sends keepalive progress while the
        question is open, and stops asking when the approval is answered another way (HTTP, agent_approve).
        """
        approval = job.pending_approval
        if approval is None or not _can_elicit(ctx):
            return False
        lang = _lang(ctx)
        message = _approval_message(job, approval, lang)
        asking = asyncio.ensure_future(
            asyncio.wait_for(ctx.elicit(message, _answer_model(lang)), settings.approval_timeout))
        try:
            while True:
                done, _ = await asyncio.wait({asking}, timeout=float(settings.mcp_progress_interval))
                if done:
                    break
                pending = job.pending_approval or {}
                if job.done or pending.get("id") != approval["id"]:
                    # Answered elsewhere (or the job ended): the question is moot.
                    asking.cancel()
                    return True
                if reporter is not None:
                    await reporter.send(job, i18n.t(reporter.lang, "progress.waiting_answer"))
            answer = asking.result()
        except NoBackChannelError:
            return mtypes.InputRequiredResult(
                input_requests={
                    f"approve-{approval['id']}": mtypes.ElicitRequest(
                        params=mtypes.ElicitRequestFormParams(message=message, requested_schema=_schema(lang))
                    )
                },
                request_state=_STATE + json.dumps({"job": job.id, "approval": approval["id"]}),
            )
        except asyncio.CancelledError:
            asking.cancel()
            raise
        except Exception:
            logger.info("elicitation failed for job %s", job.id, exc_info=True)
            return False
        decided = _answer_from(answer)
        if decided is None or not _apply(job, decided):
            return False
        await asyncio.sleep(0)
        return True

    async def _resume(ctx: Context | None) -> Job | None:
        """On a retry after InputRequiredResult: apply the user's answer, return the job it belongs to."""
        try:
            state = ctx.request_state if ctx is not None else None
        except Exception:
            state = None
        if not state or not state.startswith(_STATE):
            return None
        try:
            ref = json.loads(state[len(_STATE) :])
        except ValueError:
            return None
        job = manager.get(str(ref.get("job") or ""))
        if job is None:
            return None
        response = (ctx.input_responses or {}).get(f"approve-{ref.get('approval')}")
        pending = job.pending_approval or {}
        if response is not None and pending.get("id") == ref.get("approval"):
            decided = _answer_from(response)
            if decided is not None and _apply(job, decided):
                await asyncio.sleep(0)
        return job

    async def _wait(
        job: Job, seconds: float, ctx: Context | None, reporter: _Reporter | None = None
    ) -> mtypes.InputRequiredResult | None:
        """Wait in slices so the client sees progress; stop to ask when the job needs approval."""
        cap = float(settings.mcp_max_wait)
        left = max(0.0, min(float(seconds), cap))
        reporter = reporter or _Reporter(ctx)
        while not job.done:
            if job.status == st.INPUT_REQUIRED:
                asked = await _ask_human(job, ctx)
                if isinstance(asked, mtypes.InputRequiredResult):
                    return asked
                if not asked:
                    break
                left = max(left, min(float(settings.mcp_wait_seconds), cap))
                continue
            if left <= 0:
                break
            step = min(5.0, left)
            await manager.wait(job, step, stop_on_approval=True)
            left -= step
            await _progress(ctx, job, reporter)
        return None

    async def _follow(job: Job, ctx: Context | None) -> mtypes.InputRequiredResult | None:
        """agent_run with a progressToken: stay with the job until it ends (MCP progress, no wait cap).

        A notification goes out whenever the status or the last step changes and at least every
        MCP_PROGRESS_INTERVAL_SECONDS as keepalive. The job's own watchdogs (JOB_MAX_RUNTIME_SECONDS,
        JOB_IDLE_TIMEOUT_SECONDS, APPROVAL_TIMEOUT_SECONDS) still end it. An approval is asked once by
        elicitation when the client can; otherwise (or when the user dismissed it) the progress message
        says what the job waits for and the other approval paths stay open.
        """
        reporter = _Reporter(ctx)
        interval = float(settings.mcp_progress_interval)
        loop = asyncio.get_running_loop()
        asked: set[str] = set()
        lang = reporter.lang
        await reporter.send(job, _step_text(job, lang))
        sent_at = loop.time()
        while not job.done and not manager.closing:
            approval = job.pending_approval if job.status == st.INPUT_REQUIRED else None
            if approval is not None and approval.get("id") not in asked:
                asked.add(approval.get("id"))
                if _can_elicit(ctx):
                    what = _step_text(job, lang).removeprefix("input_required: ")
                    await reporter.send(job, i18n.t(lang, "progress.asking", what=what))
                    answer = await _ask_human(job, ctx, reporter)
                    if isinstance(answer, mtypes.InputRequiredResult):
                        return answer
                    sent_at = loop.time()
                    if answer:
                        continue
                await reporter.send(job, _step_text(job, lang))
                sent_at = loop.time()
            job.changed.clear()
            # Anything new since the last notification (it may have come while that one was sent)?
            if _step_text(job, lang) == reporter.sent_text and not job.done and job.pending_approval is approval:
                left = max(0.1, interval - (loop.time() - sent_at))
                try:
                    await asyncio.wait_for(job.changed.wait(), left)
                except asyncio.TimeoutError:
                    pass
            if job.done:
                break
            text = _step_text(job, lang)
            if text != reporter.sent_text or loop.time() - sent_at >= interval:
                await reporter.send(job, text)
                sent_at = loop.time()
        if job.done:
            await reporter.send(job, _final_text(job, lang), final=True)
        return None

    def payload(job: Job, recent: int = 0) -> dict[str, Any]:
        return job_payload(
            job,
            recent=recent,
            approval_tool=settings.approval_tool,
            poll_interval_ms=settings.task_poll_interval_ms,
            blocked_by=manager.blocked_by(job),
        )

    def _on_call_cancelled(job: Job | None, ctx: Context | None) -> None:
        """The agent_run call ended early: the client cancelled it or went away. Cancel its job (policy below)."""
        if job is None or job.done or manager.closing:
            return
        kind = _cancel_kind(ctx)
        if job.background:
            logger.info("job %s: agent_run call %s, job keeps running (background=true)", job.id, kind)
            return
        if kind == "cancelled":
            if not settings.mcp_cancel_on_cancelled:
                logger.info("job %s: agent_run call cancelled, job keeps running (MCP_CANCEL_ON_CANCELLED=false)", job.id)
                return
            reason = i18n.msg("reason.mcp_cancelled")
        else:
            if not settings.mcp_cancel_on_disconnect:
                logger.info("job %s: agent_run call ended (%s), job keeps running (MCP_CANCEL_ON_DISCONNECT=false)",
                            job.id, kind)
                return
            # On 2026-07-28 HTTP a client cancel is a closed request stream, so it lands here too.
            reason = i18n.msg("reason.mcp_disconnect")
        logger.info("job %s: %s", job.id, i18n.english(reason))
        manager.cancel(job.id, reason)

    def _conversation(ctx: Context | None, explicit: str) -> str:
        if explicit and explicit.strip():
            return explicit
        try:
            meta = ctx.request_context.meta if ctx is not None else None
        except Exception:
            meta = None
        return conversation_from_meta(meta)

    @server.tool(
        title="Run a coding agent task",
        annotations=mtypes.ToolAnnotations(
            title="Run a coding agent task", read_only_hint=False, destructive_hint=True,
            idempotent_hint=False, open_world_hint=True),
        description=(
            "Give a task to a coding agent. It can read and edit files in the workspace, run a shell, "
            "use git, ssh to servers, and call MCP servers. Returns the agent's final answer plus changed files "
            "and commits. If the task takes longer than wait_seconds (at most "
            f"{settings.mcp_max_wait}), returns status 'working' (or 'queued') and a job_id for agent_job"
            + ("; with _meta.progressToken the call instead stays open until the job ends and sends "
               "notifications/progress (approvals are asked by elicitation when the client supports it). "
               if settings.mcp_wait_with_progress else ". ")
            + 
            "Cancelling this call while it is open cancels the job; background=true keeps the job running "
            "after a cancelled call or a disconnect (stop it with agent_cancel). conversation_id (or _meta "
            "'io.github.domdus/conversation_id' / 'contextId') links the job to the client's conversation: an "
            f"opaque id, never a secret. tools narrows what the agent may use: one of {', '.join(TOOL_PROFILES)} "
            "(full = everything, local = no web search/fetch, offline = no web and no shell, mcp = only MCP tools, "
            "none = chat only). Use the same session for follow-ups that should remember the earlier turns. "
            + engines_note
        )
    )
    async def agent_run(
        task: str,
        workspace: str = "",
        session: str = "",
        model: str = "",
        tools: str = "",
        mcp_servers: dict[str, Any] | None = None,
        wait_seconds: int | None = None,
        engine: str = "",
        background: bool = False,
        conversation_id: str = "",
        ctx: Context | None = None,
    ) -> JobAnswer | mtypes.InputRequiredResult:
        job: Job | None = None
        reporter = _Reporter(ctx)
        follow = settings.mcp_wait_with_progress and _progress_token(ctx) is not None
        try:
            job = await _resume(ctx)
            if job is None:
                try:
                    job = manager.submit(
                        JobRequest(
                            task=task,
                            workspace=workspace,
                            session=session,
                            model=model,
                            tools=tools,
                            mcp_servers=mcp_servers,
                            depth=_depth(ctx),
                            origin="mcp",
                            engine=engine,
                            conversation_id=_conversation(ctx, conversation_id),
                            background=background,
                        )
                    )
                except JobError as exc:
                    return tool_result({"status": "rejected", "error": str(exc)})
                # Known before any waiting: in the log, and in the first progress message to the client.
                logger.info("agent_run job_id=%s", job.id)
                if not follow:
                    await _progress(ctx, job, reporter)
            if follow:
                asked = await _follow(job, ctx)
            else:
                wait = settings.mcp_wait_seconds if wait_seconds is None else wait_seconds
                asked = await _wait(job, wait, ctx, reporter)
            return asked if asked is not None else tool_result(payload(job, recent=5))
        except asyncio.CancelledError:
            # MCP cancellation: the SDK cancels this handler on notifications/cancelled for the call, and on
            # transports that notice it (2026-07-28 HTTP), when the client drops the connection.
            _on_call_cancelled(job, ctx)
            raise

    @server.tool(
        title="Agent job status and result",
        annotations=mtypes.ToolAnnotations(
            title="Agent job status and result", read_only_hint=True, idempotent_hint=True, open_world_hint=False),
        description=(
            "Status and result of an agent job. wait_seconds waits for the job to finish "
            f"(at most {settings.mcp_max_wait}) before answering; call again while it runs."
        )
    )
    async def agent_job(
        job_id: str, wait_seconds: int = 0, ctx: Context | None = None
    ) -> JobAnswer | mtypes.InputRequiredResult:
        job = await _resume(ctx) or manager.get(job_id)
        if job is None:
            return tool_result({"status": "unknown", "error": f"No job {job_id!r}."})
        asked = await _wait(job, wait_seconds, ctx)
        return asked if asked is not None else tool_result(payload(job, recent=5))

    @server.tool(
        title="List agent jobs",
        annotations=mtypes.ToolAnnotations(
            title="List agent jobs", read_only_hint=True, idempotent_hint=True, open_world_hint=False),
        description=(
            "List agent jobs: by default every queued, working or input_required job plus the last 20 that "
            "ended. Each row: job_id, workspace, engine, model, status and status_message, conversation_id, "
            "times, runtime, last step, whether it waits for an approval (and for what) and, for a queued job, "
            "which job blocks it. Use it to find a job_id you lost or to see why a workspace is busy. status "
            "filters (comma-separated, e.g. 'working,queued'; the old names running, needs_approval, succeeded "
            "work too); workspace and conversation_id filter by those."
        ),
    )
    async def agent_jobs(
        workspace: str = "", status: str = "", finished: int = 20, conversation_id: str = ""
    ) -> dict[str, Any]:
        rows = manager.overview(workspace, status, finished, conversation_id)
        return {"jobs": [manager.describe(job) for job in rows]}

    @server.tool(
        title="Cancel agent jobs",
        annotations=mtypes.ToolAnnotations(
            title="Cancel agent jobs", read_only_hint=False, destructive_hint=True, idempotent_hint=True,
            open_world_hint=False),
        description=(
            "Stop agent jobs: one by job_id, every queued and working job in a workspace (workspace=...), or "
            "every one of a client conversation (conversation_id=...). The engine and everything it started are "
            "stopped and the workspace is free for the next job."
        )
    )
    async def agent_cancel(
        job_id: str = "", workspace: str = "", conversation_id: str = ""
    ) -> Annotated[mtypes.CallToolResult, dict[str, Any]]:
        # isError only when nothing could be cancelled; a job this call cancelled is the tool doing its job.
        if not job_id and not workspace and not conversation_id.strip():
            return tool_result({"status": "rejected", "error": "Give a job_id, a workspace or a conversation_id."})
        if job_id:
            job = manager.cancel(job_id)
            if job is None:
                return tool_result({"status": "unknown", "error": f"No job {job_id!r}."})
            await manager.wait(job, min(15, settings.mcp_max_wait))
            data = payload(job)
            return mtypes.CallToolResult(
                content=tool_result(data).content, structured_content=data, is_error=False)
        if conversation_id.strip():
            jobs = manager.cancel_conversation(
                conversation_id, i18n.msg("reason.cancelled_via", via="agent_cancel conversation_id"))
            await asyncio.gather(*(manager.wait(job, min(15, settings.mcp_max_wait)) for job in jobs))
            return tool_result({"conversation_id": conversation_id.strip(), "cancelled": [payload(job) for job in jobs]})
        jobs = manager.cancel_workspace(workspace)
        await asyncio.gather(*(manager.wait(job, min(15, settings.mcp_max_wait)) for job in jobs))
        return tool_result({
            "workspace": (workspace or "").strip().lower() or settings.default_workspace,
            "cancelled": [payload(job) for job in jobs],
        })

    if settings.approval_tool:

        @server.tool(
            title="Pass the user's approval answer",
            annotations=mtypes.ToolAnnotations(
                title="Pass the user's approval answer", read_only_hint=False, destructive_hint=True,
                idempotent_hint=False, open_world_hint=True),
            description=(
                "Pass the USER's answer to a job waiting for approval (status input_required). Only call this "
                "after asking the user and getting their explicit answer; never decide on your own."
            )
        )
        async def agent_approve(
            job_id: str, approve: bool, note: str = "", wait_seconds: int | None = None, ctx: Context | None = None
        ) -> JobAnswer | mtypes.InputRequiredResult:
            try:
                job = manager.decide(job_id, approve, note)
            except JobError as exc:
                return tool_result({"status": "rejected", "error": str(exc)})
            await asyncio.sleep(0)
            wait = settings.mcp_wait_seconds if wait_seconds is None else wait_seconds
            asked = await _wait(job, wait, ctx)
            return asked if asked is not None else tool_result(payload(job, recent=5))

    @server.tool(
        name="auth_status",
        title="Engine sign-in status",
        annotations=mtypes.ToolAnnotations(
            title="Engine sign-in status", read_only_hint=True, idempotent_hint=True, open_world_hint=False),
        description=(
            "Read-only: which engines are signed in and ready (credential source, plan when known, whether a "
            "sign-in is waiting). Signing in is done by the user in a UI or over the worker's HTTP API, not here."
        )
    )
    async def auth_status_tool() -> dict[str, Any]:
        return auth_status()

    @server.tool(
        title="List workspaces",
        annotations=mtypes.ToolAnnotations(
            title="List workspaces", read_only_hint=True, idempotent_hint=True, open_world_hint=False),
        description="Workspaces the agents can use, and which one is the default.",
    )
    async def agent_workspaces() -> dict[str, Any]:
        return {
            "workspaces": list_workspaces(settings),
            "create_on_demand": settings.create_workspaces,
            "default_models": manager.default_models(),
            "parallelism": PARALLELISM,
        }

    return server
