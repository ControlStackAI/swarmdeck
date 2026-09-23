"""Tracks the swarms this process is supervising."""

from __future__ import annotations

import asyncio
import contextlib
import time
from pathlib import Path
from typing import Any

from .background import spawn
from .spec import SwarmSpec
from .supervisor import Supervisor


class Registry:
    def __init__(self, root: Path, *, keep_finished: int = 20) -> None:
        self.root = root
        self.keep_finished = keep_finished
        self._swarms: dict[str, Supervisor] = {}
        self._order: list[str] = []
        self._lock = asyncio.Lock()

    def get(self, swarm_id: str) -> Supervisor | None:
        return self._swarms.get(swarm_id)

    def list(self) -> list[dict[str, Any]]:
        return [
            self._swarms[sid].state.summary()
            for sid in reversed(self._order)
            if sid in self._swarms
        ]

    async def launch(self, spec: SwarmSpec, *, capture_logs: bool = True) -> Supervisor:
        async with self._lock:
            sup = Supervisor(
                spec,
                run_dir=self.root / "runs" / time.strftime("%Y%m%d-%H%M%S"),
                capture_logs=capture_logs,
            )
            # Two swarms started in the same second must not share a run dir.
            sup.run_dir = sup.run_dir.with_name(f"{sup.run_dir.name}-{sup.swarm_id}")
            self._swarms[sup.swarm_id] = sup
            self._order.append(sup.swarm_id)
            sup.on_finish(lambda state: self._on_finish(sup))
            await sup.start()
            self._evict()
            return sup

    def _on_finish(self, sup: Supervisor) -> None:
        with contextlib.suppress(Exception):
            sup.write_results()
        spawn(sup.logs.close(), name=f"logclose-{sup.swarm_id}")

    def _evict(self) -> None:
        """Drop the oldest finished swarms so a long-lived server does not
        accumulate state for every run it has ever done."""
        finished = [
            sid
            for sid in self._order
            if sid in self._swarms and self._swarms[sid].state.phase == "finished"
        ]
        for sid in finished[: max(0, len(finished) - self.keep_finished)]:
            self._swarms.pop(sid, None)
            with contextlib.suppress(ValueError):
                self._order.remove(sid)

    async def remove(self, swarm_id: str) -> bool:
        sup = self._swarms.pop(swarm_id, None)
        if sup is None:
            return False
        with contextlib.suppress(ValueError):
            self._order.remove(swarm_id)
        await sup.shutdown()
        return True

    async def shutdown_all(self) -> None:
        await asyncio.gather(
            *(sup.shutdown() for sup in list(self._swarms.values())),
            return_exceptions=True,
        )
