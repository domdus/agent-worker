"""0.7.2: tool annotations, isError, the agent_run/agent_job outputSchema, and the 2026-07-28 HTTP path."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace

import httpx
import jsonschema
import pytest
from mcp import Client

from agent_worker.app import create_app
from agent_worker.jobs import JobManager, Store
from agent_worker.mcp_server import build_mcp

MODERN = "2026-07-28"
META = {"io.modelcontextprotocol/protocolVersion": MODERN, "io.modelcontextprotocol/clientCapabilities": {}}
ACCEPT = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


def _manager(settings, engine, **overrides):
    return JobManager(replace(settings, **overrides), engine, Store(settings.data_dir / "worker.sqlite"))


async def _tools(settings, engine, **overrides):
    async with Client(build_mcp(_manager(settings, engine, approval_tool=True, **overrides))) as client:
        return {tool.name: tool for tool in (await client.list_tools()).tools}


# ---- annotations ---------------------------------------------------------------------------------


async def test_every_tool_has_a_title_and_annotations(settings, engine):
    tools = await _tools(settings, engine)
    assert set(tools) >= {"agent_run", "agent_job", "agent_jobs", "agent_cancel", "agent_workspaces",
                          "auth_status", "agent_approve"}
    for tool in tools.values():
        ann = tool.annotations
        assert tool.title and ann is not None and ann.title == tool.title, tool.name
        assert ann.read_only_hint is not None and ann.idempotent_hint is not None, tool.name
        assert ann.open_world_hint is not None, tool.name
    for name in ("agent_jobs", "agent_job", "agent_workspaces", "auth_status"):
        assert tools[name].annotations.read_only_hint is True, name
    for name in ("agent_run", "agent_cancel"):
        ann = tools[name].annotations
        assert ann.read_only_hint is False and ann.destructive_hint is True, name
    assert tools["agent_run"].annotations.idempotent_hint is False
    assert tools["agent_run"].annotations.open_world_hint is True
    assert tools["agent_cancel"].annotations.idempotent_hint is True


# ---- outputSchema --------------------------------------------------------------------------------


async def test_job_tools_publish_a_real_output_schema(settings, engine):
    tools = await _tools(settings, engine)
    for name in ("agent_run", "agent_job", "agent_approve"):
        schema = tools[name].output_schema
        assert schema["type"] == "object" and schema["required"] == ["status"], name
        props = schema["properties"]
        assert {"job_id", "status", "status_message", "result", "error", "task", "approval", "changes"} <= set(props)
        assert "completed" in props["status"]["enum"] and "rejected" in props["status"]["enum"]
        jsonschema.Draft202012Validator.check_schema(schema)


async def _call(client, name, args):
    return await client.call_tool(name, args)


async def test_structured_content_matches_the_schema_and_is_error(settings, engine):
    manager = _manager(settings, engine, mcp_wait_seconds=0)
    async with Client(build_mcp(manager)) as client:
        schema = {t.name: t for t in (await client.list_tools()).tools}["agent_run"].output_schema

        def check(result, error):
            jsonschema.validate(result.structured_content, schema)
            assert result.is_error is error, result.structured_content
            return result.structured_content

        # Still running: not an error.
        engine.gate = asyncio.Event()
        running = check(await _call(client, "agent_run", {"task": "slow"}), False)
        assert running["status"] in {"queued", "working"} and running["job_id"]
        engine.gate.set()
        done = check(await _call(client, "agent_job", {"job_id": running["job_id"], "wait_seconds": 5}), False)
        assert done["status"] == "completed" and done["result"] == "Done: slow"

        # Failed job: isError, structuredContent kept.
        engine.fail = "boom"
        failed = check(await _call(client, "agent_run", {"task": "x", "wait_seconds": 5}), True)
        assert failed["status"] == "failed" and "boom" in failed["error"]
        engine.fail = ""

        # Unknown job and a refused request: isError.
        assert check(await _call(client, "agent_job", {"job_id": "nope"}), True)["status"] == "unknown"
        rejected = check(await _call(client, "agent_run", {"task": "x", "workspace": "../up"}), True)
        assert rejected["status"] == "rejected"

        # Cancelled: agent_cancel did its job (no error); the job's own result is an error.
        engine.gate = asyncio.Event()
        job = check(await _call(client, "agent_run", {"task": "slow"}), False)
        cancel = await _call(client, "agent_cancel", {"job_id": job["job_id"]})
        assert cancel.is_error is False and cancel.structured_content["status"] == "cancelled"
        cancelled = check(await _call(client, "agent_job", {"job_id": job["job_id"]}), True)
        assert cancelled["status"] == "cancelled"
        assert (await _call(client, "agent_cancel", {})).is_error is True
        engine.gate.set()


async def test_request_state_outlives_the_approval_timeout(settings, engine):
    """A 2026-07-28 approval answered after the SDK default of 600 s must not get "Invalid or expired
    requestState" (HTTP 400)."""
    server = build_mcp(_manager(settings, engine, approval_timeout=3600))
    assert _security_ttl(server) >= 3600


def _security_ttl(server) -> float:
    seen = set()
    stack = [server]
    while stack:
        obj = stack.pop()
        if id(obj) in seen:
            continue
        seen.add(id(obj))
        if type(obj).__name__ == "RequestStateSecurity":
            return obj.ttl
        for value in getattr(obj, "__dict__", {}).values():
            if hasattr(value, "__dict__") or isinstance(value, (list, tuple)):
                stack.extend(value if isinstance(value, (list, tuple)) else [value])
    return 0.0


# ---- the 2026-07-28 HTTP path behind a proxy -------------------------------------------------------


def _body(method, params=None, request_id=1):
    params = dict(params or {})
    params["_meta"] = META
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}


@asynccontextmanager
async def _http(tmp_path, engine, **overrides):
    from tests.conftest import make_settings

    app = create_app(make_settings(tmp_path, **overrides), engine=engine)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            yield client


@pytest.mark.parametrize(
    "headers",
    [
        {"mcp-protocol-version": MODERN, "mcp-method": "tools/call", "mcp-name": "agent_jobs"},
        {},  # a proxy dropped every MCP header
        {"mcp-method": "tools/call", "mcp-name": "agent_jobs"},  # ... MCP-Protocol-Version only
        {"mcp-protocol-version": MODERN},  # ... Mcp-Method and Mcp-Name
        {"mcp-protocol-version": MODERN, "mcp-session-id": "stale"},
    ],
)
async def test_modern_requests_survive_dropped_headers(tmp_path, engine, headers):
    async with _http(tmp_path, engine) as http:
        response = await http.post("/mcp", json=_body("tools/call", {"name": "agent_jobs", "arguments": {}}),
                                   headers={**ACCEPT, **headers})
        assert response.status_code == 200, response.text
        assert response.json()["result"]["structuredContent"] == {"jobs": []}
        discover = await http.post("/mcp", json=_body("server/discover"), headers=ACCEPT)
        assert discover.status_code == 200 and MODERN in discover.json()["result"]["supportedVersions"]


async def test_a_header_that_disagrees_is_still_refused(tmp_path, engine):
    async with _http(tmp_path, engine) as http:
        response = await http.post(
            "/mcp", json=_body("tools/call", {"name": "agent_jobs", "arguments": {}}),
            headers={**ACCEPT, "mcp-protocol-version": MODERN, "mcp-method": "tools/list"})
        assert response.status_code == 400 and response.json()["error"]["code"] == -32020


async def test_initialize_with_a_stale_modern_header_is_served(tmp_path, engine):
    async with _http(tmp_path, engine) as http:
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
        response = await http.post("/mcp", json=init, headers={**ACCEPT, "mcp-protocol-version": MODERN})
        assert response.status_code == 200 and response.headers.get("mcp-session-id")


async def test_modern_off_sends_clients_to_the_handshake(tmp_path, engine):
    async with _http(tmp_path, engine, mcp_protocol_modern="off") as http:
        response = await http.post("/mcp", json=_body("server/discover"),
                                   headers={**ACCEPT, "mcp-protocol-version": MODERN, "mcp-method": "server/discover"})
        assert response.status_code == 400
        error = response.json()["error"]
        assert error["code"] == -32022 and MODERN not in error["data"]["supported"]
        assert "2025-11-25" in error["data"]["supported"]
        note = await http.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/x", "params": {"_meta": META}},
                               headers={**ACCEPT, "mcp-protocol-version": MODERN})
        assert note.status_code == 202
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
        response = await http.post("/mcp", json=init, headers=ACCEPT)
        assert response.status_code == 200 and response.headers.get("mcp-session-id")


async def test_python_client_auto_mode_falls_back_when_modern_is_off():
    """The SDK's own era negotiation (mode=auto): -32022 naming only handshake versions -> initialize."""
    from mcp.client._probe import _parse_supported
    from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS, MODERN_PROTOCOL_VERSIONS

    from agent_worker.mcp_http import _unsupported

    data = json.loads(_unsupported(1, MODERN))["error"]["data"]
    supported = _parse_supported(data)
    assert not [v for v in MODERN_PROTOCOL_VERSIONS if v in supported]
    assert any(v in HANDSHAKE_PROTOCOL_VERSIONS for v in supported)


def test_setting_from_env(monkeypatch, tmp_path):
    from agent_worker.settings import Settings

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    assert Settings.from_env().mcp_protocol_modern == "auto"
    for raw, expected in (("off", "off"), ("false", "off"), ("AUTO", "auto"), ("weird", "auto")):
        monkeypatch.setenv("MCP_PROTOCOL_MODERN", raw)
        assert Settings.from_env().mcp_protocol_modern == expected
