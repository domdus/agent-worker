"""Sign in with ChatGPT for the Codex engine, and where Codex keeps its credential.

Codex keeps its login in CODEX_HOME/auth.json (DATA_DIR/codex in the data volume). For a ChatGPT
plan that file holds OAuth tokens, which Codex refreshes in place while it works, so the file
must stay writable and must not be shared live with another machine.

Three ways to get it there:
  1. `docker exec -it agent-worker python -m agent_worker.codex_login`: OpenAI's own
     `codex login --device-auth` (device-code flow: open a link, type a code).
  2. POST /auth/codex/login: the same device-code flow through the Codex SDK, for UIs.
  3. CODEX_AUTH_JSON: an auth.json from `codex login` on another computer (a mounted file or the
     JSON itself). It is copied into CODEX_HOME once, when CODEX_HOME has none.
OPENAI_API_KEY (or CODEX_API_KEY) is the fallback when there is no auth.json; it is handed to
Codex per job in memory and never written to disk.

Device-code login has to be switched on once for the ChatGPT account (Settings, Security) or by a
workspace admin. Without it OpenAI refuses the code.
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import logging
import os
import secrets
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from agent_worker.settings import Settings

logger = logging.getLogger(__name__)

LINK_SECONDS = 45
# A sign-in nobody finishes is dropped after this long (OpenAI's device codes last 15 minutes).
LOGIN_TTL_SECONDS = 15 * 60
# Never passed on to Codex or the agent's shell: the worker's own credentials.
SECRET_ENV = (
    "OPENAI_API_KEY",
    "CODEX_API_KEY",
    "CODEX_AUTH_JSON",
    "CURSOR_API_KEY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "WORKER_TOKEN",
)
DEVICE_AUTH_HINT = (
    "Device-code sign-in must be enabled once: turn on device code login in ChatGPT's Settings > Security "
    "(for a workspace account, the admin allows it in the workspace permissions)."
)


class CodexLoginError(Exception):
    """`key`: cli_missing, no_code, expired, failed."""

    def __init__(self, key: str, detail: str = "") -> None:
        super().__init__(detail or key)
        self.key = key
        self.detail = detail


# ---- the runtime ---------------------------------------------------------------------------------


def sdk_installed() -> bool:
    return importlib.util.find_spec("openai_codex") is not None


def cli_path() -> str:
    """The Codex CLI pinned with the SDK (openai-codex-cli-bin), CODEX_BIN, or one on PATH."""
    configured = os.environ.get("CODEX_BIN", "").strip()
    if configured:
        return configured if os.access(configured, os.X_OK) else ""
    try:
        from codex_cli_bin import bundled_codex_path

        bundled = Path(bundled_codex_path())
        if bundled.is_file() and os.access(bundled, os.X_OK):
            return str(bundled)
    except Exception:
        pass
    return shutil.which("codex") or ""


def child_env(settings: Settings, workspace: str = "") -> dict[str, str]:
    """Merged onto os.environ for Codex: the worker's secrets blanked, CODEX_HOME in the data volume.

    The agent's shell inherits this, so it is also what a command run by the agent can read.
    GIT_TOKEN stays: git in the workspace uses it through the credential helper.
    """
    from agent_worker.engine import WORKSPACE_ENV

    env = {key: "" for key in SECRET_ENV if key in os.environ}
    env["CODEX_HOME"] = str(settings.codex_home)
    if workspace:
        env[WORKSPACE_ENV] = workspace
    return env


def launch_overrides(store: str = "file") -> tuple[str, ...]:
    """`--config` values for the Codex app-server process."""
    return (
        f'cli_auth_credentials_store="{store}"',
        "check_for_update_on_startup=false",
        "analytics.enabled=false",
    )


def sdk_config(settings: Settings, *, store: str = "file", cwd: str | None = None, workspace: str = "") -> Any:
    from openai_codex import CodexConfig

    settings.codex_home.mkdir(parents=True, exist_ok=True)
    os.chmod(settings.codex_home, stat.S_IRWXU)
    return CodexConfig(
        codex_bin=os.environ.get("CODEX_BIN", "").strip() or None,
        config_overrides=launch_overrides(store) + tuple(settings.codex_config),
        cwd=cwd,
        env=child_env(settings, workspace),
        client_name="agent_worker",
        client_title="Agent Worker",
    )


# ---- auth.json -----------------------------------------------------------------------------------


def _jwt_claims(token: str) -> dict[str, Any]:
    """The payload of a JWT, unverified: only for showing the plan in /health."""
    try:
        part = token.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (IndexError, ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def auth_info(path: Path) -> dict[str, Any]:
    """{"mode": "chatgpt" | "api-key" | "", "plan": str | None} from an auth.json. Never the secrets."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"mode": "", "plan": None}
    if not isinstance(data, dict):
        return {"mode": "", "plan": None}
    tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
    if tokens.get("refresh_token") or tokens.get("access_token"):
        claims = _jwt_claims(str(tokens.get("id_token") or ""))
        auth = claims.get("https://api.openai.com/auth")
        plan = auth.get("chatgpt_plan_type") if isinstance(auth, dict) else None
        return {"mode": "chatgpt", "plan": str(plan) if plan else None}
    if str(data.get("OPENAI_API_KEY") or "").strip():
        return {"mode": "api-key", "plan": None}
    return {"mode": "", "plan": None}


def _seed_text(raw: str) -> str:
    value = (raw or "").strip()
    if not value:
        return ""
    if not value.startswith("{"):
        try:
            return Path(value).expanduser().read_text(encoding="utf-8")
        except OSError as exc:
            logger.error("CODEX_AUTH_JSON points to %s, which cannot be read: %s", value, exc)
            return ""
    return value


def write_auth(path: Path, text: str) -> None:
    """Owner-only, replacing an older file in one step."""
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, stat.S_IRWXU)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text if text.endswith("\n") else text + "\n")
    os.replace(tmp, path)


def seed_auth(settings: Settings) -> bool:
    """Copy CODEX_AUTH_JSON into CODEX_HOME once. Codex refreshes the copy; the seed is never rewritten."""
    if not settings.codex_auth_json or settings.codex_auth_path.exists():
        return False
    text = _seed_text(settings.codex_auth_json)
    if not text:
        return False
    try:
        json.loads(text)
    except ValueError:
        logger.error("CODEX_AUTH_JSON is not valid JSON; it is ignored")
        return False
    write_auth(settings.codex_auth_path, text)
    logger.info("Codex auth.json seeded from CODEX_AUTH_JSON (%s)", auth_info(settings.codex_auth_path)["mode"] or "?")
    return True


def forget_auth(settings: Settings) -> bool:
    try:
        settings.codex_auth_path.unlink()
    except FileNotFoundError:
        return False
    return True


def credential_source(settings: Settings) -> str:
    """For /health and the engine: chatgpt, api-key (in auth.json), env-api or none."""
    seed_auth(settings)
    mode = auth_info(settings.codex_auth_path)["mode"]
    if mode:
        return mode
    return "env-api" if (settings.openai_api_key or "").strip() else "none"


# ---- device-code sign-in through the SDK ---------------------------------------------------------


class CodexLogin:
    """One device-code sign-in: a Codex app-server waits for the user to enter the code."""

    def __init__(self, codex: Any, handle: Any) -> None:
        self.id = secrets.token_urlsafe(16)
        self.created = time.monotonic()
        self.codex = codex
        self.handle = handle
        self.url = str(getattr(handle, "verification_url", "") or "")
        self.code = str(getattr(handle, "user_code", "") or "")
        self.status = "pending"  # pending | succeeded | failed | cancelled | expired
        self.error = ""
        self.done = asyncio.Event()
        self.task: asyncio.Task | None = None

    @property
    def expired(self) -> bool:
        return time.monotonic() - self.created > LOGIN_TTL_SECONDS

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "status": self.status, "url": self.url, "code": self.code}
        if self.error:
            out["error"] = self.error
        return out

    async def _close(self) -> None:
        try:
            await asyncio.wait_for(self.codex.close(), 10)
        except Exception:
            logger.debug("codex login close failed", exc_info=True)

    async def watch(self) -> None:
        try:
            left = max(1.0, LOGIN_TTL_SECONDS - (time.monotonic() - self.created))
            result = await asyncio.wait_for(self.handle.wait(), left)
            if getattr(result, "success", False):
                self.status = "succeeded"
            else:
                self.status = "failed"
                self.error = str(getattr(result, "error", "") or "OpenAI did not accept the sign-in.")[:300]
        except asyncio.TimeoutError:
            self.status = "expired"
        except asyncio.CancelledError:
            if self.status == "pending":
                self.status = "cancelled"
            raise
        except Exception as exc:
            self.status = "failed"
            self.error = (str(exc) or type(exc).__name__)[:300]
        finally:
            self.done.set()
            await self._close()

    async def cancel(self) -> None:
        if self.status == "pending":
            self.status = "cancelled"
            try:
                await asyncio.wait_for(self.handle.cancel(), 5)
            except Exception:
                logger.debug("codex login cancel failed", exc_info=True)
        if self.task is not None and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass
        else:
            await self._close()
        self.done.set()


class CodexLoginManager:
    """At most one sign-in at a time; a new one replaces the old one."""

    def __init__(self, settings: Settings, factory: Callable[[Settings], Any] | None = None) -> None:
        self.settings = settings
        self._factory = factory or self._default_factory
        self._current: CodexLogin | None = None

    @staticmethod
    def _default_factory(settings: Settings) -> Any:
        if not sdk_installed() or not cli_path():
            raise CodexLoginError("cli_missing")
        from openai_codex import AsyncCodex

        return AsyncCodex(sdk_config(settings, store="file"))

    async def start(self) -> CodexLogin:
        await self._drop()
        codex = self._factory(self.settings)
        try:
            handle = await asyncio.wait_for(codex.login_chatgpt_device_code(), LINK_SECONDS)
        except CodexLoginError:
            raise
        except Exception as exc:
            try:
                await codex.close()
            except Exception:
                pass
            raise CodexLoginError("no_code", (str(exc) or type(exc).__name__)[:300]) from exc
        login = CodexLogin(codex, handle)
        login.task = asyncio.create_task(login.watch(), name="codex-login")
        self._current = login
        return login

    def get(self, login_id: str) -> CodexLogin:
        login = self._current
        if login is None or login.id != str(login_id or ""):
            raise CodexLoginError("expired")
        return login

    async def wait(self, login_id: str, seconds: float) -> CodexLogin:
        login = self.get(login_id)
        if seconds > 0 and login.status == "pending":
            try:
                await asyncio.wait_for(login.done.wait(), seconds)
            except asyncio.TimeoutError:
                pass
        return login

    async def cancel(self, login_id: str) -> None:
        if self._current is not None and self._current.id == str(login_id or ""):
            await self._drop()

    async def _drop(self) -> None:
        login, self._current = self._current, None
        if login is not None:
            await login.cancel()

    async def aclose(self) -> None:
        await self._drop()


# ---- docker exec -it agent-worker python -m agent_worker.codex_login ----------------------------


def _describe(settings: Settings) -> str:
    source = credential_source(settings)
    if source == "chatgpt":
        plan = auth_info(settings.codex_auth_path)["plan"]
        return f"signed in with ChatGPT{f' ({plan} plan)' if plan else ''}"
    if source == "api-key":
        return "an OpenAI API key is stored in auth.json"
    if source == "env-api":
        return "not signed in; OPENAI_API_KEY from the environment is used"
    return "not signed in"


def _cli_env(settings: Settings) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key not in SECRET_ENV}
    env["CODEX_HOME"] = str(settings.codex_home)
    return env


def main(argv: list[str] | None = None) -> None:
    from agent_worker.settings import Settings

    args = list(sys.argv[1:] if argv is None else argv)
    command = args[0] if args else "login"
    settings = Settings.from_env()
    settings.codex_home.mkdir(parents=True, exist_ok=True)
    os.chmod(settings.codex_home, stat.S_IRWXU)
    if command == "status":
        print(f"Codex: {_describe(settings)}. CODEX_HOME={settings.codex_home}")
        raise SystemExit(0 if credential_source(settings) != "none" else 1)
    if command == "logout":
        removed = forget_auth(settings)
        print("Signed out: auth.json removed." if removed else "There was no auth.json to remove.")
        raise SystemExit(0)
    if command != "login":
        print("Usage: python -m agent_worker.codex_login [login|status|logout]", file=sys.stderr)
        raise SystemExit(2)
    cli = cli_path()
    if not cli:
        print("The Codex CLI was not found. Is openai-codex installed in this image?", file=sys.stderr)
        raise SystemExit(1)
    if not sys.stdin.isatty():
        print("Run it with `docker exec -it` so the sign-in can show its code.", file=sys.stderr)
    print("Starting ChatGPT sign-in for Codex (device code). " + DEVICE_AUTH_HINT + "\n", flush=True)
    result = subprocess.run(
        [cli, "login", "--device-auth", "-c", 'cli_auth_credentials_store="file"'],
        env=_cli_env(settings),
        check=False,
    )
    if result.returncode != 0:
        print(f"\nSign-in failed (exit {result.returncode}). " + DEVICE_AUTH_HINT, file=sys.stderr)
        raise SystemExit(result.returncode or 1)
    print(f"\nCodex: {_describe(settings)}. Saved in {settings.codex_auth_path}; no restart needed.")
    raise SystemExit(0)


if __name__ == "__main__":
    main()
