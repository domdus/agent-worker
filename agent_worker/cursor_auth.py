"""The Cursor API key: CURSOR_API_KEY from the environment, or one set at runtime over the API.

PUT /auth/cursor/key stores a key owner-only in DATA_DIR/cursor/api-key, so it survives restarts
and needs none. CURSOR_API_KEY on the container still wins. The key is never returned or logged.
"""

from __future__ import annotations

import importlib.util
import os
import re
import stat
from pathlib import Path

from agent_worker.settings import Settings

# Cursor user API keys are opaque tokens (today "key_" + hex, older "crsr_..."): only the shape is checked.
_KEY = re.compile(r"[A-Za-z0-9_\-.:]{16,512}")
VERIFY_SECONDS = 20


class CursorKeyError(Exception):
    """`key`: bad_key, key_rejected, unreachable."""

    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


def sdk_installed() -> bool:
    return importlib.util.find_spec("cursor_sdk") is not None


def key_path(settings: Settings) -> Path:
    return settings.data_dir / "cursor" / "api-key"


def clean_key(value: str) -> str:
    key = str(value or "").strip()
    if not _KEY.fullmatch(key):
        raise CursorKeyError("bad_key")
    return key


def save_key(path: Path, key: str) -> None:
    """Owner-only, replacing an older key in one step."""
    value = clean_key(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, stat.S_IRWXU)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(value + "\n")
    os.replace(tmp, path)


def load_key(path: Path) -> str:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return value if _KEY.fullmatch(value) else ""


def forget_key(path: Path) -> bool:
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def api_key(settings: Settings) -> str:
    """The key Cursor jobs use: CURSOR_API_KEY first, then the stored one."""
    return (settings.cursor_api_key or "").strip() or load_key(key_path(settings))


def credential_source(settings: Settings) -> str:
    """env, stored or none."""
    if (settings.cursor_api_key or "").strip():
        return "env"
    return "stored" if load_key(key_path(settings)) else "none"


def verify_key(key: str) -> int:
    """Ask Cursor which models the key may use (blocking). Returns how many; raises CursorKeyError."""
    from cursor_sdk import Cursor

    try:
        from cursor_sdk import AuthenticationError, PermissionDeniedError

        refused: tuple[type[BaseException], ...] = (AuthenticationError, PermissionDeniedError)
    except ImportError:  # pragma: no cover - older SDKs
        refused = ()
    try:
        return len(Cursor.models.list(api_key=key))
    except refused:
        raise CursorKeyError("key_rejected") from None
    except Exception:
        raise CursorKeyError("unreachable") from None
