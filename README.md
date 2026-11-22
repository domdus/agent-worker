# Agent Worker

A headless coding agent in a Docker container: Cursor, Claude Code or OpenAI Codex. You give it a task, it works in a workspace folder (files, shell, git, MCP), and returns the result, the changed files and any commits.

It has three ways in, all on one port and protected by one token:

| Endpoint | For | What it is |
|---|---|---|
| `/mcp` | Hermes, Claude Code, Open WebUI tools, AI Workspace, any MCP client | MCP server (Streamable HTTP) with an `agent_run` tool |
| `/v1` | Open WebUI, any OpenAI client | OpenAI-compatible `chat/completions` and `models`, so Cursor, Claude Code and Codex show up as chat models |
| `/jobs` | Scripts, CI, your own apps | Start a job, check it, stream its events, cancel it |

It runs on the official [Cursor SDK](https://cursor.com/docs) with your own `CURSOR_API_KEY`. Usage is billed to that Cursor account. It can also run **Claude Code** (Anthropic's [Claude Agent SDK](https://pypi.org/project/claude-agent-sdk/)) with your Claude plan or an Anthropic API key: see [Claude Code](#claude-code). And it can run **OpenAI Codex** (the official [Codex SDK](https://pypi.org/project/openai-codex/)) with your ChatGPT plan or an OpenAI API key: see [OpenAI Codex](#openai-codex). Each job picks one engine.

## Installation

You need Docker with Compose, and a Cursor user API key from [cursor.com/dashboard/integrations](https://cursor.com/dashboard/integrations) and/or a Claude credential (see [Claude Code](#claude-code)) and/or a ChatGPT plan or OpenAI API key (see [OpenAI Codex](#openai-codex)). The image `ghcr.io/domdus/agent-worker` is built for amd64 and arm64.

### 1. Make a folder with two files

```bash
mkdir agent-worker && cd agent-worker
```

`.env` (keep it private, it holds both secrets):

```bash
CURSOR_API_KEY=key_...
WORKER_TOKEN=          # fill with: openssl rand -hex 32
CURSOR_MODEL=composer-2.5
```

`docker-compose.yml`:

```yaml
services:
  agent-worker:
    image: ghcr.io/domdus/agent-worker:latest   # or pin a version, e.g. :0.7.2
    container_name: agent-worker
    env_file: .env
    ports:
      - "127.0.0.1:8787:8787"     # this machine only; "8787:8787" opens it to your LAN
    extra_hosts:
      - "host.docker.internal:host-gateway"
    volumes:
      - agent-worker-data:/data  # jobs, sessions, workspaces
      # A project on the host as the default workspace (writable for uid 1000),
      # plus WORKSPACE_DIR=/workspace in .env:
      # - /path/to/project:/workspace
      # SSH to your servers (see "SSH to your servers"):
      # - ./ssh:/run/secrets/ssh:ro
    restart: unless-stopped

volumes:
  agent-worker-data:
```

All other settings go into `.env`. [.env.example](.env.example) lists every one of them with a short explanation.

### 2. Start it and check it

```bash
docker compose up -d
curl http://127.0.0.1:8787/health
curl -s http://127.0.0.1:8787/v1/models -H "Authorization: Bearer $(grep ^WORKER_TOKEN= .env | cut -d= -f2)"
```

`/health` answers without a token. Every other request needs `Authorization: Bearer <WORKER_TOKEN>`. `/v1/models` should list the models of your Cursor account. If it lists only `CURSOR_MODEL`, the worker could not reach Cursor with your API key, and `docker compose logs -f` shows why.

### 3. Connect a client

See [Connect a client](#connect-a-client) below.

### Update

```bash
docker compose pull && docker compose up -d
```

Jobs, sessions and workspaces live in the `agent-worker-data` volume and are kept.

### From the repository instead

The repository's [docker-compose.yml](docker-compose.yml) does the same, can build the image itself, and has an optional Open WebUI already connected (`--profile webui`):

```bash
git clone https://github.com/domdus/agent-worker.git && cd agent-worker
cp .env.example .env    # set CURSOR_API_KEY and WORKER_TOKEN
docker compose up -d                   # or: docker compose --profile webui up -d
```

## Connect a client

### Claude Code

```bash
claude mcp add --transport http agent-worker http://127.0.0.1:8787/mcp \
  --header "Authorization: Bearer $WORKER_TOKEN"
```

### Hermes Agent

Add the worker as an HTTP MCP server in `~/.hermes/config.yaml`:

```yaml
mcp_servers:
  agent-worker:
    url: http://127.0.0.1:8787/mcp
    headers:
      Authorization: "Bearer <WORKER_TOKEN>"
```

Hermes then has `agent_run`, `agent_job`, `agent_jobs`, `agent_cancel` and `agent_workspaces` as tools.

Until 0.2.0 this project was **cursor-worker** with `cursor_*` tools. From 0.3.0 the tools are `agent_run`, `agent_job`, `agent_cancel`, `agent_approve` and `agent_workspaces`, the image is `ghcr.io/domdus/agent-worker`, and the headers are `X-Agent-Workspace`, `X-Agent-Tools` and `X-Agent-Session`. Update existing client configs.

### Open WebUI

There are two ways to connect it, and you can use both:

- **As a model:** Admin Settings → Connections → OpenAI API. URL `http://agent-worker:8787/v1` (or `http://host.docker.internal:8787/v1`), API key = `WORKER_TOKEN`. Cursor's models and the `claude-code/…` and `codex/…` models show up in the model picker. The agent's steps appear as "thinking", and the stop button cancels the job.
- **As a tool for other models:** Admin Settings → External Tools → add an MCP server (Streamable HTTP) at `.../mcp` with Bearer auth.

`docker compose --profile webui up -d` starts Open WebUI already connected to the worker.

Set `ENABLE_FORWARD_USER_INFO_HEADERS=true` on Open WebUI. It then sends the chat ID, so each chat keeps one Cursor agent. Without the chat ID, every message starts a new agent and gets the earlier turns pasted in. Also give Open WebUI a cheap **task model** (for titles and tags). Otherwise those small background requests also run on a Cursor agent.

### Other OpenAI clients (LibreChat, Hermes as a provider, scripts)

Any client that speaks OpenAI Chat Completions works with the same settings: base URL `http://agent-worker:8787/v1`, API key = `WORKER_TOKEN`, model as `/v1/models` lists it (`composer-2.5`, `claude-code/<model>`, `codex/<model>`, optionally `@workspace`). `cursor/<model>` picks Cursor explicitly, even when `DEFAULT_ENGINE` is another engine.

- **Streaming:** with `"stream": true` the answer arrives live as `delta.content`. Claude Code streams token by token; Cursor and Codex send their text as it comes. A keepalive comment is sent while the agent works quietly, `stream_options.include_usage` adds a usage chunk at the end, and a failed job ends with an OpenAI `error` chunk before `[DONE]`.
- **Agent steps** (tool calls, status) go to `delta.reasoning_content` by default, which Open WebUI and Hermes show as thinking. `OPENAI_STEPS=reasoning` uses the `reasoning` field instead, `none` sends only the answer. The header `X-Agent-Steps` sets it per request.
- **One agent per chat:** the worker takes the chat's id from `X-OpenWebUI-Chat-Id`, `X-Session-Id`, `X-Agent-Session`, `X-Conversation-Id` or `X-Chat-Id`, or from `session_id`, `conversation_id` or `chat_id` in the body (also inside `metadata`). Placeholders such as `new` or an unfilled `{{...}}` template are ignored.
- **Stop:** when the client closes the request (stop button, timeout), the job is cancelled, with and without streaming.
- **Errors** come as OpenAI error objects (`{"error": {"message", "type", "code"}}`), so clients show the reason, for example a wrong API key.
- **JSON mode:** with `response_format` `{"type": "json_schema", "json_schema": {"schema": …}}` or `{"type": "json_object"}` the job runs with tool profile `none` (`X-Agent-Tools` cannot widen it), the agent is told to answer with only JSON matching the schema, and `message.content` is just that JSON: code fences are stripped, and otherwise the first JSON object or array in the answer is taken. No JSON is an error (HTTP 502, `code: "invalid_json"`), never prose. With `"stream": true` the JSON is sent as one chunk at the end. The answer is not validated against the schema.

### Home Assistant AI Workspace

Add an MCP connection with URL `http://<worker-host>:8787/mcp` and the token as Bearer. Any agent (OpenAI, Claude, Ollama) can then hand work to Cursor with `agent_run`. What the client should send (conversation id, cancel) is in [Jobs, cancellation and standards](#jobs-cancellation-and-standards).

## Claude Code

Jobs can run on Claude Code instead of Cursor. The worker image includes Claude Code. It runs in the job's workspace with the same tool profiles, workspaces, sessions, MCP servers, SSH guard and approvals as Cursor. It loads no Claude Code settings, `CLAUDE.md`, skills or hooks from disk, and subagents are off.

### Credentials

One of these, the first one set wins:

| Where | What |
|---|---|
| `CLAUDE_CODE_OAUTH_TOKEN` | A Claude Plan token (`sk-ant-oat…`) from `claude setup-token`. Uses your Claude subscription (Pro, Max). Lasts one year. |
| `ANTHROPIC_API_KEY` | An Anthropic API key from the [Claude Console](https://console.anthropic.com/settings/keys), billed as API credits. |
| Sign in with Claude | The worker gets a Plan token itself and keeps it in the data volume. No Claude Code on your computer, no restart. |

Only the chosen credential reaches Claude Code, so a plan job never falls back to API billing or the other way round.

### Sign in with Claude

```bash
docker exec -it agent-worker python -m agent_worker.claude_login
```

It prints a sign-in link. Open it on any device, sign in with the Claude account that has your plan, and paste the code Anthropic shows. The Plan token is saved in `/data/claude/oauth-token` (owner-only) and used from the next job on.

The same over HTTP, for scripts or a UI:

```bash
curl -s -X POST localhost:8787/auth/claude/login -H "$H"                  # -> {"id": ..., "url": ...}
curl -s -X POST localhost:8787/auth/claude/login/<id>/code -H "$H" \
  -H 'Content-Type: application/json' -d '{"code": "<code from Anthropic>"}'   # saves the token
curl -s -X DELETE localhost:8787/auth/claude/token -H "$H"                # sign out again
```

The token is never returned or logged. The sign-in runs Anthropic's own `claude setup-token` in a temporary folder that is deleted afterwards. A code works once: if it is refused or older than 15 minutes, start again. The full flow for a UI is in [Sign in from a UI](#sign-in-from-a-ui). `GET /health` shows which credential is in use (`claude.credential`: `env-plan`, `env-api`, `signed-in` or `none`).

Anthropic's terms for the Agent SDK say third-party products should use API keys and not offer Claude.ai sign-in ([Agent SDK overview](https://code.claude.com/docs/en/agent-sdk/overview), [Legal and compliance](https://code.claude.com/docs/en/legal-and-compliance)). Whether your own Plan token on your own worker is fine for you is your decision.

### Choosing the engine

- **Jobs:** `"engine": "claude"`, `"codex"` (or `"cursor"`) in `POST /jobs`.
- **MCP:** the `engine` argument of `agent_run`.
- **`/v1`:** Claude models are listed as `claude-code/claude-sonnet-5-5` and so on, and `claude-code/<model>@workspace` works like the Cursor names. Cursor's own Claude models (for example `claude-4.5-sonnet`) still run on Cursor. Codex models are listed as `codex/<model>` (`codex` alone means Codex's default model); `codex/<model>@workspace` works too.
- Without one, `DEFAULT_ENGINE` (`cursor`, `claude` or `codex`) is used, else Cursor, else the first other engine that is set up. With `FALLBACK_ENGINES`, a job without engine and model starts at the top of the chain (see [Engine fallback chain](#engine-fallback-chain)).

`CLAUDE_MODEL` (default `claude-sonnet-5-5`) is the model when a Claude job names none. The tool profiles mean the same as for Cursor: `full` has files, the shell and the web; `local` has no web search or fetch; `offline` has no shell either; `mcp` has only MCP tools; `none` is chat only. Effort `fast`, `high` and `high_fast` become Claude Code's low, high and medium effort. A `session` resumes the Claude Code conversation; its state is kept in `/data/claude/config`.

Claude Code makes the image about 500 MB larger.

## OpenAI Codex

Jobs can also run on OpenAI Codex. The image includes the official Python [Codex SDK](https://pypi.org/project/openai-codex/) (`openai-codex`) and the Codex CLI pinned with it, so no Node.js is needed. Each job starts its own `codex app-server` in the job's workspace and talks to it over JSON-RPC, with the same tool profiles, workspaces, sessions, MCP servers, SSH guard and approvals as the other engines. It loads no `AGENTS.md`, skills, hooks, plugins, apps or memories.

### Credentials

The first one found wins:

| Where | What |
|---|---|
| `/data/codex/auth.json` | "Sign in with ChatGPT": uses your ChatGPT plan (Plus, Pro, Business, …). Written by the sign-in below, or copied from `CODEX_AUTH_JSON`. An API key saved by `codex login --with-api-key` also lives here. |
| `OPENAI_API_KEY` (or `CODEX_API_KEY`) | An OpenAI API key, billed as API usage. Handed to Codex in memory for each job and never written to disk. |

`CODEX_HOME` is `/data/codex`, in the data volume, so the sign-in survives restarts and image updates. Codex refreshes the ChatGPT tokens in `auth.json` by itself while it works, so the file must stay writable. Treat it like a password. The worker blanks its own secrets (`OPENAI_API_KEY`, `CURSOR_API_KEY`, `ANTHROPIC_API_KEY`, `WORKER_TOKEN`, …) in Codex's environment, so the agent's shell can't read them.

### Sign in with ChatGPT

Device-code sign-in has to be switched on once: enable device code login in ChatGPT's **Settings → Security** (for a workspace account, the admin allows it in the workspace permissions). Then:

```bash
docker exec -it agent-worker python -m agent_worker.codex_login          # runs `codex login --device-auth`
docker exec -it agent-worker python -m agent_worker.codex_login status   # who is signed in
docker exec -it agent-worker python -m agent_worker.codex_login logout   # removes auth.json
```

It prints a link (`https://auth.openai.com/codex/device`) and a one-time code. Open the link on any device, sign in with the ChatGPT account that has your plan, and enter the code. `auth.json` goes to `/data/codex/` and is used from the next job on. No restart is needed.

The same over HTTP, for scripts or a UI:

```bash
curl -s -X POST localhost:8787/auth/codex/login -H "$H"            # -> {"id", "url", "code", "status": "pending"}
curl -s "localhost:8787/auth/codex/login/<id>?wait=60" -H "$H"     # -> "succeeded" once the code was entered
curl -s -X DELETE localhost:8787/auth/codex/login/<id> -H "$H"     # cancel
curl -s -X DELETE localhost:8787/auth/codex/token -H "$H"          # sign out (removes auth.json)
```

**Already signed in on another computer?** Run `codex login` there and hand the worker a copy of `~/.codex/auth.json`: mount it read-only and set `CODEX_AUTH_JSON=/run/secrets/codex_auth.json`. You can also put the JSON itself in the variable. The worker copies it into `/data/codex/auth.json` once, when there is none yet, and from then on Codex refreshes that copy. Don't keep using the same `auth.json` on two machines: a refresh on one can sign the other out. To swap the account later, `codex_login logout` first.

`GET /health` shows `codex.credential` (`chatgpt`, `api-key`, `env-api` or `none`), the ChatGPT `plan`, `installed`, `ready`, `model` and `sandbox`.

### Models, tools and sandbox

- `CODEX_MODEL` is the model when a Codex job names none. If it's empty, Codex uses its own default for the account, and the job shows which model ran. A job can still name any model (`"model": "gpt-5.5"`).
- Effort `fast`, `high` and `high_fast` become Codex's `low`, `high` and `medium` reasoning effort. Codex's own names (`minimal`, `xhigh`, …) pass through as they are.
- A `session` continues the Codex thread across jobs and restarts. Threads are stored in `/data/codex/sessions`. A job without a session leaves no thread behind.
- MCP servers from `/data/mcp.json` and from the request are passed on: stdio and Streamable HTTP, with headers. Codex can't use SSE-only servers. The job skips them and says so in a status event.
- Codex's own Linux sandbox (bubblewrap) needs user namespaces, which a normal Docker container doesn't allow. So by default (`CODEX_SANDBOX=container`) the container is the sandbox, as it is for Cursor and Claude Code, and Codex runs with full access inside it. Nothing asks for approval: whatever would ask is refused.

| Profile | `CODEX_SANDBOX=container` (default) | `CODEX_SANDBOX=codex` |
|---|---|---|
| `full` | shell, file edits, live web search, MCP | the same, but commands may write only in the workspace (network on) |
| `local` | shell, file edits, MCP; no web search | like `full` without web search |
| `offline` | file edits (`apply_patch`) and MCP; no shell, no web | shell in the sandbox without network, file edits, MCP |
| `mcp` | MCP tools only (read-only, no shell, no file tools) | the same |
| `none` | chat only, no MCP servers | the same |

Only use `CODEX_SANDBOX=codex` if the container may create user namespaces (for example `security_opt: [seccomp=unconfined, apparmor=unconfined]`). Otherwise every command fails with a bwrap error. In that mode Codex also keeps `.git` read-only, so the agent can't commit.

`usage` has `prompt_tokens`, `completion_tokens`, `cache_read_tokens`, `cache_write_tokens`, `reasoning_tokens` and `total_tokens` for that job alone, even in a resumed thread. Codex doesn't report a price, so `cost_usd` and `charged_cents` are `null`: plan usage isn't billed per token, and API prices depend on the model.

`CODEX_CONFIG` takes extra Codex settings, one `key=value` per line (TOML values, as with `codex -c`), for example a custom model provider. The Codex runtime makes the image about 450 MB larger.

## Sign in from a UI

A UI (for example AI Workspace) can offer a "Sign in" button for every engine without a terminal, `.env` edits or restarts. Every endpoint below needs `Authorization: Bearer <WORKER_TOKEN>`. Credentials are saved owner-only in the data volume (`/data/claude/oauth-token`, `/data/codex/auth.json`, `/data/cursor/api-key`), so they survive restarts. Jobs use them from the next job on. No response ever contains a token or key.

Environment variables still win: `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY` beat a Claude sign-in, and `CURSOR_API_KEY` beats a stored Cursor key. For Codex, a ChatGPT sign-in (`auth.json`) beats `OPENAI_API_KEY`. When a saved credential is hidden by the environment, the response says so in `note`, and `GET /auth` shows `env_override: true`.

### One call for the status: `GET /auth`

```json
{
  "default_engine": "cursor",
  "configured": ["cursor", "claude", "codex"],
  "engines": {
    "cursor": {"ready": true, "installed": true, "credential": "stored", "kind": "api-key", "plan": null,
               "stored": true, "env_override": false, "login": null, "method": "api_key"},
    "claude": {"ready": false, "installed": true, "credential": "none", "kind": null, "plan": null,
               "stored": false, "env_override": false, "method": "oauth_code",
               "login": {"id": "Qm9…", "status": "awaiting_code", "url": "https://claude.com/cai/oauth/authorize?…", "expires_in": 812}},
    "codex":  {"ready": true, "installed": true, "credential": "chatgpt", "kind": "subscription", "plan": "plus",
               "stored": true, "env_override": false, "login": null, "method": "device_code"}
  }
}
```

| Field | Meaning |
|---|---|
| `ready` | Jobs can run on this engine now. |
| `credential` | Where the credential comes from. Cursor: `env`, `stored`, `none`. Claude: `env-plan`, `env-api`, `signed-in`, `none`. Codex: `chatgpt`, `api-key` (in auth.json), `env-api`, `none`. |
| `kind` | `subscription` (Claude/ChatGPT plan), `api-key`, or `null`. |
| `plan` | The ChatGPT plan for Codex (`plus`, `pro`, `team`, …). A Claude Plan token does not say which plan it belongs to, so Claude always shows `null`. |
| `login` | A sign-in that is still waiting for the user (with its link and, for Codex, the code), else `null`. A UI can reload and pick it up again. |
| `method` | Which button to show: `api_key` (a text field), `oauth_code` (link + paste a code), `device_code` (link + show a code). |

`GET /health` (no token) shows the credential source too: `cursor_credential`, `claude.credential`, `codex.credential` and `codex.plan`. It never shows sign-in links.

### Claude: link, then paste the code

Anthropic's sign-in for Claude Code (`claude setup-token`) cannot redirect back to the worker. After sign-in, Anthropic's page shows a code, and the user pastes it into the UI.

1. `POST /auth/claude/login` starts the sign-in. It takes a few seconds and answers:
   ```json
   {"id": "Qm9…", "status": "awaiting_code", "url": "https://claude.com/cai/oauth/authorize?code=true&…", "expires_in": 900,
    "next": "Open the url, sign in, then POST {\"code\": ...} (the code Anthropic shows) to /auth/claude/login/Qm9…/code."}
   ```
   Open `url` in a new tab. Anthropic shows a code after sign-in.
2. `POST /auth/claude/login/{id}/code` with `{"code": "<code from Anthropic>"}` (takes up to a minute):
   ```json
   {"ok": true, "id": "Qm9…", "status": "succeeded", "credential": "signed-in", "note": "Saved. Claude jobs use it now."}
   ```
   Errors (`detail`): `400 {"error": "bad_code", "attempts_left": 4}` means it doesn't look like a code, and the sign-in stays open. `400 code_rejected` means Anthropic refused it, and the sign-in has ended. `429 too_many_attempts` comes after 5 malformed codes. `409 busy` means a code is already being checked. `410 expired` means unknown, finished, replaced or older than 15 minutes. `504 timeout`.
3. Optional: `GET /auth/claude/login/{id}` returns the state: `awaiting_code`, `verifying`, `succeeded` (with `credential`), `failed` (with `error`), `cancelled` or `expired`. It returns `404` for an unknown id.
4. `DELETE /auth/claude/login/{id}` cancels (204). `DELETE /auth/claude/token` signs out: `{"removed": true, "credential": "none"}`.

`POST /auth/claude/login/{id}` (without `/code`) still works for older clients.

### Codex: link and a code to enter

1. `POST /auth/codex/login`:
   ```json
   {"id": "x7…", "status": "pending", "url": "https://auth.openai.com/codex/device", "code": "ABCD-1234", "expires_in": 900, "next": "…"}
   ```
   Show the code and open `url`. The user signs in to ChatGPT and enters the code. Device-code sign-in has to be enabled once in ChatGPT's Settings → Security.
2. Poll `GET /auth/codex/login/{id}?wait=60` (long poll, up to 120 s) until `status` is no longer `pending`:
   ```json
   {"id": "x7…", "status": "succeeded", "url": "…", "code": "ABCD-1234", "credential": "chatgpt"}
   ```
   Other outcomes: `failed` (with `error`), `cancelled`, `expired`. An unknown id gives `404`.
3. `DELETE /auth/codex/login/{id}` cancels (204). `DELETE /auth/codex/token` signs out: `{"removed": true, "credential": "none"}`.

### Cursor: an API key

Cursor has no sign-in flow for headless use. The user creates a user API key at [cursor.com/dashboard/integrations](https://cursor.com/dashboard/integrations) and pastes it into the UI.

1. `PUT /auth/cursor/key` with `{"api_key": "key_…", "verify": true}`. With `verify` (the default), the worker first asks Cursor for the key's model list:
   ```json
   {"ok": true, "credential": "stored", "verified": true, "note": "Saved. Cursor jobs use it now."}
   ```
   Errors (`detail`): `400 bad_key` (not shaped like a key), `400 key_rejected` (Cursor refused it), `502 unreachable` (Cursor could not be asked; send `"verify": false` to store it anyway). A refused key is not stored.
2. `DELETE /auth/cursor/key` gives `{"removed": true, "credential": "none"}` (or `"env"` if `CURSOR_API_KEY` is set).

### Limits

- One sign-in per engine at a time. Starting a new one cancels the old one.
- A sign-in nobody finishes is stopped after 15 minutes. For Claude, the `claude setup-token` process is killed and its temporary folder deleted.
- `429 {"error": "rate_limited", "retry_after": N}` (plus a `Retry-After` header): at most 6 sign-in starts per engine and 10 Cursor key updates per 10 minutes.

### MCP

The MCP endpoint has one read-only tool, `auth_status`, with the same overview as `GET /auth`. It leaves out sign-in links and codes, so a model that reads it cannot finish a sign-in for someone else. Nothing on MCP can set, change or remove a credential.

## Model and workspace from the chat side

- `model` picks the model, for example `composer-2.5` or `gpt-5` for Cursor, `claude-opus-5-5` for Claude Code, or `gpt-5.5` for Codex. If it is empty, `CURSOR_MODEL`, `CLAUDE_MODEL` or `CODEX_MODEL` is used.
- On `/v1`, `model@workspace` picks the workspace too, for example `composer-2.5@my-repo`. You can also send the header `X-Agent-Workspace`.
- `X-Agent-Tools: offline` (on `/v1`) or `tools: "offline"` (MCP and jobs) narrows what the agent may use for that request.

## The MCP tool

`agent_run(task, workspace?, session?, model?, tools?, mcp_servers?, wait_seconds?, engine?, background?, conversation_id?)`

- Waits up to `wait_seconds` (default `MCP_WAIT_SECONDS`, 20) for the agent to finish, then returns `result`, `changes` (`files` from `git status`, new `commits`), `usage` and `model`.
- **With `_meta.progressToken`** (MCP progress) the call does not return early: it stays open until the job is `completed`, `failed` or `cancelled` and sends `notifications/progress` meanwhile; see [MCP progress](#mcp-progress-_metaprogresstoken). `wait_seconds` and `MCP_MAX_WAIT_SECONDS` do not apply then.
- If the task takes longer (and the call has no `progressToken`), it returns `status: "working"` (or `"queued"`) and a `job_id`. The caller then uses `agent_job(job_id, wait_seconds)` to wait for it, or `agent_cancel(job_id)` to stop it.
- **No wait is longer than `MCP_MAX_WAIT_SECONDS` (25)**, whatever `wait_seconds` asks for. This applies to `agent_run`, `agent_job`, `agent_approve` and `agent_cancel`. Many MCP clients give up on a tool call after 30 s; if the call timed out, the client would never get the `job_id`. Raise the cap only if your client waits longer. The `job_id` is fixed before the wait starts: it is in the worker log (`agent_run job_id=...`), in every MCP progress message (`job <id>: ...`) and in `agent_jobs`.
- `model` in the answer is the model the job runs on: the one you asked for, else the engine's default (`CURSOR_MODEL`, `CLAUDE_MODEL`, `CODEX_MODEL`). It is `null` for Codex without `CODEX_MODEL` until the job has run; then it shows the model Codex picked.
- `status: "input_required"` (before 0.5.0: `needs_approval`): a command on an approval host is waiting for the user; `status_message` says which (see [Approval for selected hosts](#approval-for-selected-hosts)).
- Statuses, `legacy_status`, the `task` object, `background`, `conversation_id` and what cancelling the call does: see [Jobs, cancellation and standards](#jobs-cancellation-and-standards).
- `session`: calls with the same session reuse the same agent, so follow-ups remember earlier turns.
- `mcp_servers`: extra HTTP/SSE MCP servers for this task, in `{"name": {"url": ..., "headers": {...}}}` format.

`agent_jobs(workspace?, status?, finished?, conversation_id?)` (read-only) lists jobs: by default every `queued`, `working` and `input_required` job plus the last 20 that ended. Use it to find a `job_id` you never got, or to see why a workspace is busy. `status` filters by status (comma-separated, for example `working,queued`; the old names `running`, `needs_approval`, `succeeded` work too). `conversation_id` shows one client conversation's jobs. Each row has:

- `job_id`, `status`, `legacy_status`, `status_message`, `workspace`, `engine`, `model` (effective, see above), `session`, `tools`, `origin`, `conversation_id`, `background`
- `task_preview`: the first 120 characters of the task, never the full prompt; no secrets
- `created_at`, `updated_at` (last status change), `started_at`, `finished_at`, `runtime_seconds`
- `last_step` (latest tool call or status) and `last_event_at` (latest output of any kind)
- `waiting_for_approval` and `approval` (tool, host, user, command, `requested_at`)
- for a queued job: `waiting_for` (`workspace`, `session` or `slot`) and `blocked_by` (the job holding that workspace or session)
- `error` (short) for a job that ended with one

`agent_cancel(job_id)` stops one job; `agent_cancel(workspace="my-repo")` stops every queued and running job in that workspace; `agent_cancel(conversation_id="…")` every one of that conversation. A cancel stops the engine (the engine gets 10 s to stop on its own), then kills the processes the job started (shells, ssh, builds; see [Watchdogs](#watchdogs-and-cancel)). The workspace is free right after, and the next queued job starts.

**Annotations and results (0.7.2).** Every tool has a `title` and MCP tool annotations (spec 2025-11-25): `agent_jobs`, `agent_job`, `agent_workspaces`, `auth_status` are `readOnlyHint`; `agent_run`, `agent_cancel` (and `agent_approve`) are `destructiveHint`; `agent_run` is `openWorldHint` (shell, ssh, web) and not idempotent, `agent_cancel` is idempotent. `agent_run`, `agent_job` and `agent_approve` publish a real `outputSchema` (`status` required; `job_id`, `status_message`, `task`, `approval`, `result`, `error`, `changes`, `usage`, `engines_used`, `fallback`, …; unknown fields allowed for later versions) and their `structuredContent` matches it. A job that `failed` or was `cancelled`, a refused request (`status: "rejected"`) and an unknown `job_id` (`status: "unknown"`) come back with `isError: true`, `structuredContent` kept. `agent_cancel` that cancelled something is not an error.

`agent_workspaces()` also returns `default_models` (each engine's model for jobs that name none) and a short note on parallelism. `auth_status()` (read-only) shows which engines are signed in and their `default_model`. See [Sign in from a UI](#sign-in-from-a-ui).

## Parallel jobs

What runs at the same time, as the code does it (`agent_worker/jobs.py`):

- **Different workspaces run in parallel**, up to `MAX_CONCURRENT_JOBS` (default 2) jobs at once across the whole worker. More jobs wait for a free slot (`waiting_for: "slot"`).
- **In one workspace, jobs that may edit files (`tools` `full`, `local`, `offline`) run one after another,** in the order they came in. Later ones are `queued` with `waiting_for: "workspace"` and `blocked_by: <job_id>`, and their events say "Waiting for another job in this workspace (<job_id>)...".
- **Chat-only jobs (`tools` `mcp` or `none`) do not take the workspace lock.** They run next to a file job in the same workspace.
- **Jobs with the same `session` always run one after another,** whatever their tools.
- A job takes its workspace (and session) lock before a slot. A job that only waits for its workspace never takes a slot from a job in another workspace. (Before 0.5.0 it did: two jobs in one workspace could block a third workspace with the default of 2 slots.)
- Cursor runs one bridge process per workspace and Claude Code and Codex one process per job, but this does not change the rules above.

## Engine fallback chain

Off by default. With `FALLBACK_ENGINES` set, a job that fails on the provider side goes on with the next engine of the chain, as the same job (same `job_id`); it does not fail.

```env
FALLBACK_ENGINES=claude,cursor,codex:gpt-5.5   # order of preference; engine:model picks a model for that engine
FALLBACK_PROMOTE=turn                          # turn (default) | reset | off
FALLBACK_BACKOFF=5m,15m,60m                    # optional: steps for failures without a known reset time
FALLBACK_LOCK_SECONDS=120                      # optional: anti-flapping lock
```

**Where a job starts.** A job that names no engine and no model starts at the top of the chain. An explicit `engine` (`agent_run`, `POST /jobs`, a `/v1` model like `codex/gpt-5.5`) stays the starting point; the chain goes on *below* it (all of the chain when that engine is not part of it). A model without an engine (`/v1` `composer-2.5`) starts on `DEFAULT_ENGINE` as before. Engines further down run with the model from `engine:model`, else their own default (`CURSOR_MODEL`, `CLAUDE_MODEL`, `CODEX_MODEL`).

**When it switches.** Only on provider-side failures: a plan, usage or token limit (with its reset time when the provider names one), 429, an expired, invalid or missing credential, timeouts, connection errors, 5xx and "overloaded". Never on a cancel, a watchdog stop, a refused approval, a refusal or other answer of the model, tool errors or invalid input (bad request, unknown model, context too long): those end the job as before. One classifier decides (`agent_worker/fallback.py`, `classify`); it reads the engines' structured error codes first (Claude Code: `rate_limit`, `billing_error`, `authentication_failed`, …; Codex: `usageLimitExceeded`, `unauthorized`, …), then the error text. Engines that are not signed in (`auth_status` `ready: false`) are skipped.

**Handoff.** Sessions do not carry over between engines. The engine that takes over gets the original task, the conversation of the session so far, a journal of the tool calls and commands already executed (with their results, as far as the engine reported them), the workspace's uncommitted files and new commits, and the partial answer, with the explicit instruction not to repeat side-effecting steps (file writes, commits, pushes, ssh commands, deployments) but to check the state and go on. The journal is kept in memory only; tool output never goes into the stored events.

**Visibility.** Each switch is a step, so `status_message` / `last_step` show it, e.g. `claude at its usage limit (reset 18:30) – cursor takes over` (in the user's language over MCP progress and `/v1`, see [Languages](#languages)), and `/v1` streams show it as a thinking line (`reasoning_content`). The result has `engines_used` (e.g. `["claude", "cursor"]`) and `fallback` (every try: engine, model, status `handed_off` / `skipped` / `completed` / `failed`, reason); `/v1` answers have `agent_worker.engines_used`. A last step `Engines: claude → cursor` is added when more than one ran. `GET /health` → `fallback` shows the chain and which engines are blocked until when.

**No engine left.** The job goes to `queued` with `waiting_for: "engine"` and a message like `No engine available (claude at its usage limit (reset 18:30); cursor not signed in) – the job waits and goes on automatically at 18:30`. It gives its `MAX_CONCURRENT_JOBS` slot back (it keeps its workspace lock), looks again at the known reset time or every 60 s (an engine may get signed in), and then goes on from the top of the chain. Waiting does not count towards `JOB_MAX_RUNTIME_SECONDS`. A cancel ends it as usual.

**Back up to the preferred engine (promotion).** Only at the start of a job (a turn), never in the middle of a run, and always from the top of the chain. A blocked engine is not tried before its known reset time; failures without one (connection errors, 5xx, 429 without a time, auth) block it for the next `FALLBACK_BACKOFF` step (every failure in a row takes the next step; a success clears it). The probe is cheap: the engine's sign-in status, no model prompt. A new sign-in (`/auth/...`) lifts an auth block at once. `FALLBACK_LOCK_SECONDS` is the shortest block, and a session that just switched stays put that long (no flapping). An engine that comes back for a session gets the session's conversation, since its own session there is stale.

| `FALLBACK_PROMOTE` | A session that fell back … |
| --- | --- |
| `turn` (default) | goes back up at its next turn once the preferred engine's block is over. |
| `reset` | goes back up only after a known reset time; without one, after the longest backoff step. |
| `off` | stays on the engine that took over (new sessions and jobs without a session still start at the top). |

Blocks are kept in `/data/fallback-state.json`, so a known reset time survives a restart. A job that was waiting for an engine when the worker restarted ends as `failed` ("The worker restarted …"), like every running job.

## Watchdogs and cancel

No job blocks a workspace forever. Every running job is watched:

| Setting | Default | What happens |
| --- | --- | --- |
| `JOB_MAX_RUNTIME_SECONDS` | 3600 (60 min) | The job ran longer: it fails with "The job ran longer than JOB_MAX_RUNTIME_SECONDS (…s) and was stopped." Time in `input_required` counts. Before 0.5.0 this was `JOB_TIMEOUT_SECONDS` (1800); that name still works. |
| `JOB_IDLE_TIMEOUT_SECONDS` | 900 (15 min) | No output or progress from the engine (no text, tool call or status) for that long: the job fails with a message naming the setting. Time in `input_required` does not count. `0` turns it off. A single silent command (a long build) counts as idle, so raise it if you run those. |
| `APPROVAL_TIMEOUT_SECONDS` | 1800 (30 min) | A job waits in `input_required` that long without an answer: the job is **cancelled** with the error "approval timed out: …". Before 0.5.0 this was `SSH_APPROVAL_TIMEOUT` (900), and a timeout only refused the one command; the old name still works. |

When a job is stopped (cancel, or one of the limits), the worker:

1. asks the engine to stop (Cursor: cancel the run; Claude Code: close the CLI; Codex: interrupt the turn) and gives it 10 s;
2. kills every process the job left running, with SIGTERM and then SIGKILL. Claude Code and Codex processes carry `AGENT_WORKER_JOB=<job_id>` in their environment, and so does everything the agent starts from them. For Cursor, whose bridge serves the whole workspace, the bridge and everything under it are killed, unless another Cursor job is still running in that workspace. The next Cursor job starts a fresh bridge; sessions resume by agent id;
3. ends the job (`cancelled` or `failed`, with the reason in `error`) and frees the workspace, so the next queued job starts.

This needs Linux `/proc`, which the container has.

## Jobs, cancellation and standards

Job handling follows MCP where MCP has a standard for it. The tools `agent_run`, `agent_job`, `agent_jobs` and `agent_cancel` stay and work with every client; the standard parts come on top.

### Statuses (MCP Task vocabulary)

| `status` | MCP Task status | Before 0.5.0 (`legacy_status`) | Meaning |
| --- | --- | --- | --- |
| `queued` | `working` | `queued` | Waits for its workspace, its session or a free slot, or (engine fallback) for an engine to become available. `status_message` says which, and which job blocks it. |
| `working` | `working` | `running` | The engine runs. `status_message` is the last step. |
| `input_required` | `input_required` | `needs_approval` | Paused for a human: `status_message` names what is awaited, e.g. "waiting for a human to approve ssh on root@prod: uptime". |
| `completed` | `completed` | `succeeded` | Ended with an answer. |
| `failed` | `failed` | `failed` | Ended with an error, or a watchdog stopped it. |
| `cancelled` | `cancelled` | `cancelled` | Stopped by a cancel, a cancelled MCP call, a disconnect or an approval timeout; the reason is in `error`/`status_message`. |

`queued` is the one extra state; MCP Tasks has none, so as a Task it is `working` with a status message. Backwards compatibility:

- Every answer (`agent_run`, `agent_job`, `agent_jobs`, `agent_cancel`, `GET /jobs…`) also has `legacy_status` with the old name.
- Every status filter (`agent_jobs status=…`, `GET /jobs?status=…`, `GET /jobs/overview?status=…`) takes old and new names.
- Jobs saved by an older worker are read with the new names.
- `JOB_STATUS_NAMES=legacy` makes `status` itself report the old names again, for a client that compares strings and is not updated yet. Default: `mcp`.

The `/v1` chat API is not affected; it never exposed job statuses except in `agent_worker.status` metadata (which follows `JOB_STATUS_NAMES`).

### MCP progress (`_meta.progressToken`)

The MCP way to run a long tool call: the client sends a `progressToken` and simply waits for the result, resetting its timeout on every progress notification (most SDKs: `resetTimeoutOnProgress` or similar).

- `agent_run` with `params._meta.progressToken` keeps the call open **until the job ends** (`completed`, `failed`, `cancelled`) and then returns the same payload as `agent_job` (`result`, `changes`, `usage`, `task`, …). `wait_seconds`, `MCP_WAIT_SECONDS` and `MCP_MAX_WAIT_SECONDS` do not apply; the job's watchdogs (`JOB_MAX_RUNTIME_SECONDS`, `JOB_IDLE_TIMEOUT_SECONDS`, `APPROVAL_TIMEOUT_SECONDS`) still end it.
- `notifications/progress` goes out on the first step, whenever the status or the last step changes (for example `job <id>: Calling Bash…`, `job <id>: queued: waiting for another job in this workspace`) and at least every `MCP_PROGRESS_INTERVAL_SECONDS` (10) as keepalive. `message` always starts with `job <id>:`, so a client that gives up still knows the `job_id`. `progress` grows with every notification (roughly the job's event count); `total` is only set on the last one (`progress == total`, message `completed`/`failed`/`cancelled`), because an agent has no known number of steps.
- **Approvals** (`input_required`): if the client declared the `elicitation` capability, the worker asks once per approval with `elicitation/create` (the command, the host, an `approve` boolean and an optional note) and goes on with the answer; while the question is open it keeps sending progress. If the approval is answered another way meanwhile (`POST /jobs/<id>/approve`, `agent_approve`), the question is withdrawn. Without elicitation, or when the user dismisses it, a progress message says what the job waits for (`input_required: waiting for a human to approve ssh on root@prod: reboot …`) and the call keeps waiting; the other approval paths work as before. On 2026-07-28 HTTP (no server-to-client requests) the worker returns an `InputRequiredResult` and the client retries the call with the answer, which again waits with progress.
- Without a `progressToken` nothing changes: `agent_run` answers after `wait_seconds`/`MCP_WAIT_SECONDS` with the `job_id` and status, for clients that poll with `agent_job`.
- `MCP_WAIT_WITH_PROGRESS=false` turns this off: every client gets the old behaviour (progress messages during the capped wait only).

| Setting | Default | |
| --- | --- | --- |
| `MCP_WAIT_WITH_PROGRESS` | `true` | `agent_run` with a `progressToken` waits until the job ends. |
| `MCP_PROGRESS_INTERVAL_SECONDS` | `10` | Longest gap between two progress notifications (keepalive). |

### MCP protocol versions (2026-07-28 and the initialize era)

The worker speaks both: the initialize handshake with `Mcp-Session-Id` (2024-11-05 … 2025-11-25) and 2026-07-28, where every POST carries its own envelope (`params._meta`) plus the routing headers `MCP-Protocol-Version`, `Mcp-Method` and `Mcp-Name`. The MCP SDK picks the era by the `MCP-Protocol-Version` header alone, so a proxy that forwards only some headers used to turn a 2026-07-28 request into an HTTP 400 (`Missing session ID`, or `-32020` header mismatch). With `MCP_PROTOCOL_MODERN=auto` (default) the worker fills in a missing header from the request body and serves an `initialize` that arrives with a stale 2026-07-28 header on the handshake path; a header that is present and contradicts the body is still refused. `MCP_PROTOCOL_MODERN=off` answers every 2026-07-28 request with `-32022` (`UNSUPPORTED_PROTOCOL_VERSION`, `supported` = the handshake versions); clients that negotiate (`server/discover` first) fall back to `initialize`.

| Setting | Default | |
| --- | --- | --- |
| `MCP_PROTOCOL_MODERN` | `auto` | `off`: no 2026-07-28, clients use the initialize handshake. |

### MCP cancellation (`notifications/cancelled`)

An `agent_run` call is bound to the job it starts **while the call is open** (up to `MCP_MAX_WAIT_SECONDS`, or with a `progressToken` until the job ends):

- **The client cancels the call** (`notifications/cancelled` with the call's request id): the job is cancelled with "Cancelled by the MCP client (notifications/cancelled for the agent_run call)." The engine is stopped, its process tree killed and the workspace freed, exactly like `agent_cancel` (see [Watchdogs and cancel](#watchdogs-and-cancel)).
- **The client disconnects while the call is open**, where the worker can see it: the job is cancelled with "Cancelled: the MCP client closed the agent_run call while it was open (…)". On protocol 2026-07-28 over HTTP, a closed request stream *is* the cancel signal (that version has no client-to-server notification on HTTP), so a cancel there reads as a disconnect. On the older stateful HTTP transport (2025-x), a dropped response stream is not reported to the tool, so the job keeps running; stop it with `agent_cancel`.
- **`background: true`** on `agent_run`: the job is not bound to the call. A cancelled call or a disconnect leaves it running; stop it with `agent_cancel`.
- Once `agent_run` has answered (finished, or returned a `job_id` because the job takes longer), the call is over and nothing is bound any more. Cancelling a later `agent_job` wait never cancels the job; use `agent_cancel`.
- A worker shutdown does not count as a client disconnect; the shutdown itself stops the jobs that are still open.

| Setting | Default | |
| --- | --- | --- |
| `MCP_CANCEL_ON_CANCELLED` | `true` | `notifications/cancelled` for an open `agent_run` cancels its job. `false`: the job runs on. |
| `MCP_CANCEL_ON_DISCONNECT` | `true` | A disconnect (and, on 2026-07-28 HTTP, a client cancel) during an open `agent_run` cancels its job. `false`: the job runs on. |

Mind that some clients send `notifications/cancelled` on their own timeout. With the default `MCP_MAX_WAIT_SECONDS` (25) the worker answers before the usual 30 s timeouts; if your client gives up sooner, lower the cap or start jobs with `background: true`. With a `progressToken` the client must reset its timeout on progress (or have a long enough one), otherwise its timeout cancels the job.

How it works: the MCP SDK (mcp 2.3.0) cancels the tool handler for `notifications/cancelled` (and on 2026-07-28 HTTP, for a closed stream); `agent_run` catches that and cancels the job. To tell a cancel from a disconnect the worker reads the request's `cancel_requested` flag, which the SDK sets but does not expose on the tool context yet; if that ever fails, the call counts as a disconnect.

### MCP Tasks (`tasks/*`, spec 2025-11-25, experimental)

**Not served yet** (checked again for 0.7.0, mcp 2.3.0 is still the newest release): the MCP SDK this worker uses has no server-side Tasks. It ships the 2025-11-25 Task wire types, but leaves `tasks/get`, `tasks/list`, `tasks/cancel`, `tasks/result` and `notifications/tasks/status` out of its method tables on purpose, and the 2026 revision moves Tasks into an extension (SEP-2663, `io.modelcontextprotocol/tasks`). Rather than hand-roll the protocol, the worker maps its jobs 1:1 onto Tasks, so serving them is a switch once the SDK has it:

| MCP Tasks | Worker today |
| --- | --- |
| `taskId` | `job_id` |
| `status`, `statusMessage` | job status (`queued` → `working`), `status_message` |
| `createdAt`, `lastUpdatedAt` | `created_at`, `updated_at` |
| `ttl` | `null`: jobs are kept by count (`KEEP_JOBS`, 200), not by time |
| `pollInterval` | `TASK_POLL_INTERVAL_MS` (5000), only while the job is open |
| task-augmented `tools/call` → `CreateTaskResult` | `agent_run` returning a `job_id` (or `background: true`) |
| `tasks/get` | `agent_job(job_id)`; the answer has the Task object as `task` (also `GET /jobs/<id>`) |
| `tasks/list` | `agent_jobs` |
| `tasks/cancel` | `agent_cancel(job_id)` |
| `tasks/result` | `agent_job(job_id, wait_seconds)` on an ended job (the same payload `agent_run` returns) |
| `notifications/tasks/status` | MCP progress messages during open calls; `GET /jobs/<id>/events` (SSE) |

What is missing to switch Tasks on, all on the SDK side unless noted:

1. Routing for `tasks/get`, `tasks/list`, `tasks/cancel`, `tasks/result` at 2025-11-25 (excluded in `mcp_types.methods`), or a published Tasks extension for 2026-07-28 (the SDK's extension API, `MethodBinding`/`intercept_tool_call`, could carry it, but the extension's wire format is not final).
2. Advertising `capabilities.tasks` (`list`, `cancel`, `requests.tools.call`) and `execution.taskSupport` on the `agent_run` tool definition.
3. Returning `CreateTaskResult` from a task-augmented `tools/call` through `MCPServer` (today a tool returns a `CallToolResult`).
4. Sending `notifications/tasks/status` outside a request.
5. Worker side: honouring a requested `ttl` (time-based retention next to `KEEP_JOBS`) and stamping `io.modelcontextprotocol/related-task` in `_meta` of task results.

The code for the mapping is `agent_worker/tasks.py` (`task_view`, `task_result`).

### Conversation id (`_meta`)

A client can link jobs to its conversation. The worker reads it from the `tools/call` request's `_meta`, first key that is set:

1. `io.github.domdus/conversation_id`: **preferred**; namespaced as the MCP `_meta` naming rules ask for keys that are not MCP's own
2. `contextId`: A2A's name for the same thing
3. `conversationId`, `conversation_id`

`agent_run(conversation_id=…)` and `POST /jobs` with `conversation_id` set it explicitly (the argument wins over `_meta`). The id is stored on the job and shown in `agent_run`, `agent_job`, `agent_jobs`, `GET /jobs…`; `agent_jobs(conversation_id=…)`, `GET /jobs?conversation_id=…` and `GET /jobs/overview?conversation_id=…` filter by it; `agent_cancel(conversation_id=…)` and `POST /conversations/<id>/cancel` cancel all its open jobs.

It must be an opaque id, **never a secret**: it shows up in job lists and the log. The worker accepts at most 128 characters of `A–Z a–z 0–9 . _ : - / @ # + =` and drops (with a log line, not the value) anything else, anything that looks like a credential (`sk-…`, `ghp_…`, JWTs, `Bearer …`, …) and the worker's own tokens.

### Parallelism and watchdogs

Jobs in different workspaces run in parallel (up to `MAX_CONCURRENT_JOBS`); in one workspace one file-touching job runs at a time, the rest are `queued` (details: [Parallel jobs](#parallel-jobs)). Every job is watched by `JOB_MAX_RUNTIME_SECONDS`, `JOB_IDLE_TIMEOUT_SECONDS` and, while `input_required`, `APPROVAL_TIMEOUT_SECONDS` (details: [Watchdogs and cancel](#watchdogs-and-cancel)), so no cancelled, hung or forgotten job holds a workspace.

### What a client (e.g. Home Assistant AI Workspace) should send

- `agent_run` with `_meta: {"progressToken": <unique token>, "io.github.domdus/conversation_id": "<conversation id>"}` on every call. The conversation id is an opaque id such as the chat's UUID, not a token.
- With the `progressToken`, wait for the call's result and reset the call's timeout on every `notifications/progress` (keepalive at least every `MCP_PROGRESS_INTERVAL_SECONDS`, 10 s). Show the progress `message` (last step) if you like.
- Send the user's language as `Accept-Language` on the MCP HTTP requests (e.g. `de-DE,de;q=0.9`): the approval question, its form and the progress messages come in that language ([Languages](#languages)).
- Declare the `elicitation` capability in `initialize` and show `elicitation/create` requests to the **user** (not the model): that is how the worker asks for approvals of commands on approval hosts. Without it, approvals go through `POST /jobs/<id>/approve` or `agent_approve`.
- When the user presses stop: send `notifications/cancelled` for the open `agent_run` call (most MCP SDKs do this when the call is cancelled), **and** `agent_cancel(conversation_id="<conversation id>")`, which also catches jobs whose `agent_run` call has already returned a `job_id`.
- Without a `progressToken` (older clients): if the answer has a `job_id` and `status` `queued`/`working`, poll `agent_job(job_id, wait_seconds=20)` about every `task.pollInterval` ms until the status is `completed`, `failed` or `cancelled`. Treat `input_required` as "ask the user" (or answer the elicitation the worker sends).
- Compare `status` against the MCP names; read `legacy_status` only if you still need the old ones.
- For work that must outlive the chat (a long refactor the user walks away from), set `background: true`.

### Languages

Texts the worker shows to **people** follow the standard `Accept-Language` header of the request: on MCP (HTTP transport) the approval question by elicitation (message, the form's titles and descriptions), the progress messages (`job <id>: …`, steps, what the job waits for, the final status with the reason it was stopped), and on `/v1/chat/completions` the approval question with its reply hint, the thinking lines and the error text of a stopped job. Supported are the 15 languages of AI Workspace: `en`, `de`, `nl`, `fr`, `es`, `it`, `pl`, `cs`, `sv`, `ru`, `ko`, `zh-Hans`, `zh-Hant`, `az`, `eo` (`zh-CN`/`zh-SG` → `zh-Hans`, `zh-TW`/`zh-HK`/`zh-MO` → `zh-Hant`, regions are ignored otherwise); anything else, or no header, is English.

Unchanged in every language: commands, hosts, job ids, engine names, setting names, endpoints, the reply words **approve**/**deny** (plain yes/no words of these languages work too), and everything machine-readable: status names (a localized progress message keeps the status, e.g. `abgeschlossen (completed)`), JSON keys and the tool results. Tool descriptions, server instructions and hints for the calling model stay English, as do stored events, `status_message`, `error` and the job API. Engine error messages are passed on as the engine wrote them. Status events carry their text as an `i18n` key with parameters (`agent_worker/i18n.py`).

## Job API

```bash
H="Authorization: Bearer $WORKER_TOKEN"
# start (wait up to 30 s for the result; 202 + job if still running)
curl -s -X POST localhost:8787/jobs -H "$H" -H 'Content-Type: application/json' \
  -d '{"task":"Add a --json flag to cli.py and a test for it","workspace":"my-repo","wait":30}'
curl -s localhost:8787/jobs/<id>?wait=60 -H "$H"          # status / result
curl -N  localhost:8787/jobs/<id>/events -H "$H"           # SSE: status, tool, text, result, end
curl -s -X POST localhost:8787/jobs/<id>/cancel -H "$H"
curl -s -X POST localhost:8787/jobs/<id>/approve -H "$H" -H 'Content-Type: application/json' \
  -d '{"approve": true}'                                    # answer a job in input_required
curl -s localhost:8787/jobs -H "$H"                         # recent jobs
curl -s localhost:8787/jobs/overview -H "$H"                # like agent_jobs: open jobs + last 20 ended
curl -s 'localhost:8787/jobs/overview?workspace=my-repo&status=working,queued' -H "$H"
curl -s 'localhost:8787/jobs/overview?conversation_id=chat-42' -H "$H"
curl -s -X POST localhost:8787/workspaces/my-repo/cancel -H "$H"   # cancel every open job there
curl -s -X POST localhost:8787/conversations/chat-42/cancel -H "$H" # cancel every open job of a conversation
curl -s localhost:8787/workspaces -H "$H"                   # also default_models per engine
```

`GET /jobs/<id>` has `status`, `legacy_status`, `status_message` and the MCP Task object as `task`; on a job that has not ended also `runtime_seconds`, `last_step`, `last_event_at` and `blocked_by`.

`POST /jobs` body: `task`, `engine` (`cursor`, `claude` or `codex`), `workspace`, `session`, `model`, `effort`, `tools`, `mcp_servers`, `images` (base64 or data URI, at most 4), `instructions`, `wait` (0–600 s), `conversation_id`.

Jobs and their events are stored in `/data/worker.sqlite` and survive restarts. A job that was running during a restart is marked failed.

## Workspaces

Each workspace is a folder under `/data/workspaces/<name>`. A name a caller has not used before is created empty, unless you set `CREATE_WORKSPACES=false`. Only one job that can edit files runs in a workspace at a time; the rest wait in a queue. Jobs limited to `mcp` or `none` tools don't take that lock. See [Parallel jobs](#parallel-jobs).

- Set `WORKSPACE_DIR=/workspace` and mount a host folder there to make it the default workspace. The container runs as uid 1000, so that folder must be writable for uid 1000.
- `GIT_REPO` clones a repository into the default workspace on the first start. Authentication uses `GIT_TOKEN` (HTTPS) or `GIT_SSH_KEY`. The agent's own `git pull`/`git push` use the same credentials, and the secret is never written to `.git/config`.

## SSH to your servers

The agent uses plain `ssh`, `scp`, `sftp`, `rsync` and git over SSH. You set up access with environment variables. Each value is either the content itself or a path to a mounted file:

| Variable | What it is |
|---|---|
| `SSH_KEY` | One private key |
| `SSH_KEYS_DIR` | A folder of private keys. Each one ends up at `~/.ssh/keys/<file name>` |
| `SSH_CONFIG` | A normal ssh config: `Host` aliases, `HostName`, `User`, `Port`, `IdentityFile` |
| `SSH_KNOWN_HOSTS` | Your servers' host keys. With it, unknown or changed keys are refused (`StrictHostKeyChecking yes`). Without it, the first key seen is kept and a later change is refused (`accept-new`) |
| `SSH_APPROVAL_HOSTS` | Patterns such as `prod-*,db01,10.0.1.*`, matched against the alias and its `HostName`. On these hosts the job pauses before the first command |

```yaml
# docker-compose.yml
    volumes:
      - ./ssh:/run/secrets/ssh:ro
# .env
SSH_KEYS_DIR=/run/secrets/ssh/keys
SSH_CONFIG=/run/secrets/ssh/config
SSH_KNOWN_HOSTS=/run/secrets/ssh/known_hosts
SSH_APPROVAL_HOSTS=prod-*
```

```
# ./ssh/config
Host web01
  HostName 10.0.0.5
  User deploy
  IdentityFile ~/.ssh/keys/web
Host prod-db
  HostName 10.0.9.9
  User root
  IdentityFile ~/.ssh/keys/prod
```

At startup the worker writes `~/.ssh` from these variables. Your config comes first. After it come defaults for every host: `BatchMode yes` (never a password prompt), a connect timeout, keep-alives, and connection reuse. Commands to the same host, user and port share one connection for 60 s after the last one ends, so a loop of quick commands doesn't log in each time. Every command still goes through the guard. Set `ControlMaster no` in your `SSH_CONFIG` to turn reuse off. `GIT_SSH_KEY` is added as one more key, so git over SSH works in every workspace. If you mount a read-only `~/.ssh` of your own, it is used as it is.

Use a key made for the worker, not your personal one. Ideally use one per server or group of servers, for a user that can do just what the worker needs, so you can revoke it alone.

### Every command is in the job

Before the real program runs, `ssh`, `scp` and `sftp` pass through a guard. `rsync` and git call `ssh`, so they pass through it too. The host, user and command show up as a `tool` event in the job. You can read them in `/jobs/<id>/events`, in Open WebUI's thinking stream, and in the MCP progress messages.

### Approval for selected hosts

When the agent first runs a command on a host in `SSH_APPROVAL_HOSTS`, the job switches to `input_required` (before 0.5.0: `needs_approval`). The command waits until a human answers or `APPROVAL_TIMEOUT_SECONDS` (1800 s; formerly `SSH_APPROVAL_TIMEOUT`) runs out. Once a host is approved, it stays approved for the rest of that job. A denial makes the command fail, and the agent is told to report that instead of trying again. If nobody answers in time, the whole job is cancelled with "approval timed out", so a headless job never holds its workspace waiting for a human who isn't there.

Where the question shows up:

- **MCP clients that support elicitation** (for example Claude Code) ask the user directly in their own UI. The answer comes from the person, not from the model that called the tool.
- **Other MCP clients (Hermes, Bifrost)** get `status: "input_required"` (`legacy_status: "needs_approval"`) with the host and command, and are told to ask the user. The answer goes to `POST /jobs/<id>/approve`. If you set `APPROVAL_TOOL=true`, there is also a `agent_approve` tool for passing on the user's answer. It's off by default, because the calling model could otherwise approve on its own.
- **Open WebUI and other `/v1` chats:** the reply ends with the command and the question. Reply **approve** or **deny** in the same chat, and the same job continues.
- **Job API:** check with `GET /jobs/<id>` and answer with `POST /jobs/<id>/approve`.

The guard sees what the agent runs with the normal commands. It is a guardrail for an agent that follows its instructions, not a sandbox: a process that calls `/usr/bin/ssh` directly bypasses it. Hard limits belong on the server, for example a separate user, or `command=` and `from=` in `authorized_keys`.

## Security

- **Token:** Without `WORKER_TOKEN`, the worker answers only when it listens on loopback and the `Host` header is loopback. Otherwise every request is refused.
- **`TOOL_PROFILE`** is the upper limit (`full` > `local` > `offline` > `mcp` > `none`). Requests can narrow it but never widen it.
- **The agent can do whatever its tools allow inside the container,** including running shell commands, without asking. Treat the token like a shell login on the container. Don't mount anything you wouldn't hand to a shell.
- **Codex's `auth.json`** in `/data/codex` holds ChatGPT tokens. Like the Claude token in `/data/claude` and a Cursor key set over the API in `/data/cursor`, it lives in the data volume and is readable by the agent's shell (same user). Don't give the worker a volume or token you wouldn't give that shell.
- **MCP servers sent with a request** must be HTTP/SSE. Stdio servers (which start processes) and `${ENV}` substitution are only allowed in `/data/mcp.json`. Set `ALLOW_REQUEST_MCP=false` to accept only that file.
- **SSH:** The guard listens only to callers inside the container. The worker's keys never leave the container, and requests cannot send SSH keys or config. See [SSH to your servers](#ssh-to-your-servers) for what approvals do and don't cover.
- **Loops:** The worker adds `X-Agent-Worker-Depth` to every HTTP MCP server it hands to an agent. If an agent calls a worker back through MCP, that call is refused (`MAX_CALL_DEPTH`, default 1).

## Configuration

See [.env.example](.env.example). The most important settings: `CURSOR_API_KEY`, `WORKER_TOKEN`, `CURSOR_MODEL`, `CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_API_KEY`, `CLAUDE_MODEL`, `OPENAI_API_KEY`, `CODEX_MODEL`, `CODEX_AUTH_JSON`, `CODEX_SANDBOX`, `CODEX_CONFIG`, `DEFAULT_ENGINE`, `FALLBACK_ENGINES`, `FALLBACK_PROMOTE` (turn), `FALLBACK_BACKOFF` (5m,15m,60m), `FALLBACK_LOCK_SECONDS` (120), `TOOL_PROFILE`, `MAX_CONCURRENT_JOBS` (2), `JOB_MAX_RUNTIME_SECONDS` (3600), `JOB_IDLE_TIMEOUT_SECONDS` (900), `APPROVAL_TIMEOUT_SECONDS` (1800), `MCP_WAIT_SECONDS` (20), `MCP_MAX_WAIT_SECONDS` (25), `MCP_WAIT_WITH_PROGRESS` (true), `MCP_PROGRESS_INTERVAL_SECONDS` (10), `MCP_PROTOCOL_MODERN` (auto), `MCP_CANCEL_ON_CANCELLED` (true), `MCP_CANCEL_ON_DISCONNECT` (true), `JOB_STATUS_NAMES` (mcp), `TASK_POLL_INTERVAL_MS` (5000), `WORKSPACE_DIR`, `GIT_REPO`, `SSH_KEY`, `SSH_KEYS_DIR`, `SSH_CONFIG`, `SSH_KNOWN_HOSTS`, `SSH_APPROVAL_HOSTS`, `APPROVAL_TOOL`, `WORKER_INSTRUCTIONS`, `REWRITE_LOOPBACK_HOST`.

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest -q
python -m agent_worker.app      # HOST=127.0.0.1 DATA_DIR=./data for a local run
```

The tests use a fake engine, so they need neither a Cursor key nor network access.
