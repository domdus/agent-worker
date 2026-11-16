"""Job statuses in the MCP Task vocabulary, and a job seen as an MCP Task (spec 2025-11-25, basic/utilities/tasks).

A job has one of these statuses (the MCP Task statuses plus `queued`):

    queued          waits for its workspace, its session or a free slot (as a Task: working)
    working         the engine runs                                   (before 0.5.0: running)
    input_required  paused until a human answers an approval          (before 0.5.0: needs_approval)
    completed       ended with an answer                              (before 0.5.0: succeeded)
    failed          ended with an error, or a watchdog stopped it
    cancelled       stopped by a cancel (agent_cancel, MCP notifications/cancelled, a disconnect, approval timeout)

The names before 0.5.0 are still accepted everywhere a status is read (filters, a job saved by an older
worker) and reported as `legacy_status`. JOB_STATUS_NAMES=legacy makes `status` itself use them again.

`task_view` maps a job 1:1 onto the Task object (taskId, status, statusMessage, createdAt, lastUpdatedAt,
ttl, pollInterval) and `task_result` onto what tasks/result would return (the CallToolResult of the
agent_run call). The MCP SDK this worker uses (mcp 2.3.0) does not serve tasks/* yet, so these back the
agent_job/agent_jobs answers today and the Tasks methods once the SDK has them (see README,
"Jobs, cancellation and standards").
"""

from __future__ import annotations

from typing import Any

QUEUED = "queued"
WORKING = "working"
INPUT_REQUIRED = "input_required"
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"

STATUSES = (QUEUED, WORKING, INPUT_REQUIRED, COMPLETED, FAILED, CANCELLED)
TERMINAL = frozenset({COMPLETED, FAILED, CANCELLED})
# MCP TaskStatus has no "queued": a queued job is a working task whose statusMessage says what it waits for.
TASK_STATUS = {QUEUED: WORKING}

_LEGACY = {WORKING: "running", INPUT_REQUIRED: "needs_approval", COMPLETED: "succeeded"}
_FROM_LEGACY = {old: new for new, old in _LEGACY.items()}


def normalize(status: str | None) -> str:
    """The current name for a status given in either vocabulary ("" for an unknown one)."""
    value = (status or "").strip().lower()
    value = _FROM_LEGACY.get(value, value)
    return value if value in STATUSES else ""


def legacy(status: str) -> str:
    """The name before 0.5.0 (running, needs_approval, succeeded; the others are unchanged)."""
    return _LEGACY.get(status, status)


def task_status(status: str) -> str:
    """The MCP TaskStatus for a job status."""
    return TASK_STATUS.get(status, status)


def parse_filter(raw: str | None) -> set[str]:
    """Comma-separated statuses in either vocabulary -> current names. Unknown ones are dropped."""
    out = set()
    for part in (raw or "").split(","):
        name = normalize(part)
        if name:
            out.add(name)
    return out


def task_view(job: Any, *, poll_interval_ms: int | None = None, status_message: str | None = None) -> dict[str, Any]:
    """The job as an MCP Task object (wire field names). ttl is null: jobs are kept by count (KEEP_JOBS),
    not by time, so there is no fixed retention to promise."""
    out: dict[str, Any] = {
        "taskId": job.id,
        "status": task_status(job.status),
        "statusMessage": status_message if status_message is not None else job.status_message(),
        "createdAt": job.created_at,
        "lastUpdatedAt": job.updated_at or job.created_at,
        "ttl": None,
    }
    if poll_interval_ms and not job.done:
        out["pollInterval"] = int(poll_interval_ms)
    return out


def task_result(payload: dict[str, Any], *, failed: bool) -> dict[str, Any]:
    """What tasks/result would return for an agent_run task: the CallToolResult of the call."""
    import json

    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
        "structuredContent": payload,
        "isError": bool(failed),
    }
