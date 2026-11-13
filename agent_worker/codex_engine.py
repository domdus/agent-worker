"""Run one task on OpenAI Codex (the official openai-codex Python SDK) and report what happened.

The SDK drives a local `codex app-server` over JSON-RPC (stdio) and ships the matching Codex CLI
(openai-codex-cli-bin), so the worker needs no Node.js. Every job gets its own app-server process in
the job's workspace: the agent's shell inherits AGENT_WORKER_WORKSPACE from it (the ssh guard needs
it), a cancelled job takes its process with it, and nothing leaks from one job into the next.
Sessions continue across jobs and restarts through Codex threads kept in CODEX_HOME.

Credentials, first one found wins:
  CODEX_HOME/auth.json  "Sign in with ChatGPT" (plan usage) or an API key stored by `codex login`
  OPENAI_API_KEY        an OpenAI API key (API billing), handed to Codex in memory per job
The worker's own secrets (OPENAI_API_KEY, CURSOR_API_KEY, WORKER_TOKEN, ...) are blanked in Codex's
environment, so the agent's shell cannot read them.

Tool profiles. Codex's own Linux sandbox (bubblewrap) needs user namespaces, which a default Docker
container does not allow. So by default (CODEX_SANDBOX=container) the container is the sandbox:
  full     full access, shell, live web search, MCP
  local    full access, shell, no web search (the shell can still reach the network)
  offline  no shell, no web: apply_patch file edits and MCP only
  mcp      read-only, no shell, no file tools: MCP only
  none     read-only, no tools, no MCP servers
With CODEX_SANDBOX=codex (container started with user namespaces allowed), shell commands run in
Codex's sandbox instead: full/local write only inside the workspace with network on, offline keeps
the shell but without network, mcp/none stay read-only.
No AGENTS.md, skills, hooks, plugins, apps or memories are loaded: only what the job sends.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from agent_worker.codex_login import (
    child_env,
    cli_path,
    credential_source,
    sdk_config,
    sdk_installed,
)
from agent_worker.engine import (
    AgentIdStore,
    Emit,
    EngineError,
    Outcome,
    RunSpec,
    compose_message,
    git_changes,
    git_head,
)
from agent_worker.events import _tool_event
from agent_worker.redact import redact
from agent_worker.settings import Settings

logger = logging.getLogger(__name__)

MISSING_KEY = (
    "No Codex credential is set. Sign in with ChatGPT: `docker exec -it agent-worker python -m "
    "agent_worker.codex_login` (or POST /auth/codex/login), mount an auth.json (CODEX_AUTH_JSON), "
    "or set OPENAI_API_KEY on the worker."
)
NOT_INSTALLED = "openai-codex is not installed in this worker image."
CLI_MISSING = "The Codex CLI that ships with openai-codex (openai-codex-cli-bin) was not found in this worker image."
START_SECONDS = 60
INTERRUPT_SECONDS = 10
# Codex answers this when a thread to resume is gone (for example a new data volume).
_THREAD_GONE = ("no rollout found", "thread not found", "unknown thread", "no thread found")
# Worker effort choices (shared with Cursor) -> Codex reasoning efforts. Codex's own names pass through.
_EFFORT = {"fast": "low", "high": "high", "high_fast": "medium"}
_CODEX_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
_SAFE_NAME = re.compile(r"[^A-Za-z0-9_-]")
# Never wanted in a headless worker job, whatever the profile.
_ALWAYS_OFF = {
    "hooks": False,
    "apps": False,
    "plugins": False,
    "memories": False,
    "goals": False,
    "browser_use": False,
    "computer_use": False,
    "in_app_browser": False,
}
_NO_SHELL = {"shell_tool": False, "unified_exec": False}
_API_ERRORS = {
    "unauthorized": (
        "OpenAI refused the Codex credential. Sign in again (python -m agent_worker.codex_login) or check "
        "OPENAI_API_KEY."
    ),
    "usageLimitExceeded": "This ChatGPT plan or API key hit its Codex usage limit. Try again later.",
    "rateLimitExceeded": "This Codex credential hit a rate limit. Try again later.",
    "serverOverloaded": "OpenAI is overloaded right now. Try again later.",
}


# ---- options -----------------------------------------------------------------------------------


def resolve_model(settings: Settings, model: str) -> str:
    """'' means Codex's own default model for the account."""
    value = (model or "").strip()
    if value.lower() in {"default", "codex"}:
        value = ""
    return value or settings.codex_model


def effort_for(effort: str) -> str | None:
    value = (effort or "").strip().lower()
    if value in _EFFORT:
        return _EFFORT[value]
    return value if value in _CODEX_EFFORTS else None


def mcp_config(servers: dict[str, dict[str, Any]]) -> tuple[dict[str, Any], list[str]]:
    """Worker MCP servers -> Codex mcp_servers. Codex speaks stdio and streamable HTTP, not SSE."""
    out: dict[str, Any] = {}
    skipped: list[str] = []
    for name, cfg in sorted((servers or {}).items()):
        key = _SAFE_NAME.sub("_", str(name))[:64] or "server"
        kind = str(cfg.get("type") or "").lower()
        if kind == "sse":
            skipped.append(str(name))
            continue
        if cfg.get("url"):
            item: dict[str, Any] = {"url": str(cfg["url"])}
            if cfg.get("headers"):
                item["http_headers"] = {str(k): str(v) for k, v in cfg["headers"].items()}
        elif cfg.get("command"):
            item = {"command": str(cfg["command"]), "args": [str(a) for a in cfg.get("args") or []]}
            if cfg.get("env"):
                item["env"] = {str(k): str(v) for k, v in cfg["env"].items()}
            if cfg.get("cwd"):
                item["cwd"] = str(cfg["cwd"])
        else:
            skipped.append(str(name))
            continue
        out[key] = item
    return out, skipped


def tool_policy(profile: str, sandbox_mode: str, servers: dict[str, dict[str, Any]]) -> tuple[str, dict[str, Any], list[str]]:
    """(sandbox preset name, thread config, skipped MCP servers) for a tool profile."""
    own_sandbox = sandbox_mode == "codex"
    features: dict[str, Any] = dict(_ALWAYS_OFF)
    config: dict[str, Any] = {
        # Like the Claude engine: no instructions from files in the workspace or CODEX_HOME.
        "project_doc_max_bytes": 0,
        "skills": {"bundled": {"enabled": False}},
        "web_search": "live" if profile == "full" else "disabled",
    }
    if profile == "full":
        sandbox = "workspace_write" if own_sandbox else "full_access"
    elif profile == "local":
        sandbox = "workspace_write" if own_sandbox else "full_access"
        features["image_generation"] = False
    elif profile == "offline":
        features["image_generation"] = False
        if own_sandbox:
            # The shell stays, but the sandbox has no network.
            sandbox = "workspace_write"
        else:
            sandbox = "full_access"
            features.update(_NO_SHELL)
    else:  # mcp, none
        sandbox = "read_only"
        features.update(_NO_SHELL)
        features["image_generation"] = False
        features["view_image"] = False
    if sandbox == "workspace_write":
        config["sandbox_workspace_write"] = {"network_access": profile in {"full", "local"}}
    config["features"] = features
    skipped: list[str] = []
    if profile != "none":
        mapped, skipped = mcp_config(servers)
        if mapped:
            config["mcp_servers"] = mapped
    return sandbox, config, skipped


def turn_input(message: str, images: list[dict[str, str]] | None) -> Any:
    pictures = [item for item in (images or []) if item.get("data") and item.get("mime")]
    if not pictures:
        return message
    from openai_codex import ImageInput, TextInput

    items: list[Any] = [ImageInput(url=f"data:{item['mime']};base64,{item['data']}") for item in pictures]
    items.append(TextInput(message))
    return items


# ---- events ------------------------------------------------------------------------------------


def as_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            data = dump(by_alias=True, mode="json", exclude_none=True)
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}
    return {}


def _item(payload: dict[str, Any]) -> dict[str, Any]:
    item = payload.get("item") or {}
    return item if isinstance(item, dict) else {}


def _short(text: str, limit: int = 160) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _paths(item: dict[str, Any]) -> str:
    names = [str(change.get("path") or "").rsplit("/", 1)[-1] for change in item.get("changes") or [] if isinstance(change, dict)]
    names = [name for name in names if name]
    if len(names) > 3:
        return ", ".join(names[:3]) + f" and {len(names) - 3} more"
    return ", ".join(names)


def _tool_name(item: dict[str, Any]) -> str:
    kind = item.get("type")
    if kind == "commandExecution":
        return "shell"
    if kind == "fileChange":
        return "edit"
    if kind == "mcpToolCall":
        return f"mcp__{item.get('server') or 'mcp'}__{item.get('tool') or 'tool'}"
    if kind == "webSearch":
        return "web_search"
    if kind == "dynamicToolCall":
        return str(item.get("tool") or item.get("name") or "tool")
    return ""


def _failed(item: dict[str, Any]) -> bool:
    status = str(item.get("status") or "").lower()
    if status in {"failed", "declined", "error"}:
        return True
    if item.get("type") == "commandExecution" and item.get("exitCode") not in (None, 0):
        return True
    return bool(item.get("error"))


@dataclass
class TurnState:
    """What one Codex turn produced."""

    parts: list[str] = field(default_factory=list)
    streamed: set[str] = field(default_factory=set)
    messages: list[tuple[str, str]] = field(default_factory=list)  # (phase, text)
    usage_first: dict[str, Any] | None = None
    usage_last: dict[str, Any] | None = None
    status: str = ""
    error: dict[str, Any] | None = None
    stream_error: dict[str, Any] | None = None
    duration_ms: int | None = None

    def final_text(self) -> str:
        for phase, text in reversed(self.messages):
            if phase == "final_answer" and text.strip():
                return text.strip()
        for _phase, text in reversed(self.messages):
            if text.strip():
                return text.strip()
        return "".join(self.parts).strip()


def map_notification(method: str, payload: Any, state: TurnState) -> list[dict[str, Any]]:
    """Codex app-server notifications -> worker events; records text, usage and the end of the turn."""
    data = as_dict(payload)
    events: list[dict[str, Any]] = []
    if method == "item/agentMessage/delta":
        delta = str(data.get("delta") or "")
        if delta:
            state.streamed.add(str(data.get("itemId") or ""))
            state.parts.append(delta)
            events.append({"type": "text", "delta": delta})
    elif method == "item/started":
        item = _item(data)
        kind = item.get("type")
        if kind == "reasoning":
            events.append({"type": "status", "text": "Thinking..."})
        elif kind == "commandExecution":
            events.append(_tool_event("shell", "start", _short(f"Running {item.get('command') or ''}"), str(item.get("id") or "")))
        elif kind == "fileChange":
            events.append(_tool_event("edit", "start", _short(f"Editing {_paths(item)}"), str(item.get("id") or "")))
        elif kind == "webSearch":
            events.append(_tool_event("web_search", "start", _short(f"Searching the web: {item.get('query') or ''}"), str(item.get("id") or "")))
        elif _tool_name(item):
            name = _tool_name(item)
            events.append(_tool_event(name, "start", f"Calling {name}...", str(item.get("id") or "")))
    elif method == "item/completed":
        item = _item(data)
        kind = item.get("type")
        if kind == "agentMessage":
            text = str(item.get("text") or "")
            item_id = str(item.get("id") or "")
            state.messages.append((str(item.get("phase") or ""), text))
            if text.strip() and item_id not in state.streamed:
                # Not streamed as deltas: the whole message at once.
                if state.parts and not state.parts[-1].endswith("\n"):
                    state.parts.append("\n\n")
                    events.append({"type": "text", "delta": "\n\n"})
                state.parts.append(text)
                events.append({"type": "text", "delta": text})
        elif _tool_name(item):
            name = _tool_name(item)
            text = "Tool failed" if _failed(item) else "Finished"
            events.append(_tool_event(name, "end", text, str(item.get("id") or "")))
    elif method == "thread/tokenUsage/updated":
        usage = data.get("tokenUsage") or {}
        if isinstance(usage, dict):
            if state.usage_first is None:
                state.usage_first = usage
            state.usage_last = usage
    elif method == "error":
        error = data.get("error") or {}
        if data.get("willRetry"):
            events.append({"type": "status", "text": _short(str(error.get("message") or "Reconnecting..."), 120)})
        elif isinstance(error, dict):
            state.stream_error = error
    elif method == "turn/completed":
        turn = data.get("turn") or {}
        state.status = str(turn.get("status") or "")
        state.error = turn.get("error") if isinstance(turn.get("error"), dict) else None
        duration = turn.get("durationMs")
        state.duration_ms = int(duration) if isinstance(duration, (int, float)) else None
    return events


# ---- results -----------------------------------------------------------------------------------


def _n(blob: dict[str, Any] | None, key: str) -> int:
    try:
        return max(0, int((blob or {}).get(key) or 0))
    except (TypeError, ValueError):
        return 0


def usage_from(state: TurnState) -> dict[str, Any] | None:
    """This turn's tokens. Codex reports thread totals, so a resumed thread's earlier turns are taken off."""
    if state.usage_last is None:
        return None
    first = state.usage_first or state.usage_last
    end = state.usage_last.get("total") or {}
    keys = ("inputTokens", "cachedInputTokens", "cacheWriteInputTokens", "outputTokens", "reasoningOutputTokens", "totalTokens")
    # Totals before this turn = totals after the first update minus what that update added.
    base = {key: _n(first.get("total"), key) - _n(first.get("last"), key) for key in keys}
    turn = {key: max(0, _n(end, key) - max(0, base[key])) for key in keys}
    prompt = turn["inputTokens"]
    completion = turn["outputTokens"]
    if not (prompt or completion):
        return None
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "cache_read_tokens": turn["cachedInputTokens"],
        "cache_write_tokens": turn["cacheWriteInputTokens"],
        "reasoning_tokens": turn["reasoningOutputTokens"],
        "total_tokens": turn["totalTokens"] or prompt + completion,
        "charged_cents": None,
        # Codex reports no price: plan usage is not billed per token, and API prices change per model.
        "cost_usd": None,
    }


def _error_kind(error: dict[str, Any] | None) -> str:
    info = (error or {}).get("codexErrorInfo")
    if isinstance(info, str):
        return info
    if isinstance(info, dict) and info:
        key = next(iter(info))
        status = info[key].get("httpStatusCode") if isinstance(info[key], dict) else None
        if status == 401:
            return "unauthorized"
        if status == 429:
            return "rateLimitExceeded"
        return str(key)
    return ""


def error_text(error: dict[str, Any] | None, model: str, secrets: list[str]) -> str:
    kind = _error_kind(error)
    message = redact(str((error or {}).get("message") or ""), secrets).strip()
    if kind == "modelNotFound":
        return f"Codex does not offer the model {model or '(default)'} for this account. {message}".strip()[:400]
    known = _API_ERRORS.get(kind)
    if known:
        return (known + (f" ({_short(message, 200)})" if message else ""))[:400]
    return (message or "Codex stopped without an answer.")[:400]


# ---- the engine --------------------------------------------------------------------------------


class CodexEngine:
    def __init__(self, settings: Settings, agent_ids: AgentIdStore | None = None, factory=None) -> None:
        self.settings = settings
        self._get_session, self._set_session = agent_ids or (lambda _w, _s: None, lambda _w, _s, _a: None)
        # Test seam: (CodexConfig) -> an AsyncCodex-like object.
        self._factory = factory
        self._models: tuple[float, list[dict[str, str]]] | None = None

    @property
    def available(self) -> bool:
        return sdk_installed() and credential_source(self.settings) != "none"

    @staticmethod
    def _key(workspace: str) -> str:
        return f"{workspace}@codex"

    def forget(self, workspace: str, session: str) -> None:
        self._set_session(self._key(workspace), session, "")

    def _codex(self, *, api_key: bool, cwd: str | None, workspace: str) -> Any:
        config = sdk_config(self.settings, store="ephemeral" if api_key else "file", cwd=cwd, workspace=workspace)
        if self._factory is not None:
            return self._factory(config)
        from openai_codex import AsyncCodex

        return AsyncCodex(config)

    def _secrets(self) -> list[str]:
        return [self.settings.openai_api_key]

    def _public(self, exc: BaseException) -> str:
        if isinstance(exc, FileNotFoundError) and "codex" in str(exc).lower():
            return CLI_MISSING
        return (redact(str(exc), self._secrets()).strip() or type(exc).__name__)[:400]

    async def _session(self, codex: Any, spec: RunSpec, resume: str | None, emit: Emit) -> tuple[Any, bool]:
        """(thread, created) for this job: the session's thread, or a new one."""
        from openai_codex import ApprovalMode, Sandbox

        sandbox_name, config, skipped = tool_policy(spec.profile, self.settings.codex_sandbox, spec.mcp_servers)
        if skipped:
            await emit({"type": "status", "text": "Codex cannot use these MCP servers (SSE only): " + ", ".join(skipped)})
        model = resolve_model(self.settings, spec.model) or None
        common = {
            # Nobody can answer an approval in a headless job: anything that would ask is refused.
            "approval_mode": ApprovalMode.deny_all,
            "cwd": str(spec.path),
            "sandbox": getattr(Sandbox, sandbox_name),
            "config": config,
            "model": model,
        }
        if resume:
            try:
                return await codex.thread_resume(resume, **common), False
            except Exception as exc:
                text = str(exc).lower()
                if not any(mark in text for mark in _THREAD_GONE):
                    raise
                logger.info("codex thread %s is gone, starting a new one", resume)
                self.forget(spec.workspace, spec.session)
        # A job without a session is never resumed: keep no rollout of it on disk.
        thread = await codex.thread_start(ephemeral=not spec.session, **common)
        return thread, True

    async def _model_of(self, thread: Any) -> str:
        try:
            read = await asyncio.wait_for(thread.read(), 10)
        except Exception:
            return ""
        info = as_dict(getattr(read, "thread", None)) or as_dict(read).get("thread") or {}
        return str(info.get("model") or "")

    async def run(self, spec: RunSpec, emit: Emit) -> Outcome:
        if not sdk_installed():
            raise EngineError(NOT_INSTALLED)
        if not cli_path() and self._factory is None:
            raise EngineError(CLI_MISSING)
        source = credential_source(self.settings)
        if source == "none":
            raise EngineError(MISSING_KEY)
        api_key = source == "env-api"
        started = time.monotonic()
        spec.path.mkdir(parents=True, exist_ok=True)
        before = await git_head(spec.path)
        await emit({"type": "status", "text": "Starting Codex..."})
        key = self._key(spec.workspace)
        resume = self._get_session(key, spec.session) if spec.session else None
        codex = self._codex(api_key=api_key, cwd=str(spec.path), workspace=spec.workspace)
        state = TurnState()
        thread_id = ""
        used_model = resolve_model(self.settings, spec.model)
        try:
            try:
                await asyncio.wait_for(codex.__aenter__(), START_SECONDS)
                if api_key:
                    await codex.login_api_key(self.settings.openai_api_key)
                thread, created = await self._session(codex, spec, resume, emit)
                thread_id = str(getattr(thread, "id", "") or "")
                if spec.session and thread_id:
                    # Kept even if the turn fails or is cancelled: the thread holds the conversation so far.
                    self._set_session(key, spec.session, thread_id)
                used_model = await self._model_of(thread) or used_model
                message = compose_message(spec, created, self.settings.instructions)
                handle = await thread.turn(turn_input(message, spec.images), effort=effort_for(spec.effort))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise EngineError(self._public(exc)) from exc
            stream = handle.stream()
            try:
                async for note in stream:
                    for event in map_notification(str(getattr(note, "method", "")), getattr(note, "payload", None), state):
                        await emit(event)
            except asyncio.CancelledError:
                try:
                    await asyncio.shield(asyncio.wait_for(handle.interrupt(), INTERRUPT_SECONDS))
                except Exception:
                    logger.debug("codex interrupt failed", exc_info=True)
                raise
            except Exception as exc:
                if not state.status:
                    raise EngineError(self._public(exc)) from exc
                logger.debug("codex stream ended after the turn job=%s", spec.job_id, exc_info=True)
            finally:
                try:
                    await stream.aclose()
                except Exception:
                    pass
        finally:
            try:
                await asyncio.shield(asyncio.wait_for(codex.close(), 10))
            except Exception:
                logger.debug("codex close failed", exc_info=True)
        changes = await git_changes(spec.path, before)
        duration = state.duration_ms or int((time.monotonic() - started) * 1000)
        usage = usage_from(state)
        if state.status != "completed":
            error = state.error or state.stream_error
            if state.status == "interrupted" and not error:
                text = "The Codex turn was interrupted."
            elif not state.status:
                text = error_text(error, used_model, self._secrets()) if error else "Codex stopped without finishing the turn."
            else:
                text = error_text(error, used_model, self._secrets())
            return Outcome(
                status="failed",
                error=text,
                text=state.final_text(),
                run_id=thread_id,
                agent_id=thread_id,
                model=used_model,
                usage=usage,
                changes=changes,
                duration_ms=duration,
            )
        return Outcome(
            status="succeeded",
            text=state.final_text(),
            run_id=thread_id,
            agent_id=thread_id,
            model=used_model,
            usage=usage,
            changes=changes,
            duration_ms=duration,
        )

    async def models(self) -> list[dict[str, str]]:
        """Models the Codex account offers, cached for an hour."""
        fallback = [{"id": self.settings.codex_model or "default", "name": self.settings.codex_model or "Codex default"}]
        if self._models and self._models[0] > time.monotonic():
            return self._models[1]
        source = credential_source(self.settings)
        if source == "none" or not sdk_installed():
            return fallback
        codex = self._codex(api_key=source == "env-api", cwd=None, workspace="")
        try:
            await asyncio.wait_for(codex.__aenter__(), START_SECONDS)
            if source == "env-api":
                await codex.login_api_key(self.settings.openai_api_key)
            listed = await asyncio.wait_for(codex.models(), 30)
            rows = [
                {"id": str(item.id), "name": str(getattr(item, "display_name", "") or item.id)}
                for item in getattr(listed, "data", []) or []
                if getattr(item, "id", "") and not getattr(item, "hidden", False)
            ]
        except Exception as exc:
            logger.warning("codex model list failed: %s", self._public(exc))
            return fallback
        finally:
            try:
                await asyncio.wait_for(codex.close(), 10)
            except Exception:
                pass
        if self.settings.codex_model and not any(row["id"] == self.settings.codex_model for row in rows):
            rows.insert(0, fallback[0])
        rows = rows or fallback
        self._models = (time.monotonic() + 3600, rows)
        return rows

    async def aclose(self) -> None:
        return None


__all__ = ["CodexEngine", "child_env", "credential_source", "sdk_installed", "tool_policy", "usage_from"]
