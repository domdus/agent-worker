"""Jobs: queue, run, record events, cancel, watch. One job writes to a workspace at a time.

Parallelism: a job that may touch files (tools full, local, offline) holds its workspace's lock, so such
jobs in one workspace run one after another, in the order they came in. Jobs in different workspaces,
and chat-only jobs (tools mcp, none), run side by side, up to MAX_CONCURRENT_JOBS at once. Jobs with
the same session also take turns. A job takes its locks before a slot, so a job that only waits for
its workspace never holds a slot that a job in another workspace could use.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from agent_worker import proctree
from agent_worker import tasks as st
from agent_worker.engine import JOB_ENV, Engine, EngineError, RunSpec
from agent_worker.mcp_config import McpConfigError, load_mcp_servers, servers_from_request, with_depth
from agent_worker.settings import Settings, narrow_profile, touches_files
from agent_worker.workspaces import WorkspaceError, resolve

logger = logging.getLogger(__name__)

# Status names: agent_worker.tasks (MCP Task vocabulary plus "queued"; the names before 0.5.0 still read).
TERMINAL = st.TERMINAL
_MAX_EVENTS = 5000
# Past _MAX_EVENTS only these still count: dropping answer text, the outcome or an approval would break
# a live chat stream. Text past the cap stays in memory only (the result is saved with the job anyway).
_PAST_CAP = frozenset({"text", "result", "error", "approval"})
# A stopped engine gets this long to wind down on its own before its processes are killed.
STOP_GRACE = 10.0
# Longest the watchdog sleeps between checks.
_TICK = 5.0
_STEP_TYPES = frozenset({"tool", "status", "error", "approval"})


class JobError(ValueError):
    """The request cannot become a job (bad workspace, bad MCP config, nested call)."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _parse(stamp: str) -> datetime | None:
    try:
        return datetime.fromisoformat(stamp) if stamp else None
    except ValueError:
        return None


def _short(text: str, limit: int) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


class _Stopped(Exception):
    """The job was stopped (cancel or a watchdog): job.stop_status and job.stop_reason say how and why."""


@dataclass
class JobRequest:
    task: str
    workspace: str = ""
    session: str = ""
    model: str = ""
    effort: str = ""
    tools: str = ""
    mcp_servers: dict[str, Any] | None = None
    images: list[dict[str, str]] = field(default_factory=list)
    context: str = ""
    instructions: str = ""
    depth: int = 0
    origin: str = "api"
    engine: str = ""
    # Opaque id of the client's conversation (MCP _meta, see conversation_from_meta). Never a secret.
    conversation_id: str = ""
    # MCP: the job is not bound to the tools/call that started it (a cancelled call or disconnect leaves it).
    background: bool = False


# Keys accepted in a request's _meta for the conversation id, preferred first. The namespaced one follows
# the MCP _meta naming rules (reverse-DNS prefix + "/"); contextId is A2A's name for the same thing.
CONVERSATION_META_KEYS = (
    "io.github.domdus/conversation_id",
    "contextId",
    "conversationId",
    "conversation_id",
)
_ID_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-/@#+=")
# Values that look like credentials are refused: the id is shown in job lists and logs.
_SECRET_PREFIXES = ("sk-", "sk_", "ghp_", "gho_", "ghs_", "ghu_", "github_pat_", "xox", "glpat-", "eyj", "bearer",
                    "akia", "aiza")


def clean_conversation_id(value: Any, secrets: tuple[str, ...] = ()) -> str:
    """A usable conversation id, or "" (missing, too long, odd characters or looks like a secret)."""
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return ""
    text = str(value).strip()
    if not text or len(text) > 128 or any(ch not in _ID_CHARS for ch in text):
        if text:
            logger.warning("conversation id ignored: not an opaque id of at most 128 safe characters")
        return ""
    low = text.lower()
    if low.startswith(_SECRET_PREFIXES) or any(secret and secret in text for secret in secrets):
        logger.warning("conversation id ignored: it looks like a credential")
        return ""
    return text


def conversation_from_meta(meta: Any) -> str:
    """The raw conversation id from an MCP request's _meta (first known key that is set), else ""."""
    if not isinstance(meta, dict):
        try:
            meta = dict(meta or {})
        except (TypeError, ValueError):
            return ""
    for key in CONVERSATION_META_KEYS:
        value = meta.get(key)
        if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value).strip():
            return str(value)
    return ""


@dataclass
class Job:
    id: str
    task: str
    workspace: str
    session: str
    model: str
    profile: str
    origin: str
    engine: str = "cursor"
    status: str = st.QUEUED
    created_at: str = field(default_factory=_now)
    # Last status change (the MCP Task lastUpdatedAt).
    updated_at: str = ""
    started_at: str = ""
    finished_at: str = ""
    result: str = ""
    error: str = ""
    run_id: str = ""
    agent_id: str = ""
    usage: dict[str, Any] | None = None
    changes: dict[str, Any] | None = None
    duration_ms: int | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    pending_approval: dict[str, Any] | None = None
    approved_hosts: set[str] = field(default_factory=set)
    changed: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    task_handle: asyncio.Task | None = field(default=None, repr=False)
    approval_future: asyncio.Future | None = field(default=None, repr=False)
    approval_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    # Watchdog clocks (event loop time): when the engine started, last event, when an approval was asked.
    started_clock: float = field(default=0.0, repr=False)
    activity_clock: float = field(default=0.0, repr=False)
    approval_clock: float = field(default=0.0, repr=False)
    # A cancel or a watchdog asks the running job to stop through this; the reason becomes the error.
    stop: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    stop_status: str = ""
    stop_reason: str = ""
    # While queued: what it waits for ("workspace", "session" or "slot") and the lock's key.
    waiting_for: str = ""
    waiting_key: tuple = field(default=(), repr=False)
    held: list[tuple] = field(default_factory=list, repr=False)
    spec: RunSpec | None = field(default=None, repr=False)
    conversation_id: str = ""
    background: bool = False
    # JOB_STATUS_NAMES=legacy: `status` in answers uses the names before 0.5.0.
    legacy_names: bool = field(default=False, repr=False)

    @property
    def done(self) -> bool:
        return self.status in TERMINAL

    def set_status(self, status: str) -> None:
        self.status = status
        self.updated_at = _now()

    def public_status(self) -> str:
        return st.legacy(self.status) if self.legacy_names else self.status

    def status_message(self, blocked_by: str | None = None) -> str:
        """What the status means right now, for people and for the MCP Task statusMessage."""
        if self.status == st.QUEUED:
            what = {"workspace": "another job in this workspace", "session": "another job in this session",
                    "slot": "a free slot (MAX_CONCURRENT_JOBS)"}.get(self.waiting_for, "its start")
            return f"queued: waiting for {what}" + (f" (job {blocked_by})" if blocked_by else "")
        if self.status == st.INPUT_REQUIRED:
            approval = self.pending_approval or {}
            if not approval:
                return "input_required: waiting for a human answer"
            target = f"{approval.get('user')}@{approval.get('host')}" if approval.get("user") else approval.get("host")
            command = _short(approval.get("command") or "", 160)
            return (f"input_required: waiting for a human to approve {approval.get('tool', 'ssh')} on {target}"
                    + (f": {command}" if command else "") + " (POST /jobs/{id}/approve or MCP elicitation)").replace(
                        "{id}", self.id)
        if self.status == st.WORKING:
            step, _ = self.last_step()
            return step or "working"
        if self.status == st.COMPLETED:
            return _short(self.result, 200) or "completed"
        return _short(self.error, 300) or self.status

    def last_step(self) -> tuple[str, str]:
        """(text, at) of the latest step (tool call, status, error, approval) and the time of the latest event."""
        step = ""
        for event in reversed(self.events):
            if event.get("type") in _STEP_TYPES:
                step = str(event.get("text") or event.get("message") or "")
                if step:
                    break
        last = self.events[-1].get("at", "") if self.events else ""
        return _short(step, 200), last

    def runtime_seconds(self) -> int | None:
        started = _parse(self.started_at)
        if started is None:
            return None
        end = _parse(self.finished_at) or datetime.now(timezone.utc)
        return max(0, int((end - started).total_seconds()))

    def summary(self, *, task_chars: int = 300) -> dict[str, Any]:
        task = self.task if len(self.task) <= task_chars else self.task[:task_chars] + "…"
        return {
            "id": self.id,
            "status": self.public_status(),
            "legacy_status": st.legacy(self.status),
            "status_message": self.status_message(),
            "engine": self.engine,
            "workspace": self.workspace,
            "session": self.session or None,
            "conversation_id": self.conversation_id or None,
            "background": self.background,
            "model": self.model,
            "tools": self.profile,
            "origin": self.origin,
            "task": task,
            "created_at": self.created_at,
            "updated_at": self.updated_at or self.created_at,
            "started_at": self.started_at or None,
            "finished_at": self.finished_at or None,
            "approval": self.pending_approval,
        }

    def live(self) -> dict[str, Any]:
        """Progress fields for a job that has not ended (computed, never stored)."""
        step, last = self.last_step()
        return {"runtime_seconds": self.runtime_seconds(), "last_step": step or None, "last_event_at": last or None}

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.summary(task_chars=10_000),
            "result": self.result or None,
            "error": self.error or None,
            "run_id": self.run_id or None,
            "agent_id": self.agent_id or None,
            "usage": self.usage,
            "changes": self.changes,
            "duration_ms": self.duration_ms,
            "events": len(self.events),
            "approved_hosts": sorted(self.approved_hosts),
        }


class Store:
    """SQLite record of jobs, their events and session -> agent ids. Survives restarts."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._lock = threading.Lock()
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, created_at TEXT, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (job_id TEXT, seq INTEGER, data TEXT NOT NULL, PRIMARY KEY (job_id, seq));
            CREATE TABLE IF NOT EXISTS sessions (workspace TEXT, session TEXT, agent_id TEXT, PRIMARY KEY (workspace, session));
            """
        )

    def save(self, job: Job) -> None:
        data = json.dumps({**job.as_dict(), "task": job.task, "status": job.status}, ensure_ascii=False)
        with self._lock:
            self._db.execute(
                "INSERT INTO jobs (id, created_at, data) VALUES (?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET data = excluded.data",
                (job.id, job.created_at, data),
            )

    def add_event(self, job_id: str, event: dict[str, Any]) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO events (job_id, seq, data) VALUES (?, ?, ?)",
                (job_id, event["seq"], json.dumps(event, ensure_ascii=False)),
            )

    def load(self, limit: int) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
        with self._lock:
            rows = self._db.execute("SELECT id, data FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
            out = []
            for job_id, data in rows:
                events = [
                    json.loads(raw)
                    for (raw,) in self._db.execute("SELECT data FROM events WHERE job_id = ? ORDER BY seq", (job_id,))
                ]
                out.append((json.loads(data), events))
            return out

    def prune(self, keep: int) -> None:
        with self._lock:
            old = [
                row[0]
                for row in self._db.execute("SELECT id FROM jobs ORDER BY created_at DESC LIMIT -1 OFFSET ?", (keep,))
            ]
            for job_id in old:
                self._db.execute("DELETE FROM events WHERE job_id = ?", (job_id,))
                self._db.execute("DELETE FROM jobs WHERE id = ?", (job_id,))

    def agent_id(self, workspace: str, session: str) -> str | None:
        with self._lock:
            row = self._db.execute(
                "SELECT agent_id FROM sessions WHERE workspace = ? AND session = ?", (workspace, session)
            ).fetchone()
        return (row[0] or None) if row else None

    def set_agent_id(self, workspace: str, session: str, agent_id: str) -> None:
        with self._lock:
            if agent_id:
                self._db.execute(
                    "INSERT OR REPLACE INTO sessions (workspace, session, agent_id) VALUES (?, ?, ?)",
                    (workspace, session, agent_id),
                )
            else:
                self._db.execute("DELETE FROM sessions WHERE workspace = ? AND session = ?", (workspace, session))

    def close(self) -> None:
        with self._lock:
            self._db.close()


class JobManager:
    def __init__(self, settings: Settings, engine: Engine, store: Store) -> None:
        self.settings = settings
        self.engine = engine
        self.store = store
        self.jobs: dict[str, Job] = {}
        self._slots = asyncio.Semaphore(settings.max_concurrent_jobs)
        # ("workspace", name) or ("session", name, session) -> lock, and which job holds it.
        self._locks: dict[tuple, asyncio.Lock] = {}
        self._holders: dict[tuple, str] = {}
        self._legacy_names = settings.job_status_names == "legacy"
        self.closing = False
        self._restore()

    def _restore(self) -> None:
        for data, events in reversed(self.store.load(self.settings.keep_jobs)):
            job = Job(
                id=data["id"],
                task=data.get("task") or "",
                workspace=data.get("workspace") or "",
                session=data.get("session") or "",
                model=data.get("model") or "",
                profile=data.get("tools") or "",
                origin=data.get("origin") or "api",
                engine=data.get("engine") or "cursor",
                # Jobs saved before 0.5.0 carry the old names (succeeded, running, ...).
                status=st.normalize(data.get("status")) or st.FAILED,
                created_at=data.get("created_at") or _now(),
                updated_at=data.get("updated_at") or "",
                conversation_id=data.get("conversation_id") or "",
                background=bool(data.get("background")),
                legacy_names=self._legacy_names,
                started_at=data.get("started_at") or "",
                finished_at=data.get("finished_at") or "",
                result=data.get("result") or "",
                error=data.get("error") or "",
                run_id=data.get("run_id") or "",
                agent_id=data.get("agent_id") or "",
                usage=data.get("usage"),
                changes=data.get("changes"),
                duration_ms=data.get("duration_ms"),
                events=events,
            )
            if not job.done:
                job.set_status(st.FAILED)
                job.error = "The worker restarted while this job was running."
                job.finished_at = _now()
                self.store.save(job)
            job.changed.set()
            self.jobs[job.id] = job

    # ---- submit ------------------------------------------------------------------------------

    def _build(self, req: JobRequest, job_id: str) -> RunSpec:
        if req.depth >= self.settings.max_call_depth:
            raise JobError(
                "This call came from an agent worker job. Nested worker jobs are refused (MAX_CALL_DEPTH).",
                status=409,
            )
        task = (req.task or "").strip()
        if not task:
            raise JobError("task is empty.")
        try:
            name, path = resolve(self.settings, req.workspace)
        except WorkspaceError as exc:
            raise JobError(str(exc)) from exc
        pick = getattr(self.engine, "pick", None)
        try:
            engine = pick(req.engine) if callable(pick) else "cursor"
        except EngineError as exc:
            raise JobError(str(exc)) from exc
        if req.mcp_servers and not self.settings.allow_request_mcp:
            raise JobError("This worker does not accept MCP servers with a job (ALLOW_REQUEST_MCP=false).")
        try:
            servers = load_mcp_servers(self.settings.mcp_config_path)
            servers.update(servers_from_request(req.mcp_servers))
        except McpConfigError as exc:
            raise JobError(str(exc)) from exc
        return RunSpec(
            job_id=job_id,
            task=task,
            workspace=name,
            path=path,
            session=(req.session or "").strip()[:200],
            model=(req.model or "").strip(),
            effort=(req.effort or "").strip(),
            profile=narrow_profile(req.tools, self.settings.tool_profile),
            mcp_servers=with_depth(servers, req.depth + 1),
            images=list(req.images or []),
            context=req.context,
            instructions=req.instructions,
            engine=engine,
        )

    def default_model(self, engine: str) -> str:
        model_for = getattr(self.engine, "model_for", None)
        if not callable(model_for):
            return self.settings.model
        # Codex without CODEX_MODEL has no fixed default: the job shows the model once it has run.
        return model_for(engine) or ("" if engine == "codex" else self.settings.model)

    def default_models(self) -> dict[str, str | None]:
        """Every engine's model for jobs that do not name one (None: the engine decides, Codex without CODEX_MODEL)."""
        engines = getattr(self.engine, "engines", None) or {"cursor": self.engine}
        return {name: self.default_model(name) or None for name in engines}

    def submit(self, req: JobRequest) -> Job:
        job_id = uuid.uuid4().hex[:16]
        spec = self._build(req, job_id)
        job = Job(
            id=job_id,
            task=spec.task,
            workspace=spec.workspace,
            session=spec.session,
            model=spec.model or self.default_model(spec.engine),
            profile=spec.profile,
            origin=req.origin,
            engine=spec.engine,
            spec=spec,
            conversation_id=clean_conversation_id(req.conversation_id, self._secrets()),
            background=bool(req.background),
            legacy_names=self._legacy_names,
        )
        job.updated_at = job.created_at
        self.jobs[job.id] = job
        self.store.save(job)
        self._prune()
        job.task_handle = asyncio.create_task(self._run(job, spec), name=f"job-{job.id}")
        job.task_handle.add_done_callback(lambda _t, job=job: self._settle(job))
        logger.info(
            "job %s queued engine=%s workspace=%s session=%s tools=%s origin=%s conversation=%s",
            job.id, job.engine, job.workspace, job.session or "-", job.profile, job.origin, job.conversation_id or "-",
        )
        return job

    def _secrets(self) -> tuple[str, ...]:
        s = self.settings
        return tuple(v for v in (s.token, s.cursor_api_key, s.claude_oauth_token, s.anthropic_api_key,
                                 s.openai_api_key) if v)

    def _prune(self) -> None:
        keep = self.settings.keep_jobs
        if len(self.jobs) <= keep:
            return
        finished = sorted((j for j in self.jobs.values() if j.done), key=lambda j: j.created_at)
        for job in finished[: len(self.jobs) - keep]:
            self.jobs.pop(job.id, None)
        self.store.prune(keep)

    # ---- run ---------------------------------------------------------------------------------

    async def _emit(self, job: Job, event: dict[str, Any]) -> None:
        full = len(job.events) >= _MAX_EVENTS
        if full and event.get("type") not in _PAST_CAP:
            return
        entry = {"seq": len(job.events) + 1, "at": _now(), **event}
        job.events.append(entry)
        job.activity_clock = asyncio.get_running_loop().time()
        if not full or event.get("type") != "text":
            self.store.add_event(job.id, entry)
        job.changed.set()

    def _settle(self, job: Job) -> None:
        """A task cancelled before its first step never enters _run; close the job here."""
        if not job.done:
            self._finish(job, st.CANCELLED, error=job.stop_reason or "Cancelled before it started.")

    def _finish(self, job: Job, status: str, **fields: Any) -> None:
        if job.approval_future is not None and not job.approval_future.done():
            job.approval_future.set_result((False, "The job ended."))
        job.pending_approval = None
        job.set_status(status)
        job.finished_at = _now()
        for key, value in fields.items():
            setattr(job, key, value)
        self.store.save(job)
        job.changed.set()
        logger.info("job %s %s", job.id, status)

    def _lock_keys(self, spec: RunSpec) -> list[tuple]:
        keys: list[tuple] = []
        if touches_files(spec.profile):
            keys.append(("workspace", spec.workspace))
        if spec.session:
            keys.append(("session", spec.workspace, spec.session))
        return keys

    def _release(self, job: Job) -> None:
        while job.held:
            key = job.held.pop()
            if self._holders.get(key) == job.id:
                self._holders.pop(key, None)
            self._locks[key].release()

    async def _run(self, job: Job, spec: RunSpec) -> None:
        loop = asyncio.get_running_loop()
        try:
            # Locks before a slot: a job that waits for its workspace must not keep a job elsewhere from running.
            for key in self._lock_keys(spec):
                lock = self._locks.setdefault(key, asyncio.Lock())
                if lock.locked():
                    job.waiting_for, job.waiting_key = key[0], key
                    holder = self._holders.get(key, "")
                    where = "this workspace" if key[0] == "workspace" else "this session"
                    await self._emit(
                        job, {"type": "status", "text": f"Waiting for another job in {where} ({holder or '?'})..."}
                    )
                await lock.acquire()
                job.held.append(key)
                self._holders[key] = job.id
            if self._slots.locked():
                job.waiting_for, job.waiting_key = "slot", ()
                await self._emit(
                    job,
                    {"type": "status", "text": f"Waiting for a free slot (MAX_CONCURRENT_JOBS={self.settings.max_concurrent_jobs})..."},
                )
            async with self._slots:
                job.waiting_for, job.waiting_key = "", ()
                job.set_status(st.WORKING)
                job.started_at = _now()
                job.started_clock = job.activity_clock = loop.time()
                self.store.save(job)
                await self._emit(job, {"type": "status", "text": "Running"})
                try:
                    outcome = await self._supervise(job, spec)
                except _Stopped:
                    status, message = job.stop_status or st.CANCELLED, job.stop_reason or "Cancelled."
                    await self._emit(job, {"type": "error" if status == st.FAILED else "status", "message": message,
                                           "text": message})
                    self._finish(job, status, error=message)
                    await self._cleanup(job, spec)
                    return
            fields = dict(
                result=outcome.text,
                error=outcome.error,
                run_id=outcome.run_id,
                agent_id=outcome.agent_id,
                usage=outcome.usage,
                changes=outcome.changes,
                duration_ms=outcome.duration_ms,
            )
            if outcome.model:
                fields["model"] = outcome.model
            # Engines report succeeded/failed/cancelled; the job uses the MCP Task names.
            status = st.normalize(outcome.status) or st.FAILED
            if status == st.COMPLETED:
                await self._emit(job, {"type": "result", "text": outcome.text})
            else:
                await self._emit(job, {"type": "error", "message": outcome.error})
            self._finish(job, status, **fields)
        except asyncio.CancelledError:
            await self._emit(job, {"type": "status", "text": "Cancelled"})
            self._finish(job, st.CANCELLED, error=job.stop_reason or job.error or "Cancelled.")
        except EngineError as exc:
            await self._emit(job, {"type": "error", "message": str(exc)})
            self._finish(job, st.FAILED, error=str(exc))
        except Exception as exc:
            logger.exception("job %s crashed", job.id)
            message = f"The worker failed: {type(exc).__name__}"
            await self._emit(job, {"type": "error", "message": message})
            self._finish(job, st.FAILED, error=message)
        finally:
            job.waiting_for, job.waiting_key = "", ()
            self._release(job)

    def _overdue(self, job: Job, now: float) -> tuple[str, str] | None:
        """(status, reason) when a watchdog limit is past, else None."""
        settings = self.settings
        if job.status == st.INPUT_REQUIRED:
            if job.approval_clock and now - job.approval_clock >= settings.approval_timeout:
                return st.CANCELLED, self._approval_timeout_reason(job)
        elif settings.job_idle_timeout > 0 and now - job.activity_clock >= settings.job_idle_timeout:
            return st.FAILED, (
                f"No output or progress from the engine for {settings.job_idle_timeout} s "
                "(JOB_IDLE_TIMEOUT_SECONDS); the job was stopped."
            )
        if now - job.started_clock >= settings.job_timeout:
            return st.FAILED, f"The job ran longer than JOB_MAX_RUNTIME_SECONDS ({settings.job_timeout}s) and was stopped."
        return None

    def _approval_timeout_reason(self, job: Job) -> str:
        approval = job.pending_approval or {}
        what = f" for {approval.get('tool', 'ssh')} {approval.get('host', '')}".rstrip() if approval else ""
        return f"approval timed out: no answer within {self.settings.approval_timeout} s{what} (APPROVAL_TIMEOUT_SECONDS)."

    def _next_check(self, job: Job, now: float) -> float:
        settings = self.settings
        due = [job.started_clock + settings.job_timeout]
        if job.status == st.INPUT_REQUIRED and job.approval_clock:
            due.append(job.approval_clock + settings.approval_timeout)
        elif settings.job_idle_timeout > 0:
            due.append(job.activity_clock + settings.job_idle_timeout)
        return min(_TICK, max(0.01, min(due) - now))

    async def _supervise(self, job: Job, spec: RunSpec):
        """Run the engine under the watchdogs. Returns its Outcome; raises _Stopped after a cancel or a limit."""
        loop = asyncio.get_running_loop()
        runner = asyncio.ensure_future(self.engine.run(spec, lambda event: self._emit(job, event)))
        stopper = asyncio.ensure_future(job.stop.wait())
        try:
            while not job.stop.is_set():
                late = self._overdue(job, loop.time())
                if late is not None:
                    self.request_stop(job, *late)
                    break
                await asyncio.wait({runner, stopper}, timeout=self._next_check(job, loop.time()),
                                   return_when=asyncio.FIRST_COMPLETED)
                if runner.done():
                    return runner.result()
            if runner.done():
                return runner.result()
            runner.cancel()
            finished, _ = await asyncio.wait({runner}, timeout=STOP_GRACE)
            if not finished:
                logger.warning("job %s: engine did not stop within %ss, killing its processes", job.id, STOP_GRACE)
            runner.add_done_callback(_swallow)
            raise _Stopped()
        except asyncio.CancelledError:
            if not runner.done():
                runner.cancel()
                await asyncio.wait({runner}, timeout=STOP_GRACE)
            runner.add_done_callback(_swallow)
            raise
        finally:
            stopper.cancel()

    async def _cleanup(self, job: Job, spec: RunSpec) -> None:
        """After a stop: kill what the job left running. Its workspace lock is released right after."""
        try:
            await proctree.kill_tagged(JOB_ENV, job.id)
            kill = getattr(self.engine, "kill", None)
            # The Cursor bridge serves the whole workspace: leave it to a job of the same engine still running there.
            shared = any(
                other is not job and not other.done and other.started_at
                and other.workspace == job.workspace and other.engine == job.engine
                for other in self.jobs.values()
            )
            if callable(kill) and not shared:
                await asyncio.wait_for(kill(spec), 15)
        except Exception:
            logger.warning("job %s: cleanup after stop failed", job.id, exc_info=True)

    # ---- read / control ----------------------------------------------------------------------

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    def list(self, limit: int = 50, workspace: str = "", status: str = "", conversation_id: str = "") -> list[Job]:
        rows = sorted(self.jobs.values(), key=lambda j: j.created_at, reverse=True)
        if workspace:
            rows = [j for j in rows if j.workspace == workspace]
        if status:
            wanted = st.parse_filter(status)
            rows = [j for j in rows if j.status in wanted]
        if conversation_id:
            rows = [j for j in rows if j.conversation_id == conversation_id.strip()]
        return rows[: max(1, min(limit, 500))]

    def request_stop(self, job: Job, status: str, reason: str) -> bool:
        """Ask a running job to stop; it ends as `status` with `reason` as its error. The first reason wins."""
        if job.done or job.stop.is_set():
            return False
        job.stop_status, job.stop_reason = st.normalize(status) or st.CANCELLED, reason
        if job.approval_future is not None and not job.approval_future.done():
            job.approval_future.set_result((False, reason))
        job.stop.set()
        logger.info("job %s stopping: %s", job.id, reason)
        return True

    def cancel(self, job_id: str, reason: str = "Cancelled.") -> Job | None:
        job = self.jobs.get(job_id)
        if job is None:
            return None
        if job.done:
            return job
        if job.started_at:
            self.request_stop(job, st.CANCELLED, reason)
        elif job.task_handle is not None:
            job.stop_reason = job.stop_reason or reason
            job.task_handle.cancel()
        return job

    def cancel_workspace(self, workspace: str, reason: str = "Cancelled.") -> list[Job]:
        """Cancel every job in the workspace that has not ended (queued ones first, so none starts in between)."""
        name = (workspace or "").strip().lower() or self.settings.default_workspace
        pending = [j for j in self.jobs.values() if j.workspace == name and not j.done]
        pending.sort(key=lambda j: (bool(j.started_at), j.created_at))
        for job in pending:
            self.cancel(job.id, reason)
        return pending

    def cancel_conversation(self, conversation_id: str, reason: str = "Cancelled.") -> list[Job]:
        """Cancel every job of this conversation that has not ended (queued ones first)."""
        wanted = (conversation_id or "").strip()
        if not wanted:
            return []
        pending = [j for j in self.jobs.values() if j.conversation_id == wanted and not j.done]
        pending.sort(key=lambda j: (bool(j.started_at), j.created_at))
        for job in pending:
            self.cancel(job.id, reason)
        return pending

    def blocked_by(self, job: Job) -> str | None:
        """The job holding the lock a queued job waits for."""
        if job.status != st.QUEUED or not job.waiting_key:
            return None
        return self._holders.get(job.waiting_key) or None

    def describe(self, job: Job) -> dict[str, Any]:
        """One row of agent_jobs / GET /jobs/overview: what runs where, for how long, waiting for what.
        No secrets and no full prompt (a short preview of the task only)."""
        step, last = job.last_step()
        approval = job.pending_approval if job.status == st.INPUT_REQUIRED else None
        blocked = self.blocked_by(job)
        row: dict[str, Any] = {
            "job_id": job.id,
            "status": job.public_status(),
            "legacy_status": st.legacy(job.status),
            "status_message": job.status_message(blocked),
            "workspace": job.workspace,
            "engine": job.engine,
            "model": job.model or None,
            "session": job.session or None,
            "tools": job.profile,
            "origin": job.origin,
            "conversation_id": job.conversation_id or None,
            "background": job.background,
            "task_preview": _short(job.task, 120),
            "created_at": job.created_at,
            "updated_at": job.updated_at or job.created_at,
            "started_at": job.started_at or None,
            "finished_at": job.finished_at or None,
            "runtime_seconds": job.runtime_seconds(),
            "last_step": step or None,
            "last_event_at": last or None,
            "waiting_for_approval": approval is not None,
            "approval": {
                "tool": approval.get("tool"),
                "host": approval.get("host"),
                "user": approval.get("user") or None,
                "command": _short(approval.get("command") or "", 200) or None,
                "requested_at": approval.get("requested_at"),
            } if approval else None,
            "waiting_for": (job.waiting_for or None) if job.status == st.QUEUED else None,
            "blocked_by": blocked,
        }
        if job.done and job.error:
            row["error"] = _short(job.error, 300)
        return row

    def overview(self, workspace: str = "", status: str = "", finished: int = 20, conversation_id: str = "") -> list[Job]:
        """Jobs that have not ended plus the last `finished` ones; or only the given statuses (comma-separated,
        current or pre-0.5.0 names). conversation_id narrows to one client conversation."""
        rows = sorted(self.jobs.values(), key=lambda j: j.created_at, reverse=True)
        if workspace:
            name = workspace.strip().lower()
            rows = [j for j in rows if j.workspace == name]
        if conversation_id:
            rows = [j for j in rows if j.conversation_id == conversation_id.strip()]
        if status and status.strip():
            wanted = st.parse_filter(status)
            return [j for j in rows if j.status in wanted][:500]
        active = [j for j in rows if not j.done]
        ended = [j for j in rows if j.done][: max(0, min(finished, 500))]
        return active + ended

    async def wait(self, job: Job, timeout: float, *, stop_on_approval: bool = False) -> Job:
        """Wait until the job ends (or asks for approval) or the timeout passes, whichever is first."""

        def settled() -> bool:
            return job.done or (stop_on_approval and job.status == st.INPUT_REQUIRED)

        if settled() or timeout <= 0:
            return job
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not settled():
            left = deadline - loop.time()
            if left <= 0:
                break
            job.changed.clear()
            if settled():
                break
            try:
                await asyncio.wait_for(job.changed.wait(), left)
            except asyncio.TimeoutError:
                break
        return job

    async def follow(self, job: Job, after: int = 0, ping: float = 15) -> AsyncIterator[dict[str, Any]]:
        """Events with seq > after, then new ones as they come, until the job ends. A quiet spell of
        `ping` seconds yields {"type": "ping"}, so a stream can send a keepalive."""
        seen = max(0, after)
        while True:
            while seen < len(job.events):
                seen += 1
                yield job.events[seen - 1]
            if job.done:
                return
            job.changed.clear()
            if seen < len(job.events) or job.done:
                continue
            try:
                await asyncio.wait_for(job.changed.wait(), ping)
            except asyncio.TimeoutError:
                yield {"type": "ping"}

    # ---- remote calls and approvals --------------------------------------------------------

    def active_job(self, workspace: str) -> Job | None:
        """The job whose agent has a shell in this workspace. File jobs hold the workspace lock, so at most one."""
        for job in sorted(self.jobs.values(), key=lambda j: j.created_at, reverse=True):
            if job.workspace == workspace and job.status in {st.WORKING, st.INPUT_REQUIRED} and touches_files(job.profile):
                return job
        return None

    async def remote_call(
        self, workspace: str, tool: str, alias: str, hostname: str, user: str, command: str
    ) -> tuple[bool, str]:
        """Record an ssh/scp/sftp call in its job; hold it for a human on approval hosts."""
        alias = (alias or hostname or "?").strip()
        hostname = (hostname or alias).strip()
        target = f"{user}@{alias}" if user else alias
        label = f"{tool} {target}" + (f": {command}" if command and tool == "ssh" else "")
        label = label if len(label) <= 300 else label[:300] + "…"
        needs = self.settings.needs_approval(alias, hostname)
        job = self.active_job(workspace)
        if job is None:
            logger.warning("remote call outside a job workspace=%r %s", workspace, label)
            if needs:
                return False, f"{alias} needs approval, and no running job was found to ask in."
            return True, ""
        await self._emit(job, {"type": "tool", "name": tool, "phase": "start", "text": label,
                               "host": alias, "hostname": hostname, "command": command})
        if not needs:
            return True, ""
        async with job.approval_lock:
            if alias.lower() in job.approved_hosts:
                return True, ""
            if job.done:
                return False, "The job ended."
            approval = {
                "id": uuid.uuid4().hex[:12],
                "tool": tool,
                "host": alias,
                "hostname": hostname,
                "user": user,
                "command": command,
                "requested_at": _now(),
            }
            loop = asyncio.get_running_loop()
            job.approval_future = loop.create_future()
            job.pending_approval = approval
            job.approval_clock = loop.time()
            job.set_status(st.INPUT_REQUIRED)
            self.store.save(job)
            await self._emit(job, {"type": "approval", **approval, "text": f"Approval needed: {label}"})
            logger.info("job %s waits for approval: %s", job.id, label)
            try:
                allow, note = await asyncio.wait_for(
                    asyncio.shield(job.approval_future), self.settings.approval_timeout
                )
            except asyncio.TimeoutError:
                # Nobody answered: the whole job ends, it does not go on without the command.
                note = self._approval_timeout_reason(job)
                allow = False
                self.request_stop(job, st.CANCELLED, note)
            finally:
                job.approval_future = None
                job.pending_approval = None
                job.approval_clock = 0.0
                job.activity_clock = asyncio.get_running_loop().time()
                if not job.done and job.status != st.WORKING:
                    job.set_status(st.WORKING)
                    self.store.save(job)
            if allow:
                job.approved_hosts.add(alias.lower())
                await self._emit(job, {"type": "status", "text": f"Approved: {alias}" + (f" ({note})" if note else "")})
                return True, ""
            await self._emit(job, {"type": "status", "text": f"Denied: {alias}" + (f" ({note})" if note else "")})
            return False, (
                f"The user did not approve {tool} to {alias}"
                + (f": {note}" if note else ".")
                + " Do not retry or work around this; report it in your answer."
            )

    def decide(self, job_id: str, allow: bool, note: str = "") -> Job:
        job = self.jobs.get(job_id)
        if job is None:
            raise JobError("Job not found.", status=404)
        future = job.approval_future
        if job.status != st.INPUT_REQUIRED or future is None or future.done():
            raise JobError("This job is not waiting for an approval.", status=409)
        future.set_result((bool(allow), (note or "").strip()[:300]))
        # Switch now, not when the waiting call wakes up, so a caller that reads the job next sees it moving.
        job.pending_approval = None
        job.set_status(st.WORKING)
        self.store.save(job)
        job.changed.set()
        return job

    def pending_for(self, workspace: str, session: str, origin: str) -> Job | None:
        for job in sorted(self.jobs.values(), key=lambda j: j.created_at, reverse=True):
            if job.status == st.INPUT_REQUIRED and job.workspace == workspace and job.origin == origin:
                if not session or job.session == session:
                    return job
        return None

    async def aclose(self) -> None:
        # From here on, MCP calls that end because the server stops do not count as client disconnects.
        self.closing = True
        running = [j.task_handle for j in self.jobs.values() if j.task_handle is not None and not j.done]
        for handle in running:
            handle.cancel()
        if running:
            await asyncio.gather(*running, return_exceptions=True)


def _swallow(task: asyncio.Future) -> None:
    """Retrieve the result of an engine task nobody waits for any more, so it is not logged as lost."""
    if not task.cancelled():
        task.exception()
