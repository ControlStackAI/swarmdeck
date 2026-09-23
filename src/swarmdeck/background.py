"""Keeping fire-and-forget tasks alive.

asyncio only holds a weak reference to a running task. A task nobody keeps a
strong reference to can be garbage collected mid-flight, which shows up as a
container that never gets unpaused or a log file that never gets flushed --
intermittently, and only under memory pressure. Every detached task in
swarmdeck goes through here.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

log = logging.getLogger("swarmdeck")

_PENDING: set[asyncio.Task] = set()


def spawn(coro: Coroutine[Any, Any, Any], *, name: str | None = None) -> asyncio.Task:
    """Run ``coro`` detached, holding a reference until it completes."""
    task = asyncio.ensure_future(coro)
    if name:
        task.set_name(name)
    _PENDING.add(task)
    task.add_done_callback(_finish)
    return task


def _finish(task: asyncio.Task) -> None:
    _PENDING.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        # A detached task that dies silently is how observability tools end up
        # lying to their operators.
        log.warning("background task %s failed: %r", task.get_name(), exc)


def pending() -> int:
    return len(_PENDING)
