"""agent_run with _meta.progressToken: the call stays open until the job ends (MCP progress, 0.7.0)."""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace

import pytest
from mcp import Client
from mcp import types as mtypes

from agent_worker import jobs as jobs_module
from agent_worker import tasks as st
from agent_worker.jobs import JobManager, Store
from agent_worker.mcp_server import build_mcp
from agent_worker.settings import Settings


def _manager(settings, engine, **overrides):
    # Small caps: a call that followed them would answer within ~1 s.
    overrides.setdefault("mcp_wait_seconds", 1)
    overrides.setdefault("mcp_max_wait", 1)
    return JobManager(replace(settings, **overrides), engine, Store(settings.data_dir / "worker.sqlite"))


async def _until(check, seconds=5.0):
    deadline = time.monotonic() + seconds
    while not check():
        assert time.monotonic() < deadline, "condition not reached"
        await asyncio.sleep(0.02)


def _only(manager):
    assert len(manager.jobs) == 1
    return next(iter(manager.jobs.values()))


class _Progress:
    def __init__(self) -> None:
        self.rows: list[tuple[float, float | None, str]] = []

    async def __call__(self, progress, total, message):
        self.rows.append((progress, total, message or ""))

    @property
    def messages(self) -> list[str]:
        return [m for _, _, m in self.rows]


def _ssh_hook(manager, results):
    async def hook(spec):
        results.append(await manager.remote_call(spec.workspace, "ssh", "prod", "prod", "root", "reboot"))

    return hook


@pytest.fixture
def kills(monkeypatch):
    seen = []

    async def kill_tagged(name, value, grace=3.0):
        seen.append(value)
        return 0

    monkeypatch.setattr(jobs_module.proctree, "kill_tagged", kill_tagged)
    return seen


# ---- 1. waiting with progress --------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["legacy", "auto"])
async def test_progress_token_keeps_the_call_open_until_the_job_ends(settings, engine, mode):
    engine.gate = asyncio.Event()
    manager = _manager(settings, engine, mcp_progress_interval=1)
    progress = _Progress()
    async with Client(build_mcp(manager), mode=mode) as client:
        call = asyncio.create_task(
            client.call_tool("agent_run", {"task": "slow", "wait_seconds": 1}, progress_callback=progress)
        )
        await asyncio.sleep(2.5)  # well past MCP_WAIT_SECONDS and MCP_MAX_WAIT_SECONDS
        assert not call.done()
        engine.gate.set()
        data = (await asyncio.wait_for(call, 10)).structured_content
    job = _only(manager)
    # The same final shape as agent_job.
    assert data["status"] == "completed" and data["job_id"] == job.id and data["result"] == "Done: slow"
    assert data["changes"]["files"] == [" M README.md"] and data["task"]["status"] == "completed"
    assert all(m.startswith(f"job {job.id}: ") for m in progress.messages)
    assert any("Calling shell" in m for m in progress.messages)
    # Keepalive while nothing happened (interval 1 s, 2.5 s quiet).
    assert sum("Calling shell" in m for m in progress.messages) >= 2
    values = [p for p, _, _ in progress.rows]
    assert values == sorted(values) and len(set(values)) == len(values), "progress must increase"
    last_progress, last_total, last_message = progress.rows[-1]
    assert last_total == last_progress and last_message.endswith("completed")


async def test_progress_reports_a_failed_job(settings, engine):
    engine.fail = "boom"
    manager = _manager(settings, engine)
    progress = _Progress()
    async with Client(build_mcp(manager)) as client:
        data = (await client.call_tool("agent_run", {"task": "x"}, progress_callback=progress)).structured_content
    assert data["status"] == "failed" and "boom" in data["error"]
    assert progress.messages[-1].endswith("failed")


async def test_runtime_watchdog_still_ends_a_followed_call(settings, engine):
    engine.gate = asyncio.Event()
    manager = _manager(settings, engine, job_timeout=1)
    async with Client(build_mcp(manager)) as client:
        data = (await asyncio.wait_for(
            client.call_tool("agent_run", {"task": "slow"}, progress_callback=_Progress()), 15
        )).structured_content
    assert data["status"] == "failed"
    engine.gate.set()


# ---- 3. no progressToken / switched off: as before -------------------------------------------------


async def test_without_progress_token_the_call_answers_with_the_job_id(settings, engine):
    engine.gate = asyncio.Event()
    manager = _manager(settings, engine)
    async with Client(build_mcp(manager)) as client:
        started = time.monotonic()
        first = (await client.call_tool("agent_run", {"task": "slow", "wait_seconds": 600})).structured_content
        assert time.monotonic() - started < 5
        assert first["status"] == "working" and first["job_id"] and "hint" in first
        engine.gate.set()
        done = (await client.call_tool("agent_job", {"job_id": first["job_id"], "wait_seconds": 5})).structured_content
    assert done["status"] == "completed"


async def test_wait_with_progress_can_be_switched_off(settings, engine):
    engine.gate = asyncio.Event()
    manager = _manager(settings, engine, mcp_wait_with_progress=False)
    progress = _Progress()
    async with Client(build_mcp(manager)) as client:
        started = time.monotonic()
        first = (await client.call_tool(
            "agent_run", {"task": "slow", "wait_seconds": 600}, progress_callback=progress
        )).structured_content
        assert time.monotonic() - started < 5
    assert first["status"] == "working" and progress.messages
    engine.gate.set()


def test_progress_settings_from_env(monkeypatch):
    base = Settings.from_env()
    assert base.mcp_wait_with_progress is True and base.mcp_progress_interval == 10
    monkeypatch.setenv("MCP_WAIT_WITH_PROGRESS", "false")
    monkeypatch.setenv("MCP_PROGRESS_INTERVAL_SECONDS", "3")
    new = Settings.from_env()
    assert new.mcp_wait_with_progress is False and new.mcp_progress_interval == 3


# ---- 2. approvals while waiting ------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["legacy", "auto"])
async def test_followed_call_asks_by_elicitation_and_goes_on(settings, engine, mode):
    manager = _manager(settings, engine, ssh_approval_hosts=("prod",))
    results: list = []
    engine.hook = _ssh_hook(manager, results)
    asked: list[str] = []

    async def elicit(_ctx, params):
        asked.append(params.message)
        await asyncio.sleep(1.5)  # longer than MCP_MAX_WAIT_SECONDS: the call must not give up
        return mtypes.ElicitResult(action="accept", content={"approve": True, "note": ""})

    progress = _Progress()
    async with Client(build_mcp(manager), mode=mode, elicitation_callback=elicit) as client:
        data = (await asyncio.wait_for(
            client.call_tool("agent_run", {"task": "reboot prod"}, progress_callback=progress), 15
        )).structured_content
    assert data["status"] == "completed" and results == [(True, "")]
    assert len(asked) == 1 and "reboot" in asked[0] and "root@prod" in asked[0]
    if mode == "legacy":
        assert any("elicitation" in m and "reboot" in m for m in progress.messages)


async def test_followed_call_denied_by_elicitation(settings, engine):
    manager = _manager(settings, engine, ssh_approval_hosts=("prod",))
    results: list = []
    engine.hook = _ssh_hook(manager, results)

    async def elicit(_ctx, _params):
        return mtypes.ElicitResult(action="accept", content={"approve": False, "note": "not now"})

    async with Client(build_mcp(manager), mode="legacy", elicitation_callback=elicit) as client:
        data = (await client.call_tool(
            "agent_run", {"task": "reboot prod"}, progress_callback=_Progress()
        )).structured_content
    assert data["status"] == "completed" and results[0][0] is False


async def test_followed_call_without_elicitation_says_what_it_waits_for(settings, engine):
    manager = _manager(settings, engine, ssh_approval_hosts=("prod",), mcp_progress_interval=1)
    results: list = []
    engine.hook = _ssh_hook(manager, results)
    progress = _Progress()
    async with Client(build_mcp(manager), mode="legacy") as client:
        call = asyncio.create_task(
            client.call_tool("agent_run", {"task": "reboot prod"}, progress_callback=progress)
        )
        await _until(lambda: manager.jobs and _only(manager).status == st.INPUT_REQUIRED)
        await asyncio.sleep(1.5)
        assert not call.done()
        job = _only(manager)
        assert any("approve" in m and "reboot" in m and "/approve" in m for m in progress.messages)
        # The existing approval path (POST /jobs/{id}/approve, agent_approve) answers it.
        manager.decide(job.id, True)
        data = (await asyncio.wait_for(call, 10)).structured_content
    assert data["status"] == "completed" and results == [(True, "")]


async def test_open_elicitation_ends_when_the_approval_is_answered_elsewhere(settings, engine):
    manager = _manager(settings, engine, ssh_approval_hosts=("prod",), mcp_progress_interval=1)
    results: list = []
    engine.hook = _ssh_hook(manager, results)
    never = asyncio.Event()

    async def elicit(_ctx, _params):
        await never.wait()
        return mtypes.ElicitResult(action="cancel")

    async with Client(build_mcp(manager), mode="legacy", elicitation_callback=elicit) as client:
        call = asyncio.create_task(
            client.call_tool("agent_run", {"task": "reboot prod"}, progress_callback=_Progress())
        )
        await _until(lambda: manager.jobs and _only(manager).status == st.INPUT_REQUIRED)
        await asyncio.sleep(0.2)
        manager.decide(_only(manager).id, True)
        data = (await asyncio.wait_for(call, 10)).structured_content
    assert data["status"] == "completed" and results == [(True, "")]


# ---- 4. cancellation -----------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["legacy", "auto"])
async def test_cancelling_a_followed_call_cancels_the_job(settings, engine, kills, mode):
    engine.gate = asyncio.Event()
    manager = _manager(settings, engine)
    async with Client(build_mcp(manager), mode=mode) as client:
        call = asyncio.create_task(
            client.call_tool("agent_run", {"task": "slow"}, progress_callback=_Progress())
        )
        await _until(lambda: manager.jobs and _only(manager).status == st.WORKING)
        await asyncio.sleep(1.5)  # past the old caps: the call is still open and bound to the job
        call.cancel()
        with pytest.raises(BaseException):
            await call
        job = _only(manager)
        await manager.wait(job, 15)
    assert job.status == st.CANCELLED and "agent_run" in job.error
    if mode == "legacy":
        assert "notifications/cancelled" in job.error
    engine.gate.set()


async def test_cancelling_a_followed_background_call_keeps_the_job(settings, engine, kills):
    engine.gate = asyncio.Event()
    manager = _manager(settings, engine)
    async with Client(build_mcp(manager), mode="legacy") as client:
        call = asyncio.create_task(
            client.call_tool("agent_run", {"task": "slow", "background": True}, progress_callback=_Progress())
        )
        await _until(lambda: manager.jobs and _only(manager).status == st.WORKING)
        call.cancel()
        with pytest.raises(BaseException):
            await call
        job = _only(manager)
        await asyncio.sleep(0.1)
        assert job.status == st.WORKING
        engine.gate.set()
        await manager.wait(job, 5)
    assert job.status == st.COMPLETED and not kills
