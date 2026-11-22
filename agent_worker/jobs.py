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
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from agent_worker import fallback as fb
from agent_worker import i18n
from agent_worker import proctree
from agent_worker import tasks as st
from agent_worker.engine import JOB_ENV, Engine, EngineError, Outcome, RunSpec, git_changes, git_head
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
# Event fields that only feed the in-memory handoff journal: never stored, streamed or listed.
_JOURNAL_ONLY = ("detail", "output", "failed")
# Earlier turns of a session handed to an engine that takes over (count, characters per field).
_HISTORY_TURNS = 10
_HISTORY_CHARS = 2000


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


def _step(key: str, **params: Any) -> dict[str, Any]:
    """A status event: English text (stored, for the API and the model), the i18n message for people."""
    message = i18n.msg(key, **params)
    return {"type": "status", "text": i18n.english(message), "i18n": message}


def _short(text: str, limit: int) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _clip(text: str, limit: int) -> str:
    text = str(text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def fb_stamp(stamp: str) -> float | None:
    parsed = _parse(stamp)
    return parsed.timestamp() if parsed is not None else None


class _Slot:
    """One MAX_CONCURRENT_JOBS slot that a job can give back while it waits for an engine, and take again."""

    def __init__(self, semaphore: asyncio.Semaphore) -> None:
        self._semaphore = semaphore
        self.held = False

    async def acquire(self) -> None:
        if not self.held:
            await self._semaphore.acquire()
            self.held = True

    def release(self) -> None:
        if self.held:
            self.held = False
            self._semaphore.release()


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
    # Engine fallback: every engine the job was tried on, in order (agent_worker.fallback.Attempt rows).
    attempts: list[dict[str, Any]] = field(default_factory=list)
    # Tool calls so far (with what they did and returned), in memory only: for a handoff to the next engine.
    journal: list[dict[str, Any]] = field(default_factory=list, repr=False)
    # While queued for an engine (waiting_for == "engine"): why, and when it goes on.
    wait_note: str = ""
    # The same texts as i18n messages, rendered per language for people (MCP progress, /v1 chat); in memory.
    wait_i18n: dict[str, Any] | None = field(default=None, repr=False)
    stop_i18n: dict[str, Any] | None = field(default=None, repr=False)
    error_i18n: dict[str, Any] | None = field(default=None, repr=False)

    @property
    def done(self) -> bool:
        return self.status in TERMINAL

    def set_status(self, status: str) -> None:
        self.status = status
        self.updated_at = _now()

    def public_status(self) -> str:
        return st.legacy(self.status) if self.legacy_names else self.status

    def status_message(self, blocked_by: str | None = None, lang: str = i18n.DEFAULT) -> str:
        """What the status means right now, for people and for the MCP Task statusMessage.

        lang: the reader's language (i18n); the status name in front and commands stay as they are."""
        if self.status == st.QUEUED and self.waiting_for == "engine":
            note = i18n.render(lang, self.wait_i18n) if self.wait_i18n else self.wait_note
            return "queued: " + (note or i18n.t(lang, "queue.engine"))
        if self.status == st.QUEUED:
            key = {"workspace": "queue.workspace", "session": "queue.session", "slot": "queue.slot"}.get(
                self.waiting_for, "queue.start")
            return "queued: " + i18n.t(lang, key) + (f" (job {blocked_by})" if blocked_by else "")
        if self.status == st.INPUT_REQUIRED:
            approval = self.pending_approval or {}
            if not approval:
                return "input_required: " + i18n.t(lang, "input.answer")
            target = f"{approval.get('user')}@{approval.get('host')}" if approval.get("user") else approval.get("host")
            command = _short(approval.get("command") or "", 160)
            return ("input_required: " + i18n.t(lang, "input.approve", tool=approval.get("tool", "ssh"), target=target)
                    + (f": {command}" if command else "") + " " + i18n.t(lang, "input.how", job=self.id))
        if self.status == st.WORKING:
            step, _ = self.last_step(lang)
            return step or i18n.t(lang, "progress.working")
        if self.status == st.COMPLETED:
            return _short(self.result, 200) or "completed"
        if self.error_i18n and lang != i18n.DEFAULT:
            return _short(i18n.render(lang, self.error_i18n), 300)
        return _short(self.error, 300) or self.status

    def last_step(self, lang: str = i18n.DEFAULT) -> tuple[str, str]:
        """(text, at) of the latest step (tool call, status, error, approval) and the time of the latest event."""
        step = ""
        for event in reversed(self.events):
            if event.get("type") in _STEP_TYPES:
                step = i18n.event_text(event, lang)
                if step:
                    break
        last = self.events[-1].get("at", "") if self.events else ""
        return _short(step, 200), last

    def engines_used(self) -> list[str]:
        """Engines that ran this job (skipped ones not), in order."""
        out: list[str] = []
        for row in self.attempts:
            name = row.get("engine")
            if row.get("status") != "skipped" and name and (not out or out[-1] != name):
                out.append(name)
        return out

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
            # Engine fallback (FALLBACK_ENGINES): which engines ran, and every try with why it ended.
            "engines_used": self.engines_used() or None,
            "fallback": self.attempts or None,
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
        # Engine fallback chain: which engines are blocked until when (see agent_worker.fallback).
        self.health = fb.EngineHealth(
            settings.fallback_backoff,
            lock_seconds=settings.fallback_lock_seconds,
            promote=settings.fallback_promote,
            path=settings.data_dir / "fallback-state.json",
        )
        self.wait_poll = fb.WAIT_POLL_SECONDS
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
                attempts=list(data.get("fallback") or []),
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
        model = (req.model or "").strip()
        try:
            if self.fallback_on and not (req.engine or "").strip() and not model:
                # Nothing asked for: the chain's first engine, with its model.
                engine, model = self.settings.fallback_engines[0]
            else:
                engine = pick(req.engine) if callable(pick) else "cursor"
        except EngineError as exc:
            raise JobError(str(exc)) from exc
        if self.fallback_on and not model:
            model = dict(self.settings.fallback_engines).get(engine, "")
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
            model=model,
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
        if event.get("type") == "tool":
            job.journal.append({**event, "engine": job.engine})
            if any(key in event for key in _JOURNAL_ONLY):
                event = {k: v for k, v in event.items() if k not in _JOURNAL_ONLY}
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
            reason = job.stop_i18n if job.stop_reason else i18n.msg("reason.not_started")
            self._finish(job, st.CANCELLED, error=job.stop_reason or i18n.english(reason), error_i18n=reason)

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
                    step = i18n.msg("step.wait_workspace" if key[0] == "workspace" else "step.wait_session",
                                    holder=holder or "?")
                    await self._emit(job, {"type": "status", "text": i18n.english(step), "i18n": step})
                await lock.acquire()
                job.held.append(key)
                self._holders[key] = job.id
            slot = _Slot(self._slots)
            await self._take_slot(job, slot)
            try:
                job.waiting_for, job.waiting_key = "", ()
                job.set_status(st.WORKING)
                job.started_at = _now()
                job.started_clock = job.activity_clock = loop.time()
                self.store.save(job)
                await self._emit(job, _step("step.running"))
                try:
                    if self.fallback_on:
                        outcome = await self._run_chain(job, spec, slot)
                    else:
                        outcome = await self._supervise(job, spec)
                except _Stopped:
                    status, message = job.stop_status or st.CANCELLED, job.stop_reason or "Cancelled."
                    reason = job.stop_i18n if job.stop_reason else i18n.msg("reason.cancelled")
                    await self._emit(job, {"type": "error" if status == st.FAILED else "status", "message": message,
                                           "text": message, **({"i18n": reason} if reason else {})})
                    self._finish(job, status, error=message, error_i18n=reason)
                    await self._cleanup(job, job.spec or spec)
                    return
            finally:
                slot.release()
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
            used = job.engines_used()
            if len(used) > 1:
                await self._emit(job, _step("step.engines", engines=" → ".join(used)))
            if status == st.COMPLETED:
                await self._emit(job, {"type": "result", "text": outcome.text})
            else:
                await self._emit(job, {"type": "error", "message": outcome.error})
            self._finish(job, status, **fields)
        except asyncio.CancelledError:
            await self._emit(job, _step("step.cancelled"))
            reason = job.stop_i18n if job.stop_reason else (None if job.error else i18n.msg("reason.cancelled"))
            self._finish(job, st.CANCELLED, error=job.stop_reason or job.error or "Cancelled.", error_i18n=reason)
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

    # ---- engine fallback ---------------------------------------------------------------------

    @property
    def fallback_on(self) -> bool:
        """FALLBACK_ENGINES names a chain and the worker has several engines (an EngineRouter)."""
        return bool(self.settings.fallback_engines) and isinstance(getattr(self.engine, "engines", None), dict)

    def engine_signed_in(self, name: str) -> None:
        """A new credential for an engine: an auth block from the old one no longer holds. Waiting jobs
        notice on their next look (at most wait_poll seconds)."""
        state = self.health.state(name)
        if state is not None and state.kind == fb.AUTH:
            self.health.mark_ok(name)

    async def _take_slot(self, job: Job, slot: _Slot) -> None:
        if self._slots.locked():
            job.waiting_for, job.waiting_key = "slot", ()
            await self._emit(job, _step("step.wait_slot", limit=self.settings.max_concurrent_jobs))
        await slot.acquire()

    def _chain(self, spec: RunSpec) -> list[tuple[str, str]]:
        """The engines to try, in order: the job's own engine first, then the chain below it (all of it when
        the job's engine is not part of the chain)."""
        chain = list(self.settings.fallback_engines)
        names = [name for name, _ in chain]
        if spec.engine in names:
            at = names.index(spec.engine)
            rest = chain[at + 1 :]
        else:
            rest = chain
        return [(spec.engine, spec.model)] + [(name, model) for name, model in rest if name != spec.engine]

    def _ready(self, name: str) -> bool:
        """The cheap probe: is the engine set up and signed in (auth_status "ready")? No model prompt."""
        engine = (getattr(self.engine, "engines", None) or {}).get(name)
        try:
            return bool(engine is not None and engine.available)
        except Exception:
            return False

    def _previous_job(self, job: Job) -> Job | None:
        """The last job of the same session before this one."""
        if not job.session:
            return None
        rows = [other for other in self.jobs.values()
                if other is not job and other.workspace == job.workspace and other.session == job.session
                and other.created_at <= job.created_at and other.done and other.started_at]
        return max(rows, key=lambda other: other.created_at, default=None)

    def _history(self, job: Job) -> str:
        """Earlier turns of the session, for an engine that has never seen it."""
        if not job.session:
            return ""
        rows = sorted((other for other in self.jobs.values()
                       if other is not job and other.workspace == job.workspace and other.session == job.session
                       and other.created_at <= job.created_at and other.status == st.COMPLETED),
                      key=lambda other: other.created_at)[-_HISTORY_TURNS:]
        turns = []
        for other in rows:
            turns.append(f"User: {_clip(other.task, _HISTORY_CHARS)}\nAssistant: {_clip(other.result, _HISTORY_CHARS)}")
        return "\n\n".join(turns)

    def _start_index(self, job: Job, chain: list[tuple[str, str]]) -> tuple[int, str]:
        """Where in the chain this turn starts, and the engine that served the session last ('' if none).
        Promotion only happens here (a job's start), never in the middle of a run."""
        previous = self._previous_job(job)
        last = (previous.engine if previous is not None else "") or ""
        names = [name for name, _ in chain]
        if not last or last not in names or names.index(last) == 0:
            return 0, last
        at = names.index(last)
        fell_back = previous is not None and len(previous.engines_used()) > 1 or (
            previous is not None and previous.attempts and previous.attempts[0].get("engine") != last)
        if self.settings.fallback_promote == "off":
            # The session stays on the engine that took over.
            return at, last
        finished = fb_stamp(previous.finished_at) if previous is not None else None
        if fell_back and finished is not None and self.health.clock() - finished < self.settings.fallback_lock_seconds:
            # It only just switched: no flapping back and forth.
            return at, last
        return 0, last

    def _next_engine(self, job: Job, chain: list[tuple[str, str]], start: int, noted: set[str]):
        """(index, engine, model) of the first usable engine from `start`; skipped ones are noted once."""
        for index in range(start, len(chain)):
            name, model = chain[index]
            if not self._ready(name):
                if name not in noted:
                    noted.add(name)
                    job.attempts.append(fb.Attempt(name, model, "skipped", "nicht angemeldet").as_dict())
                continue
            if self.health.blocked_until(name):
                if name not in noted:
                    noted.add(name)
                    job.attempts.append(fb.Attempt(name, model, "skipped", self.health.note(name)).as_dict())
                continue
            return index, name, model
        return None

    def _wait_note(self, chain: list[tuple[str, str]]) -> tuple[dict[str, Any], float | None]:
        notes: list[Any] = []
        wake = None
        for name, _ in chain:
            if not self._ready(name):
                notes.append(i18n.msg("fb.not_signed_in", engine=name))
                continue
            until = self.health.blocked_until(name)
            if until:
                notes.append(self.health.note_msg(name) or name)
                wake = until if wake is None else min(wake, until)
        when = (i18n.msg("wait.resume_at", time=fb.clock(wake)) if wake else i18n.msg("wait.resume_signin"))
        return i18n.msg("wait.no_engine", notes=notes, when=when), wake

    async def _wait_for_engine(self, job: Job, chain: list[tuple[str, str]], slot: _Slot) -> None:
        """No engine of the chain can run: wait (without a slot) until one can, instead of failing."""
        loop = asyncio.get_running_loop()
        message, wake = self._wait_note(chain)
        note = i18n.english(message)
        job.wait_note, job.wait_i18n, job.waiting_for, job.waiting_key = note, message, "engine", ()
        job.set_status(st.QUEUED)
        self.store.save(job)
        await self._emit(job, {"type": "status", "text": note, "i18n": message})
        logger.info("job %s waits for an engine: %s", job.id, note)
        slot.release()
        paused = loop.time()
        try:
            while True:
                delay = self.wait_poll
                if wake is not None:
                    delay = min(delay, max(0.01, wake - self.health.clock()))
                try:
                    await asyncio.wait_for(job.stop.wait(), delay)
                except asyncio.TimeoutError:
                    pass
                if job.stop.is_set():
                    raise _Stopped()
                if any(self._ready(name) and not self.health.blocked_until(name) for name, _ in chain):
                    break
                fresh, wake = self._wait_note(chain)
                if i18n.english(fresh) != job.wait_note:
                    job.wait_note, job.wait_i18n = i18n.english(fresh), fresh
                    job.changed.set()
            await self._take_slot(job, slot)
        finally:
            job.wait_note, job.wait_i18n = "", None
        if job.stop.is_set():
            slot.release()
            raise _Stopped()
        # Waiting is no runtime: the watchdogs count from here.
        now = loop.time()
        job.started_clock += now - paused
        job.activity_clock = now
        job.waiting_for, job.waiting_key = "", ()
        job.set_status(st.WORKING)
        self.store.save(job)
        await self._emit(job, _step("step.engine_back"))

    def _forget(self, name: str, spec: RunSpec) -> None:
        """The engine's own session for this conversation is stale (it missed turns on another engine)."""
        engine = (getattr(self.engine, "engines", None) or {}).get(name)
        forget = getattr(engine, "forget", None)
        if spec.session and callable(forget):
            try:
                forget(spec.workspace, spec.session)
            except Exception:
                logger.debug("forget %s session failed", name, exc_info=True)

    async def _run_chain(self, job: Job, spec: RunSpec, slot: _Slot) -> Outcome:
        """Run the job down the fallback chain: on a provider-side failure the next engine takes over (same
        job, with a handoff). No engine left: wait for one. Raises _Stopped and EngineError like _supervise."""
        chain = self._chain(spec)
        start, last = self._start_index(job, chain)
        noted: set[str] = set()
        handoff: fb.Handoff | None = None
        start_head = await git_head(spec.path)
        first = True
        while True:
            pick = self._next_engine(job, chain, start, noted)
            if pick is None:
                await self._wait_for_engine(job, chain, slot)
                # After a wait the whole chain is tried again, from the top.
                start, noted = 0, set()
                continue
            index, name, model = pick
            run = replace(spec, engine=name, model=model)
            if handoff is not None:
                run.task, run.context = fb.handoff_message(handoff, name), ""
                self._forget(name, spec)
            elif spec.session and last and last != name:
                # The session moves to another engine (back up, or down at the start): give it the conversation.
                self._forget(name, spec)
                if not run.context.strip():
                    run.context = self._history(job)
            if first and last and name != last:
                names = [n for n, _ in chain]
                if last in names and names.index(name) < names.index(last):
                    await self._emit(job, _step("step.promoted", engine=name, previous=last))
            first = False
            job.engine, job.model = name, model or self.default_model(name)
            job.spec = run
            attempt = fb.Attempt(name, job.model, "running", started_at=_now())
            job.attempts.append(attempt.as_dict())
            self.store.save(job)
            mark = len(job.events)
            failure: fb.Failure | None = None
            try:
                outcome = await self._supervise(job, run)
            except EngineError as exc:
                failure = fb.classify(exc)
                if failure is None:
                    job.attempts[-1] = {**attempt.as_dict(), "status": "failed", "reason": _short(str(exc), 200),
                                        "finished_at": _now()}
                    raise
                outcome = None
            else:
                if st.normalize(outcome.status) == st.COMPLETED:
                    self.health.mark_ok(name)
                else:
                    failure = fb.classify(outcome)
                if failure is None:
                    ended = "completed" if st.normalize(outcome.status) == st.COMPLETED else "failed"
                    job.attempts[-1] = {**attempt.as_dict(), "status": ended, "finished_at": _now(),
                                        **({"reason": _short(outcome.error, 200)} if outcome.error else {})}
                    return outcome
            # Provider-side failure: block the engine, hand the job to the next one.
            self.health.mark_failed(name, failure)
            reason_msg = fb.describe_msg(name, failure)
            reason = i18n.english(reason_msg)
            job.attempts[-1] = {**attempt.as_dict(), "status": "handed_off", "reason": reason,
                                "kind": failure.kind, "finished_at": _now()}
            logger.info("job %s: %s (%s)", job.id, reason, failure.message)
            try:
                await proctree.kill_tagged(JOB_ENV, job.id)
            except Exception:
                logger.debug("job %s: leftovers of %s not killed", job.id, name, exc_info=True)
            partial = "".join(str(e.get("delta") or "") for e in job.events[mark:] if e.get("type") == "text")
            if handoff is None:
                handoff = fb.Handoff(task=spec.task, history=spec.context.strip() or self._history(job))
            handoff.reasons.append(reason)
            handoff.partial.append((name, partial))
            handoff.journal = list(job.journal)
            handoff.changes = await git_changes(spec.path, start_head)
            if partial.strip():
                await self._emit(job, {"type": "text", "delta": "\n\n"})
            following = self._next_engine(job, chain, index + 1, noted)
            if following is not None:
                await self._emit(job, _step("step.handoff", reason=reason_msg, engine=following[1]))
            else:
                await self._emit(job, _step("step.no_engine_left", reason=reason_msg))
            start = index + 1

    def _overdue(self, job: Job, now: float) -> tuple[str, dict[str, Any]] | None:
        """(status, reason as an i18n message) when a watchdog limit is past, else None."""
        settings = self.settings
        if job.status == st.INPUT_REQUIRED:
            if job.approval_clock and now - job.approval_clock >= settings.approval_timeout:
                return st.CANCELLED, self._approval_timeout_reason(job)
        elif settings.job_idle_timeout > 0 and now - job.activity_clock >= settings.job_idle_timeout:
            return st.FAILED, i18n.msg("reason.idle", seconds=settings.job_idle_timeout)
        if now - job.started_clock >= settings.job_timeout:
            return st.FAILED, i18n.msg("reason.runtime", seconds=settings.job_timeout)
        return None

    def _approval_timeout_reason(self, job: Job) -> dict[str, Any]:
        approval = job.pending_approval or {}
        what = i18n.msg("reason.approval_for", tool=approval.get("tool", "ssh"), host=approval.get("host", "")) \
            if approval else ""
        return i18n.msg("reason.approval_timeout", seconds=self.settings.approval_timeout, what=what)

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

    def request_stop(self, job: Job, status: str, reason: str | dict[str, Any]) -> bool:
        """Ask a running job to stop; it ends as `status` with `reason` as its error. The first reason wins.

        reason: plain text, or an i18n.msg() (stored in English, shown to people in their language)."""
        if job.done or job.stop.is_set():
            return False
        reason, job.stop_i18n = i18n.split(reason)
        job.stop_status, job.stop_reason = st.normalize(status) or st.CANCELLED, reason
        if job.approval_future is not None and not job.approval_future.done():
            job.approval_future.set_result((False, reason))
        job.stop.set()
        logger.info("job %s stopping: %s", job.id, reason)
        return True

    def cancel(self, job_id: str, reason: str | dict[str, Any] | None = None) -> Job | None:
        job = self.jobs.get(job_id)
        if job is None:
            return None
        if job.done:
            return job
        if job.started_at:
            self.request_stop(job, st.CANCELLED, reason or i18n.msg("reason.cancelled"))
        elif job.task_handle is not None:
            if not job.stop_reason:
                job.stop_reason, job.stop_i18n = i18n.split(reason or i18n.msg("reason.cancelled"))
            job.task_handle.cancel()
        return job

    def cancel_workspace(self, workspace: str, reason: str | dict[str, Any] | None = None) -> list[Job]:
        """Cancel every job in the workspace that has not ended (queued ones first, so none starts in between)."""
        name = (workspace or "").strip().lower() or self.settings.default_workspace
        pending = [j for j in self.jobs.values() if j.workspace == name and not j.done]
        pending.sort(key=lambda j: (bool(j.started_at), j.created_at))
        for job in pending:
            self.cancel(job.id, reason)
        return pending

    def cancel_conversation(self, conversation_id: str, reason: str | dict[str, Any] | None = None) -> list[Job]:
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
            await self._emit(job, {"type": "approval", **approval, "text": f"Approval needed: {label}",
                                   "i18n": i18n.msg("step.approval_needed", label=label)})
            logger.info("job %s waits for approval: %s", job.id, label)
            try:
                allow, note = await asyncio.wait_for(
                    asyncio.shield(job.approval_future), self.settings.approval_timeout
                )
            except asyncio.TimeoutError:
                # Nobody answered: the whole job ends, it does not go on without the command.
                timed_out = self._approval_timeout_reason(job)
                note = i18n.english(timed_out)
                allow = False
                self.request_stop(job, st.CANCELLED, timed_out)
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
                await self._emit(job, _step("step.approved", alias=alias, note=f" ({note})" if note else ""))
                return True, ""
            await self._emit(job, _step("step.denied", alias=alias, note=f" ({note})" if note else ""))
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
