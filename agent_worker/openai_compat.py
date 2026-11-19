"""OpenAI-compatible chat endpoint, so Open WebUI, LibreChat, Hermes (or any OpenAI client) can pick Cursor,
Claude Code or Codex as a model.

Each request becomes a job. The newest user message is the task; earlier turns are pasted as context
only when the agent is new. With "stream": true the reply is an OpenAI chat.completion.chunk stream fed
by the job's live events: answer text as delta.content, agent steps as delta.reasoning_content (Open WebUI
and Hermes show it as thinking; OPENAI_STEPS / X-Agent-Steps change or drop it), a keepalive comment
while the agent is quiet, a usage chunk if stream_options.include_usage, then [DONE]. A failed job ends
with an OpenAI error chunk before [DONE]. A client that goes away cancels the job.

JSON mode: a request with response_format {"type": "json_schema"} or {"type": "json_object"} runs with tool
profile "none" (no header can widen it), the agent is told to answer with only JSON (the schema included),
and the answer is cleaned up (code fences stripped, else the first JSON object/array taken). No JSON in the
answer is an OpenAI error (502), never prose. A stream is buffered and sends the JSON as one chunk.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, AsyncIterator, Awaitable, Callable

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from agent_worker import cursor_auth
from agent_worker.images import parse_image
from agent_worker.jobs import Job, JobError, JobManager, JobRequest
from agent_worker.mcp_config import DEPTH_HEADER, parse_depth
from agent_worker.settings import steps_mode
from agent_worker.tasks import CANCELLED, COMPLETED, INPUT_REQUIRED
from agent_worker.workspaces import WorkspaceError, resolve

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1")

# A comment line after this many quiet seconds keeps proxies and clients from timing out the stream.
KEEPALIVE_SECONDS = 15.0
# Non-stream requests: how often to check whether the client is still there.
DISCONNECT_POLL_SECONDS = 2.0

_CONTEXT_CHARS = 60_000
# Where a chat's id may come from: Open WebUI (ENABLE_FORWARD_USER_INFO_HEADERS), LibreChat and others
# (a header with the conversation id), or the request body.
_SESSION_HEADERS = ("x-openwebui-chat-id", "x-session-id", "x-agent-session", "x-conversation-id", "x-chat-id")
_SESSION_FIELDS = ("session_id", "conversation_id", "chat_id")
_NOT_A_SESSION = {"new", "null", "none", "undefined", "false", "0"}
_YES = {"approve", "approved", "yes", "y", "ok", "okay", "allow", "go", "go ahead", "continue", "ja", "jawohl"}
_NO = {"deny", "denied", "no", "n", "stop", "cancel", "abort", "nein"}


def decision_word(text: str) -> bool | None:
    word = " ".join(str(text or "").strip().lower().strip(".!").split())
    if word in _YES:
        return True
    if word in _NO:
        return False
    return None


def approval_prompt(job: Job) -> str:
    item = job.pending_approval or {}
    target = f"{item.get('user')}@{item.get('host')}" if item.get("user") else str(item.get("host") or "?")
    command = str(item.get("command") or "").strip() or "(interactive session or file transfer)"
    return (
        f"\n\n**Approval needed:** `{item.get('tool', 'ssh')}` on **{target}**\n\n```\n{command}\n```\n\n"
        "Reply **approve** to run it or **deny** to stop."
    )


def split_model(raw: str) -> tuple[str, str]:
    """'composer-2.5@my-repo' -> ('composer-2.5', 'my-repo'). A bare name uses the default workspace."""
    name = str(raw or "").strip()
    if name.startswith("cursor/"):
        name = name[len("cursor/") :]
    model, _, workspace = name.partition("@")
    if model in {"", "cursor", "agent-worker"}:
        model = ""
    return model, workspace


def model_engine(raw: str) -> tuple[str, str, str]:
    """(engine, model, workspace): 'claude-code/claude-opus-5-5@repo' -> ('claude', 'claude-opus-5-5', 'repo')."""
    from agent_worker.engines import split_engine

    model, workspace = split_model(raw)
    name = str(raw or "").strip().lower()
    if name.startswith("cursor/") or name.partition("@")[0] == "cursor":
        # Named Cursor explicitly: not DEFAULT_ENGINE, even if that is another one.
        return "cursor", model, workspace
    engine, model = split_engine(model)
    if engine and model in {"", "claude", "claude-code", "codex", "default"}:
        model = ""
    return engine, model, workspace


def _text_and_images(content: Any) -> tuple[str, list[dict[str, str]]]:
    if isinstance(content, str):
        return content, []
    texts: list[str] = []
    images: list[dict[str, str]] = []
    for part in content or []:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind == "text":
            texts.append(str(part.get("text") or ""))
        elif kind == "image_url":
            url = part.get("image_url")
            url = url.get("url") if isinstance(url, dict) else url
            parsed = parse_image(str(url or "")) if str(url or "").startswith("data:") else None
            if parsed:
                images.append(parsed)
    return "\n".join(t for t in texts if t), images


def messages_to_request(messages: list[dict[str, Any]]) -> tuple[str, str, str, list[dict[str, str]]]:
    """(task, context, instructions, images) from an OpenAI messages list."""
    system: list[str] = []
    turns: list[tuple[str, str]] = []
    images: list[dict[str, str]] = []
    last_user = -1
    for item in messages:
        role = str(item.get("role") or "")
        text, found = _text_and_images(item.get("content"))
        if role in {"system", "developer"}:
            if text.strip():
                system.append(text.strip())
            continue
        if role not in {"user", "assistant"}:
            continue
        turns.append((role, text))
        if role == "user":
            last_user = len(turns) - 1
            images = found
    if last_user < 0:
        raise HTTPException(status_code=400, detail="messages needs at least one user message.")
    task = turns[last_user][1].strip()
    earlier = turns[:last_user]
    lines = [f"{'User' if role == 'user' else 'Assistant'}: {text.strip()}" for role, text in earlier if text.strip()]
    context = "\n\n".join(lines)
    if len(context) > _CONTEXT_CHARS:
        context = "…" + context[-_CONTEXT_CHARS:]
    return task, context, "\n\n".join(system), images


# ---- JSON mode (response_format) ---------------------------------------------------------------

JSON_FORMATS = ("json_schema", "json_object")


def json_format(body: dict[str, Any]) -> dict[str, Any] | None:
    """The request's response_format when it asks for JSON, else None (plain text, as before)."""
    fmt = body.get("response_format")
    if isinstance(fmt, dict) and fmt.get("type") in JSON_FORMATS:
        return fmt
    return None


def json_instruction(fmt: dict[str, Any]) -> str:
    """What the agent is told for a JSON-mode request: only the JSON value, no prose, no code fences."""
    if fmt.get("type") == "json_schema":
        spec = fmt.get("json_schema") if isinstance(fmt.get("json_schema"), dict) else {}
        schema = spec.get("schema")
        name = str(spec.get("name") or "").strip()
        head = "Reply with ONLY a single JSON value that matches this JSON Schema"
        head += f" (name: {name})" if name else ""
        if schema is not None:
            text = json.dumps(schema, ensure_ascii=False, indent=2)
            head += ":\n\n" + text
        else:
            head = "Reply with ONLY a single valid JSON value"
    else:
        head = "Reply with ONLY a single JSON object"
    return (
        "OUTPUT FORMAT (mandatory): " + head + "\n\n"
        "Your final answer is parsed by a program: output nothing but the JSON itself. No explanation, "
        "no prose before or after it, no Markdown, no code fences (no ```)."
    )


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        first_newline = text.find("\n")
        # "```json\n...\n```" or "```\n...```"; a one-line "```{...}```" keeps everything after the ticks.
        text = text[first_newline + 1 :] if first_newline >= 0 else text[3:]
        text = text.rstrip()
        if text.endswith("```"):
            text = text[:-3]
    return text.strip()


def extract_json(text: str, object_only: bool = False) -> str | None:
    """The answer as JSON text: the whole answer (fences stripped) if it parses, else the first balanced
    top-level JSON object or array inside it. None if there is none (or, object_only, no JSON object)."""
    cleaned = _strip_fences(str(text or ""))
    try:
        value = json.loads(cleaned)
    except ValueError:
        pass
    else:
        if not object_only or isinstance(value, dict):
            return cleaned
    decoder = json.JSONDecoder()
    starts = "{" if object_only else "{["
    for index, char in enumerate(cleaned):
        if char not in starts:
            continue
        try:
            value, end = decoder.raw_decode(cleaned, index)
        except ValueError:
            continue
        if isinstance(value, (dict, list)) and (not object_only or isinstance(value, dict)):
            return cleaned[index:end]
    return None


def json_answer(job: Job, fmt: dict[str, Any]) -> tuple[str | None, str, str]:
    """(json_text, error_message, error_code) for a finished JSON-mode job; json_text None on error."""
    if job.status == INPUT_REQUIRED:
        return None, "The agent asked for an approval; JSON mode cannot answer it.", "approval_required"
    if job.status == CANCELLED:
        return None, job.error or "The job was cancelled.", "cancelled"
    if job.status != COMPLETED:
        return None, job.error or "The agent failed.", job.public_status()
    found = extract_json(job.result, object_only=fmt.get("type") == "json_object")
    if found is None:
        logger.info("job %s: no valid JSON in the answer (%d chars)", job.id, len(job.result or ""))
        return None, "The agent's answer contained no valid JSON for the requested response_format.", "invalid_json"
    return found, "", ""


# ---- OpenAI wire format ------------------------------------------------------------------------


def error_body(message: str, kind: str = "invalid_request_error", code: str | None = None) -> dict[str, Any]:
    """OpenAI's error object. "detail" stays for callers of the worker's own API."""
    return {"error": {"message": message, "type": kind, "param": None, "code": code}, "detail": message}


def _error_response(status: int, message: str, kind: str = "invalid_request_error", code: str | None = None):
    return JSONResponse(error_body(message, kind, code), status_code=status)


def session_of(request: Request, body: dict[str, Any]) -> str:
    """The chat's id from a header or the body; empty for none. LibreChat sends "new" before a chat has an id,
    and an unfilled header template stays "{{...}}": neither may tie unrelated chats to one agent."""
    meta = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
    found = [request.headers.get(h, "") for h in _SESSION_HEADERS]
    found += [body.get(k) for k in _SESSION_FIELDS] + [meta.get(k) for k in _SESSION_FIELDS]
    for value in found:
        text = str(value or "").strip() if isinstance(value, (str, int)) else ""
        if text and text.lower() not in _NOT_A_SESSION and "{{" not in text and "${" not in text:
            return text[:200]
    return ""


def _usage(job: Job) -> dict[str, int]:
    usage = job.usage or {}
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}


def _answer(job: Job) -> str:
    if job.status == COMPLETED:
        return job.result
    if job.status == CANCELLED:
        return job.result or "(cancelled)"
    detail = job.error or "The agent failed."
    return f"{job.result}\n\n**Agent worker error:** {detail}".strip()


class ChunkWriter:
    """chat.completion.chunk lines for one reply: the same id and created on every chunk, `usage: null` on
    each one when the client asked for a usage chunk (as OpenAI does)."""

    def __init__(self, cid: str, model: str, include_usage: bool) -> None:
        self.cid = cid
        self.model = model
        self.include_usage = include_usage
        self.created = int(time.time())

    def _line(self, body: dict[str, Any]) -> str:
        return f"data: {json.dumps(body, ensure_ascii=False)}\n\n"

    def chunk(self, delta: dict[str, Any], finish: str | None = None) -> str:
        body: dict[str, Any] = {
            "id": self.cid,
            "object": "chat.completion.chunk",
            "created": self.created,
            "model": self.model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        if self.include_usage:
            body["usage"] = None
        return self._line(body)

    def usage(self, usage: dict[str, int]) -> str:
        return self._line({"id": self.cid, "object": "chat.completion.chunk", "created": self.created,
                           "model": self.model, "choices": [], "usage": usage})

    def error(self, message: str, kind: str = "agent_error", code: str | None = None) -> str:
        # No "choices": Open WebUI shows it as the reply's error; the OpenAI SDKs raise APIError on it.
        return self._line({"error": {"message": message, "type": kind, "param": None, "code": code}})

    @staticmethod
    def keepalive() -> str:
        return ": keepalive\n\n"

    @staticmethod
    def done() -> str:
        return "data: [DONE]\n\n"

    def finish(self, usage: dict[str, int] | None) -> list[str]:
        lines = [self.chunk({}, "stop")]
        if self.include_usage:
            lines.append(self.usage(usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}))
        lines.append(self.done())
        return lines


async def sse_reply(
    manager: JobManager,
    job: Job,
    out: ChunkWriter,
    *,
    after: int = 0,
    steps: str = "reasoning_content",
    disconnected: Callable[[], Awaitable[bool]] | None = None,
) -> AsyncIterator[str]:
    """The job's live events as an OpenAI chat stream. If the stream ends before the job does (the client
    went away, or pressed stop), the job is cancelled."""
    finished = False
    try:
        yield out.chunk({"role": "assistant", "content": ""})
        streamed = False
        last_step = ""
        async for event in manager.follow(job, after, ping=KEEPALIVE_SECONDS):
            if disconnected is not None and await disconnected():
                return
            kind = event.get("type")
            if kind == "approval":
                # End this reply with the question; the job waits. The user's next message answers it.
                yield out.chunk({"content": approval_prompt(job)})
                for line in out.finish(_usage(job)):
                    yield line
                finished = True
                return
            if kind == "text":
                delta = str(event.get("delta") or "")
                if delta:
                    streamed = True
                    yield out.chunk({"content": delta})
            elif kind in {"tool", "status"} and steps != "none":
                line = str(event.get("text") or "").strip()
                if line and line != last_step:
                    last_step = line
                    yield out.chunk({steps: line + "\n"})
            elif kind == "ping":
                yield out.keepalive()
        if not job.done:
            return
        if job.status == COMPLETED:
            if not streamed and job.result:
                yield out.chunk({"content": job.result})
            for line in out.finish(_usage(job)):
                yield line
        else:
            if not streamed and job.result:
                yield out.chunk({"content": job.result})
            if job.status == CANCELLED:
                yield out.error(job.error or "The job was cancelled.", "agent_cancelled", "cancelled")
            else:
                yield out.error(job.error or "The agent failed.", "agent_error", job.public_status())
            yield out.done()
        finished = True
    finally:
        if not finished and not job.done:
            logger.info("job %s: the chat stream ended early, cancelling", job.id)
            manager.cancel(job.id)


@router.get("/models")
async def models(request: Request) -> dict[str, Any]:
    manager: JobManager = request.app.state.jobs
    rows = await manager.engine.models()
    default = manager.settings.model
    if default and cursor_auth.api_key(manager.settings) and not any(row["id"] == default for row in rows):
        rows = [{"id": default, "name": default, "engine": "cursor"}, *rows]
    return {
        "object": "list",
        "data": [
            {"id": row["id"], "object": "model", "created": 0, "owned_by": row.get("engine") or "cursor"}
            for row in rows
        ],
    }


@router.post("/chat/completions")
async def chat_completions(request: Request):
    manager: JobManager = request.app.state.jobs
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _error_response(400, "Body is not JSON.")
    if not isinstance(body, dict) or not isinstance(body.get("messages"), list):
        return _error_response(400, "messages is required.", code="missing_messages")
    engine, model, workspace = model_engine(str(body.get("model") or ""))
    workspace = request.headers.get("x-agent-workspace", "").strip() or workspace
    session = session_of(request, body)
    try:
        task, context, instructions, images = messages_to_request(body["messages"])
    except HTTPException as exc:
        return _error_response(exc.status_code, str(exc.detail))
    fmt = json_format(body)
    tools = request.headers.get("x-agent-tools", "").strip()
    if fmt is not None:
        # "none" is the narrowest profile: narrow_profile keeps it whatever the ceiling or X-Agent-Tools say.
        tools = "none"
        rule = json_instruction(fmt)
        instructions = f"{instructions}\n\n{rule}".strip()
        if session:
            # A warm agent (resumed session) gets only the task, not the instructions: repeat the rule there.
            task = f"{task}\n\n{rule}"
    try:
        resolved_ws = resolve(manager.settings, workspace, create=False)[0]
    except WorkspaceError:
        resolved_ws = workspace
    paused = manager.pending_for(resolved_ws, session, "openai")
    if paused is not None:
        answer = decision_word(task)
        if answer is None:
            return _reply(body, paused, approval_prompt(paused).strip())
        manager.decide(paused.id, answer, "" if answer else "denied in chat")
        await asyncio.sleep(0)
        return await _respond(request, manager, body, paused, after=len(paused.events))
    try:
        job = manager.submit(
            JobRequest(
                task=task,
                workspace=workspace,
                session=session,
                model=model,
                effort=str(body.get("reasoning_effort") or ""),
                tools=tools,
                images=images,
                context=context,
                instructions=instructions,
                depth=parse_depth(request.headers.get(DEPTH_HEADER)),
                origin="openai",
                engine=engine,
            )
        )
    except JobError as exc:
        return _error_response(exc.status, str(exc), "invalid_request_error" if exc.status < 500 else "server_error")
    return await _respond(request, manager, body, job)


def _include_usage(body: dict) -> bool:
    options = body.get("stream_options")
    return bool(options.get("include_usage")) if isinstance(options, dict) else False


def _sse_response(lines: AsyncIterator[str]) -> StreamingResponse:
    return StreamingResponse(
        lines,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


def _completion(cid: str, shown: str, text: str, usage: dict[str, int], extra: dict | None = None) -> JSONResponse:
    return JSONResponse({
        "id": cid, "object": "chat.completion", "created": int(time.time()), "model": shown,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": usage,
        **(extra or {}),
    })


def _reply(body: dict, job: Job, text: str):
    """A fixed answer (no new job), in the shape the caller asked for."""
    cid = f"chatcmpl-{job.id}"
    shown = str(body.get("model") or job.model)
    if not body.get("stream"):
        return _completion(cid, shown, text, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})
    out = ChunkWriter(cid, shown, _include_usage(body))

    async def lines():
        yield out.chunk({"role": "assistant", "content": text})
        for line in out.finish(None):
            yield line

    return _sse_response(lines())


async def _wait_or_leave(request: Request, manager: JobManager, job: Job) -> bool:
    """Non-stream: wait for the job (or its approval question). False if the client hung up first; the job
    is cancelled then, as the stop button does in a stream."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + manager.settings.job_timeout + 30
    while True:
        left = deadline - loop.time()
        await manager.wait(job, min(DISCONNECT_POLL_SECONDS, max(left, 0)), stop_on_approval=True)
        if job.done or job.status == INPUT_REQUIRED or left <= 0:
            return True
        if await request.is_disconnected():
            logger.info("job %s: the client hung up, cancelling", job.id)
            manager.cancel(job.id)
            return False


async def _respond(request: Request, manager: JobManager, body: dict, job: Job, after: int = 0):
    cid = f"chatcmpl-{job.id}"
    shown = str(body.get("model") or job.model)

    fmt = json_format(body)
    if fmt is not None:
        return await _respond_json(request, manager, body, job, fmt, cid, shown)

    if not body.get("stream"):
        if not await _wait_or_leave(request, manager, job):
            return _error_response(499, "The client closed the request; the job was cancelled.", "agent_cancelled")
        if job.status == INPUT_REQUIRED:
            return _reply(body, job, (job.result or "") + approval_prompt(job))
        return _completion(cid, shown, _answer(job), _usage(job),
                           {"agent_worker": {"job_id": job.id, "status": job.public_status(), "changes": job.changes}})

    steps = steps_mode(request.headers.get("x-agent-steps")) or manager.settings.openai_steps
    out = ChunkWriter(cid, shown, _include_usage(body))
    return _sse_response(sse_reply(manager, job, out, after=after, steps=steps, disconnected=request.is_disconnected))


async def _respond_json(request: Request, manager: JobManager, body: dict, job: Job, fmt: dict, cid: str,
                        shown: str):
    """JSON mode: wait for the whole answer, then send only the JSON (or an OpenAI error)."""
    extra = {"agent_worker": {"job_id": job.id, "status": job.public_status(), "changes": job.changes}}
    if not body.get("stream"):
        if not await _wait_or_leave(request, manager, job):
            return _error_response(499, "The client closed the request; the job was cancelled.", "agent_cancelled")
        if not job.done and job.status != INPUT_REQUIRED:
            manager.cancel(job.id)
            return _error_response(504, "The agent did not finish in time.", "server_error", "timeout")
        text, message, code = json_answer(job, fmt)
        if job.status == INPUT_REQUIRED:
            manager.cancel(job.id, "JSON mode cannot answer an approval.")
        if text is None:
            return _error_response(502, message, "agent_error", code)
        extra["agent_worker"]["status"] = job.public_status()
        return _completion(cid, shown, text, _usage(job), extra)

    steps = steps_mode(request.headers.get("x-agent-steps")) or manager.settings.openai_steps
    out = ChunkWriter(cid, shown, _include_usage(body))
    return _sse_response(sse_json_reply(manager, job, out, fmt, steps=steps, disconnected=request.is_disconnected))


async def sse_json_reply(
    manager: JobManager,
    job: Job,
    out: ChunkWriter,
    fmt: dict[str, Any],
    *,
    steps: str = "reasoning_content",
    disconnected: Callable[[], Awaitable[bool]] | None = None,
) -> AsyncIterator[str]:
    """JSON mode as a stream: steps live (as reasoning), the answer's text held back; once the job ends,
    one content chunk with the JSON, or an error chunk. A stream that ends early cancels the job."""
    finished = False
    try:
        yield out.chunk({"role": "assistant", "content": ""})
        last_step = ""
        async for event in manager.follow(job, 0, ping=KEEPALIVE_SECONDS):
            if disconnected is not None and await disconnected():
                return
            kind = event.get("type")
            if kind == "approval":
                break
            if kind in {"tool", "status"} and steps != "none":
                line = str(event.get("text") or "").strip()
                if line and line != last_step:
                    last_step = line
                    yield out.chunk({steps: line + "\n"})
            elif kind == "ping":
                yield out.keepalive()
        if not job.done and job.status != INPUT_REQUIRED:
            return
        text, message, code = json_answer(job, fmt)
        if text is None:
            yield out.error(message, "agent_error", code)
            yield out.done()
        else:
            yield out.chunk({"content": text})
            for line in out.finish(_usage(job)):
                yield line
        finished = True
    finally:
        if not job.done:
            # Ended early (client gone) or an approval JSON mode cannot answer: no job is left waiting.
            logger.info("job %s: the JSON chat stream ended before the job (finished=%s), cancelling", job.id, finished)
            manager.cancel(job.id)
