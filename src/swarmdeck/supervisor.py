"""The control plane: schedule, run, and steer a swarm of agents.

Design notes worth knowing before changing anything here:

* Work is pulled by a **fixed pool of worker tasks**, not one task per agent.
  A thousand parked coroutines is survivable but wasteful, and a pool makes
  live concurrency changes trivial -- which matters, because the single most
  useful control at this scale is "slow down, I am burning money".
* Timeouts are enforced by a watchdog that subtracts paused time, so pausing
  a swarm to inspect it does not cause a mass timeout when you resume.
* Cancellation goes to the process *group*. Agents shell out; killing only
  the direct child leaks the expensive part.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

from .events import Event, EventType, parse_line
from .logstore import LogStore
from .runtimes import LaunchRequest, get_runtime
from .spec import SwarmSpec, iter_tags, render_argv
from .state import AgentState, Status, SwarmState

log = logging.getLogger("swarmdeck.supervisor")

WATCHDOG_TICK = 0.5


class Supervisor:
    def __init__(
        self,
        spec: SwarmSpec,
        *,
        run_dir: Path | None = None,
        swarm_id: str | None = None,
        capture_logs: bool = True,
    ) -> None:
        self.spec = spec
        self.swarm_id = swarm_id or uuid.uuid4().hex[:12]
        self.run_dir = Path(run_dir or Path.cwd() / ".swarmdeck" / "runs" / self.swarm_id)
        self.runtime = get_runtime(spec.agent.runtime)
        self.logs = LogStore(self.run_dir, enabled=capture_logs)

        agents = [
            AgentState(
                index=i,
                id=f"a{i:05d}",
                task=task,
                tags=tuple(iter_tags(spec, i)),
            )
            for i, task in enumerate(spec.tasks)
        ]
        self.state = SwarmState(self.swarm_id, spec.name, agents)

        self.warnings: list[str] = []
        self._queue: asyncio.Queue[int] = asyncio.Queue()
        self._workers: set[asyncio.Task] = set()
        self._target_workers = spec.concurrency
        self._live: dict[int, Any] = {}
        self._paused_since: dict[int, float] = {}
        self._paused_total: dict[int, float] = {}
        self._cancelled: set[int] = set()
        self._outstanding = 0
        self._done = asyncio.Event()
        self._started = False
        self._stopping = False
        self._on_finish: list[Callable[[SwarmState], Any]] = []

    # -- lifecycle ------------------------------------------------------

    async def start(self) -> None:
        if self._started:
            raise RuntimeError("supervisor already started")
        self._started = True

        self.warnings = await self.runtime.preflight(self.spec.concurrency)
        for warning in self.warnings:
            log.warning("%s", warning)
            self.state.push_feed("warn", -1, warning)

        await self.logs.start()
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._write_manifest()

        self.state.phase = "running"
        self.state.agg.started_at = time.time()
        for agent in self.state.agents:
            self._queue.put_nowait(agent.index)
            self._outstanding += 1
        self._scale_workers()

    def _write_manifest(self) -> None:
        manifest = {
            "swarm_id": self.swarm_id,
            "name": self.spec.name,
            "description": self.spec.description,
            "created_at": self.state.created_at,
            "agents": self.spec.size,
            "concurrency": self.spec.concurrency,
            "runtime": self.spec.agent.runtime,
            "command": self.spec.agent.command,
            "spec_path": str(self.spec.source_path) if self.spec.source_path else None,
        }
        with contextlib.suppress(OSError):
            (self.run_dir / "manifest.json").write_text(
                json.dumps(manifest, indent=2), encoding="utf-8"
            )

    def _scale_workers(self) -> None:
        while len(self._workers) < self._target_workers:
            task = asyncio.create_task(self._worker())
            self._workers.add(task)
            task.add_done_callback(self._workers.discard)

    async def _worker(self) -> None:
        while True:
            if len(self._workers) > self._target_workers or self._stopping:
                return
            try:
                index = await asyncio.wait_for(self._queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                if self._outstanding <= 0:
                    return
                continue

            agent = self.state.agents[index]
            try:
                if index in self._cancelled:
                    self.state.set_status(agent, Status.CANCELLED)
                else:
                    await self._run_agent(agent)
            except asyncio.CancelledError:
                self.state.set_status(agent, Status.CANCELLED)
                raise
            except Exception as exc:  # a broken runtime must not kill the pool
                log.exception("agent %s crashed the supervisor path", agent.id)
                agent.error = f"{type(exc).__name__}: {exc}"
                self.state.set_status(agent, Status.FAILED)
                self.state.push_feed("error", agent.index, agent.error)
            finally:
                self._outstanding -= 1
                self._queue.task_done()
                if self._outstanding <= 0:
                    self._finish()

    def _finish(self) -> None:
        if self.state.phase == "finished":
            return
        self.state.phase = "finished"
        self.state.finished_at = time.time()
        self.state.agg.ended_at = self.state.finished_at
        self._done.set()
        for hook in self._on_finish:
            with contextlib.suppress(Exception):
                hook(self.state)

    def on_finish(self, hook: Callable[[SwarmState], Any]) -> None:
        self._on_finish.append(hook)

    async def wait(self) -> SwarmState:
        await self._done.wait()
        return self.state

    async def shutdown(self, grace: float | None = None) -> None:
        """Stop everything. Safe to call twice."""
        self._stopping = True
        self.state.phase = "stopping"
        grace = grace if grace is not None else self.spec.agent.shutdown_grace

        live = list(self._live.items())
        if live:
            await asyncio.gather(
                *(handle.terminate(grace) for _, handle in live),
                return_exceptions=True,
            )
        for task in list(self._workers):
            task.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)

        for agent in self.state.agents:
            if agent.status not in Status.TERMINAL:
                self.state.set_status(agent, Status.CANCELLED)

        await self.logs.close()
        self._finish()

    # -- running one agent ----------------------------------------------

    async def _run_agent(self, agent: AgentState) -> None:
        spec = self.spec.agent
        attempts = spec.retries + 1

        for attempt in range(attempts):
            if agent.index in self._cancelled or self._stopping:
                self.state.set_status(agent, Status.CANCELLED)
                return

            agent.attempt = attempt
            status = await self._attempt(agent)

            if status in (Status.SUCCEEDED, Status.CANCELLED):
                self.state.set_status(agent, status)
                return
            if attempt < attempts - 1:
                self.state.set_status(agent, Status.RETRYING)
                backoff = spec.retry_backoff * (2**attempt)
                self.state.push_feed(
                    "warn",
                    agent.index,
                    f"attempt {attempt + 1}/{attempts} failed "
                    f"({Status.name(status)}); retrying in {backoff:.1f}s",
                )
                try:
                    await asyncio.sleep(backoff)
                except asyncio.CancelledError:
                    self.state.set_status(agent, Status.CANCELLED)
                    raise
            else:
                self.state.set_status(agent, status)
                return

    async def _attempt(self, agent: AgentState) -> int:
        spec = self.spec.agent
        # Clear the previous attempt's verdict. Leaving it set makes a clean
        # retry inherit the old failure and report FAILED anyway.
        agent.error = None
        agent.exit_code = None
        self.state.set_status(agent, Status.STARTING)

        spec_dir = str(self.spec.source_path.parent) if self.spec.source_path else ""
        try:
            argv = render_argv(
                spec.command,
                task=agent.task,
                index=agent.index,
                agent_id=agent.id,
                attempt=agent.attempt,
                spec_dir=spec_dir,
            )
        except Exception as exc:
            agent.error = str(exc)
            return Status.FAILED

        request = LaunchRequest(
            argv=argv,
            env={**spec.env, **self._agent_env(agent)},
            cwd=spec.cwd or (spec_dir or None),
            merge_stderr=spec.merge_stderr,
            shutdown_grace=spec.shutdown_grace,
            image=spec.image,
        )

        try:
            handle = await self.runtime.launch(request)
        except (OSError, ValueError) as exc:
            agent.error = f"launch failed: {exc}"
            self.state.push_feed("error", agent.index, agent.error)
            return Status.FAILED

        agent.pid = handle.pid
        self._live[agent.index] = handle
        self._paused_total[agent.index] = 0.0
        self.state.set_status(agent, Status.RUNNING)

        consumer = asyncio.create_task(self._consume(agent, handle))
        watchdog: asyncio.Task | None = None
        timed_out = asyncio.Event()
        if spec.timeout:
            watchdog = asyncio.create_task(
                self._watchdog(agent, handle, spec.timeout, timed_out)
            )

        try:
            exit_code = await handle.wait()
        except asyncio.CancelledError:
            await handle.terminate(spec.shutdown_grace)
            raise
        finally:
            if watchdog is not None:
                watchdog.cancel()
                await asyncio.gather(watchdog, return_exceptions=True)
            # Drain whatever the agent wrote before exiting.
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(consumer, timeout=5.0)
            if not consumer.done():
                consumer.cancel()
                await asyncio.gather(consumer, return_exceptions=True)
            self._live.pop(agent.index, None)
            self._paused_since.pop(agent.index, None)

        agent.exit_code = exit_code
        agent.pid = None

        if agent.index in self._cancelled:
            return Status.CANCELLED
        if timed_out.is_set():
            agent.error = agent.error or f"timed out after {spec.timeout}s"
            return Status.TIMEOUT
        if exit_code != 0:
            agent.error = agent.error or f"exited with code {exit_code}"
            return Status.FAILED
        # An agent can exit 0 and still report failure in its result event;
        # _consume records that in agent.error.
        if agent.error:
            return Status.FAILED
        return Status.SUCCEEDED

    def _agent_env(self, agent: AgentState) -> dict[str, str]:
        task = agent.task
        return {
            "SWARMDECK_SWARM_ID": self.swarm_id,
            "SWARMDECK_AGENT_ID": agent.id,
            "SWARMDECK_AGENT_INDEX": str(agent.index),
            "SWARMDECK_ATTEMPT": str(agent.attempt),
            "SWARMDECK_TOTAL": str(self.state.agg.total),
            "SWARMDECK_TASK": (
                task if isinstance(task, str) else json.dumps(task, default=str)
            ),
            "SWARMDECK_RUN_DIR": str(self.run_dir),
        }

    async def _consume(self, agent: AgentState, handle: Any) -> None:
        apply = self.state.apply
        append = self.logs.append
        index = agent.index
        try:
            async for line in handle.lines():
                if not line.text:
                    continue
                ev: Event = parse_line(line.text, index)
                if ev.type == EventType.LOG:
                    level = "warn" if (line.stderr and ev.level == "info") else ev.level
                    ev.level = level
                    append(index, level, ev.msg)
                apply(ev)
                if ev.type == EventType.RESULT and ev.ok is False:
                    agent.error = ev.error or "agent reported failure"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("output consumer for %s stopped: %s", agent.id, exc)

    async def _watchdog(
        self, agent: AgentState, handle: Any, timeout: float, flag: asyncio.Event
    ) -> None:
        while True:
            await asyncio.sleep(WATCHDOG_TICK)
            started = agent.started_at
            if started is None:
                continue
            paused = self._paused_total.get(agent.index, 0.0)
            if agent.index in self._paused_since:
                paused += time.time() - self._paused_since[agent.index]
            if (time.time() - started - paused) > timeout:
                flag.set()
                self.state.push_feed(
                    "warn", agent.index, f"timed out after {timeout}s -- terminating"
                )
                await handle.terminate(self.spec.agent.shutdown_grace)
                return

    # -- operator controls ----------------------------------------------

    def _resolve(self, selector: dict[str, Any] | None) -> list[AgentState]:
        selector = selector or {}
        return self.state.select(
            indices=selector.get("indices"),
            status=selector.get("status"),
            tag=selector.get("tag"),
        )

    def pause(self, selector: dict[str, Any] | None = None) -> int:
        affected = 0
        for agent in self._resolve(selector):
            handle = self._live.get(agent.index)
            if handle is not None and handle.pause():
                self._paused_since[agent.index] = time.time()
                self.state.set_status(agent, Status.PAUSED)
                affected += 1
        if affected and not selector:
            self.state.phase = "paused"
        return affected

    def resume(self, selector: dict[str, Any] | None = None) -> int:
        affected = 0
        for agent in self._resolve(selector):
            handle = self._live.get(agent.index)
            if handle is not None and handle.resume():
                since = self._paused_since.pop(agent.index, None)
                if since is not None:
                    self._paused_total[agent.index] = self._paused_total.get(
                        agent.index, 0.0
                    ) + (time.time() - since)
                self.state.set_status(agent, Status.RUNNING)
                affected += 1
        if self.state.phase == "paused":
            self.state.phase = "running"
        return affected

    async def cancel(self, selector: dict[str, Any] | None = None) -> int:
        handles = []
        affected = 0
        for agent in self._resolve(selector):
            # Agents that already finished are not cancellable; counting them
            # would report "cancelled 100" for a selection where 71 had
            # already succeeded.
            if agent.status in Status.TERMINAL:
                continue
            affected += 1
            self._cancelled.add(agent.index)
            handle = self._live.get(agent.index)
            if handle is not None:
                handles.append(handle)
            else:
                self.state.set_status(agent, Status.CANCELLED)
        if handles:
            await asyncio.gather(
                *(h.terminate(self.spec.agent.shutdown_grace) for h in handles),
                return_exceptions=True,
            )
        return affected

    def retry(self, selector: dict[str, Any] | None = None) -> int:
        """Requeue terminal agents. This is the whole point of bulk selectors:
        `retry(status="failed")` after a provider outage beats 300 clicks."""
        requeued = 0
        for agent in self._resolve(selector):
            if agent.status not in Status.TERMINAL:
                continue
            self._cancelled.discard(agent.index)
            agent.exit_code = None
            agent.error = None
            agent.ended_at = None
            agent.started_at = None
            agent.progress = 0.0
            agent.result = None
            self.state.set_status(agent, Status.PENDING)
            self._queue.put_nowait(agent.index)
            self._outstanding += 1
            requeued += 1
        if requeued:
            if self.state.phase == "finished":
                self.state.phase = "running"
                self.state.finished_at = None
                self.state.agg.ended_at = None
                self._done = asyncio.Event()
            self._scale_workers()
        return requeued

    def set_concurrency(self, value: int) -> int:
        value = max(1, min(int(value), self.state.agg.total))
        self._target_workers = value
        self.spec.concurrency = value
        self._scale_workers()
        return value

    def extend(self, tasks: Sequence[Any]) -> int:
        """Append new agents to a swarm that is already running."""
        start = len(self.state.agents)
        for offset, task in enumerate(tasks):
            index = start + offset
            agent = AgentState(index=index, id=f"a{index:05d}", task=task)
            self.state.add_agent(agent)
            self._queue.put_nowait(index)
            self._outstanding += 1
        if tasks:
            if self.state.phase == "finished":
                self.state.phase = "running"
                self._done = asyncio.Event()
            self._scale_workers()
        return len(tasks)

    # -- read paths ------------------------------------------------------

    def agent_detail(self, index: int) -> dict[str, Any] | None:
        if not 0 <= index < len(self.state.agents):
            return None
        detail = self.state.agents[index].detail()
        detail["logs"] = self.logs.tail(index)
        return detail

    def results(self) -> Iterable[dict[str, Any]]:
        for agent in self.state.agents:
            yield {
                "index": agent.index,
                "id": agent.id,
                "status": Status.name(agent.status),
                "task": agent.task,
                "result": agent.result,
                "error": agent.error,
                "duration": agent.duration(),
                "attempt": agent.attempt,
                "tokens_in": agent.tokens_in,
                "tokens_out": agent.tokens_out,
                "cost_usd": agent.cost_usd,
            }

    def write_results(self, path: Path | None = None) -> Path:
        path = path or (self.run_dir / "results.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            for row in self.results():
                fh.write(json.dumps(row, default=str, ensure_ascii=False) + "\n")
        return path
