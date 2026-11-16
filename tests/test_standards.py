"""MCP cancellation, the MCP Task status vocabulary, and the _meta conversation id (0.5.0)."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace

import httpx
import pytest
from mcp import Client

from agent_worker import jobs as jobs_module
from agent_worker import tasks as st
from agent_worker.app import create_app
from agent_worker.jobs import JobManager, JobRequest, Store, clean_conversation_id, conversation_from_meta
from agent_worker.mcp_server import build_mcp


def _manager(settings, engine, **overrides):
    overrides.setdefault("mcp_wait_seconds", 20)
    return JobManager(replace(settings, **overrides), engine, Store(settings.data_dir / "worker.sqlite"))


async def _until(check, seconds=5.0):
    deadline = time.monotonic() + seconds
    while not check():
        assert time.monotonic() < deadline, "condition not reached"
        await asyncio.sleep(0.02)


def _only(manager):
    assert len(manager.jobs) == 1
    return next(iter(manager.jobs.values()))


async def _cancel_open_call(manager, client, args, meta=None):
    """Start agent_run, wait until its job works, then abandon the call (the client cancels it)."""
    call = asyncio.create_task(client.call_tool("agent_run", args, meta=meta))
    await _until(lambda: manager.jobs and _only(manager).status == st.WORKING)
    call.cancel()
    with pytest.raises(BaseException):
        await call
    return _only(manager)


@pytest.fixture
def kills(monkeypatch):
    seen = []

    async def kill_tagged(name, value, grace=3.0):
        seen.append(value)
        return 0

    monkeypatch.setattr(jobs_module.proctree, "kill_tagged", kill_tagged)
    return seen


# ---- MCP cancellation ----------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["legacy", "auto"])
async def test_cancelled_agent_run_cancels_the_job_and_frees_the_workspace(settings, engine, kills, mode):
    """legacy: initialize handshake, the client sends notifications/cancelled. auto: 2026-07-28, where the
    call ending early is the cancel signal."""
    engine.gate = asyncio.Event()
    manager = _manager(settings, engine)
    async with Client(build_mcp(manager), mode=mode) as client:
        job = await _cancel_open_call(manager, client, {"task": "slow", "workspace": "docs"})
        await manager.wait(job, 15)
        assert job.status == st.CANCELLED
        assert "MCP client" in job.error and "agent_run" in job.error
        if mode == "legacy":
            assert "notifications/cancelled" in job.error
        # The engine's process tree was killed and the workspace lock released: the next job runs.
        assert job.id in kills
        engine.gate.set()
        nxt = manager.submit(JobRequest(task="next", workspace="docs"))
        await manager.wait(nxt, 5)
        assert nxt.status == st.COMPLETED


async def test_background_job_survives_a_cancelled_call(settings, engine, kills):
    engine.gate = asyncio.Event()
    manager = _manager(settings, engine)
    async with Client(build_mcp(manager), mode="legacy") as client:
        job = await _cancel_open_call(manager, client, {"task": "slow", "background": True})
        await asyncio.sleep(0.1)
        assert job.status == st.WORKING and job.background
        engine.gate.set()
        await manager.wait(job, 5)
    assert job.status == st.COMPLETED and not kills


@pytest.mark.parametrize(
    ("mode", "flag"), [("legacy", "mcp_cancel_on_cancelled"), ("auto", "mcp_cancel_on_disconnect")]
)
async def test_cancel_policy_can_be_switched_off(settings, engine, mode, flag):
    engine.gate = asyncio.Event()
    manager = _manager(settings, engine, **{flag: False})
    async with Client(build_mcp(manager), mode=mode) as client:
        job = await _cancel_open_call(manager, client, {"task": "slow"})
        await asyncio.sleep(0.1)
        assert job.status == st.WORKING
        engine.gate.set()
        await manager.wait(job, 5)
    assert job.status == st.COMPLETED


async def test_cancelled_poll_does_not_cancel_the_job(settings, engine):
    """Only the agent_run call is bound to its job; abandoning an agent_job wait leaves the job alone."""
    engine.gate = asyncio.Event()
    manager = _manager(settings, engine, mcp_wait_seconds=0)
    async with Client(build_mcp(manager), mode="legacy") as client:
        first = (await client.call_tool("agent_run", {"task": "slow"})).structured_content
        poll = asyncio.create_task(client.call_tool("agent_job", {"job_id": first["job_id"], "wait_seconds": 20}))
        await asyncio.sleep(0.2)
        poll.cancel()
        with pytest.raises(BaseException):
            await poll
        job = manager.get(first["job_id"])
        await asyncio.sleep(0.1)
        assert job.status == st.WORKING
        engine.gate.set()
        await manager.wait(job, 5)
    assert job.status == st.COMPLETED


async def test_finished_call_is_not_cancelled_by_closing_the_client(settings, engine):
    engine.gate = asyncio.Event()
    manager = _manager(settings, engine, mcp_wait_seconds=0)
    async with Client(build_mcp(manager)) as client:
        first = (await client.call_tool("agent_run", {"task": "slow"})).structured_content
    job = manager.get(first["job_id"])
    await asyncio.sleep(0.1)
    assert job.status == st.WORKING
    engine.gate.set()
    await manager.wait(job, 5)
    assert job.status == st.COMPLETED


# ---- statuses and the Tasks view -----------------------------------------------------------------


def test_status_vocabulary_and_legacy_names():
    assert st.normalize("running") == st.WORKING
    assert st.normalize("needs_approval") == st.INPUT_REQUIRED
    assert st.normalize("succeeded") == st.COMPLETED
    assert st.normalize("queued") == st.QUEUED and st.normalize("bogus") == ""
    assert st.parse_filter("running, needs_approval,completed,x") == {st.WORKING, st.INPUT_REQUIRED, st.COMPLETED}
    assert st.task_status(st.QUEUED) == st.WORKING
    assert {st.task_status(s) for s in st.STATUSES} == {"working", "input_required", "completed", "failed",
                                                         "cancelled"}


async def test_agent_job_carries_an_mcp_task_view(settings, engine):
    engine.gate = asyncio.Event()
    manager = _manager(settings, engine, mcp_wait_seconds=0, task_poll_interval_ms=3000)
    blocker = manager.submit(JobRequest(task="first"))
    async with Client(build_mcp(manager)) as client:
        first = (await client.call_tool("agent_run", {"task": "second"})).structured_content
        await asyncio.sleep(0.05)
        queued = (await client.call_tool("agent_job", {"job_id": first["job_id"]})).structured_content
        assert queued["status"] == "queued" and queued["legacy_status"] == "queued"
        task = queued["task"]
        assert task["taskId"] == queued["job_id"] and task["status"] == "working"
        assert "waiting for another job in this workspace" in task["statusMessage"]
        assert blocker.id in task["statusMessage"]
        assert task["pollInterval"] == 3000 and task["ttl"] is None and task["createdAt"] and task["lastUpdatedAt"]
        engine.gate.set()
        done = (await client.call_tool("agent_job", {"job_id": queued["job_id"], "wait_seconds": 5})).structured_content
    assert done["status"] == "completed" and done["legacy_status"] == "succeeded"
    assert done["task"]["status"] == "completed" and "pollInterval" not in done["task"]
    assert done["task"]["statusMessage"] == "Done: second"
    assert st.task_result(done, failed=False)["structuredContent"] is done


async def test_input_required_says_what_it_waits_for(settings, engine):
    manager = _manager(replace(settings, ssh_approval_hosts=("prod",)), engine)
    engine.hook = lambda spec: manager.remote_call(spec.workspace, "ssh", "prod", "prod.example", "root", "uptime")
    job = manager.submit(JobRequest(task="check prod"))
    await _until(lambda: job.status == st.INPUT_REQUIRED)
    row = manager.describe(job)
    assert row["status"] == "input_required" and row["legacy_status"] == "needs_approval"
    assert "approve ssh on root@prod: uptime" in row["status_message"]
    assert manager.overview(status="needs_approval") == [job]
    manager.decide(job.id, True)
    await manager.wait(job, 5)
    assert job.status == st.COMPLETED


async def test_legacy_status_names_setting_and_old_saved_jobs(settings, engine):
    manager = _manager(settings, engine, job_status_names="legacy")
    job = manager.submit(JobRequest(task="a"))
    await manager.wait(job, 5)
    assert job.status == st.COMPLETED
    assert job.as_dict()["status"] == "succeeded" and manager.describe(job)["status"] == "succeeded"
    # Stored with the current name; a job a 0.4 worker saved as "succeeded" reads back as completed.
    store = Store(settings.data_dir / "worker.sqlite")
    data = json.loads(store._db.execute("SELECT data FROM jobs WHERE id = ?", (job.id,)).fetchone()[0])
    assert data["status"] == "completed"
    data["status"] = "succeeded"
    store._db.execute("UPDATE jobs SET data = ? WHERE id = ?", (json.dumps(data), job.id))
    again = _manager(settings, engine)
    assert again.get(job.id).status == st.COMPLETED and again.get(job.id).as_dict()["status"] == "completed"


# ---- conversation id -----------------------------------------------------------------------------


def test_conversation_id_from_meta_and_its_checks():
    assert conversation_from_meta({"io.github.domdus/conversation_id": "c-1", "contextId": "other"}) == "c-1"
    assert conversation_from_meta({"contextId": "ctx-9"}) == "ctx-9"
    assert conversation_from_meta({"progressToken": 3}) == "" and conversation_from_meta(None) == ""
    assert clean_conversation_id("chat-42:7") == "chat-42:7"
    assert clean_conversation_id("sk-ant-oat01-abc") == ""
    assert clean_conversation_id("ghp_abcdef") == ""
    assert clean_conversation_id("my-token-value", ("my-token-value",)) == ""
    assert clean_conversation_id("a b") == "" and clean_conversation_id("x" * 129) == ""
    assert clean_conversation_id(True) == ""


async def test_conversation_id_is_stored_listed_filtered_and_cancellable(settings, engine):
    engine.gate = asyncio.Event()
    manager = _manager(settings, engine, mcp_wait_seconds=0)
    async with Client(build_mcp(manager)) as client:
        a = (await client.call_tool("agent_run", {"task": "a", "workspace": "w1"},
                                    meta={"io.github.domdus/conversation_id": "conv-A"})).structured_content
        b = (await client.call_tool("agent_run", {"task": "b", "workspace": "w2"},
                                    meta={"contextId": "conv-A"})).structured_content
        c = (await client.call_tool("agent_run", {"task": "c", "workspace": "w3",
                                                  "conversation_id": "conv-B"})).structured_content
        bad = (await client.call_tool("agent_run", {"task": "d", "workspace": "w4"},
                                      meta={"contextId": "sk-live-secret"})).structured_content
        assert a["conversation_id"] == "conv-A" and b["conversation_id"] == "conv-A"
        assert "conversation_id" not in bad

        rows = (await client.call_tool("agent_jobs", {"conversation_id": "conv-A"})).structured_content["jobs"]
        assert {r["job_id"] for r in rows} == {a["job_id"], b["job_id"]}
        assert all(r["conversation_id"] == "conv-A" for r in rows)

        out = (await client.call_tool("agent_cancel", {"conversation_id": "conv-A"})).structured_content
        assert out["conversation_id"] == "conv-A"
        assert {j["job_id"] for j in out["cancelled"]} == {a["job_id"], b["job_id"]}
        assert all(j["status"] == "cancelled" for j in out["cancelled"])
        assert manager.get(c["job_id"]).status == st.WORKING
        none = (await client.call_tool("agent_cancel", {})).structured_content
        assert none["status"] == "rejected"
    engine.gate.set()
    for job_id in (c["job_id"], bad["job_id"]):
        await manager.wait(manager.get(job_id), 5)


async def test_http_api_conversation_id(tmp_path, engine):
    from tests.conftest import make_settings

    engine.gate = asyncio.Event()
    app = create_app(make_settings(tmp_path), engine=engine)
    manager = app.state.jobs
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as http:
        created = (await http.post("/jobs", json={"task": "x", "conversation_id": "conv-H"})).json()
        assert created["conversation_id"] == "conv-H" and created["legacy_status"] in {"queued", "running"}
        await _until(lambda: manager.get(created["id"]).status == st.WORKING)
        listed = (await http.get("/jobs/overview", params={"conversation_id": "conv-H"})).json()["jobs"]
        assert [r["job_id"] for r in listed] == [created["id"]]
        one = (await http.get(f"/jobs/{created['id']}")).json()
        assert one["task"]["taskId"] == created["id"] and one["task"]["status"] == "working"
        by_status = (await http.get("/jobs", params={"status": "running"})).json()["jobs"]
        assert [j["id"] for j in by_status] == [created["id"]]
        out = (await http.post("/conversations/conv-H/cancel")).json()
        assert [j["job_id"] for j in out["cancelled"]] == [created["id"]]
    assert manager.get(created["id"]).status == st.CANCELLED
    await manager.aclose()


def test_settings_from_env(monkeypatch, tmp_path):
    from agent_worker.settings import Settings

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    defaults = Settings.from_env()
    assert defaults.mcp_cancel_on_cancelled and defaults.mcp_cancel_on_disconnect
    assert defaults.job_status_names == "mcp" and defaults.task_poll_interval_ms == 5000
    monkeypatch.setenv("MCP_CANCEL_ON_CANCELLED", "false")
    monkeypatch.setenv("MCP_CANCEL_ON_DISCONNECT", "0")
    monkeypatch.setenv("JOB_STATUS_NAMES", "Legacy")
    monkeypatch.setenv("TASK_POLL_INTERVAL_MS", "100")
    changed = Settings.from_env()
    assert not changed.mcp_cancel_on_cancelled and not changed.mcp_cancel_on_disconnect
    assert changed.job_status_names == "legacy" and changed.task_poll_interval_ms == 500


async def test_agent_run_description_names_the_tool_profiles(settings, engine):
    async with Client(build_mcp(_manager(settings, engine))) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    description = tools["agent_run"].description
    assert "full, local, offline, mcp, none" in description and "{" not in description
    props = tools["agent_run"].input_schema["properties"]
    assert "background" in props and "conversation_id" in props
    assert "conversation_id" in tools["agent_cancel"].input_schema["properties"]
