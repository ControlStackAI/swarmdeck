"""In-memory swarm state and the delta protocol that feeds the dashboard.

The scale constraint shapes this whole module. At 1000 agents an
object-per-agent JSON snapshot is roughly a megabyte, and pushing that at
10Hz is 10MB/s to every connected browser. So state is stored in ``__slots__``
objects, serialised column-wise as arrays, and only agents whose revision
moved since the client's last acknowledged version are sent.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Final

from .events import Event, EventType


class Status:
    """Agent lifecycle states, as ints because they go on the wire."""

    PENDING = 0
    STARTING = 1
    RUNNING = 2
    SUCCEEDED = 3
    FAILED = 4
    CANCELLED = 5
    TIMEOUT = 6
    PAUSED = 7
    RETRYING = 8

    NAMES: Final = {
        PENDING: "pending",
        STARTING: "starting",
        RUNNING: "running",
        SUCCEEDED: "succeeded",
        FAILED: "failed",
        CANCELLED: "cancelled",
        TIMEOUT: "timeout",
        PAUSED: "paused",
        RETRYING: "retrying",
    }
    TERMINAL: Final = frozenset({SUCCEEDED, FAILED, CANCELLED, TIMEOUT})
    LIVE: Final = frozenset({STARTING, RUNNING, PAUSED})

    @classmethod
    def name(cls, value: int) -> str:
        return cls.NAMES.get(value, "unknown")


#: Wire column order for an agent row. Clients receive this in the ``hello``
#: frame rather than hardcoding it, so adding a column is backwards
#: compatible.
AGENT_COLUMNS: Final = (
    "i",  # index
    "s",  # status int
    "p",  # progress 0-1
    "a",  # attempt
    "ti",  # tokens_in
    "to",  # tokens_out
    "c",  # cost_usd
    "d",  # duration seconds (live or final)
    "m",  # short label / last message
    "e",  # exit code (null while running)
)


@dataclass(slots=True)
class AgentState:
    index: int
    id: str
    status: int = Status.PENDING
    attempt: int = 0
    progress: float = 0.0
    pid: int | None = None
    started_at: float | None = None
    ended_at: float | None = None
    exit_code: int | None = None
    label: str = ""
    error: str | None = None
    result: Any = None
    tokens_in: float = 0.0
    tokens_out: float = 0.0
    cost_usd: float = 0.0
    metrics: dict[str, float] = field(default_factory=dict)
    edges: list[tuple[str, str]] = field(default_factory=list)
    artifacts: list[tuple[str, str | None]] = field(default_factory=list)
    task: Any = None
    tags: tuple[str, ...] = ()
    log_count: int = 0
    dropped_logs: int = 0
    #: Bumped on every observable change; the delta cursor compares against it.
    rev: int = 0

    def duration(self, now: float | None = None) -> float:
        if self.started_at is None:
            return 0.0
        end = self.ended_at if self.ended_at is not None else (now or time.time())
        return max(0.0, end - self.started_at)

    def row(self, now: float) -> list[Any]:
        return [
            self.index,
            self.status,
            round(self.progress, 4),
            self.attempt,
            self.tokens_in,
            self.tokens_out,
            round(self.cost_usd, 6),
            round(self.duration(now), 2),
            self.label,
            self.exit_code,
        ]

    def detail(self) -> dict[str, Any]:
        """Full record for the inspector panel. Never sent in bulk."""
        return {
            "index": self.index,
            "id": self.id,
            "status": Status.name(self.status),
            "attempt": self.attempt,
            "progress": self.progress,
            "pid": self.pid,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration": self.duration(),
            "exit_code": self.exit_code,
            "label": self.label,
            "error": self.error,
            "result": self.result,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "cost_usd": self.cost_usd,
            "metrics": dict(self.metrics),
            "edges": [{"to": t, "kind": k} for t, k in self.edges],
            "artifacts": [{"name": n, "path": p} for n, p in self.artifacts],
            "task": self.task,
            "tags": list(self.tags),
            "log_count": self.log_count,
            "dropped_logs": self.dropped_logs,
        }


@dataclass(slots=True)
class Aggregates:
    """Whole-swarm rollup. Maintained incrementally, never recomputed."""

    total: int = 0
    by_status: dict[int, int] = field(default_factory=dict)
    tokens_in: float = 0.0
    tokens_out: float = 0.0
    cost_usd: float = 0.0
    started_at: float | None = None
    ended_at: float | None = None
    completed_durations_sum: float = 0.0
    completed_count: int = 0
    dropped_events: int = 0
    peak_running: int = 0

    def as_dict(self, now: float) -> dict[str, Any]:
        by_name = {Status.name(k): v for k, v in self.by_status.items() if v}
        running = self.by_status.get(Status.RUNNING, 0) + self.by_status.get(Status.STARTING, 0)
        done = sum(self.by_status.get(s, 0) for s in Status.TERMINAL)
        elapsed = 0.0
        if self.started_at is not None:
            elapsed = (self.ended_at or now) - self.started_at
        return {
            "total": self.total,
            "by_status": by_name,
            "running": running,
            "done": done,
            "pending": self.total - done - running,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "cost_usd": round(self.cost_usd, 6),
            "elapsed": round(elapsed, 2),
            "throughput": round(done / elapsed, 3) if elapsed > 0 else 0.0,
            "mean_duration": (
                round(self.completed_durations_sum / self.completed_count, 2)
                if self.completed_count
                else 0.0
            ),
            "peak_running": self.peak_running,
            "dropped_events": self.dropped_events,
        }


class SwarmState:
    """Authoritative state for one swarm run."""

    def __init__(self, swarm_id: str, name: str, agents: list[AgentState]) -> None:
        self.swarm_id = swarm_id
        self.name = name
        self.agents = agents
        self.agg = Aggregates(total=len(agents))
        self.agg.by_status[Status.PENDING] = len(agents)
        self.version = 0
        self.phase = "created"  # created | running | paused | stopping | finished
        self.created_at = time.time()
        self.finished_at: float | None = None
        self._by_id = {a.id: a for a in agents}
        # Ring of recent notable lines for the global activity feed; the
        # per-agent log firehose never lands here.
        self.feed: list[dict[str, Any]] = []
        self._feed_max = 300

    # -- mutation -------------------------------------------------------

    def _touch(self, agent: AgentState) -> None:
        self.version += 1
        agent.rev = self.version

    def by_id(self, agent_id: str) -> AgentState | None:
        return self._by_id.get(agent_id)

    def add_agent(self, agent: AgentState) -> None:
        """Append an agent to a swarm that may already be running."""
        self.agents.append(agent)
        self._by_id[agent.id] = agent
        self.agg.total += 1
        self.agg.by_status[Status.PENDING] = self.agg.by_status.get(Status.PENDING, 0) + 1
        self._touch(agent)

    def set_status(self, agent: AgentState, status: int) -> None:
        if agent.status == status:
            return
        old = agent.status
        self.agg.by_status[old] = max(0, self.agg.by_status.get(old, 0) - 1)
        self.agg.by_status[status] = self.agg.by_status.get(status, 0) + 1
        agent.status = status

        now = time.time()
        if status == Status.STARTING and agent.started_at is None:
            agent.started_at = now
            if self.agg.started_at is None:
                self.agg.started_at = now
        if status in Status.TERMINAL:
            agent.ended_at = now
            self.agg.completed_durations_sum += agent.duration(now)
            self.agg.completed_count += 1
            if status == Status.SUCCEEDED:
                agent.progress = 1.0

        live = sum(self.agg.by_status.get(s, 0) for s in (Status.RUNNING, Status.STARTING))
        if live > self.agg.peak_running:
            self.agg.peak_running = live

        self._touch(agent)

    def apply(self, ev: Event) -> None:
        """Fold one agent event into state."""
        agent = self.agents[ev.agent_index]

        if ev.type == EventType.PROGRESS:
            if ev.pct is not None:
                agent.progress = ev.pct
            if ev.step:
                agent.label = ev.step
            self._touch(agent)

        elif ev.type == EventType.METRIC:
            for key, value in ev.metrics.items():
                if key == "tokens_in":
                    delta = value - agent.tokens_in if ev.absolute else value
                    agent.tokens_in += delta
                    self.agg.tokens_in += delta
                elif key == "tokens_out":
                    delta = value - agent.tokens_out if ev.absolute else value
                    agent.tokens_out += delta
                    self.agg.tokens_out += delta
                elif key == "cost_usd":
                    delta = value - agent.cost_usd if ev.absolute else value
                    agent.cost_usd += delta
                    self.agg.cost_usd += delta
                else:
                    agent.metrics[key] = (
                        value if ev.absolute else agent.metrics.get(key, 0.0) + value
                    )
            self._touch(agent)

        elif ev.type == EventType.RESULT:
            agent.result = ev.data
            if ev.error:
                agent.error = ev.error
            self._touch(agent)

        elif ev.type == EventType.EDGE:
            if ev.to and len(agent.edges) < 64:
                agent.edges.append((ev.to, ev.kind or "delegate"))
                self._touch(agent)

        elif ev.type == EventType.ARTIFACT:
            if len(agent.artifacts) < 64:
                agent.artifacts.append((ev.name or "artifact", ev.path))
                self._touch(agent)

        elif ev.type == EventType.LABEL:
            agent.label = ev.text or ""
            self._touch(agent)

        elif ev.type == EventType.STATUS:
            if ev.state:
                agent.label = ev.state
                self._touch(agent)

        elif ev.type == EventType.LOG:
            agent.log_count += 1
            if ev.level in ("warn", "error"):
                agent.label = ev.msg[:64]
                self.push_feed(ev.level, ev.agent_index, ev.msg)
                self._touch(agent)

    def push_feed(self, level: str, index: int, msg: str) -> None:
        self.feed.append({"ts": time.time(), "level": level, "index": index, "msg": msg[:400]})
        if len(self.feed) > self._feed_max:
            del self.feed[: len(self.feed) - self._feed_max]

    # -- serialisation --------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Everything, for a new or desynced client."""
        now = time.time()
        return {
            "type": "snapshot",
            "v": self.version,
            "swarm_id": self.swarm_id,
            "name": self.name,
            "phase": self.phase,
            "cols": list(AGENT_COLUMNS),
            "rows": [a.row(now) for a in self.agents],
            "agg": self.agg.as_dict(now),
            "feed": self.feed[-50:],
        }

    def delta(self, since: int) -> dict[str, Any]:
        """Only what changed after version ``since``."""
        now = time.time()
        rows = [a.row(now) for a in self.agents if a.rev > since]
        return {
            "type": "delta",
            "v": self.version,
            "phase": self.phase,
            "rows": rows,
            "agg": self.agg.as_dict(now),
            "feed": [f for f in self.feed if f["ts"] > now - 2.0][-25:],
        }

    def changed_since(self, since: int) -> bool:
        return self.version > since

    def summary(self) -> dict[str, Any]:
        now = time.time()
        return {
            "swarm_id": self.swarm_id,
            "name": self.name,
            "phase": self.phase,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "agg": self.agg.as_dict(now),
        }

    def select(
        self,
        *,
        indices: Iterable[int] | None = None,
        status: str | None = None,
        tag: str | None = None,
    ) -> list[AgentState]:
        """Resolve an operator selector to concrete agents.

        Bulk control at this scale is only usable if you can say "retry
        everything that failed" rather than clicking 300 cells.
        """
        out = self.agents
        if indices is not None:
            wanted = set(indices)
            out = [a for a in out if a.index in wanted]
        if status is not None:
            want = {k for k, v in Status.NAMES.items() if v == status}
            out = [a for a in out if a.status in want]
        if tag is not None:
            out = [a for a in out if tag in a.tags]
        return list(out)
