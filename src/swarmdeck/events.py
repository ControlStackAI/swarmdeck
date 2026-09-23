"""The swarmdeck event protocol.

Agents talk to the control plane by writing newline-delimited JSON to stdout.
There is no SDK requirement and no wire handshake: any process that prints
text is a valid agent, and any line that is not a recognised JSON event is
recorded as a log line. Structured reporting is strictly opt-in.

    {"t": "progress", "pct": 0.4}
    {"t": "metric", "tokens_in": 812, "cost_usd": 0.0031}
    {"t": "result", "ok": true, "data": {"answer": 42}}

Keeping the protocol this thin is deliberate. The control plane has to stay
useful when the thing it supervises is a shell one-liner, a Go binary, or an
LLM framework nobody has written an adapter for yet.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Final

# A single agent can produce pathological output (a stack trace with an
# embedded base64 blob, a runaway loop). Lines are truncated rather than
# dropped so the operator still sees that something went wrong.
MAX_LINE_BYTES: Final = 64 * 1024
MAX_MSG_CHARS: Final = 2000
MAX_LABEL_CHARS: Final = 64

LOG_LEVELS: Final = ("debug", "info", "warn", "error")

#: Metric keys the dashboard understands natively. Anything else an agent
#: reports is still stored, but lives under ``Event.extra`` and is only
#: visible in the inspector.
KNOWN_METRICS: Final = (
    "tokens_in",
    "tokens_out",
    "cost_usd",
    "latency_ms",
    "tool_calls",
    "errors",
)


class EventType:
    STATUS = "status"
    LOG = "log"
    METRIC = "metric"
    PROGRESS = "progress"
    RESULT = "result"
    EDGE = "edge"
    ARTIFACT = "artifact"
    LABEL = "label"

    ALL: Final = (STATUS, LOG, METRIC, PROGRESS, RESULT, EDGE, ARTIFACT, LABEL)

    #: Lifecycle events are never shed under backpressure — losing one would
    #: leave an agent stuck in the UI forever. Log events are droppable.
    CRITICAL: Final = (STATUS, RESULT, PROGRESS, METRIC, EDGE)


@dataclass(slots=True)
class Event:
    """One decoded line from an agent."""

    type: str
    agent_index: int
    ts: float = field(default_factory=time.time)
    # log
    level: str = "info"
    msg: str = ""
    # progress
    pct: float | None = None
    step: str | None = None
    # status / result
    state: str | None = None
    ok: bool | None = None
    data: Any = None
    error: str | None = None
    # metric
    metrics: dict[str, float] = field(default_factory=dict)
    absolute: bool = False
    # edge
    to: str | None = None
    kind: str | None = None
    # artifact / label
    name: str | None = None
    path: str | None = None
    text: str | None = None
    # anything the agent sent that we do not model
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def droppable(self) -> bool:
        return self.type not in EventType.CRITICAL


def _clip(value: Any, limit: int) -> str:
    s = value if isinstance(value, str) else json.dumps(value, default=str)
    if len(s) > limit:
        return s[: limit - 1] + "…"
    return s


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        f = float(value)
        # NaN/inf would poison every aggregate downstream.
        return f if f == f and f not in (float("inf"), float("-inf")) else None
    return None


def parse_line(line: str, agent_index: int) -> Event:
    """Decode one line of agent output into an :class:`Event`.

    Never raises. Unparseable input becomes a log event, because an agent
    writing malformed output is a thing the operator needs to *see*, not a
    thing that should take down the supervisor reading it.
    """
    raw = line.rstrip("\r\n")
    if len(raw) > MAX_LINE_BYTES:
        raw = raw[:MAX_LINE_BYTES]

    stripped = raw.lstrip()
    if not stripped.startswith("{"):
        return Event(EventType.LOG, agent_index, msg=_clip(raw, MAX_MSG_CHARS))

    try:
        payload = json.loads(stripped)
    except (ValueError, RecursionError):
        return Event(EventType.LOG, agent_index, msg=_clip(raw, MAX_MSG_CHARS))

    if not isinstance(payload, dict):
        return Event(EventType.LOG, agent_index, msg=_clip(raw, MAX_MSG_CHARS))

    etype = payload.get("t") or payload.get("type")
    if etype not in EventType.ALL:
        # Valid JSON, but not ours. The agent is probably emitting its own
        # structured logs; keep them readable instead of discarding them.
        return Event(EventType.LOG, agent_index, msg=_clip(stripped, MAX_MSG_CHARS))

    return _build(etype, payload, agent_index)


def _build(etype: str, p: dict[str, Any], idx: int) -> Event:
    ev = Event(etype, idx)
    ts = _as_float(p.get("ts"))
    if ts is not None:
        ev.ts = ts

    if etype == EventType.LOG:
        level = str(p.get("level", "info")).lower()
        ev.level = level if level in LOG_LEVELS else "info"
        ev.msg = _clip(p.get("msg", p.get("message", "")), MAX_MSG_CHARS)

    elif etype == EventType.PROGRESS:
        pct = _as_float(p.get("pct", p.get("percent")))
        if pct is not None:
            # Accept 0-1 and 0-100; agents get this wrong constantly.
            if pct > 1.0:
                pct = pct / 100.0
            ev.pct = min(max(pct, 0.0), 1.0)
        step = p.get("step")
        if step is not None:
            ev.step = _clip(step, MAX_LABEL_CHARS)

    elif etype == EventType.STATUS:
        state = p.get("state")
        ev.state = _clip(state, MAX_LABEL_CHARS) if state is not None else None

    elif etype == EventType.METRIC:
        ev.absolute = bool(p.get("set", False))
        for key, value in p.items():
            if key in ("t", "type", "ts", "set"):
                continue
            f = _as_float(value)
            if f is not None:
                ev.metrics[str(key)[:48]] = f
            else:
                ev.extra[str(key)[:48]] = value

    elif etype == EventType.RESULT:
        ev.ok = bool(p.get("ok", True))
        ev.data = p.get("data")
        err = p.get("error")
        ev.error = _clip(err, MAX_MSG_CHARS) if err is not None else None

    elif etype == EventType.EDGE:
        to = p.get("to")
        ev.to = _clip(to, MAX_LABEL_CHARS) if to is not None else None
        ev.kind = _clip(p.get("kind", "delegate"), 24)

    elif etype == EventType.ARTIFACT:
        ev.name = _clip(p.get("name", "artifact"), MAX_LABEL_CHARS)
        path = p.get("path")
        ev.path = _clip(path, 512) if path is not None else None

    elif etype == EventType.LABEL:
        ev.text = _clip(p.get("text", ""), MAX_LABEL_CHARS)

    return ev
