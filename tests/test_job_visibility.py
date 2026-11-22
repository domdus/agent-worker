"""Job visibility, cancel that frees the workspace, watchdogs, effective models (0.5.0)."""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import replace

import httpx
from mcp import Client

from agent_worker import jobs as jobs_module
from agent_worker import proctree
from agent_worker.app import create_app
from agent_worker.engine import JOB_ENV, Outcome
from agent_worker.jobs import JobManager, JobRequest, Store
from agent_worker.mcp_server import build_mcp


def _manager(settings, engine, **overrides):
    return JobManager(replace(settings, **overrides), engine, Store(settings.data_dir / "worker.sqlite"))


async def _until(check, seconds=5.0):
    deadline = time.monotonic() + seconds
    while not check():
        assert time.monotonic() < deadline, "condition not reached"
        await asyncio.sleep(0.02)


# ---- parallelism -------------------------------------------------------------------------------


async def test_job_waiting_for_its_workspace_does_not_hold_a_slot(settings, engine):
    engine.gate = asyncio.Event()
    manager = _manager(settings, engine, max_concurrent_jobs=2)
    first = manager.submit(JobRequest(task="a"))
    second = manager.submit(JobRequest(task="b"))
    third = manager.submit(JobRequest(task="c", workspace="other"))
    await asyncio.sleep(0.05)
    # Before 0.5.0 b took the second slot while it waited for a, and c (another workspace) had to wait.
    assert first.status == "working" and third.status == "working" and second.status == "queued"
    engine.gate.set()
    for job in (first, second, third):
        await manager.wait(job, 5)
        assert job.status == "completed"


async def test_slot_wait_is_reported(settings, engine):
    engine.gate = asyncio.Event()
    manager = _manager(settings, engine, max_concurrent_jobs=1)
    first = manager.submit(JobRequest(task="a"))
    other = manager.submit(JobRequest(task="b", workspace="other"))
    await asyncio.sleep(0.05)
    row = manager.describe(other)
    assert row["status"] == "queued" and row["waiting_for"] == "slot" and row["blocked_by"] is None
    assert "MAX_CONCURRENT_JOBS" in row["last_step"]
    engine.gate.set()
    await manager.wait(first, 5)
    await manager.wait(other, 5)


# ---- overview ----------------------------------------------------------------------------------


async def test_overview_shows_open_jobs_blockers_and_no_full_prompt(settings, engine):
    manager = _manager(settings, engine)
    finished = [manager.submit(JobRequest(task=f"old {n}")) for n in range(3)]
    for job in finished:
        await manager.wait(job, 5)
    engine.gate = asyncio.Event()
    long_task = "secret plan " + "x" * 5000
    running = manager.submit(JobRequest(task=long_task, model="gpt-5"))
    queued = manager.submit(JobRequest(task="next"))
    await asyncio.sleep(0.05)

    rows = [manager.describe(job) for job in manager.overview(finished=2)]
    assert [r["job_id"] for r in rows[:2]] == [queued.id, running.id]
    assert len(rows) == 4  # two open, the last two that ended
    run_row = next(r for r in rows if r["job_id"] == running.id)
    assert run_row["status"] == "working" and run_row["model"] == "gpt-5" and run_row["engine"] == "cursor"
    assert len(run_row["task_preview"]) <= 120 and long_task not in str(rows)
    assert run_row["runtime_seconds"] is not None and run_row["last_step"] == "Calling shell..."
    assert run_row["last_event_at"] and run_row["waiting_for_approval"] is False
    queue_row = next(r for r in rows if r["job_id"] == queued.id)
    assert queue_row["waiting_for"] == "workspace" and queue_row["blocked_by"] == running.id
    assert queue_row["runtime_seconds"] is None
    # The default model of the engine when none was asked for.
    assert queue_row["model"] == "composer-2.5"

    only = manager.overview(status="queued,needs_approval")
    assert [job.id for job in only] == [queued.id]
    assert manager.overview(workspace="other") == []
    engine.gate.set()
    await manager.wait(queued, 5)


# ---- cancel ------------------------------------------------------------------------------------


async def test_cancel_workspace_frees_it_for_the_next_job(settings, engine):
    engine.gate = asyncio.Event()
    manager = _manager(settings, engine)
    running = manager.submit(JobRequest(task="a"))
    queued = manager.submit(JobRequest(task="b"))
    elsewhere = manager.submit(JobRequest(task="c", workspace="other"))
    await asyncio.sleep(0.05)
    cancelled = manager.cancel_workspace("default")
    assert {job.id for job in cancelled} == {running.id, queued.id}
    for job in cancelled:
        await manager.wait(job, 5)
        assert job.status == "cancelled"
    assert not elsewhere.done
    engine.gate = None
    after = manager.submit(JobRequest(task="d"))
    await manager.wait(after, 5)
    assert after.status == "completed"
    engine.gate = asyncio.Event()
    engine.gate.set()


class StubbornEngine:
    """An engine that ignores cancellation (a hung SDK call), to see that the workspace is freed anyway."""

    available = True

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.runs = 0

    async def run(self, spec, emit):
        self.runs += 1
        if self.runs > 1:
            return Outcome(status="succeeded", text="second")
        await emit({"type": "status", "text": "stuck"})
        while not self.release.is_set():
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                continue
        return Outcome(status="succeeded", text="late")

    async def models(self):
        return []

    async def aclose(self):
        return None


async def test_cancel_frees_the_workspace_when_the_engine_hangs(settings, monkeypatch):
    monkeypatch.setattr(jobs_module, "STOP_GRACE", 0.2)
    engine = StubbornEngine()
    manager = _manager(settings, engine)
    stuck = manager.submit(JobRequest(task="a"))
    behind = manager.submit(JobRequest(task="b"))
    await asyncio.sleep(0.05)
    manager.cancel(stuck.id)
    await manager.wait(stuck, 5)
    assert stuck.status == "cancelled"
    await manager.wait(behind, 5)
    assert behind.status == "completed" and behind.result == "second"
    engine.release.set()
    await asyncio.sleep(0.01)


async def test_cancel_kills_the_engine_process_tree(settings, engine):
    manager = _manager(settings, engine)
    started: list[int] = []

    async def spawn(spec):
        # What an agent's shell looks like: a child (and grandchild) carrying the job marker.
        proc = await asyncio.create_subprocess_exec(
            "sh", "-c", "sleep 60 & sleep 61; wait", env={**os.environ, JOB_ENV: spec.job_id}
        )
        started.append(proc.pid)
        return proc

    engine.hook = spawn
    engine.gate = asyncio.Event()
    job = manager.submit(JobRequest(task="a"))
    await _until(lambda: len(engine.hook_results) == 1)
    await _until(lambda: len(proctree.find(JOB_ENV, job.id)) >= 3)
    tree = proctree.find(JOB_ENV, job.id)
    manager.cancel(job.id)
    await manager.wait(job, 10)
    assert job.status == "cancelled"
    await engine.hook_results[0].wait()
    await _until(lambda: not any(proctree._alive(pid) for pid in tree))
    assert proctree.find(JOB_ENV, job.id) == []
    engine.gate.set()


def test_proctree_never_finds_the_worker_itself(monkeypatch):
    marker = "test-" + os.urandom(6).hex()
    monkeypatch.setenv(JOB_ENV, marker)
    # os.environ changes do not show in /proc/self/environ; a re-exec of ourselves would. Either way
    # this process and its parents are protected.
    assert os.getpid() not in proctree.find(JOB_ENV, marker)
    assert proctree.find(JOB_ENV, "") == []


# ---- watchdogs ---------------------------------------------------------------------------------


async def test_idle_timeout_fails_the_job_and_frees_the_workspace(settings, engine):
    engine.gate = asyncio.Event()
    manager = _manager(settings, engine, job_idle_timeout=1)
    job = manager.submit(JobRequest(task="a"))
    behind = manager.submit(JobRequest(task="b"))
    await manager.wait(job, 5)
    assert job.status == "failed" and "JOB_IDLE_TIMEOUT_SECONDS" in job.error
    engine.gate.set()
    await manager.wait(behind, 5)
    assert behind.status == "completed"


async def test_max_runtime_fails_the_job(settings, engine):
    engine.gate = asyncio.Event()
    manager = _manager(settings, engine, job_timeout=1, job_idle_timeout=0)
    job = manager.submit(JobRequest(task="a"))
    await manager.wait(job, 5)
    assert job.status == "failed" and "JOB_MAX_RUNTIME_SECONDS (1s)" in job.error
    engine.gate.set()


async def test_approval_watchdog_cancels_a_forgotten_approval(settings, engine):
    """Even when the call that asked is gone (the guard hung up), the job does not wait forever."""
    manager = _manager(settings, engine, ssh_approval_hosts=("prod",), approval_timeout=1)
    engine.gate = asyncio.Event()
    job = manager.submit(JobRequest(task="a"))
    await asyncio.sleep(0.05)
    call = asyncio.create_task(manager.remote_call("default", "ssh", "prod", "", "root", "reboot"))
    await _until(lambda: job.status == "input_required")
    row = manager.describe(job)
    assert row["waiting_for_approval"] and row["approval"]["host"] == "prod" and row["approval"]["command"] == "reboot"
    await manager.wait(job, 5)
    assert job.status == "cancelled" and job.error.startswith("approval timed out")
    allow, reason = await call
    assert not allow
    engine.gate.set()


def test_watchdog_settings_from_env(monkeypatch):
    from agent_worker.settings import Settings

    for name in ("JOB_MAX_RUNTIME_SECONDS", "JOB_TIMEOUT_SECONDS", "JOB_IDLE_TIMEOUT_SECONDS",
                 "APPROVAL_TIMEOUT_SECONDS", "SSH_APPROVAL_TIMEOUT", "MCP_MAX_WAIT_SECONDS"):
        monkeypatch.delenv(name, raising=False)
    base = Settings.from_env()
    assert (base.job_timeout, base.job_idle_timeout, base.approval_timeout, base.mcp_max_wait) == (3600, 900, 1800, 25)
    monkeypatch.setenv("JOB_TIMEOUT_SECONDS", "1200")
    monkeypatch.setenv("SSH_APPROVAL_TIMEOUT", "600")
    old = Settings.from_env()
    assert old.job_timeout == 1200 and old.approval_timeout == 600
    monkeypatch.setenv("JOB_MAX_RUNTIME_SECONDS", "7200")
    monkeypatch.setenv("APPROVAL_TIMEOUT_SECONDS", "300")
    monkeypatch.setenv("JOB_IDLE_TIMEOUT_SECONDS", "0")
    monkeypatch.setenv("MCP_MAX_WAIT_SECONDS", "50")
    new = Settings.from_env()
    assert (new.job_timeout, new.job_idle_timeout, new.approval_timeout, new.mcp_max_wait) == (7200, 0, 300, 50)


# ---- MCP ---------------------------------------------------------------------------------------


def _data(result):
    return result.structured_content or {}


async def test_agent_run_wait_is_capped_and_returns_the_job_id(settings, engine):
    engine.gate = asyncio.Event()
    # Progress messages without waiting for the end (MCP_WAIT_WITH_PROGRESS=false; see test_mcp_progress).
    manager = _manager(settings, engine, mcp_max_wait=1, mcp_wait_with_progress=False)
    progress: list[str] = []

    async def on_progress(_progress, _total, message):
        progress.append(message or "")

    async with Client(build_mcp(manager)) as client:
        started = time.monotonic()
        first = _data(await client.call_tool(
            "agent_run", {"task": "slow", "wait_seconds": 600}, progress_callback=on_progress
        ))
        assert time.monotonic() - started < 5
        assert first["status"] == "working" and first["job_id"] and first["model"] == "composer-2.5"
        assert first["runtime_seconds"] is not None and first["last_step"]
        assert progress and progress[0].startswith(f"job {first['job_id']}")
        listed = _data(await client.call_tool("agent_jobs", {}))
        assert [row["job_id"] for row in listed["jobs"]] == [first["job_id"]]
        engine.gate.set()
        done = _data(await client.call_tool("agent_job", {"job_id": first["job_id"], "wait_seconds": 5}))
    assert done["status"] == "completed" and done["model"] == "composer-2.5"


async def test_agent_cancel_by_workspace(settings, engine):
    engine.gate = asyncio.Event()
    manager = _manager(settings, engine, mcp_wait_seconds=0)
    async with Client(build_mcp(manager)) as client:
        a = _data(await client.call_tool("agent_run", {"task": "a", "workspace": "repo"}))
        b = _data(await client.call_tool("agent_run", {"task": "b", "workspace": "repo"}))
        await asyncio.sleep(0.05)
        jobs = _data(await client.call_tool("agent_jobs", {"workspace": "repo"}))["jobs"]
        queued = next(row for row in jobs if row["job_id"] == b["job_id"])
        assert queued["blocked_by"] == a["job_id"]
        out = _data(await client.call_tool("agent_cancel", {"workspace": "repo"}))
        nothing = _data(await client.call_tool("agent_cancel", {}))
        spaces = _data(await client.call_tool("agent_workspaces", {}))
    assert out["workspace"] == "repo"
    assert {row["job_id"]: row["status"] for row in out["cancelled"]} == {a["job_id"]: "cancelled", b["job_id"]: "cancelled"}
    assert nothing["status"] == "rejected"
    assert spaces["default_models"] == {"cursor": "composer-2.5"} and "parallel" in spaces["parallelism"]
    engine.gate.set()


# ---- HTTP --------------------------------------------------------------------------------------


async def test_http_overview_and_workspace_cancel(settings, engine):
    app = create_app(settings, engine)
    engine.gate = asyncio.Event()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 40000))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8787") as http:
        first = (await http.post("/jobs", json={"task": "a"})).json()
        second = (await http.post("/jobs", json={"task": "b"})).json()
        await asyncio.sleep(0.05)
        rows = (await http.get("/jobs/overview")).json()["jobs"]
        assert {row["job_id"] for row in rows} == {first["id"], second["id"]}
        assert next(r for r in rows if r["job_id"] == second["id"])["blocked_by"] == first["id"]
        live = (await http.get(f"/jobs/{second['id']}")).json()
        assert live["blocked_by"] == first["id"] and live["model"] == "composer-2.5"
        spaces = (await http.get("/workspaces")).json()
        assert spaces["default_models"] == {"cursor": "composer-2.5"}
        out = (await http.post("/workspaces/default/cancel")).json()
        assert sorted(row["status"] for row in out["cancelled"]) == ["cancelled", "cancelled"]
        engine.gate.set()
        third = (await http.post("/jobs", json={"task": "c", "wait": 5})).json()
        assert third["status"] == "completed"
