# Changelog

## 0.7.0 (2026-11-22)

MCP progress for long `agent_run` calls: a client that sends a `progressToken` simply waits for the result.

### Added

- `agent_run` with `_meta.progressToken` keeps the call open until the job is `completed`, `failed` or `cancelled` (no `MCP_WAIT_SECONDS`/`MCP_MAX_WAIT_SECONDS` cap) and returns the same payload as `agent_job`. Job watchdogs (`JOB_MAX_RUNTIME_SECONDS`, `JOB_IDLE_TIMEOUT_SECONDS`, `APPROVAL_TIMEOUT_SECONDS`) still apply.
- `notifications/progress` on every change of status or last step (`job <id>: Calling Bash…`) and at least every `MCP_PROGRESS_INTERVAL_SECONDS` (10) as keepalive; `progress` always grows, the last notification has `total == progress` and the final status.
- Approvals during such a call: asked once by elicitation (`elicitation/create`) when the client declared the capability, with keepalive progress while the question is open; withdrawn when the approval is answered another way. Without elicitation (or when dismissed) a progress message says what the job waits for, and `POST /jobs/<id>/approve` / `agent_approve` work as before.
- `notifications/cancelled` for such a call cancels its job (unless `background=true`), as for the capped wait.
- `MCP_WAIT_WITH_PROGRESS` (default `true`): `false` restores the old behaviour for every client.

### Changed

- Progress values during the capped wait now always increase (spec), and a pending elicitation is withdrawn when its approval is answered elsewhere.

### Unchanged

- Without a `progressToken`, `agent_run` answers after `wait_seconds`/`MCP_WAIT_SECONDS` with `job_id` and status, as before.
- MCP Tasks (`tasks/*`) are still not served: mcp 2.3.0 (newest release) has the 2025-11-25 Task types but routes none of the methods; the job↔Task mapping stays.

### Upgrade notes

- Image: `ghcr.io/domdus/agent-worker:0.7.0` (or `:latest`): `docker compose pull && docker compose up -d --no-build` in the compose project dir with the existing `.env`. Recreating the container stops running jobs; do it when none are open.
- Clients that send a `progressToken` (many SDKs do whenever a progress callback is set) now wait until the job ends: they should reset their call timeout on progress. If one cannot, set `MCP_WAIT_WITH_PROGRESS=false`.

## 0.6.0 (2026-11-20)

Engine fallback chain. Also the first published release with the JSON mode of 0.5.1 (0.5.1 was not released on its own).

### Added

- `FALLBACK_ENGINES` (e.g. `claude,cursor,codex:gpt-5.5`): on a provider-side failure (plan/usage limit, 429, expired or missing sign-in, timeout, connection error, 5xx/overloaded) the job goes on with the next engine of the chain under the same `job_id`, with a handoff (original task, the session's conversation, a journal of executed tool calls with results, the workspace state, and the instruction not to repeat side effects). Never on a cancel, watchdog, refused approval, refusal, tool error or invalid input. Engines that are not signed in are skipped. Empty = off, nothing changes.
- An explicit `engine` stays the starting point; the chain goes on below it.
- Switches are steps (`claude am Limit (Reset 18:30) – cursor übernimmt`), also in `/v1` streams as thinking; results carry `engines_used` and `fallback`; `GET /health` → `fallback`.
- No engine left: the job waits as `queued` (`waiting_for: "engine"`), gives its slot back and resumes at the known reset time or when an engine is signed in.
- Promotion back up only at a job's start: `FALLBACK_PROMOTE` (`turn` default, `reset`, `off`), `FALLBACK_BACKOFF` (5m,15m,60m) for failures without a reset time, `FALLBACK_LOCK_SECONDS` (120) against flapping; the probe is the sign-in status, no model prompt. Blocks persist in `/data/fallback-state.json`.
- Engines report structured error codes (`Outcome.error_kind`, `EngineError.kind`) and tool details/output for the in-memory handoff journal (never stored with the events).

### Upgrade notes

- Image: `ghcr.io/domdus/agent-worker:0.6.0` (or `:latest`): `docker compose pull && docker compose up -d --no-build` in the compose project dir with the existing `.env`.
- Nothing changes without `FALLBACK_ENGINES`. To turn the chain on, set e.g. `FALLBACK_ENGINES=claude,cursor,codex` in `.env` (optional: `FALLBACK_PROMOTE`, `FALLBACK_BACKOFF`, `FALLBACK_LOCK_SECONDS`) and recreate the container. Every engine in the chain needs its own sign-in.

## 0.5.1 (2026-11-19)

OpenAI-compatible endpoint: JSON mode for `response_format`.

### Added

- `/v1/chat/completions` honours `response_format` `{"type": "json_schema", ...}` and `{"type": "json_object"}`:
  - the job runs with tool profile `none`, whatever `TOOL_PROFILE` is; `X-Agent-Tools` cannot widen it;
  - the agent is told (system level, with the schema) to answer with only the JSON, no prose and no code fences; with a session id the rule is also added to the task, since a resumed agent does not see the instructions again;
  - the answer is cleaned up: surrounding whitespace and `` ``` ``/`` ```json `` fences are stripped; if it still is not JSON, the first balanced JSON object or array in it is used (`json_object`: objects only);
  - no valid JSON, a failed or cancelled job is an OpenAI error object with HTTP 502 (`code` `invalid_json`, or the job's status), never prose;
  - `stream: true` holds back the answer's text (steps still stream as reasoning) and sends the JSON as one chunk, or an error chunk.
- Requests without `response_format` (or with `{"type": "text"}`) behave as before. The schema is not validated against the answer.

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
