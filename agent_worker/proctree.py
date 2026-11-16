"""Find and kill the processes a job left behind, by a marker in their environment.

Every engine subprocess gets AGENT_WORKER_JOB (Claude Code, Codex) or AGENT_WORKER_WORKSPACE (the Cursor
bridge), and everything the agent starts (shells, ssh, builds) inherits it. Linux only (/proc); elsewhere
nothing is found. The worker itself and its parents are never touched.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from pathlib import Path

logger = logging.getLogger(__name__)

_PROC = Path("/proc")


def _environ(pid: int) -> dict[str, str]:
    try:
        raw = (_PROC / str(pid) / "environ").read_bytes()
    except OSError:
        return {}
    out: dict[str, str] = {}
    for item in raw.split(b"\0"):
        key, sep, value = item.partition(b"=")
        if sep:
            out[key.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
    return out


def _parents() -> dict[int, int]:
    """pid -> parent pid of every visible process."""
    out: dict[int, int] = {}
    try:
        entries = list(_PROC.iterdir())
    except OSError:
        return out
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
        except OSError:
            continue
        # "pid (comm) state ppid ...": comm may hold spaces and parentheses, so split after the last ")".
        fields = stat.rpartition(")")[2].split()
        if len(fields) >= 2 and fields[1].isdigit():
            out[int(entry.name)] = int(fields[1])
    return out


def _protected(parents: dict[int, int]) -> set[int]:
    """This process and its ancestors."""
    keep: set[int] = set()
    pid = os.getpid()
    while pid and pid not in keep:
        keep.add(pid)
        pid = parents.get(pid, 0)
    return keep


def find(name: str, value: str, *, unless: str = "") -> list[int]:
    """Processes with name=value in their environment (and without `unless` set), plus all their descendants."""
    if not value or not _PROC.is_dir():
        return []
    parents = _parents()
    keep = _protected(parents)
    roots = set()
    for pid in parents:
        if pid in keep:
            continue
        env = _environ(pid)
        if env.get(name) == value and not (unless and env.get(unless)):
            roots.add(pid)
    found = set(roots)
    grew = True
    while grew:
        grew = False
        for pid, parent in parents.items():
            if parent in found and pid not in found and pid not in keep:
                found.add(pid)
                grew = True
    return sorted(found)


def _alive(pid: int) -> bool:
    try:
        stat = (_PROC / str(pid) / "stat").read_text()
    except OSError:
        return False
    fields = stat.rpartition(")")[2].split()
    return bool(fields) and fields[0] != "Z"


def _signal(pids: list[int], sig: int) -> None:
    for pid in pids:
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass


async def kill(pids: list[int], grace: float = 3.0) -> list[int]:
    """SIGTERM, then SIGKILL for whatever is still alive after `grace` seconds. Returns the pids signalled."""
    if not pids:
        return []
    _signal(pids, signal.SIGTERM)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + grace
    while loop.time() < deadline and any(_alive(pid) for pid in pids):
        await asyncio.sleep(0.1)
    left = [pid for pid in pids if _alive(pid)]
    if left:
        _signal(left, signal.SIGKILL)
    return pids


async def kill_tagged(name: str, value: str, *, unless: str = "", grace: float = 3.0) -> list[int]:
    pids = find(name, value, unless=unless)
    if pids:
        logger.info("killing %d process(es) with %s=%s", len(pids), name, value)
    return await kill(pids, grace)
