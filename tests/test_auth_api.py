"""Signing in over the HTTP API: GET /auth, Claude (link + code), Cursor (API key), Codex, limits, MCP.

The CLIs are fakes: a scripted `claude setup-token` (tests/test_claude_login.py), a fake Codex SDK
(tests/test_codex_engine.py) and a stubbed Cursor key check. Everything lives in tmp_path.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from mcp import Client

from agent_worker import claude_login, cursor_auth
from agent_worker.app import create_app
from agent_worker.auth import RateLimit, auth_status
from agent_worker.claude_engine import ClaudeEngine
from agent_worker.claude_login import LoginManager, load_token
from agent_worker.engine import CursorEngine
from agent_worker.engines import EngineRouter
from agent_worker.jobs import JobManager, Store
from agent_worker.mcp_server import build_mcp
from tests.conftest import make_settings
from tests.test_claude_login import LINK, TOKEN, fake_cli  # noqa: F401  (fixture)

AUTH = {"Authorization": "Bearer secret"}
CURSOR_KEY = "key_" + "0123456789abcdef" * 4


@pytest.fixture
def worker(tmp_path):
    """A token-protected worker with the real Cursor and Claude engines and no credentials."""
    settings = make_settings(tmp_path, cursor_api_key="", token="secret")
    router = EngineRouter({"cursor": CursorEngine(settings), "claude": ClaudeEngine(settings)})
    return settings, router


def _client(settings, engine) -> TestClient:
    return TestClient(create_app(settings, engine), base_url="http://127.0.0.1")


def _no_secrets(*texts: str) -> None:
    for text in texts:
        for secret in (TOKEN, CURSOR_KEY, "at-secret", "rt-secret"):
            assert secret not in text


# ---- GET /auth -----------------------------------------------------------------------------------


def test_auth_overview_needs_the_token_and_lists_every_engine(worker) -> None:
    settings, router = worker
    with _client(settings, router) as client:
        assert client.get("/auth").status_code == 401
        body = client.get("/auth", headers=AUTH).json()
    assert set(body["engines"]) == {"cursor", "claude", "codex"}
    assert body["configured"] == ["cursor", "claude"]
    cursor, claude = body["engines"]["cursor"], body["engines"]["claude"]
    assert cursor == {
        "ready": False,
        "installed": cursor_auth.sdk_installed(),
        "credential": "none",
        "kind": None,
        "plan": None,
        "stored": False,
        "env_override": False,
        "login": None,
        "method": "api_key",
        "default_model": "composer-2.5",
    }
    assert claude["default_model"] == "claude-sonnet-5-5" and body["engines"]["codex"]["default_model"] is None
    assert claude["credential"] == "none" and claude["login"] is None and claude["method"] == "oauth_code"
    assert body["engines"]["codex"]["method"] == "device_code" and body["engines"]["codex"]["ready"] is False


def test_rate_limit_counts_per_key_and_recovers() -> None:
    now = [0.0]
    limit = RateLimit(2, 60, clock=lambda: now[0])
    assert limit.hit("a") == 0 and limit.hit("a") == 0
    assert limit.hit("a") == 61 and limit.hit("b") == 0
    now[0] = 30
    assert limit.hit("a") == 31
    now[0] = 60
    assert limit.hit("a") == 0


# ---- Cursor --------------------------------------------------------------------------------------


def test_cursor_key_is_stored_used_at_once_and_cleared(worker, monkeypatch) -> None:
    settings, router = worker
    checked = []
    monkeypatch.setattr(cursor_auth, "sdk_installed", lambda: True)
    monkeypatch.setattr(cursor_auth, "verify_key", lambda key: checked.append(key) or 3)
    cursor = router.engines["cursor"]
    cursor._models = (1e12, [{"id": "old", "name": "old"}])
    with _client(settings, router) as client:
        assert client.put("/auth/cursor/key", json={"api_key": CURSOR_KEY}).status_code == 401
        bad = client.put("/auth/cursor/key", json={"api_key": "short"}, headers=AUTH)
        assert bad.status_code == 400 and bad.json()["detail"] == "bad_key"
        assert client.put("/auth/cursor/key", json={"api_key": "x y" * 10}, headers=AUTH).status_code == 400
        saved = client.put("/auth/cursor/key", json={"api_key": f"  {CURSOR_KEY}\n"}, headers=AUTH)
        assert saved.status_code == 200
        assert saved.json() == {"ok": True, "credential": "stored", "verified": True, "note": "Saved. Cursor jobs use it now."}
        assert checked == [CURSOR_KEY]
        path = cursor_auth.key_path(settings)
        assert oct(path.stat().st_mode & 0o777) == "0o600" and oct(path.parent.stat().st_mode & 0o777) == "0o700"
        # No restart: the running engine picks it up, and drops what belonged to the old key.
        assert cursor.available and cursor.api_key == CURSOR_KEY and cursor._models is None
        health = client.get("/health")
        assert health.json()["cursor"] is True and health.json()["cursor_credential"] == "stored"
        overview = client.get("/auth", headers=AUTH)
        assert overview.json()["engines"]["cursor"]["ready"] is True
        assert overview.json()["engines"]["cursor"]["kind"] == "api-key"
        _no_secrets(saved.text, health.text, overview.text)
        cleared = client.delete("/auth/cursor/key", headers=AUTH).json()
        assert cleared == {"removed": True, "credential": "none"}
        assert not cursor.available and client.get("/health").json()["cursor_credential"] == "none"
        assert client.delete("/auth/cursor/key", headers=AUTH).json()["removed"] is False


def test_a_refused_cursor_key_is_not_stored(worker, monkeypatch) -> None:
    settings, router = worker
    monkeypatch.setattr(cursor_auth, "sdk_installed", lambda: True)
    answer = {"error": "key_rejected"}

    def verify(_key):
        raise cursor_auth.CursorKeyError(answer["error"])

    monkeypatch.setattr(cursor_auth, "verify_key", verify)
    with _client(settings, router) as client:
        refused = client.put("/auth/cursor/key", json={"api_key": CURSOR_KEY}, headers=AUTH)
        assert refused.status_code == 400 and refused.json()["detail"] == "key_rejected"
        answer["error"] = "unreachable"
        offline = client.put("/auth/cursor/key", json={"api_key": CURSOR_KEY}, headers=AUTH)
        assert offline.status_code == 502 and offline.json()["detail"] == "unreachable"
        assert not cursor_auth.key_path(settings).exists()
        unchecked = client.put("/auth/cursor/key", json={"api_key": CURSOR_KEY, "verify": False}, headers=AUTH)
        assert unchecked.json()["verified"] is False and unchecked.json()["credential"] == "stored"
        _no_secrets(refused.text, offline.text, unchecked.text)


def test_cursor_env_key_wins(tmp_path, monkeypatch) -> None:
    settings = make_settings(tmp_path, cursor_api_key="env-key-0123456789abcdef", token="secret")
    router = EngineRouter({"cursor": CursorEngine(settings)})
    with _client(settings, router) as client:
        saved = client.put("/auth/cursor/key", json={"api_key": CURSOR_KEY, "verify": False}, headers=AUTH).json()
        assert saved["credential"] == "env" and "wins" in saved["note"]
        assert router.engines["cursor"].api_key == "env-key-0123456789abcdef"
        cursor = client.get("/auth", headers=AUTH).json()["engines"]["cursor"]
        assert cursor["credential"] == "env" and cursor["stored"] is True and cursor["env_override"] is True


def test_cursor_key_updates_are_rate_limited(worker) -> None:
    settings, router = worker
    with _client(settings, router) as client:
        codes = [
            client.put("/auth/cursor/key", json={"api_key": CURSOR_KEY, "verify": False}, headers=AUTH).status_code
            for _ in range(11)
        ]
        assert codes[:10] == [200] * 10
        limited = client.put("/auth/cursor/key", json={"api_key": CURSOR_KEY, "verify": False}, headers=AUTH)
    assert codes[10] == 429 and limited.status_code == 429
    assert limited.json()["detail"]["error"] == "rate_limited" and int(limited.headers["Retry-After"]) > 0


# ---- Claude --------------------------------------------------------------------------------------


def test_claude_sign_in_through_the_code_endpoint(worker, fake_cli, monkeypatch) -> None:  # noqa: F811
    settings, router = worker
    monkeypatch.setattr(claude_login, "cli_path", lambda: fake_cli)
    with _client(settings, router) as client:
        assert client.post("/auth/claude/login").status_code == 401
        started = client.post("/auth/claude/login", headers=AUTH)
        assert started.status_code == 200
        login = started.json()
        assert login["url"] == LINK and login["status"] == "awaiting_code" and 0 < login["expires_in"] <= 900
        assert "/code" in login["next"]
        status = client.get(f"/auth/claude/login/{login['id']}", headers=AUTH).json()
        assert status["status"] == "awaiting_code"
        # The UI can find the waiting sign-in again with one call...
        pending = client.get("/auth", headers=AUTH).json()["engines"]["claude"]["login"]
        assert pending["id"] == login["id"] and pending["url"] == LINK
        # ...but the MCP view never shows the link.
        assert client.app.state.auth_status(reveal=False)["engines"]["claude"]["login"] == {
            "status": "awaiting_code",
            "expires_in": pending["expires_in"],
        }
        assert client.get("/auth/claude/login/nope", headers=AUTH).status_code == 404
        done = client.post(f"/auth/claude/login/{login['id']}/code", json={"code": "good#state"}, headers=AUTH)
        assert done.status_code == 200
        assert done.json()["status"] == "succeeded" and done.json()["credential"] == "signed-in"
        assert load_token(settings.claude_token_path) == TOKEN
        after = client.get(f"/auth/claude/login/{login['id']}", headers=AUTH).json()
        assert after["status"] == "succeeded" and after["credential"] == "signed-in"
        again = client.post(f"/auth/claude/login/{login['id']}/code", json={"code": "good#state"}, headers=AUTH)
        assert again.status_code == 410
        claude = client.get("/auth", headers=AUTH).json()["engines"]["claude"]
        assert claude["credential"] == "signed-in" and claude["kind"] == "subscription" and claude["login"] is None
        assert claude["ready"] is True and router.engines["claude"].available
        health = client.get("/health")
        assert health.json()["claude"]["credential"] == "signed-in"
        _no_secrets(started.text, done.text, json.dumps(after), health.text)
        signed_out = client.delete("/auth/claude/token", headers=AUTH).json()
        assert signed_out == {"removed": True, "credential": "none"} and not router.engines["claude"].available


def test_claude_wrong_codes_are_limited(worker, fake_cli, monkeypatch) -> None:  # noqa: F811
    settings, router = worker
    monkeypatch.setattr(claude_login, "cli_path", lambda: fake_cli)
    with _client(settings, router) as client:
        login = client.post("/auth/claude/login", headers=AUTH).json()
        url = f"/auth/claude/login/{login['id']}/code"
        first = client.post(url, json={"code": "not a code"}, headers=AUTH)
        assert first.status_code == 400 and first.json()["detail"] == {"error": "bad_code", "attempts_left": 4}
        for _ in range(3):
            assert client.post(url, json={"code": "x y"}, headers=AUTH).status_code == 400
        last = client.post(url, json={"code": "x y"}, headers=AUTH)
        assert last.status_code == 429 and last.json()["detail"] == "too_many_attempts"
        status = client.get(f"/auth/claude/login/{login['id']}", headers=AUTH).json()
        assert status["status"] == "failed" and status["error"] == "too_many_attempts"
        assert client.post(url, json={"code": "good#state"}, headers=AUTH).status_code == 410
        # A refused code ends the sign-in too, and says so.
        second = client.post("/auth/claude/login", headers=AUTH).json()
        refused = client.post(f"/auth/claude/login/{second['id']}/code", json={"code": "wrong#state"}, headers=AUTH)
        assert refused.status_code == 400 and refused.json()["detail"] == "code_rejected"
        assert client.get(f"/auth/claude/login/{second['id']}", headers=AUTH).json()["status"] == "failed"
    assert load_token(settings.claude_token_path) == ""


def test_claude_cancel_and_old_path_still_work(worker, fake_cli, monkeypatch) -> None:  # noqa: F811
    settings, router = worker
    monkeypatch.setattr(claude_login, "cli_path", lambda: fake_cli)
    with _client(settings, router) as client:
        login = client.post("/auth/claude/login", headers=AUTH).json()
        assert client.delete(f"/auth/claude/login/{login['id']}", headers=AUTH).status_code == 204
        assert client.get(f"/auth/claude/login/{login['id']}", headers=AUTH).status_code == 404
        assert client.get("/auth", headers=AUTH).json()["engines"]["claude"]["login"] is None
        # POST /auth/claude/login/{id} (without /code) stays for older clients.
        again = client.post("/auth/claude/login", headers=AUTH).json()
        done = client.post(f"/auth/claude/login/{again['id']}", json={"code": "good#state"}, headers=AUTH)
        assert done.status_code == 200 and done.json()["credential"] == "signed-in"


def test_a_forgotten_claude_sign_in_is_stopped(fake_cli, monkeypatch) -> None:  # noqa: F811
    monkeypatch.setattr(claude_login, "LOGIN_TTL_SECONDS", 2.0)

    async def run():
        manager = LoginManager(cli_finder=lambda: fake_cli)
        login = await manager.start()
        assert manager.current() is login
        await asyncio.sleep(2.5)
        # Nobody asked again: the timer stopped the CLI by itself.
        assert login.status == "expired" and login._proc is None and manager.current() is None
        assert manager.get(login.id).as_dict()["status"] == "expired"
        manager.close()

    asyncio.run(run())


def test_claude_env_token_wins_and_shows(tmp_path, fake_cli, monkeypatch) -> None:  # noqa: F811
    monkeypatch.setattr(claude_login, "cli_path", lambda: fake_cli)
    settings = make_settings(tmp_path, cursor_api_key="", token="secret", claude_oauth_token="sk-ant-oat01-" + "e" * 40)
    router = EngineRouter({"claude": ClaudeEngine(settings)})
    with _client(settings, router) as client:
        login = client.post("/auth/claude/login", headers=AUTH).json()
        done = client.post(f"/auth/claude/login/{login['id']}/code", json={"code": "good#state"}, headers=AUTH).json()
        assert done["credential"] == "env-plan" and "wins" in done["note"]
        claude = client.get("/auth", headers=AUTH).json()["engines"]["claude"]
    assert claude["credential"] == "env-plan" and claude["stored"] is True and claude["env_override"] is True
    assert "e" * 40 not in json.dumps(claude)


# ---- Codex ---------------------------------------------------------------------------------------


def test_codex_login_shows_in_the_overview_and_starts_are_limited(worker, monkeypatch) -> None:
    pytest.importorskip("openai_codex")
    from agent_worker import codex_login
    from tests.test_codex_engine import FakeLoginSdk

    settings, router = worker
    done = asyncio.Event()
    monkeypatch.setattr(codex_login.CodexLoginManager, "_default_factory", staticmethod(lambda s: FakeLoginSdk(s, done)))
    with _client(settings, router) as client:
        assert client.post("/auth/codex/login").status_code == 401
        started = client.post("/auth/codex/login", headers=AUTH).json()
        assert started["status"] == "pending" and started["expires_in"] > 0
        codex = client.get("/auth", headers=AUTH).json()["engines"]["codex"]
        assert codex["login"]["code"] == "ABCD-1234" and codex["login"]["id"] == started["id"]
        hidden = client.app.state.auth_status(reveal=False)["engines"]["codex"]["login"]
        assert set(hidden) == {"status", "expires_in"}
        done.set()
        finished = client.get(f"/auth/codex/login/{started['id']}?wait=5", headers=AUTH).json()
        assert finished["status"] == "succeeded" and "expires_in" not in finished
        codex = client.get("/auth", headers=AUTH)
        assert codex.json()["engines"]["codex"]["credential"] == "chatgpt"
        assert codex.json()["engines"]["codex"]["plan"] == "plus" and codex.json()["engines"]["codex"]["login"] is None
        _no_secrets(codex.text)
        for _ in range(5):
            assert client.post("/auth/codex/login", headers=AUTH).status_code == 200
        limited = client.post("/auth/codex/login", headers=AUTH)
        assert limited.status_code == 429 and "Retry-After" in limited.headers


# ---- MCP -----------------------------------------------------------------------------------------


async def test_mcp_auth_status_is_read_only_and_hides_links(tmp_path, monkeypatch) -> None:
    settings = make_settings(tmp_path, cursor_api_key="")
    cursor_auth.save_key(cursor_auth.key_path(settings), CURSOR_KEY)
    router = EngineRouter({"cursor": CursorEngine(settings)})
    manager = JobManager(settings, router, Store(settings.data_dir / "worker.sqlite"))

    class Pending:
        def current(self):
            return self

        def as_dict(self):
            return {"id": "x", "status": "awaiting_code", "url": LINK, "expires_in": 100}

    async with Client(build_mcp(manager, lambda: auth_status(settings, router, Pending(), reveal=False))) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        assert tools["auth_status"].annotations.read_only_hint is True
        assert tools["auth_status"].input_schema.get("properties", {}) == {}
        result = await client.call_tool("auth_status", {})
    data = result.structured_content or {}
    assert data["engines"]["cursor"]["credential"] == "stored" and data["engines"]["cursor"]["ready"] is True
    assert data["engines"]["claude"]["login"] == {"status": "awaiting_code", "expires_in": 100}
    text = json.dumps(data)
    assert LINK not in text and CURSOR_KEY not in text
    # Nothing on MCP can set or clear a credential.
    assert not any("key" in name or "login" in name or "token" in name for name in tools)


async def test_mcp_default_auth_status(settings, engine) -> None:
    manager = JobManager(replace(settings, cursor_api_key="k" * 20), engine, Store(settings.data_dir / "worker.sqlite"))
    async with Client(build_mcp(manager)) as client:
        data = (await client.call_tool("auth_status", {})).structured_content or {}
    assert data["engines"]["cursor"]["credential"] == "env" and data["default_engine"] == "cursor"
