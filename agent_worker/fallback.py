"""Engine fallback chain: when an engine fails on the provider side, the job goes on with the next one.

Off unless FALLBACK_ENGINES names a chain (e.g. "claude,cursor,codex:gpt-5.5"); without it every job runs
on exactly one engine, as before.

What counts as a provider-side failure (classify): a plan/usage/token limit, 429, an expired or invalid
credential (or none at all), timeouts and connection errors, 5xx and "overloaded". Never: a cancel, a
watchdog stop, a refused approval, a refusal or other answer of the model, tool errors, invalid input
(bad request, unknown model, context too long). Those end the job as before.

Engine health (EngineHealth): an engine that failed is blocked until its known reset time ("resets 18:30",
"try again in 2 hours"), else for a backoff step (FALLBACK_BACKOFF, default 5m,15m,60m; every failure in a
row takes the next step). The cheap probe before an engine is used again is its sign-in status (the same as
auth_status "ready"): no model prompt is sent. A short lock (FALLBACK_LOCK_SECONDS) keeps a blocked engine
out for at least that long and keeps a session from switching back and forth.

FALLBACK_PROMOTE says when a session goes back up to its preferred engine. Promotion only happens at the
start of a job (a turn), never in the middle of one, and always tries the chain from the top:
  turn   (default) at the next turn once the engine's block is over (reset time passed, or backoff over)
  reset  only after a known reset time; without one, after the longest backoff step
  off    never: a session that fell back stays on the engine that took over
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from agent_worker import i18n

logger = logging.getLogger(__name__)

PROMOTE_MODES = ("turn", "reset", "off")
DEFAULT_BACKOFF = (300, 900, 3600)
DEFAULT_LOCK_SECONDS = 120
# How often a job with no engine left looks again (an engine may get signed in meanwhile).
WAIT_POLL_SECONDS = 60.0

# Failure kinds, in the words a person reads in status lines.
LIMIT = "limit"
RATE_LIMIT = "rate_limit"
AUTH = "auth"
TIMEOUT = "timeout"
CONNECTION = "connection"
SERVER = "server"
KINDS = (LIMIT, RATE_LIMIT, AUTH, TIMEOUT, CONNECTION, SERVER)


# ---- configuration ----------------------------------------------------------------------------


def parse_chain(raw: str, known: Iterable[str]) -> tuple[tuple[str, str], ...]:
    """'claude,cursor:composer-2.5, codex' -> (('claude', ''), ('cursor', 'composer-2.5'), ('codex', '')).
    Unknown engines and repeats are dropped."""
    names = {name.lower() for name in known}
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for part in (raw or "").replace("\n", ",").split(","):
        item = part.strip()
        if not item:
            continue
        engine, _, model = item.partition(":")
        engine = engine.strip().lower()
        if engine not in names or engine in seen:
            if engine and engine not in names:
                logger.warning("FALLBACK_ENGINES: unknown engine %r ignored", engine)
            continue
        seen.add(engine)
        out.append((engine, model.strip()))
    return tuple(out)


_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*$", re.IGNORECASE)
_UNIT = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(raw: str) -> int | None:
    match = _DURATION.match(raw or "")
    if not match:
        return None
    return int(float(match.group(1)) * _UNIT[match.group(2).lower()])


def parse_backoff(raw: str) -> tuple[int, ...]:
    """'5m,15m,60m' -> (300, 900, 3600). Bad or empty values give the default."""
    steps = [parse_duration(part) for part in (raw or "").split(",") if part.strip()]
    if not steps or any(step is None or step <= 0 for step in steps):
        if (raw or "").strip():
            logger.warning("FALLBACK_BACKOFF=%r not understood, using 5m,15m,60m", raw)
        return DEFAULT_BACKOFF
    return tuple(int(step) for step in steps if step)


def promote_mode(raw: str) -> str:
    value = (raw or "").strip().lower()
    if value in {"false", "no", "0", "never", "none"}:
        return "off"
    return value if value in PROMOTE_MODES else "turn"


# ---- classifier -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Failure:
    """A provider-side failure: the job may go on with another engine."""

    kind: str
    message: str
    reset_at: float | None = None  # epoch seconds, when the provider said


# Structured error codes from the engines (Claude Code's assistant error, Codex's codexErrorInfo).
_CODE_KINDS = {
    # Claude Code
    "authentication_failed": AUTH,
    "billing_error": LIMIT,
    "rate_limit": RATE_LIMIT,
    "server_error": SERVER,
    "overloaded_error": SERVER,
    # Codex app-server
    "unauthorized": AUTH,
    "usagelimitexceeded": LIMIT,
    "ratelimitexceeded": RATE_LIMIT,
    "serveroverloaded": SERVER,
    "internalservererror": SERVER,
    "httpconnectionfailed": CONNECTION,
    "responsestreamconnectionfailed": CONNECTION,
    "responsestreamdisconnected": CONNECTION,
    "responsetoomanyfailedattempts": SERVER,
}
# Codes that are never a reason to switch, whatever the text says.
_CODE_NEVER = frozenset({
    "invalid_request", "badrequest", "contextwindowexceeded", "modelnotfound", "sandboxerror",
    "max_output_tokens",
})

# Checked first: these end the job, they are no provider failure.
_NEVER = (
    "cancelled", "canceled", "did not approve", "approval timed out", "not approved",
    "job_max_runtime", "job_idle_timeout", "content policy", "content filter", "usage policy",
    "safety system", "prompt is too long", "context length", "context window", "maximum context",
    "too many tokens in the prompt", "does not offer the model", "model not found", "unknown model",
    "invalid model", "not a valid model", "nested worker jobs", "ssh: ", "scp: ", "sftp: ",
    "permission denied (publickey",
)
_LIMIT = (
    "usage limit", "plan limit", "limit reached", "hit your limit", "hit its usage", "hit the usage",
    "reached your limit", "out of credits", "credit balance", "insufficient_quota", "insufficient quota",
    "quota exceeded", "exceeded your current quota", "billing", "token limit", "weekly limit",
    "5-hour limit", "session limit", "usagelimitexceeded", "spending limit", "monthly limit",
)
_RATE = ("rate limit", "rate_limit", "ratelimit", "too many requests", "rate-limit")
_AUTH = (
    "authentication", "unauthorized", "unauthenticated", "invalid api key", "invalid x-api-key",
    "invalid_api_key", "incorrect api key", "api key is invalid", "token expired", "token has expired",
    "expired token", "oauth token", "refresh token", "invalid_grant", "sign in again", "log in again",
    "login again", "not signed in", "credential is set", "api key is set", "refused the", "revoked",
    "forbidden", "invalid authentication", "invalid bearer",
)
_SERVER = (
    "overloaded", "internal server error", "bad gateway", "service unavailable", "server error",
    "server_error", "upstream", "temporarily unavailable", "capacity", "try again later",
)
_TIMEOUT = ("timed out", "timeout", "time out", "deadline exceeded", "gateway timeout")
_CONNECTION = (
    "connection refused", "connection reset", "connection error", "connection aborted", "connection closed",
    "connecterror", "connection failed", "failed to connect", "could not connect", "network error",
    "network is unreachable", "econnreset", "econnrefused", "enotfound", "getaddrinfo", "name resolution",
    "temporary failure in name", "broken pipe", "stream disconnected", "disconnected", "remote end closed",
    "server disconnected", "unexpected eof", "socket hang up",
)
_STATUS = re.compile(r"(?:\b(?:http|status|error|code)\b[^0-9\n]{0,12}|^|[\s(\[])(401|403|429|5\d\d)\b", re.IGNORECASE)
_EXC_KINDS = {
    "timeouterror": TIMEOUT, "readtimeout": TIMEOUT, "connecttimeout": TIMEOUT, "apitimeouterror": TIMEOUT,
    "connecterror": CONNECTION, "connectionerror": CONNECTION, "connectionrefusederror": CONNECTION,
    "connectionreseterror": CONNECTION, "apiconnectionerror": CONNECTION, "remoteprotocolerror": CONNECTION,
    "brokenpipeerror": CONNECTION, "ratelimiterror": RATE_LIMIT, "authenticationerror": AUTH,
    "permissiondeniederror": AUTH, "internalservererror": SERVER, "overloadederror": SERVER,
    "serviceunavailableerror": SERVER,
}


def _has(text: str, needles: tuple[str, ...]) -> bool:
    return any(needle in text for needle in needles)


def _kind_of_text(text: str) -> str:
    low = text.lower()
    if not low.strip() or _has(low, _NEVER):
        return ""
    if _has(low, _LIMIT):
        return LIMIT
    status = next((m.group(1) for m in _STATUS.finditer(low)), "")
    if status == "429" or _has(low, _RATE):
        # "rate limit" with a reset well in the future is a plan limit in all but name.
        return RATE_LIMIT
    if status in {"401", "403"} or _has(low, _AUTH):
        return AUTH
    if _has(low, _TIMEOUT) or status in {"504", "408"}:
        return TIMEOUT
    if _has(low, _CONNECTION):
        return CONNECTION
    if status.startswith("5") or _has(low, _SERVER):
        return SERVER
    return ""


def _exception_kind(exc: BaseException | None) -> str:
    seen = 0
    while exc is not None and seen < 6:
        name = type(exc).__name__.lower()
        if name in _EXC_KINDS:
            return _EXC_KINDS[name]
        exc = exc.__cause__ or exc.__context__
        seen += 1
    return ""


def classify(error: Any = None, *, code: str = "", now: float | None = None) -> Failure | None:
    """A Failure if this error is provider-side (switching engines may help), else None.

    `error` is an exception (EngineError and its cause chain), an Outcome-like object (error, error_kind)
    or the error text. `code` is a structured error code from the engine, when there is one."""
    text = ""
    exc: BaseException | None = None
    if isinstance(error, BaseException):
        exc = error
        text = str(error)
        code = code or str(getattr(error, "kind", "") or "")
    elif isinstance(error, str):
        text = error
    elif error is not None:
        text = str(getattr(error, "error", "") or "")
        code = code or str(getattr(error, "error_kind", "") or "")
    low_code = (code or "").strip().lower()
    if low_code in _CODE_NEVER or _has(text.lower(), _NEVER):
        return None
    kind = _CODE_KINDS.get(low_code, "") or _kind_of_text(text)
    if not kind and exc is not None:
        if isinstance(exc, (TimeoutError,)):
            kind = TIMEOUT
        else:
            kind = _exception_kind(exc.__cause__ or exc.__context__)
    if not kind:
        return None
    # Only limits come with a reset time; "timed out after 60 s" is no promise of anything.
    reset = reset_time(text, now=now) if kind in {LIMIT, RATE_LIMIT} else None
    if kind == RATE_LIMIT and reset is not None and reset - (now or time.time()) > 30 * 60:
        kind = LIMIT
    return Failure(kind=kind, message=" ".join(text.split())[:300], reset_at=reset)


# ---- reset times ------------------------------------------------------------------------------

_EPOCH = re.compile(r"(?:\||resets?_?at[\"':= ]+|reset[\"':= ]+)(1[6-9]\d{8})\b", re.IGNORECASE)
_ISO = re.compile(r"(?:reset|retry|again|until)[^0-9\n]{0,20}(\d{4}-\d{2}-\d{2}[t ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:z|[+-]\d{2}:?\d{2})?)",
                  re.IGNORECASE)
_IN = re.compile(r"(?:in|after)\s+((?:\d+(?:\.\d+)?\s*(?:seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|days?|d)\b[\s,and]*)+)",
                 re.IGNORECASE)
_PART = re.compile(r"(\d+(?:\.\d+)?)\s*(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|days?|d)\b", re.IGNORECASE)
_RETRY_AFTER = re.compile(r"retry[-_ ]after[\"':= ]+(\d+)", re.IGNORECASE)
_CLOCK = re.compile(
    r"(?:resets?|reset at|try again at|available (?:again )?at|again at|until)\s+(?:at\s+)?(?:(?:today|tomorrow)\s+)?(?:at\s+)?"
    r"(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm|uhr)?(?:\s*\(([A-Za-z_]+/[A-Za-z_/]+|UTC)\))?",
    re.IGNORECASE,
)


def _seconds(unit: str) -> int:
    unit = unit.lower()
    if unit.startswith("d"):
        return 86400
    if unit.startswith("h"):
        return 3600
    if unit.startswith("m"):
        return 60
    return 1


def reset_time(text: str, *, now: float | None = None) -> float | None:
    """When the provider says the limit ends (epoch seconds), else None."""
    if not text:
        return None
    now = time.time() if now is None else now
    match = _EPOCH.search(text)
    if match:
        return float(match.group(1))
    match = _ISO.search(text)
    if match:
        raw = match.group(1).replace(" ", "T").replace("z", "+00:00").replace("Z", "+00:00")
        try:
            stamp = datetime.fromisoformat(raw)
            if stamp.tzinfo is None:
                stamp = stamp.astimezone()
            return stamp.timestamp()
        except ValueError:
            pass
    match = _RETRY_AFTER.search(text)
    if match:
        return now + int(match.group(1))
    match = _IN.search(text)
    if match:
        total = sum(float(n) * _seconds(u) for n, u in _PART.findall(match.group(1)))
        if total > 0:
            return now + total
    match = _CLOCK.search(text)
    if match:
        hour, minute, half, zone = int(match.group(1)), int(match.group(2) or 0), (match.group(3) or "").lower(), match.group(4)
        if half == "pm" and hour < 12:
            hour += 12
        elif half == "am" and hour == 12:
            hour = 0
        # A bare number ("until 3") is no time of day: it needs am/pm/Uhr or minutes.
        if hour > 23 or minute > 59 or not (half or match.group(2)):
            return None
        tz = None
        if zone:
            try:
                from zoneinfo import ZoneInfo

                tz = timezone.utc if zone.upper() == "UTC" else ZoneInfo(zone)
            except Exception:
                tz = None
        base = datetime.fromtimestamp(now, tz) if tz else datetime.fromtimestamp(now).astimezone()
        target = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target.timestamp() <= now:
            target += timedelta(days=1)
        return target.timestamp()
    return None


def clock(stamp: float | None) -> str:
    """18:30, or "Mo 18:30" when it is not today (container local time)."""
    if stamp is None:
        return ""
    at = datetime.fromtimestamp(stamp).astimezone()
    today = datetime.now().astimezone().date()
    if at.date() == today:
        return at.strftime("%H:%M")
    return at.strftime("%d.%m. %H:%M")


def describe_msg(engine: str, failure: Failure) -> i18n.Msg:
    """'claude at its usage limit (reset 18:30)' and the like, for status lines (rendered per language)."""
    reset = i18n.msg("fb.reset", time=clock(failure.reset_at)) if failure.reset_at else ""
    key = {
        LIMIT: "fb.limit",
        RATE_LIMIT: "fb.rate_limit",
        AUTH: "fb.auth",
        TIMEOUT: "fb.timeout",
        CONNECTION: "fb.connection",
        SERVER: "fb.server",
    }.get(failure.kind, "fb.failed")
    params = {"reset": reset} if key in {"fb.limit", "fb.rate_limit"} else {}
    return i18n.msg(key, engine=engine, **params)


def describe(engine: str, failure: Failure) -> str:
    """describe_msg() in English (attempt reasons, logs, the handoff to the next engine)."""
    return i18n.english(describe_msg(engine, failure))


# ---- engine health ----------------------------------------------------------------------------


@dataclass
class _State:
    blocked_until: float = 0.0
    kind: str = ""
    reason: str = ""
    reset_known: bool = False
    failures: int = 0
    failed_at: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "blocked_until": self.blocked_until, "kind": self.kind, "reason": self.reason,
            "reset_known": self.reset_known, "failures": self.failures, "failed_at": self.failed_at,
        }


class EngineHealth:
    """Which engines are blocked until when. Kept in DATA_DIR (fallback-state.json), so a known reset time
    survives a restart."""

    def __init__(self, backoff: tuple[int, ...] = DEFAULT_BACKOFF, *, lock_seconds: int = DEFAULT_LOCK_SECONDS,
                 promote: str = "turn", path: Path | None = None, clock: Callable[[], float] = time.time) -> None:
        self.backoff = tuple(backoff) or DEFAULT_BACKOFF
        self.lock_seconds = max(0, int(lock_seconds))
        self.promote = promote
        self.path = path
        self.clock = clock
        self._states: dict[str, _State] = {}
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        if self.path is None or not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text())
            for name, row in (data or {}).items():
                self._states[str(name)] = _State(**{k: row[k] for k in _State().as_dict() if k in row})
        except Exception:
            logger.warning("fallback state %s unreadable, starting fresh", self.path)

    def _save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({k: v.as_dict() for k, v in self._states.items()}))
            tmp.replace(self.path)
        except Exception:
            logger.debug("fallback state not saved", exc_info=True)

    def mark_failed(self, engine: str, failure: Failure) -> float:
        """Block the engine; returns until when (epoch seconds)."""
        now = self.clock()
        with self._lock:
            state = self._states.setdefault(engine, _State())
            step = self.backoff[min(state.failures, len(self.backoff) - 1)]
            if failure.reset_at and failure.reset_at > now:
                until = failure.reset_at
            elif self.promote == "reset":
                # Without a known reset time: the longest backoff step.
                until = now + max(self.backoff)
            else:
                until = now + step
            state.blocked_until = max(until, now + self.lock_seconds)
            state.kind, state.reason = failure.kind, failure.message
            state.reset_known = bool(failure.reset_at and failure.reset_at > now)
            state.failures += 1
            state.failed_at = now
            self._save()
            return state.blocked_until

    def mark_ok(self, engine: str) -> None:
        with self._lock:
            if engine in self._states:
                self._states.pop(engine, None)
                self._save()

    def blocked_until(self, engine: str) -> float:
        state = self._states.get(engine)
        if state is None:
            return 0.0
        return state.blocked_until if state.blocked_until > self.clock() else 0.0

    def state(self, engine: str) -> _State | None:
        return self._states.get(engine)

    def note_msg(self, engine: str) -> i18n.Msg | None:
        """Why an engine is blocked ('claude at its usage limit (reset 18:30)'), or None."""
        state = self._states.get(engine)
        if state is None or not self.blocked_until(engine):
            return None
        failure = Failure(state.kind, state.reason, state.blocked_until if state.reset_known else None)
        text = describe_msg(engine, failure)
        if not state.reset_known:
            return i18n.msg("fb.with_next_try", note=text, next=i18n.msg("fb.next_try", time=clock(state.blocked_until)))
        return text

    def note(self, engine: str) -> str:
        """note_msg() in English, or ''."""
        return i18n.english(self.note_msg(engine))

    def overview(self) -> dict[str, Any]:
        return {name: {**state.as_dict(), "blocked": bool(self.blocked_until(name))}
                for name, state in self._states.items()}


# ---- handoff ----------------------------------------------------------------------------------

_SIDE_EFFECTS = (
    "write", "edit", "patch", "notebook", "shell", "bash", "ssh", "scp", "sftp", "rsync", "command",
    "exec", "run", "git", "commit", "push", "deploy", "delete", "create", "mcp",
)


@dataclass
class Attempt:
    engine: str
    model: str
    status: str  # completed | failed | skipped | handed_off
    reason: str = ""
    started_at: str = ""
    finished_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v not in ("", None)}


@dataclass
class Handoff:
    """What the next engine has to know: the task, the conversation so far and what was done already."""

    task: str
    history: str = ""
    journal: list[dict[str, Any]] = field(default_factory=list)
    partial: list[tuple[str, str]] = field(default_factory=list)  # (engine, text)
    reasons: list[str] = field(default_factory=list)
    changes: dict[str, Any] | None = None


def _clip(text: Any, limit: int) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def render_journal(entries: list[dict[str, Any]], limit: int = 150) -> list[str]:
    """One line per tool call: engine, tool, what it did, how it ended."""
    calls: dict[str, dict[str, Any]] = {}
    order: list[dict[str, Any]] = []
    for entry in entries:
        key = str(entry.get("id") or "")
        if entry.get("phase") == "end" and key and key in calls:
            call = calls[key]
            call["end"] = entry
            continue
        if entry.get("phase") == "end" and not key and order and "end" not in order[-1]:
            order[-1]["end"] = entry
            continue
        call = {"start": entry}
        if key:
            calls[key] = call
        order.append(call)
    lines: list[str] = []
    for call in order[-limit:]:
        start, end = call["start"], call.get("end") or {}
        name = str(start.get("name") or "tool")
        what = _clip(start.get("detail") or start.get("command") or start.get("text") or "", 300)
        host = start.get("host")
        if host and host not in what:
            what = f"{host}: {what}"
        if end:
            status = "failed" if end.get("failed") or "fail" in str(end.get("text") or "").lower() else "done"
        else:
            status = "no result (interrupted)" if start.get("phase") == "start" else "done"
        mark = " (side effect)" if is_side_effect(name) else ""
        line = f"- [{start.get('engine') or '?'}] {name}{mark}: {what} -> {status}"
        output = end.get("output") or start.get("output")
        if output:
            line += f" | output: {_clip(output, 300)}"
        lines.append(line)
    if len(order) > limit:
        lines.insert(0, f"- ... {len(order) - limit} earlier tool calls left out")
    return lines


def handoff_message(handoff: Handoff, engine: str) -> str:
    """The task for the engine that takes over: everything it needs, and what it must not do again."""
    blocks = [
        f"ENGINE HANDOFF: you ({engine}) take over a job that another agent engine started and could not finish "
        f"({'; '.join(handoff.reasons) or 'provider failure'}). Its session does not carry over; this message has "
        "everything: the original task, the conversation so far and a journal of the steps already executed."
    ]
    if handoff.history.strip():
        blocks.append("Conversation so far (earlier turns of this session):\n" + handoff.history.strip())
    blocks.append("Original task:\n" + handoff.task.strip())
    lines = render_journal(handoff.journal)
    if lines:
        blocks.append(
            "Already executed in this job (journal of tool calls and commands, in order):\n" + "\n".join(lines)
        )
    if handoff.changes:
        files = handoff.changes.get("files") or []
        commits = handoff.changes.get("commits") or []
        if files or commits:
            blocks.append(
                "State of the workspace now: "
                + (f"uncommitted: {', '.join(f.strip() for f in files[:40])}. " if files else "")
                + (f"new commits: {'; '.join(commits[:20])}." if commits else "")
            )
    for name, text in handoff.partial:
        if text.strip():
            blocks.append(f"Partial answer {name} wrote before it stopped:\n" + _clip(text, 3000))
    blocks.append(
        "Rules for the takeover: do NOT repeat side-effecting steps that the journal shows as done: file "
        "writes or edits, git commits, pushes, tags, ssh/scp/sftp/rsync or other remote commands, deployments, "
        "or calls that change state elsewhere. Steps without a result may or may not have happened: check the "
        "current state first (read the files, git status / git log, read-only commands) before you act. "
        "Read-only steps may be repeated. Then continue the task from where it stopped and give the final "
        "answer for the whole task."
    )
    return "\n\n".join(blocks)


def is_side_effect(name: str) -> bool:
    low = (name or "").lower()
    return any(mark in low for mark in _SIDE_EFFECTS)
