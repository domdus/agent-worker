# Changelog

## 0.5.0 (2026-11-16)

Job visibility, cancel that frees the workspace, watchdogs; job handling aligned with MCP (cancellation, Task statuses, conversation id in `_meta`).

### Added

- MCP cancellation: `notifications/cancelled` for an open `agent_run` call, or a client disconnect the worker can see (2026-07-28 HTTP: a closed request stream), cancels the job it started: engine stopped, process tree killed, workspace freed, status `cancelled` with the reason. `agent_run(background=true)` keeps the job independent of the call. Settings `MCP_CANCEL_ON_CANCELLED` and `MCP_CANCEL_ON_DISCONNECT` (both `true`). Waits in `agent_job` are never bound to the job.
- MCP Task view of a job: `task` (`taskId`, `status`, `statusMessage`, `createdAt`, `lastUpdatedAt`, `ttl`, `pollInterval`) in `agent_run`/`agent_job` and `GET /jobs/<id>`; `status_message` and `updated_at` everywhere. `TASK_POLL_INTERVAL_MS` (5000). `tasks/*` themselves are not served: mcp 2.3.0 has no server-side Tasks (README lists what is missing).
- Conversation id: read from `_meta` (`io.github.domdus/conversation_id`, preferred; also `contextId`, `conversationId`, `conversation_id`), from `agent_run(conversation_id=…)` or `POST /jobs` `conversation_id`. Shown in every job answer; `agent_jobs`/`GET /jobs`/`GET /jobs/overview` filter by it; `agent_cancel(conversation_id=…)` and `POST /conversations/<id>/cancel` cancel by it. Values that look like credentials are dropped.
- README section "Jobs, cancellation and standards".

- `agent_jobs` MCP tool and `GET /jobs/overview`: every queued, working and `input_required` job plus the last 20 that ended (filters: `workspace`, `status`). Each row shows the effective model, times, runtime, last step and last output, what an approval waits for, and which job blocks a queued one. Only a 120-character task preview, never the full prompt.
- `agent_cancel(workspace=...)` and `POST /workspaces/<name>/cancel`: cancel every queued and running job in a workspace.
- Watchdogs: `JOB_MAX_RUNTIME_SECONDS` (3600), `JOB_IDLE_TIMEOUT_SECONDS` (900, no engine output for that long; 0 = off), `APPROVAL_TIMEOUT_SECONDS` (1800: an unanswered approval cancels the job with "approval timed out").
- `MCP_MAX_WAIT_SECONDS` (25): no MCP call waits longer, so the answer with the `job_id` arrives before a typical 30 s client timeout. The `job_id` is also in the log and in every MCP progress message.
- `model` (the effective model: the requested one or the engine's default) in the `agent_run`/`agent_job` answer; `default_model` per engine in `auth_status`/`GET /auth`; `default_models` in `agent_workspaces`/`GET /workspaces`.
- `GET /jobs/<id>` on an open job has `runtime_seconds`, `last_step`, `last_event_at` and `blocked_by`.
- README: what runs in parallel, as the code does it.

### Changed

- **Job statuses use the MCP Task vocabulary:** `running` → `working`, `needs_approval` → `input_required`, `succeeded` → `completed`; `queued`, `failed`, `cancelled` stay (`queued` is `working` as a Task). Backwards compatible: `legacy_status` carries the old name in every answer, every status filter takes both, jobs saved by older workers are read with the new names, and `JOB_STATUS_NAMES=legacy` makes `status` report the old names again.

- Cancel stops the engine (10 s grace), then kills every process the job left behind (tagged `AGENT_WORKER_JOB` for Claude Code and Codex; for Cursor the workspace's bridge and its children), and frees the workspace even when the engine does not react. Queued jobs behind it start.
- A job takes its workspace lock before a concurrency slot: a job that only waits for its workspace no longer keeps jobs in other workspaces from running.
- Defaults: max runtime 1800 → 3600 s, approval timeout 900 → 1800 s. An approval timeout now cancels the job (before: only the one command was refused).
- `agent_job` waits at most `MCP_MAX_WAIT_SECONDS` (was 600); call it again while the job runs.
- Renamed settings (old names still work): `JOB_TIMEOUT_SECONDS` → `JOB_MAX_RUNTIME_SECONDS`, `SSH_APPROVAL_TIMEOUT` → `APPROVAL_TIMEOUT_SECONDS`.
- `docker-compose.yml` pins the image to `ghcr.io/domdus/agent-worker:0.5.0` (was `:latest`).

### Upgrade notes

- Image: `ghcr.io/domdus/agent-worker:0.5.0` (pull and `docker compose up -d --no-build` in your compose project dir with the existing `.env`).
- **Data volume:** the compose project is `agent-worker` with the volume `agent-worker-data` (on the host: `agent-worker_agent-worker-data`). A container still started from the old cursor-worker compose uses `cursor-worker_data`; that volume is **not** reused. The new container starts empty: jobs, sessions, workspaces and stored sign-ins (`/data/claude`, `/data/codex`, `/data/cursor`) are not carried over. Sign in again, and let `GIT_REPO`/`GIT_TOKEN` clone the default workspace again. The old volume stays on the host until you remove it.
- Clients that compare `status` strings: the new names are `working`, `input_required` and `completed`. Read `legacy_status` or set `JOB_STATUS_NAMES=legacy`.
- Defaults changed: max runtime 3600 s, approval timeout 1800 s, and an unanswered approval now cancels the job. No MCP call waits longer than `MCP_MAX_WAIT_SECONDS` (25); poll `agent_job` with the `job_id`.

Earlier versions: see the [GitHub releases](https://github.com/domdus/agent-worker/releases).
