# Changelog

## 0.5.0 (unreleased)

Job visibility, cancel that frees the workspace, watchdogs.

### Added

- `agent_jobs` MCP tool and `GET /jobs/overview`: every queued, running and `needs_approval` job plus the last 20 that ended (filters: `workspace`, `status`). Each row shows the effective model, times, runtime, last step and last output, what an approval waits for, and which job blocks a queued one. Only a 120-character task preview, never the full prompt.
- `agent_cancel(workspace=...)` and `POST /workspaces/<name>/cancel`: cancel every queued and running job in a workspace.
- Watchdogs: `JOB_MAX_RUNTIME_SECONDS` (3600), `JOB_IDLE_TIMEOUT_SECONDS` (900, no engine output for that long; 0 = off), `APPROVAL_TIMEOUT_SECONDS` (1800: an unanswered approval cancels the job with "approval timed out").
- `MCP_MAX_WAIT_SECONDS` (25): no MCP call waits longer, so the answer with the `job_id` arrives before a typical 30 s client timeout. The `job_id` is also in the log and in every MCP progress message.
- `model` (the effective model: the requested one or the engine's default) in the `agent_run`/`agent_job` answer; `default_model` per engine in `auth_status`/`GET /auth`; `default_models` in `agent_workspaces`/`GET /workspaces`.
- `GET /jobs/<id>` on an open job has `runtime_seconds`, `last_step`, `last_event_at` and `blocked_by`.
- README: what runs in parallel, as the code does it.

### Changed

- Cancel stops the engine (10 s grace), then kills every process the job left behind (tagged `AGENT_WORKER_JOB` for Claude Code and Codex; for Cursor the workspace's bridge and its children), and frees the workspace even when the engine does not react. Queued jobs behind it start.
- A job takes its workspace lock before a concurrency slot: a job that only waits for its workspace no longer keeps jobs in other workspaces from running.
- Defaults: max runtime 1800 → 3600 s, approval timeout 900 → 1800 s. An approval timeout now cancels the job (before: only the one command was refused).
- `agent_job` waits at most `MCP_MAX_WAIT_SECONDS` (was 600); call it again while the job runs.
- Renamed settings (old names still work): `JOB_TIMEOUT_SECONDS` → `JOB_MAX_RUNTIME_SECONDS`, `SSH_APPROVAL_TIMEOUT` → `APPROVAL_TIMEOUT_SECONDS`.

Earlier versions: see the [GitHub releases](https://github.com/domdus/agent-worker/releases).
