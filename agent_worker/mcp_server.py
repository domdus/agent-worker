"""MCP front door: any MCP client (Hermes, Claude Code, Open WebUI, AI Workspace) can hand a task to a coding agent."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable

from mcp import types as mtypes
from mcp.server.mcpserver import Context, MCPServer
from mcp.shared.exceptions import NoBackChannelError
from pydantic import BaseModel, Field

from agent_worker import tasks as st
from agent_worker.jobs import Job, JobError, JobManager, JobRequest, conversation_from_meta
from agent_worker.mcp_config import DEPTH_HEADER, parse_depth
from agent_worker.settings import TOOL_PROFILES
from agent_worker.workspaces import list_workspaces

logger = logging.getLogger(__name__)

INSTRUCTIONS = (
    "Agent Worker runs coding tasks on a coding agent (Cursor or Claude Code) in a workspace folder on the "
    "worker's machine. Use agent_run for work that needs files, a shell, a repository or several steps. "
    "Long tasks return a job_id while still running: poll it with agent_job, stop it with agent_cancel. "
    "agent_jobs lists what runs or waits (also jobs whose job_id you never got). "
    "Statuses follow MCP Tasks: queued, working, input_required, completed, failed, cancelled. "
    "Status input_required means a remote command waits for the user: show it to them and let them decide. "
    "Cancelling an agent_run call (notifications/cancelled) cancels its job unless it was started with "
    "background=true. "
    "auth_status tells which engines are signed in; signing in is up to the user (UI or HTTP API)."
)



class ApprovalAnswer(BaseModel):
    approve: bool = Field(description="Allow this command on that server?")
    note: str = Field(default="", description="Optional note for the agent")


_STATE = "agent-worker-approval:"
_SCHEMA = {
    "type": "object",
    "properties": {
        "approve": {"type": "boolean", "title": "Allow", "description": "Allow this command on that server?"},
        "note": {"type": "string", "title": "Note", "description": "Optional note for the agent"},
    },
    "required": ["approve"],
}


def _approval_message(job: Job, approval: dict[str, Any]) -> str:
    target = f"{approval['user']}@{approval['host']}" if approval.get("user") else approval["host"]
    detail = approval.get("command") or "(interactive session or file transfer)"
    real = approval.get("hostname")
    return (
        f"Agent job {job.id} wants to run {approval['tool']} on {target}"
        + (f" ({real})" if real and real != approval["host"] else "")
        + f":\n\n{detail}\n\nAllow it?"
    )


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

    server = MCPServer(name="agent-worker", instructions=INSTRUCTIONS)
    engines_note = _engines_note(manager.engine)
    if manager.fallback_on:
        chain = " -> ".join(name for name, _ in settings.fallback_engines)
        engines_note += (
            f" Fallback chain: {chain}. On a provider failure (plan limit, 429, expired sign-in, outage) the same "
            "job goes on with the next engine below the one it started on (engines_used in the result); with "
            "no engine left it waits (queued) and resumes on its own."
        )

    async def _progress(ctx: Context | None, job: Job) -> None:
        if ctx is None:
            return
        try:
            last = next(
                (str(e.get("text") or "") for e in reversed(job.events) if e.get("type") in {"tool", "status"}),
                "",
            )
            # The job_id goes with every progress message: a client that gives up waiting has seen it.
            await ctx.report_progress(len(job.events), None, f"job {job.id}: {last or job.status}")
        except Exception:
            logger.debug("progress report failed", exc_info=True)

    def _apply(job: Job, decided: tuple[bool, str]) -> bool:
        try:
            manager.decide(job.id, *decided)
        except JobError:
            return False
        return True

    async def _ask_human(job: Job, ctx: Context | None) -> bool | mtypes.InputRequiredResult:
        """Ask the user through the MCP client (elicitation), never the calling model.

        True once decided. Older protocol versions get a server-initiated request on the open
        stream; 2026-07-28 clients get an InputRequiredResult and retry the call with the answer.
        """
        approval = job.pending_approval
        if approval is None or not _can_elicit(ctx):
            return False
        message = _approval_message(job, approval)
        try:
            answer = await asyncio.wait_for(ctx.elicit(message, ApprovalAnswer), settings.approval_timeout)
        except NoBackChannelError:
            return mtypes.InputRequiredResult(
                input_requests={
                    f"approve-{approval['id']}": mtypes.ElicitRequest(
                        params=mtypes.ElicitRequestFormParams(message=message, requested_schema=_SCHEMA)
                    )
                },
                request_state=_STATE + json.dumps({"job": job.id, "approval": approval["id"]}),
            )
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

    async def _wait(job: Job, seconds: float, ctx: Context | None) -> mtypes.InputRequiredResult | None:
        """Wait in slices so the client sees progress; stop to ask when the job needs approval."""
        cap = float(settings.mcp_max_wait)
        left = max(0.0, min(float(seconds), cap))
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
            await _progress(ctx, job)
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
            reason = "Cancelled by the MCP client (notifications/cancelled for the agent_run call)."
        else:
            if not settings.mcp_cancel_on_disconnect:
                logger.info("job %s: agent_run call ended (%s), job keeps running (MCP_CANCEL_ON_DISCONNECT=false)",
                            job.id, kind)
                return
            # On 2026-07-28 HTTP a client cancel is a closed request stream, so it lands here too.
            reason = ("Cancelled: the MCP client closed the agent_run call while it was open "
                      "(disconnected, or cancelled it on a transport without notifications/cancelled).")
        logger.info("job %s: %s", job.id, reason)
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
        description=(
            "Give a task to a coding agent. It can read and edit files in the workspace, run a shell, "
            "use git, ssh to servers, and call MCP servers. Returns the agent's final answer plus changed files "
            "and commits. If the task takes longer than wait_seconds (at most "
            f"{settings.mcp_max_wait}), returns status 'working' (or 'queued') and a job_id for agent_job. "
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
    ) -> dict[str, Any]:
        job: Job | None = None
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
                    return {"status": "rejected", "error": str(exc)}
                # Known before any waiting: in the log, and in the first progress message to the client.
                logger.info("agent_run job_id=%s", job.id)
                await _progress(ctx, job)
            wait = settings.mcp_wait_seconds if wait_seconds is None else wait_seconds
            asked = await _wait(job, wait, ctx)
            return asked if asked is not None else payload(job, recent=5)
        except asyncio.CancelledError:
            # MCP cancellation: the SDK cancels this handler on notifications/cancelled for the call, and on
            # transports that notice it (2026-07-28 HTTP), when the client drops the connection.
            _on_call_cancelled(job, ctx)
            raise

    @server.tool(
        description=(
            "Status and result of an agent job. wait_seconds waits for the job to finish "
            f"(at most {settings.mcp_max_wait}) before answering; call again while it runs."
        )
    )
    async def agent_job(job_id: str, wait_seconds: int = 0, ctx: Context | None = None) -> dict[str, Any]:
        job = await _resume(ctx) or manager.get(job_id)
        if job is None:
            return {"status": "unknown", "error": f"No job {job_id!r}."}
        asked = await _wait(job, wait_seconds, ctx)
        return asked if asked is not None else payload(job, recent=5)

    @server.tool(
        annotations=mtypes.ToolAnnotations(read_only_hint=True, idempotent_hint=True),
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
        description=(
            "Stop agent jobs: one by job_id, every queued and working job in a workspace (workspace=...), or "
            "every one of a client conversation (conversation_id=...). The engine and everything it started are "
            "stopped and the workspace is free for the next job."
        )
    )
    async def agent_cancel(job_id: str = "", workspace: str = "", conversation_id: str = "") -> dict[str, Any]:
        if not job_id and not workspace and not conversation_id.strip():
            return {"status": "rejected", "error": "Give a job_id, a workspace or a conversation_id."}
        if job_id:
            job = manager.cancel(job_id)
            if job is None:
                return {"status": "unknown", "error": f"No job {job_id!r}."}
            await manager.wait(job, min(15, settings.mcp_max_wait))
            return payload(job)
        if conversation_id.strip():
            jobs = manager.cancel_conversation(conversation_id, "Cancelled (agent_cancel conversation_id).")
            await asyncio.gather(*(manager.wait(job, min(15, settings.mcp_max_wait)) for job in jobs))
            return {"conversation_id": conversation_id.strip(), "cancelled": [payload(job) for job in jobs]}
        jobs = manager.cancel_workspace(workspace)
        await asyncio.gather(*(manager.wait(job, min(15, settings.mcp_max_wait)) for job in jobs))
        return {
            "workspace": (workspace or "").strip().lower() or settings.default_workspace,
            "cancelled": [payload(job) for job in jobs],
        }

    if settings.approval_tool:

        @server.tool(
            description=(
                "Pass the USER's answer to a job waiting for approval (status input_required). Only call this "
                "after asking the user and getting their explicit answer; never decide on your own."
            )
        )
        async def agent_approve(
            job_id: str, approve: bool, note: str = "", wait_seconds: int | None = None, ctx: Context | None = None
        ) -> dict[str, Any]:
            try:
                job = manager.decide(job_id, approve, note)
            except JobError as exc:
                return {"status": "rejected", "error": str(exc)}
            await asyncio.sleep(0)
            wait = settings.mcp_wait_seconds if wait_seconds is None else wait_seconds
            asked = await _wait(job, wait, ctx)
            return asked if asked is not None else payload(job, recent=5)

    @server.tool(
        name="auth_status",
        annotations=mtypes.ToolAnnotations(read_only_hint=True, idempotent_hint=True),
        description=(
            "Read-only: which engines are signed in and ready (credential source, plan when known, whether a "
            "sign-in is waiting). Signing in is done by the user in a UI or over the worker's HTTP API, not here."
        )
    )
    async def auth_status_tool() -> dict[str, Any]:
        return auth_status()

    @server.tool(description="Workspaces the agents can use, and which one is the default.")
    async def agent_workspaces() -> dict[str, Any]:
        return {
            "workspaces": list_workspaces(settings),
            "create_on_demand": settings.create_workspaces,
            "default_models": manager.default_models(),
            "parallelism": PARALLELISM,
        }

    return server
