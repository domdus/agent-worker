"""Codex engine: credentials, tool profiles, MCP mapping, events, usage, a whole job through a fake SDK."""

from __future__ import annotations

import asyncio
import base64
import json
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("openai_codex")

from agent_worker import codex_engine, codex_login  # noqa: E402
from agent_worker.codex_engine import (  # noqa: E402
    CodexEngine,
    TurnState,
    effort_for,
    error_text,
    map_notification,
    mcp_config,
    resolve_model,
    tool_policy,
    usage_from,
)
from agent_worker.codex_login import auth_info, child_env, credential_source, seed_auth  # noqa: E402
from agent_worker.engine import WORKSPACE_ENV, EngineError, RunSpec  # noqa: E402
from tests.conftest import make_settings  # noqa: E402


def _settings(tmp: Path, **kw):
    return make_settings(tmp, **{"cursor_api_key": "", **kw})


def _jwt(claims: dict) -> str:
    def part(data: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")

    return f"{part({'alg': 'none'})}.{part(claims)}.sig"


def chatgpt_auth(plan: str = "plus") -> str:
    return json.dumps(
        {
            "auth_mode": "chatgpt",
            "OPENAI_API_KEY": None,
            "tokens": {
                "id_token": _jwt({"https://api.openai.com/auth": {"chatgpt_plan_type": plan}}),
                "access_token": "at-secret",
                "refresh_token": "rt-secret",
                "account_id": "acc",
            },
            "last_refresh": "2026-11-13T00:00:00Z",
        }
    )


# ---- credentials ---------------------------------------------------------------------------------


def test_credentials_auth_json_first_then_api_key(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    assert credential_source(settings) == "none"
    env = replace(settings, openai_api_key="sk-proj-x")
    assert credential_source(env) == "env-api"
    codex_login.write_auth(settings.codex_auth_path, chatgpt_auth("pro"))
    assert oct(settings.codex_auth_path.stat().st_mode & 0o777) == "0o600"
    assert credential_source(env) == "chatgpt"
    assert auth_info(settings.codex_auth_path) == {"mode": "chatgpt", "plan": "pro"}
    codex_login.write_auth(settings.codex_auth_path, json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": "sk-x"}))
    assert credential_source(settings) == "api-key"
    assert codex_login.forget_auth(settings) and credential_source(settings) == "none"


def test_auth_json_is_seeded_once(tmp_path: Path) -> None:
    seed = tmp_path / "mounted-auth.json"
    seed.write_text(chatgpt_auth("team"))
    settings = _settings(tmp_path, codex_auth_json=str(seed))
    assert seed_auth(settings) is True
    assert auth_info(settings.codex_auth_path)["plan"] == "team"
    # Codex refreshes the copy in place; the seed must never overwrite it again.
    codex_login.write_auth(settings.codex_auth_path, chatgpt_auth("pro"))
    assert seed_auth(settings) is False and auth_info(settings.codex_auth_path)["plan"] == "pro"
    inline = _settings(tmp_path / "b", codex_auth_json=chatgpt_auth("plus"))
    assert credential_source(inline) == "chatgpt"
    broken = _settings(tmp_path / "c", codex_auth_json="{not json")
    assert seed_auth(broken) is False and credential_source(broken) == "none"


def test_child_env_hides_the_worker_secrets(tmp_path: Path, monkeypatch) -> None:
    for name in ("OPENAI_API_KEY", "CURSOR_API_KEY", "WORKER_TOKEN", "ANTHROPIC_API_KEY"):
        monkeypatch.setenv(name, "secret")
    monkeypatch.setenv("GIT_TOKEN", "git")
    settings = _settings(tmp_path)
    env = child_env(settings, "repo")
    assert env["OPENAI_API_KEY"] == "" and env["CURSOR_API_KEY"] == "" and env["WORKER_TOKEN"] == ""
    assert env["CODEX_HOME"] == str(settings.codex_home) and env[WORKSPACE_ENV] == "repo"
    assert "GIT_TOKEN" not in env  # inherited unchanged: git push uses it


# ---- options -------------------------------------------------------------------------------------

SERVERS = {
    "ha": {"type": "http", "url": "http://ha:8123/mcp", "headers": {"Authorization": "Bearer t"}},
    "old.sse": {"type": "sse", "url": "http://old/sse"},
    "local tool": {"type": "stdio", "command": "uvx", "args": ["thing"], "env": {"A": "1"}},
}


def test_mcp_servers_map_to_codex() -> None:
    mapped, skipped = mcp_config(SERVERS)
    assert mapped["ha"] == {"url": "http://ha:8123/mcp", "http_headers": {"Authorization": "Bearer t"}}
    assert mapped["local_tool"] == {"command": "uvx", "args": ["thing"], "env": {"A": "1"}}
    assert skipped == ["old.sse"]


def test_tool_profiles_in_the_container() -> None:
    sandbox, cfg, skipped = tool_policy("full", "container", SERVERS)
    assert sandbox == "full_access" and cfg["web_search"] == "live" and "shell_tool" not in cfg["features"]
    assert set(cfg["mcp_servers"]) == {"ha", "local_tool"} and skipped == ["old.sse"]
    assert cfg["project_doc_max_bytes"] == 0 and cfg["features"]["hooks"] is False
    sandbox, cfg, _ = tool_policy("local", "container", SERVERS)
    assert sandbox == "full_access" and cfg["web_search"] == "disabled" and "shell_tool" not in cfg["features"]
    sandbox, cfg, _ = tool_policy("offline", "container", SERVERS)
    assert sandbox == "full_access" and cfg["web_search"] == "disabled"
    assert cfg["features"]["shell_tool"] is False and cfg["features"]["unified_exec"] is False
    sandbox, cfg, _ = tool_policy("mcp", "container", SERVERS)
    assert sandbox == "read_only" and cfg["features"]["shell_tool"] is False and cfg["features"]["view_image"] is False
    assert "mcp_servers" in cfg
    sandbox, cfg, _ = tool_policy("none", "container", SERVERS)
    assert sandbox == "read_only" and "mcp_servers" not in cfg


def test_tool_profiles_in_codex_sandbox() -> None:
    sandbox, cfg, _ = tool_policy("full", "codex", {})
    assert sandbox == "workspace_write" and cfg["sandbox_workspace_write"] == {"network_access": True}
    sandbox, cfg, _ = tool_policy("offline", "codex", {})
    # The shell stays, inside a sandbox without network.
    assert sandbox == "workspace_write" and cfg["sandbox_workspace_write"] == {"network_access": False}
    assert "shell_tool" not in cfg["features"]
    assert tool_policy("mcp", "codex", {})[0] == "read_only"


def test_model_and_effort(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    assert resolve_model(settings, "") == "" and resolve_model(settings, "default") == ""
    assert resolve_model(replace(settings, codex_model="gpt-5.5"), "") == "gpt-5.5"
    assert resolve_model(settings, "gpt-6.1-sol") == "gpt-6.1-sol"
    assert effort_for("fast") == "low" and effort_for("high") == "high" and effort_for("high_fast") == "medium"
    assert effort_for("xhigh") == "xhigh" and effort_for("") is None and effort_for("bogus") is None


# ---- events and usage ----------------------------------------------------------------------------


def _usage(last: dict, total: dict) -> dict:
    keys = ("inputTokens", "cachedInputTokens", "cacheWriteInputTokens", "outputTokens", "reasoningOutputTokens")

    def full(blob: dict) -> dict:
        out = {key: blob.get(key, 0) for key in keys}
        out["totalTokens"] = out["inputTokens"] + out["outputTokens"]
        return out

    return {"tokenUsage": {"last": full(last), "total": full(total)}}


def test_usage_of_a_resumed_thread_counts_only_this_turn() -> None:
    state = TurnState()
    # Earlier turns used 1000 in / 100 out; this turn makes two model calls.
    map_notification("thread/tokenUsage/updated", _usage({"inputTokens": 300, "outputTokens": 20}, {"inputTokens": 1300, "outputTokens": 120}), state)
    map_notification(
        "thread/tokenUsage/updated",
        _usage(
            {"inputTokens": 400, "cachedInputTokens": 250, "outputTokens": 30, "reasoningOutputTokens": 9},
            {"inputTokens": 1700, "cachedInputTokens": 250, "outputTokens": 150, "reasoningOutputTokens": 9},
        ),
        state,
    )
    usage = usage_from(state)
    assert usage["prompt_tokens"] == 700 and usage["completion_tokens"] == 50
    assert usage["cache_read_tokens"] == 250 and usage["reasoning_tokens"] == 9
    assert usage["total_tokens"] == 750 and usage["cost_usd"] is None and usage["charged_cents"] is None
    assert usage_from(TurnState()) is None


def test_notifications_become_worker_events() -> None:
    state = TurnState()
    events = []
    for method, payload in [
        ("item/started", {"item": {"type": "reasoning", "id": "r1"}}),
        ("item/started", {"item": {"type": "commandExecution", "id": "c1", "command": "ls -la"}}),
        ("item/completed", {"item": {"type": "commandExecution", "id": "c1", "status": "completed", "exitCode": 2}}),
        ("item/started", {"item": {"type": "fileChange", "id": "f1", "changes": [{"path": "/w/a.py"}]}}),
        ("item/completed", {"item": {"type": "fileChange", "id": "f1", "status": "completed"}}),
        ("item/started", {"item": {"type": "mcpToolCall", "id": "m1", "server": "ha", "tool": "get_state"}}),
        ("item/completed", {"item": {"type": "mcpToolCall", "id": "m1", "server": "ha", "tool": "get_state", "status": "completed"}}),
        ("item/agentMessage/delta", {"itemId": "a1", "delta": "Work"}),
        ("item/agentMessage/delta", {"itemId": "a1", "delta": "ing."}),
        ("item/completed", {"item": {"type": "agentMessage", "id": "a1", "text": "Working.", "phase": "commentary"}}),
        ("item/completed", {"item": {"type": "agentMessage", "id": "a2", "text": "All done.", "phase": "final_answer"}}),
        ("error", {"error": {"message": "Reconnecting... 1/5"}, "willRetry": True}),
        ("turn/completed", {"turn": {"status": "completed", "durationMs": 1234}}),
    ]:
        events.extend(map_notification(method, payload, state))
    kinds = [(e["type"], e.get("name"), e.get("phase")) for e in events]
    assert ("status", None, None) == kinds[0]
    assert ("tool", "shell", "start") in kinds and ("tool", "edit", "start") in kinds
    assert ("tool", "mcp__ha__get_state", "end") in kinds
    shell_end = next(e for e in events if e.get("name") == "shell" and e["phase"] == "end")
    assert shell_end["text"] == "Tool failed" and shell_end["id"] == "c1"
    texts = "".join(e["delta"] for e in events if e["type"] == "text")
    # Streamed deltas are not repeated; a message that was not streamed comes whole.
    assert texts == "Working.\n\nAll done."
    assert state.final_text() == "All done." and state.status == "completed" and state.duration_ms == 1234


def test_error_texts() -> None:
    assert "usage limit" in error_text({"message": "x", "codexErrorInfo": "usageLimitExceeded"}, "", [])
    unauthorized = {"message": "401 key sk-live-123", "codexErrorInfo": {"httpConnectionFailed": {"httpStatusCode": 401}}}
    text = error_text(unauthorized, "", ["sk-live-123"])
    assert "refused the Codex credential" in text and "sk-live-123" not in text
    assert "gpt-9" in error_text({"message": "nope", "codexErrorInfo": "modelNotFound"}, "gpt-9", [])
    assert error_text(None, "", []) == "Codex stopped without an answer."


# ---- a whole job through a fake SDK --------------------------------------------------------------


class FakeHandle:
    def __init__(self, notes, gate: asyncio.Event | None = None) -> None:
        self.notes = notes
        self.gate = gate
        self.interrupted = False

    async def stream(self):
        for method, payload in self.notes:
            if method == "wait" and self.gate is not None:
                await self.gate.wait()
                continue
            yield SimpleNamespace(method=method, payload=payload)

    async def interrupt(self):
        self.interrupted = True


class FakeThread:
    def __init__(self, sdk: "FakeSdk", ident: str) -> None:
        self.sdk = sdk
        self.id = ident

    async def read(self):
        return SimpleNamespace(thread={"id": self.id, "model": "gpt-6.1-sol"})

    async def turn(self, input, effort=None):
        self.sdk.turns.append({"thread": self.id, "input": input, "effort": effort})
        self.sdk.handle = FakeHandle(self.sdk.script(self.id), self.sdk.gate)
        return self.sdk.handle


class FakeSdk:
    """Stands in for AsyncCodex; one instance records every app-server the engine starts."""

    def __init__(self) -> None:
        self.configs = []
        self.started = []
        self.resumed = []
        self.turns = []
        self.api_keys = []
        self.closed = 0
        self.gone: set[str] = set()
        self.gate: asyncio.Event | None = None
        self.handle: FakeHandle | None = None
        self.counter = 0
        self.fail_turn = None

    def factory(self, config):
        self.configs.append(config)
        return self

    async def __aenter__(self):
        return self

    async def close(self):
        self.closed += 1

    async def login_api_key(self, key):
        self.api_keys.append(key)

    async def thread_start(self, **kw):
        self.counter += 1
        self.started.append(kw)
        return FakeThread(self, f"thread-{self.counter}")

    async def thread_resume(self, ident, **kw):
        self.resumed.append((ident, kw))
        if ident in self.gone:
            raise RuntimeError(f"JSON-RPC error -32600: no rollout found for thread id {ident}")
        return FakeThread(self, ident)

    async def models(self):
        return SimpleNamespace(data=[SimpleNamespace(id="gpt-6.1-sol", display_name="GPT-6.1 Sol", hidden=False)])

    def script(self, thread_id):
        if self.fail_turn:
            return [("turn/completed", {"turn": {"status": "failed", "error": self.fail_turn}})]
        return [
            ("item/agentMessage/delta", {"itemId": "a1", "delta": "Done."}),
            ("wait", None),
            ("item/completed", {"item": {"type": "agentMessage", "id": "a1", "text": "Done.", "phase": "final_answer"}}),
            ("thread/tokenUsage/updated", _usage({"inputTokens": 100, "outputTokens": 7}, {"inputTokens": 100, "outputTokens": 7})),
            ("turn/completed", {"turn": {"status": "completed", "durationMs": 50}}),
        ]


def _ids():
    store: dict[tuple[str, str], str] = {}
    return store, (lambda w, s: store.get((w, s)), lambda w, s, a: store.__setitem__((w, s), a) if a else store.pop((w, s), None))


def _spec(settings, **kw) -> RunSpec:
    path = settings.workspaces_dir / "repo"
    return RunSpec(job_id="j1", task="do it", workspace="repo", path=path, engine="codex", **kw)


@pytest.fixture
def sdk(monkeypatch) -> FakeSdk:
    monkeypatch.setattr(codex_engine, "sdk_installed", lambda: True)
    monkeypatch.setattr(codex_engine, "cli_path", lambda: "/usr/bin/codex")
    return FakeSdk()


async def _collect():
    events = []

    async def emit(event):
        events.append(event)

    return events, emit


async def test_job_runs_and_session_resumes(tmp_path: Path, sdk: FakeSdk) -> None:
    settings = _settings(tmp_path)
    codex_login.write_auth(settings.codex_auth_path, chatgpt_auth())
    store, ids = _ids()
    engine = CodexEngine(settings, ids, factory=sdk.factory)
    events, emit = await _collect()
    out = await engine.run(_spec(settings, session="s1", effort="fast", mcp_servers=SERVERS), emit)
    assert out.status == "succeeded" and out.text == "Done." and out.model == "gpt-6.1-sol"
    assert out.agent_id == "thread-1" and store[("repo@codex", "s1")] == "thread-1"
    assert out.usage["prompt_tokens"] == 100 and out.usage["completion_tokens"] == 7 and out.duration_ms == 50
    started = sdk.started[0]
    assert started["cwd"] == str(settings.workspaces_dir / "repo") and started["ephemeral"] is False
    assert started["sandbox"].value == "full-access" and started["approval_mode"].value == "deny_all"
    assert set(started["config"]["mcp_servers"]) == {"ha", "local_tool"}
    assert sdk.turns[0]["effort"] == "low" and "headless worker" in sdk.turns[0]["input"]
    config = sdk.configs[0]
    assert 'cli_auth_credentials_store="file"' in config.config_overrides and config.env[WORKSPACE_ENV] == "repo"
    assert sdk.api_keys == [] and sdk.closed == 1
    assert any("SSE" in e.get("text", "") for e in events)
    # Follow-up: same thread, only the task (no instructions again).
    out = await engine.run(_spec(settings, session="s1"), emit)
    assert sdk.resumed[0][0] == "thread-1" and sdk.turns[-1]["input"] == "do it" and out.agent_id == "thread-1"


async def test_gone_thread_starts_a_new_one(tmp_path: Path, sdk: FakeSdk) -> None:
    settings = _settings(tmp_path, openai_api_key="sk-proj-test")
    store, ids = _ids()
    store[("repo@codex", "s1")] = "old"
    sdk.gone.add("old")
    engine = CodexEngine(settings, ids, factory=sdk.factory)
    _events, emit = await _collect()
    out = await engine.run(_spec(settings, session="s1"), emit)
    assert out.status == "succeeded" and store[("repo@codex", "s1")] == "thread-1"
    assert "headless worker" in sdk.turns[0]["input"]
    # API key: handed over in memory, never stored in auth.json.
    assert sdk.api_keys == ["sk-proj-test"]
    assert 'cli_auth_credentials_store="ephemeral"' in sdk.configs[0].config_overrides
    assert not settings.codex_auth_path.exists()


async def test_failed_turn_and_missing_credential(tmp_path: Path, sdk: FakeSdk) -> None:
    settings = _settings(tmp_path)
    engine = CodexEngine(settings, factory=sdk.factory)
    _events, emit = await _collect()
    with pytest.raises(EngineError, match="No Codex credential"):
        await engine.run(_spec(settings), emit)
    keyed = replace(settings, openai_api_key="sk-proj-test")
    sdk.fail_turn = {"message": "limit sk-proj-test", "codexErrorInfo": "usageLimitExceeded"}
    out = await CodexEngine(keyed, factory=sdk.factory).run(_spec(keyed), emit)
    assert out.status == "failed" and "usage limit" in out.error and "sk-proj-test" not in out.error
    assert sdk.started[-1]["ephemeral"] is True


async def test_cancel_interrupts_the_turn(tmp_path: Path, sdk: FakeSdk) -> None:
    settings = _settings(tmp_path, openai_api_key="sk-proj-test")
    store, ids = _ids()
    sdk.gate = asyncio.Event()
    engine = CodexEngine(settings, ids, factory=sdk.factory)
    _events, emit = await _collect()
    task = asyncio.create_task(engine.run(_spec(settings, session="s1"), emit))
    for _ in range(100):
        if sdk.handle is not None:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sdk.handle.interrupted and sdk.closed == 1
    # The thread is kept, so the next job in the session continues it.
    assert store[("repo@codex", "s1")] == "thread-1"


async def test_models(tmp_path: Path, sdk: FakeSdk) -> None:
    settings = _settings(tmp_path, openai_api_key="sk-proj-test", codex_model="gpt-5.5")
    rows = await CodexEngine(settings, factory=sdk.factory).models()
    assert [row["id"] for row in rows] == ["gpt-5.5", "gpt-6.1-sol"]
    none = await CodexEngine(_settings(tmp_path / "x"), factory=sdk.factory).models()
    assert none == [{"id": "default", "name": "Codex default"}]


# ---- sign-in -------------------------------------------------------------------------------------


class FakeLoginSdk:
    def __init__(self, settings, succeed: asyncio.Event) -> None:
        self.settings = settings
        self.succeed = succeed
        self.closed = False
        self.cancelled = False

    async def login_chatgpt_device_code(self):
        sdk = self

        class Handle:
            verification_url = "https://auth.openai.com/codex/device"
            user_code = "ABCD-1234"

            async def wait(self):
                await sdk.succeed.wait()
                codex_login.write_auth(sdk.settings.codex_auth_path, chatgpt_auth("plus"))
                return SimpleNamespace(success=True, error=None)

            async def cancel(self):
                sdk.cancelled = True

        return Handle()

    async def close(self):
        self.closed = True


async def test_device_code_login(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    done = asyncio.Event()
    made = []

    def factory(s):
        made.append(FakeLoginSdk(s, done))
        return made[-1]

    manager = codex_login.CodexLoginManager(settings, factory)
    login = await manager.start()
    assert login.url.endswith("/codex/device") and login.code == "ABCD-1234" and login.status == "pending"
    assert (await manager.wait(login.id, 0.05)).status == "pending"
    done.set()
    assert (await manager.wait(login.id, 2)).status == "succeeded"
    assert credential_source(settings) == "chatgpt" and made[0].closed
    # A second start replaces a pending one and cancels it.
    done.clear()
    first = await manager.start()
    second = await manager.start()
    assert first.status == "cancelled" and made[1].cancelled and second.status == "pending"
    with pytest.raises(codex_login.CodexLoginError):
        manager.get(first.id)
    await manager.aclose()


def test_login_endpoints_and_health(tmp_path: Path, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from agent_worker.app import create_app
    from tests.conftest import FakeEngine

    settings = _settings(tmp_path)
    done = asyncio.Event()
    monkeypatch.setattr(codex_login.CodexLoginManager, "_default_factory", staticmethod(lambda s: FakeLoginSdk(s, done)))
    with TestClient(create_app(settings, FakeEngine()), base_url="http://127.0.0.1") as client:
        health = client.get("/health").json()["codex"]
        assert health["credential"] == "none" and health["plan"] is None and health["sandbox"] == "container"
        started = client.post("/auth/codex/login").json()
        assert started["code"] == "ABCD-1234" and started["status"] == "pending"
        assert client.get(f"/auth/codex/login/{started['id']}").json()["status"] == "pending"
        done.set()
        finished = client.get(f"/auth/codex/login/{started['id']}?wait=5").json()
        assert finished["status"] == "succeeded" and finished["credential"] == "chatgpt"
        health = client.get("/health").json()["codex"]
        assert health["credential"] == "chatgpt" and health["plan"] == "plus"
        assert client.get("/auth/codex/login/nope").status_code == 404
        assert client.delete("/auth/codex/token").json() == {"removed": True, "credential": "none"}
        assert client.get("/health").json()["codex"]["credential"] == "none"


def test_cli_status_and_logout(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    with pytest.raises(SystemExit) as exit_info:
        codex_login.main(["status"])
    assert exit_info.value.code == 1 and "not signed in" in capsys.readouterr().out
    codex_login.write_auth(tmp_path / "data" / "codex" / "auth.json", chatgpt_auth("pro"))
    with pytest.raises(SystemExit) as exit_info:
        codex_login.main(["status"])
    assert exit_info.value.code == 0 and "ChatGPT (pro plan)" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        codex_login.main(["logout"])
    assert not (tmp_path / "data" / "codex" / "auth.json").exists()


def test_cli_login_runs_codex_device_auth(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-must-not-pass")
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setattr(codex_login, "cli_path", lambda: "/opt/codex")
    calls = []

    def fake_run(argv, env, check):
        calls.append((argv, env))
        codex_login.write_auth(Path(env["CODEX_HOME"]) / "auth.json", chatgpt_auth())
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(codex_login.subprocess, "run", fake_run)
    with pytest.raises(SystemExit) as exit_info:
        codex_login.main([])
    argv, env = calls[0]
    assert exit_info.value.code == 0
    assert argv[:3] == ["/opt/codex", "login", "--device-auth"]
    assert env["CODEX_HOME"] == str((tmp_path / "data" / "codex").resolve()) and "OPENAI_API_KEY" not in env


# ---- the real Codex runtime against a local fake model --------------------------------------------


@pytest.mark.skipif(not codex_login.cli_path(), reason="Codex CLI runtime (openai-codex-cli-bin) not installed")
async def test_real_codex_runtime_against_fake_model(tmp_path: Path) -> None:
    """The pinned Codex app-server, a fake Responses API: apply_patch edits a git workspace."""
    import subprocess

    from tests.fake_responses import FakeResponses

    patch = "*** Begin Patch\n*** Add File: hello.txt\n+hi from codex\n*** End Patch\n"
    steps = [{"type": "custom_tool_call", "name": "apply_patch", "call_id": "c1", "id": "ctc_1", "input": patch}]
    with FakeResponses(steps, final="Created hello.txt.") as fake:
        settings = _settings(
            tmp_path,
            openai_api_key="sk-fake",
            codex_config=(
                f'model_providers.fake={{name="fake",base_url="{fake.url}",wire_api="responses",requires_openai_auth=false}}',
                'model_provider="fake"',
            ),
        )
        store, ids = _ids()
        workspace = settings.workspaces_dir / "repo"
        workspace.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(workspace)], check=True)
        events, emit = await _collect()
        engine = CodexEngine(settings, ids)
        out = await asyncio.wait_for(engine.run(_spec(settings, session="s", profile="offline", model="gpt-5.5"), emit), 120)
        assert out.status == "succeeded", out.error
        assert out.text == "Created hello.txt." and out.model == "gpt-5.5"
        assert (workspace / "hello.txt").read_text() == "hi from codex\n"
        assert out.changes and any("hello.txt" in line for line in out.changes["files"])
        assert out.usage["prompt_tokens"] == 200 and out.usage["completion_tokens"] == 10
        assert ("tool", "edit", "end") in [(e["type"], e.get("name"), e.get("phase")) for e in events]
        tools = fake.tool_names()
        assert "apply_patch" in tools and "exec_command" not in tools and "web_search" not in tools
        # Second job in the session continues the same thread.
        out2 = await asyncio.wait_for(engine.run(_spec(settings, session="s", profile="offline", model="gpt-5.5"), emit), 120)
        assert out2.agent_id == out.agent_id and out2.usage["prompt_tokens"] == 100
        assert os.environ.get("OPENAI_API_KEY", "") != "sk-fake"
