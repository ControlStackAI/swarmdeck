"""Durable log capture with a bounded memory footprint.

The naive design -- one open file per agent -- costs a file descriptor per
live agent on top of the two the pipes already take, which is the thing that
breaks first at 1000 agents. Instead every line from every agent goes to a
single append-only JSONL file through one writer task, and each agent keeps a
small in-memory ring so the inspector is instant for whatever is on screen.

Writes are batched and never fsynced. These are logs; losing the last 50ms of
them on a hard kill is an acceptable trade for not blocking the event loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections import deque
from pathlib import Path
from typing import Any

#: Lines held per agent for instant retrieval.
RING_SIZE = 200

#: Queue depth before we start shedding. At 1000 agents each emitting a line
#: every 100ms this absorbs a full second of burst.
QUEUE_MAX = 16_384

FLUSH_INTERVAL = 0.05
BATCH_MAX = 2_000


class LogStore:
    def __init__(self, run_dir: Path, *, enabled: bool = True) -> None:
        self.run_dir = run_dir
        self.enabled = enabled
        self.path = run_dir / "logs.jsonl"
        self.rings: dict[int, deque[tuple[float, str, str]]] = {}
        self.dropped = 0
        self.written = 0
        self._queue: asyncio.Queue[tuple[int, float, str, str] | None] = asyncio.Queue(
            maxsize=QUEUE_MAX
        )
        self._task: asyncio.Task | None = None
        self._closed = False

    async def start(self) -> None:
        if self.enabled:
            self.run_dir.mkdir(parents=True, exist_ok=True)
            self._task = asyncio.create_task(self._writer(), name="swarmdeck-logwriter")

    def append(self, index: int, level: str, text: str) -> None:
        """Record one line. Never blocks, never raises."""
        ts = time.time()
        ring = self.rings.get(index)
        if ring is None:
            ring = self.rings[index] = deque(maxlen=RING_SIZE)
        ring.append((ts, level, text))

        if not self.enabled or self._closed:
            return
        try:
            self._queue.put_nowait((index, ts, level, text))
        except asyncio.QueueFull:
            # Shed rather than apply backpressure to the agent's stdout pipe:
            # a blocked reader would stall the agent itself, which turns an
            # observability problem into a correctness problem.
            self.dropped += 1

    async def _writer(self) -> None:
        buf: list[str] = []
        try:
            with open(self.path, "a", encoding="utf-8", buffering=1 << 16) as fh:
                while True:
                    item = await self._queue.get()
                    if item is None:
                        break
                    buf.append(_encode(item))
                    deadline = time.monotonic() + FLUSH_INTERVAL
                    while len(buf) < BATCH_MAX:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        try:
                            nxt = await asyncio.wait_for(self._queue.get(), timeout=remaining)
                        except asyncio.TimeoutError:
                            break
                        if nxt is None:
                            item = None
                            break
                        buf.append(_encode(nxt))
                    fh.write("".join(buf))
                    fh.flush()
                    self.written += len(buf)
                    buf.clear()
                    if item is None:
                        break
        except asyncio.CancelledError:
            raise
        except OSError:
            # Disk full or the run directory vanished. Degrade to memory-only
            # rather than taking the swarm down with us.
            self.enabled = False

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._task is not None:
            with contextlib.suppress(asyncio.QueueFull):
                self._queue.put_nowait(None)
            with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(self._task), timeout=5.0)
            if not self._task.done():
                self._task.cancel()
                await asyncio.gather(self._task, return_exceptions=True)

    # -- retrieval ------------------------------------------------------

    def tail(self, index: int, limit: int = RING_SIZE) -> list[dict[str, Any]]:
        ring = self.rings.get(index)
        if ring and (len(ring) >= limit or not self.path.exists()):
            items = list(ring)[-limit:]
            return [{"ts": t, "level": lv, "text": tx} for t, lv, tx in items]
        return self._scan(index, limit)

    def _scan(self, index: int, limit: int) -> list[dict[str, Any]]:
        """Read back further history for one agent from the shared file.

        A linear scan is fine for the inspector -- it runs on one agent, on
        demand, driven by a human clicking a cell.
        """
        if not self.path.exists():
            ring = self.rings.get(index, deque())
            return [{"ts": t, "level": lv, "text": tx} for t, lv, tx in ring]
        out: deque[dict[str, Any]] = deque(maxlen=limit)
        try:
            with open(self.path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    # Cheap prefilter before paying for json.loads.
                    if f'"i":{index},' not in line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if rec.get("i") == index:
                        out.append(
                            {
                                "ts": rec.get("ts", 0.0),
                                "level": rec.get("l", "info"),
                                "text": rec.get("m", ""),
                            }
                        )
        except OSError:
            pass
        return list(out)


def _encode(item: tuple[int, float, str, str]) -> str:
    index, ts, level, text = item
    return (
        json.dumps(
            {"i": index, "ts": round(ts, 3), "l": level, "m": text},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n"
    )
