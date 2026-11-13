"""Sign-in status of every engine in one place, for GET /auth, /health and the auth_status MCP tool.

Never contains a secret: only where a credential comes from, the plan when it is known, and a
sign-in that is still waiting for the user.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any

from agent_worker.settings import Settings

# How often sign-ins may be started (per engine) and Cursor keys sent: (count, seconds).
LOGIN_STARTS = (6, 600)
KEY_UPDATES = (10, 600)


class RateLimit:
    """At most `count` hits per `seconds` for each key, in memory."""

    def __init__(self, count: int, seconds: float, clock=time.monotonic) -> None:
        self.count = count
        self.seconds = seconds
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}

    def hit(self, key: str) -> int:
        """0 if allowed (and counted), else the seconds until the next try is allowed."""
        now = self._clock()
        hits = self._hits.setdefault(key, deque())
        while hits and now - hits[0] >= self.seconds:
            hits.popleft()
        if len(hits) >= self.count:
            return max(1, int(self.seconds - (now - hits[0])) + 1)
        hits.append(now)
        return 0


def engine_ready(engine: Any, name: str) -> bool:
    engines = getattr(engine, "engines", None)
    if engines is None:
        return bool(engine.available) if name == "cursor" else False
    return name in engines and bool(engines[name].available)


def _login(login: Any, reveal: bool) -> dict[str, Any] | None:
    if login is None:
        return None
    data = login.as_dict()
    if reveal:
        return data
    # The MCP view: whoever can read it must not be able to finish someone else's sign-in.
    return {key: data[key] for key in ("status", "expires_in") if key in data}


def cursor_status(settings: Settings, engine: Any) -> dict[str, Any]:
    from agent_worker import cursor_auth

    source = cursor_auth.credential_source(settings)
    stored = bool(cursor_auth.load_key(cursor_auth.key_path(settings)))
    return {
        "ready": engine_ready(engine, "cursor"),
        "installed": cursor_auth.sdk_installed(),
        "credential": source,
        "kind": "api-key" if source != "none" else None,
        "plan": None,
        "stored": stored,
        "env_override": stored and source == "env",
        "login": None,
        "method": "api_key",
    }


def claude_status(settings: Settings, engine: Any, login_manager: Any = None, reveal: bool = True) -> dict[str, Any]:
    from agent_worker.claude_engine import credential_source, sdk_installed
    from agent_worker.claude_login import load_token

    source = credential_source(settings)
    stored = bool(load_token(settings.claude_token_path))
    kind = {"env-plan": "subscription", "signed-in": "subscription", "env-api": "api-key"}.get(source)
    return {
        "ready": engine_ready(engine, "claude"),
        "installed": sdk_installed(),
        "credential": source,
        "kind": kind,
        # A Plan token does not say which plan (Pro, Max, ...) it belongs to.
        "plan": None,
        "stored": stored,
        "env_override": stored and source.startswith("env-"),
        "login": _login(login_manager.current() if login_manager is not None else None, reveal),
        "method": "oauth_code",
    }


def codex_status(settings: Settings, engine: Any, login_manager: Any = None, reveal: bool = True) -> dict[str, Any]:
    from agent_worker import codex_login

    source = codex_login.credential_source(settings)
    info = codex_login.auth_info(settings.codex_auth_path)
    kind = {"chatgpt": "subscription", "api-key": "api-key", "env-api": "api-key"}.get(source)
    return {
        "ready": engine_ready(engine, "codex"),
        "installed": codex_login.sdk_installed() and bool(codex_login.cli_path()),
        "credential": source,
        "kind": kind,
        "plan": info["plan"] if source == "chatgpt" else None,
        "stored": bool(info["mode"]),
        # auth.json wins over OPENAI_API_KEY, so the environment never hides a sign-in here.
        "env_override": False,
        "login": _login(login_manager.current() if login_manager is not None else None, reveal),
        "method": "device_code",
    }


def auth_status(
    settings: Settings,
    engine: Any,
    claude_login: Any = None,
    codex_login: Any = None,
    *,
    reveal: bool = True,
) -> dict[str, Any]:
    """Per-engine status. reveal=False leaves out sign-in links and device codes."""
    names = list(getattr(engine, "engines", None) or {"cursor": engine})
    builders = {
        "cursor": lambda: cursor_status(settings, engine),
        "claude": lambda: claude_status(settings, engine, claude_login, reveal),
        "codex": lambda: codex_status(settings, engine, codex_login, reveal),
    }
    engines = {name: builders[name]() for name in ("cursor", "claude", "codex") if name in builders}
    default = engine.default() if hasattr(engine, "default") else "cursor"
    return {
        "default_engine": default,
        "configured": [name for name in names if name in engines],
        "engines": engines,
    }
