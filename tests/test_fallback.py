"""Engine fallback chain: classifier, chain walking, auth skip, wait/resume, promotion/backoff, handoff."""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from agent_worker import fallback as fb
from agent_worker.engine import EngineError, Outcome
from agent_worker.engines import EngineRouter
from agent_worker.jobs import JobManager, JobRequest, Store
from agent_worker.mcp_server import job_payload
from agent_worker.settings import Settings
from tests.conftest import FakeEngine

# ---- classifier ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("Claude AI usage limit reached|1900000000", fb.LIMIT),
        ("You've hit your limit · resets 6:30pm (Europe/Berlin)", fb.LIMIT),
        ("This ChatGPT plan or API key hit its Codex usage limit. Try again later.", fb.LIMIT),
        ("Your credit balance is too low to access the Anthropic API.", fb.LIMIT),
        ("Error code: 429 - too many requests", fb.RATE_LIMIT),
        ("This Claude credential hit its usage or rate limit. Try again later.", fb.LIMIT),
        ("API Error: 401 invalid x-api-key", fb.AUTH),
        ("OAuth token has expired. Please obtain a new token or refresh your existing token.", fb.AUTH),
        ("No Cursor API key is set. Create a user API key at https://cursor.com", fb.AUTH),
        ("Request timed out.", fb.TIMEOUT),
        ("httpx.ConnectError: [Errno 111] Connection refused", fb.CONNECTION),
        ("stream disconnected before completion", fb.CONNECTION),
        ("API Error: 529 Overloaded", fb.SERVER),
        ("API Error: 500 Internal server error", fb.SERVER),
        ("503 Service Unavailable", fb.SERVER),
    ],
)
def test_provider_failures_are_classified(text, kind) -> None:
    failure = fb.classify(text)
    assert failure is not None and failure.kind == kind


@pytest.mark.parametrize(
    "text",
    [
        "Cancelled by the MCP client (notifications/cancelled for the agent_run call).",
        "The user did not approve ssh to prod: no. Do not retry or work around this.",
        "prompt is too long: 250000 tokens > 200000 maximum",
        "Codex does not offer the model gpt-9 for this account.",
        "ssh: connect to host db01 port 22: Connection refused",
        "The job ran longer than JOB_MAX_RUNTIME_SECONDS (3600s) and was stopped.",
        "I can't help with that request.",
        "Tool failed",
        "SyntaxError in main.py line 3",
        "",
    ],
)
def test_other_failures_never_fall_back(text) -> None:
    assert fb.classify(text) is None


def test_structured_codes_and_exceptions() -> None:
    assert fb.classify(Outcome(status="failed", error="whatever", error_kind="usageLimitExceeded")).kind == fb.LIMIT
    assert fb.classify(Outcome(status="failed", error="x", error_kind="rate_limit")).kind == fb.RATE_LIMIT
    assert fb.classify(Outcome(status="failed", error="Rate limit", error_kind="contextWindowExceeded")) is None
    assert fb.classify(EngineError("boom", kind="authentication_failed")).kind == fb.AUTH

    class ConnectError(Exception):
        pass

    try:
        try:
            raise ConnectError("x")
        except ConnectError as inner:
            raise EngineError("something odd") from inner
    except EngineError as exc:
        assert fb.classify(exc).kind == fb.CONNECTION
    assert fb.classify(TimeoutError()).kind == fb.TIMEOUT


def test_reset_times() -> None:
    now = 1_800_000_000.0
    assert fb.reset_time("usage limit reached|1900000000", now=now) == 1_900_000_000
    assert fb.reset_time("try again in 2 hours 13 minutes", now=now) == now + 2 * 3600 + 13 * 60
    assert fb.reset_time("Retry-After: 120", now=now) == now + 120
    iso = datetime(2027, 1, 15, 18, 30, tzinfo=timezone.utc).timestamp()
    assert fb.reset_time("limit resets at 2027-01-15T18:30:00Z", now=now) == iso
    at = fb.reset_time("You've hit your limit · resets 6:30pm (UTC)", now=now)
    assert at is not None and 0 < at - now <= 86400 and time.gmtime(at)[3:5] == (18, 30)
    assert fb.reset_time("until 3 files are done", now=now) is None
    # A timeout's "after 60 s" is no reset time; a far-off rate limit is a plan limit.
    assert fb.classify("Request timed out after 60 s", now=now).reset_at is None
    far = fb.classify("rate limit exceeded, try again in 5 hours", now=now)
    assert far.kind == fb.LIMIT and far.reset_at == now + 5 * 3600


def test_status_line_text() -> None:
    at = time.time() + 3600
    text = fb.describe("claude", fb.Failure(fb.LIMIT, "limit", at))
    assert text == f"claude am Limit (Reset {fb.clock(at)})"


# ---- configuration ------------------------------------------------------------------------------


def test_settings_from_env(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("FALLBACK_ENGINES", "claude, cursor:composer-2.5,codex:gpt-5.5,gemini,claude")
    monkeypatch.setenv("FALLBACK_PROMOTE", "reset")
    monkeypatch.setenv("FALLBACK_BACKOFF", "1m,10m,2h")
    s = Settings.from_env()
    assert s.fallback_engines == (("claude", ""), ("cursor", "composer-2.5"), ("codex", "gpt-5.5"))
    assert s.fallback_promote == "reset"
    assert s.fallback_backoff == (60, 600, 7200)
    monkeypatch.setenv("FALLBACK_ENGINES", "")
    monkeypatch.setenv("FALLBACK_PROMOTE", "bogus")
    monkeypatch.setenv("FALLBACK_BACKOFF", "soon")
    s = Settings.from_env()
    assert s.fallback_engines == () and s.fallback_promote == "turn" and s.fallback_backoff == (300, 900, 3600)


# ---- health / backoff ---------------------------------------------------------------------------


class Clock:
    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def test_backoff_steps_and_reset(tmp_path) -> None:
    clock = Clock()
    health = fb.EngineHealth((300, 900, 3600), lock_seconds=60, clock=clock, path=tmp_path / "state.json")
    down = fb.Failure(fb.CONNECTION, "connection refused")
    assert health.mark_failed("claude", down) == clock.now + 300
    assert health.mark_failed("claude", down) == clock.now + 900
    assert health.mark_failed("claude", down) == clock.now + 3600
    assert health.mark_failed("claude", down) == clock.now + 3600
    assert health.blocked_until("claude")
    clock.now += 3601
    assert not health.blocked_until("claude")
    # A known reset time wins over the backoff, but never shorter than the lock.
    assert health.mark_failed("codex", fb.Failure(fb.LIMIT, "x", clock.now + 7200)) == clock.now + 7200
    assert health.mark_failed("cursor", fb.Failure(fb.LIMIT, "x", clock.now + 5)) == clock.now + 60
    # Kept across restarts.
    again = fb.EngineHealth((300,), clock=clock, path=tmp_path / "state.json")
    assert again.blocked_until("codex") == clock.now + 7200
    health.mark_ok("codex")
    assert not health.blocked_until("codex")


def test_promote_reset_waits_for_longest_step_without_reset_time() -> None:
    clock = Clock()
    health = fb.EngineHealth((300, 900, 3600), lock_seconds=0, promote="reset", clock=clock)
    assert health.mark_failed("claude", fb.Failure(fb.SERVER, "overloaded")) == clock.now + 3600


# ---- journal / handoff --------------------------------------------------------------------------


def test_handoff_message_lists_steps_and_rules() -> None:
    journal = [
        {"type": "tool", "name": "Bash", "phase": "start", "id": "1", "engine": "claude", "detail": "git push origin main"},
        {"type": "tool", "name": "tool", "phase": "end", "id": "1", "engine": "claude", "output": "main -> main"},
        {"type": "tool", "name": "ssh", "phase": "start", "engine": "claude", "host": "prod", "command": "systemctl restart app",
         "text": "ssh prod: systemctl restart app"},
        {"type": "tool", "name": "Read", "phase": "start", "id": "2", "engine": "claude", "detail": "file_path=a.py"},
    ]
    text = fb.handoff_message(
        fb.Handoff(task="deploy it", history="User: hi\nAssistant: hello", journal=journal,
                   partial=[("claude", "I pushed and")], reasons=["claude am Limit"],
                   changes={"files": [" M a.py"], "commits": ["abc fix"]}),
        "cursor",
    )
    assert "ENGINE HANDOFF" in text and "claude am Limit" in text
    assert "Original task:\ndeploy it" in text and "User: hi" in text
    assert "Bash (side effect): git push origin main -> done | output: main -> main" in text
    assert "prod: systemctl restart app" in text
    assert "a.py -> no result" in text
    assert "do NOT repeat side-effecting steps" in text
    assert "abc fix" in text and "I pushed and" in text


# ---- chain walking in jobs ----------------------------------------------------------------------


class Scripted(FakeEngine):
    """An engine whose runs follow a script: "ok", an Outcome or an exception, one per run."""

    def __init__(self, name: str, settings, script=(), available: bool = True) -> None:
        super().__init__()
        self.name = name
        self.settings = settings
        self.available = available
        self.script = list(script)
        self.forgotten: list[tuple[str, str]] = []

    async def run(self, spec, emit) -> Outcome:
        self.specs.append(spec)
        await emit({"type": "tool", "name": "Bash", "phase": "start", "text": "Calling Bash...",
                    "id": f"{self.name}-{len(self.specs)}", "detail": f"git push ({self.name})"})
        await emit({"type": "tool", "name": "tool", "phase": "end", "text": "Finished",
                    "id": f"{self.name}-{len(self.specs)}", "output": "pushed"})
        step = self.script.pop(0) if self.script else "ok"
        if self.gate is not None:
            await self.gate.wait()
        if isinstance(step, BaseException):
            raise step
        if isinstance(step, Outcome):
            await emit({"type": "text", "delta": "half an answer"})
            return step
        await emit({"type": "text", "delta": f"{self.name} done"})
        return Outcome(status="succeeded", text=f"{self.name} done", model=spec.model or f"{self.name}-model")

    def forget(self, workspace: str, session: str) -> None:
        self.forgotten.append((workspace, session))


LIMIT = Outcome(status="failed", error="You've hit your limit · resets 6:30pm", error_kind="rate_limit")


def _setup(settings, *, chain="claude,cursor,codex", promote="turn", lock=0, scripts=None, available=None):
    scripts = scripts or {}
    available = available or {}
    chain_t = fb.parse_chain(chain, ("cursor", "claude", "codex"))
    s = replace(settings, fallback_engines=chain_t, fallback_promote=promote, fallback_lock_seconds=lock)
    engines = {name: Scripted(name, s, scripts.get(name, ()), available.get(name, True))
               for name in ("cursor", "claude", "codex")}
    manager = JobManager(s, EngineRouter(engines), Store(s.data_dir / "worker.sqlite"))
    manager.wait_poll = 0.05
    return manager, engines


async def test_limit_hands_over_to_next_engine(settings) -> None:
    manager, engines = _setup(settings, scripts={"claude": [LIMIT]})
    job = manager.submit(JobRequest(task="ship the release"))
    assert job.engine == "claude"
    await manager.wait(job, 5)
    assert job.status == "completed", job.error
    assert job.result == "cursor done"
    assert job.engines_used() == ["claude", "cursor"]
    assert [a["status"] for a in job.attempts] == ["handed_off", "completed"]
    steps = [e.get("text") for e in job.events if e["type"] == "status"]
    switch = next(t for t in steps if "übernimmt" in t)
    assert switch.startswith("claude am Limit (Reset ") and switch.endswith("– cursor übernimmt")
    assert "Engines: claude → cursor" in steps
    # The handoff: original task, journal of what ran, the rule not to repeat side effects.
    task = engines["cursor"].specs[0].task
    assert "ship the release" in task and "git push (claude) -> done | output: pushed" in task
    assert "do NOT repeat" in task and "half an answer" in task
    # Journal fields never reach stored or streamed events.
    assert not any("detail" in e or "output" in e for e in job.events)
    assert manager.health.blocked_until("claude")
    payload = job_payload(job)
    assert payload["engines_used"] == ["claude", "cursor"] and len(payload["fallback"]) == 2
    assert job.as_dict()["engines_used"] == ["claude", "cursor"]


async def test_explicit_engine_starts_the_chain_below_it(settings) -> None:
    manager, engines = _setup(settings, scripts={"cursor": [EngineError("API Error: 503 Service Unavailable")]})
    job = manager.submit(JobRequest(task="t", engine="cursor"))
    await manager.wait(job, 5)
    assert job.status == "completed"
    assert job.engines_used() == ["cursor", "codex"]
    assert not engines["claude"].specs


async def test_chain_model_is_used(settings) -> None:
    manager, engines = _setup(settings, chain="claude:claude-opus-5-5,codex:gpt-5.5", scripts={"claude": [LIMIT]})
    job = manager.submit(JobRequest(task="t"))
    await manager.wait(job, 5)
    assert engines["claude"].specs[0].model == "claude-opus-5-5"
    assert engines["codex"].specs[0].model == "gpt-5.5"
    assert job.model == "gpt-5.5"


async def test_not_signed_in_engines_are_skipped(settings) -> None:
    manager, engines = _setup(settings, scripts={"claude": [LIMIT]}, available={"cursor": False})
    job = manager.submit(JobRequest(task="t"))
    await manager.wait(job, 5)
    assert job.status == "completed"
    assert job.engines_used() == ["claude", "codex"]
    assert any(a["engine"] == "cursor" and a["status"] == "skipped" for a in job.attempts)
    assert not engines["cursor"].specs
    assert any("codex übernimmt" in (e.get("text") or "") for e in job.events)


async def test_non_provider_failures_end_the_job(settings) -> None:
    manager, engines = _setup(settings, scripts={"claude": [EngineError("prompt is too long: 300000 tokens")]})
    job = manager.submit(JobRequest(task="t"))
    await manager.wait(job, 5)
    assert job.status == "failed" and "prompt is too long" in job.error
    assert not engines["cursor"].specs

    manager, engines = _setup(settings, scripts={"claude": [Outcome(status="failed", error="Tool failed")]})
    job = manager.submit(JobRequest(task="t"))
    await manager.wait(job, 5)
    assert job.status == "failed" and not engines["cursor"].specs


async def test_cancel_never_falls_back(settings) -> None:
    manager, engines = _setup(settings)
    engines["claude"].gate = asyncio.Event()
    job = manager.submit(JobRequest(task="t"))
    await asyncio.sleep(0.05)
    manager.cancel(job.id)
    await manager.wait(job, 5)
    assert job.status == "cancelled"
    assert not engines["cursor"].specs


async def test_off_without_chain(settings) -> None:
    engines = {name: Scripted(name, settings, [LIMIT] if name == "claude" else ()) for name in ("cursor", "claude")}
    manager = JobManager(settings, EngineRouter(engines), Store(settings.data_dir / "worker.sqlite"))
    job = manager.submit(JobRequest(task="t", engine="claude"))
    await manager.wait(job, 5)
    assert job.status == "failed" and not job.attempts
    assert not engines["cursor"].specs


# ---- waiting for an engine ----------------------------------------------------------------------


def _soon(seconds: float) -> Outcome:
    return Outcome(status="failed", error=f"usage limit reached|{int(time.time() + seconds)}")


async def test_waits_when_no_engine_is_left_and_resumes(settings) -> None:
    manager, engines = _setup(
        replace(settings, max_concurrent_jobs=1),
        chain="claude,cursor",
        scripts={"claude": [_soon(1)], "cursor": [Outcome(status="failed", error="429 too many requests, retry after 3600")]},
    )
    job = manager.submit(JobRequest(task="long task"))
    for _ in range(100):
        await asyncio.sleep(0.02)
        if job.waiting_for == "engine":
            break
    assert job.status == "queued" and job.waiting_for == "engine"
    message = job.status_message()
    assert message.startswith("queued: Keine Engine frei (claude am Limit (Reset ") and "automatisch weiter" in message
    # The waiting job gives its slot back: another job runs meanwhile.
    other = manager.submit(JobRequest(task="chat", tools="none", engine="codex"))
    await manager.wait(other, 3)
    assert other.status == "completed"
    await manager.wait(job, 5)
    assert job.status == "completed", job.error
    assert job.engines_used() == ["claude", "cursor", "claude"]
    assert job.result == "claude done"
    assert any("Job geht weiter" in (e.get("text") or "") for e in job.events)


async def test_cancel_while_waiting(settings) -> None:
    manager, _ = _setup(settings, chain="claude", scripts={"claude": [_soon(3600)]})
    job = manager.submit(JobRequest(task="t"))
    for _ in range(100):
        await asyncio.sleep(0.02)
        if job.waiting_for == "engine":
            break
    assert job.status == "queued"
    manager.cancel(job.id, "stop it")
    await manager.wait(job, 3)
    assert job.status == "cancelled" and job.error == "stop it"


async def test_waits_for_a_sign_in(settings) -> None:
    manager, engines = _setup(settings, chain="claude,cursor", available={"claude": False, "cursor": False})
    job = manager.submit(JobRequest(task="t"))
    await asyncio.sleep(0.1)
    assert job.status == "queued" and "angemeldet" in job.status_message()
    engines["cursor"].available = True
    await manager.wait(job, 3)
    assert job.status == "completed" and job.engines_used() == ["cursor"]


# ---- promotion back up --------------------------------------------------------------------------


async def _turn(manager, task: str, session: str = "s1"):
    job = manager.submit(JobRequest(task=task, session=session))
    await manager.wait(job, 5)
    assert job.status == "completed", job.error
    return job


async def test_promotion_at_turn_start_after_backoff(settings) -> None:
    manager, engines = _setup(settings, scripts={"claude": [EngineError("Connection error.")]})
    clock = Clock(time.time())
    manager.health.clock = clock
    first = await _turn(manager, "one")
    assert first.engines_used() == ["claude", "cursor"]
    # Backoff (5 min) not over: no probe of claude, the session stays on cursor.
    second = await _turn(manager, "two")
    assert second.engines_used() == ["cursor"]
    assert len(engines["claude"].specs) == 1
    clock.now += 301
    third = await _turn(manager, "three")
    assert third.engines_used() == ["claude"]
    assert any("claude wieder verfügbar – zurück von cursor zu claude" in (e.get("text") or "") for e in third.events)
    # Claude missed turns: its own session is dropped and it gets the conversation.
    spec = engines["claude"].specs[-1]
    assert ("default", "s1") in engines["claude"].forgotten
    assert "User: two" in spec.context and "cursor done" in spec.context


async def test_promote_off_keeps_the_session_down(settings) -> None:
    manager, engines = _setup(settings, promote="off", scripts={"claude": [EngineError("Connection error.")]})
    clock = Clock(time.time())
    manager.health.clock = clock
    await _turn(manager, "one")
    clock.now += 10_000
    second = await _turn(manager, "two")
    assert second.engines_used() == ["cursor"]
    # A new session starts at the top again.
    other = await _turn(manager, "fresh", session="s2")
    assert other.engines_used() == ["claude"]


async def test_lock_against_flapping(settings) -> None:
    manager, engines = _setup(settings, lock=3600, scripts={"claude": [EngineError("Connection error.")]})
    clock = Clock(time.time())
    manager.health.clock = clock
    await _turn(manager, "one")
    clock.now += 3000  # past claude's block (5 min, the lock is longer: 1 h), but the session just switched
    manager.health.mark_ok("claude")
    second = await _turn(manager, "two")
    assert second.engines_used() == ["cursor"]
    clock.now += 1000
    third = await _turn(manager, "three")
    assert third.engines_used() == ["claude"]


async def test_sign_in_clears_an_auth_block(settings) -> None:
    manager, _ = _setup(settings, scripts={"claude": [EngineError("API Error: 401 invalid x-api-key")]})
    job = manager.submit(JobRequest(task="t"))
    await manager.wait(job, 5)
    assert manager.health.blocked_until("claude")
    manager.engine_signed_in("claude")
    assert not manager.health.blocked_until("claude")


# ---- /v1 stream and /health ---------------------------------------------------------------------


def test_v1_stream_shows_the_switch_as_thinking(settings) -> None:
    import json

    from fastapi.testclient import TestClient

    from agent_worker.app import create_app

    manager, engines = _setup(settings, scripts={"claude": [LIMIT]})
    router = manager.engine
    app_settings = manager.settings
    with TestClient(create_app(app_settings, router), base_url="http://127.0.0.1:8787") as client:
        payload = {"model": "claude-code", "stream": True, "messages": [{"role": "user", "content": "hello"}]}
        with client.stream("POST", "/v1/chat/completions", json=payload) as resp:
            lines = [line for line in resp.iter_lines() if line.startswith("data: ")]
        chunks = [json.loads(line[6:]) for line in lines[:-1]]
        thinking = "".join(c["choices"][0]["delta"].get("reasoning_content", "") for c in chunks if c.get("choices"))
        content = "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks if c.get("choices"))
        assert "– cursor übernimmt" in thinking and "Engines: claude → cursor" in thinking
        assert content.endswith("cursor done")
        health = client.get("/health").json()
        assert health["fallback"]["engines"] == ["claude", "cursor", "codex"]
        assert "claude" in health["fallback"]["blocked"]
