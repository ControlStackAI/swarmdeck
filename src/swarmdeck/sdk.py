"""Optional helper for Python agents.

Entirely optional. The protocol is "print a JSON line", so a three-line
agent needs nothing from this module::

    print(json.dumps({"t": "progress", "pct": 0.5}), flush=True)

This just makes the common calls pleasant and keeps the key names correct.
Agents in other languages should emit the same shapes; see PROTOCOL.md.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

__all__ = [
    "agent_id",
    "agent_index",
    "artifact",
    "edge",
    "emit",
    "label",
    "log",
    "metric",
    "progress",
    "result",
    "swarm_id",
    "task",
]


def emit(event_type: str, **fields: Any) -> None:
    """Write one protocol event. Flushed immediately -- a buffered event is
    an event the dashboard does not have."""
    payload = {"t": event_type, **{k: v for k, v in fields.items() if v is not None}}
    sys.stdout.write(json.dumps(payload, default=str, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def log(msg: str, level: str = "info") -> None:
    emit("log", msg=msg, level=level)


def progress(pct: float, step: str | None = None) -> None:
    """Report completion between 0 and 1 (values above 1 are read as percent)."""
    emit("progress", pct=pct, step=step)


def metric(**values: float) -> None:
    """Add to counters. ``metric(tokens_in=500, cost_usd=0.002)``.

    Values accumulate; pass ``set=True`` to overwrite instead.
    """
    emit("metric", **values)


def result(ok: bool = True, data: Any = None, error: str | None = None) -> None:
    emit("result", ok=ok, data=data, error=error)


def edge(to: str | int, kind: str = "delegate") -> None:
    """Declare a relationship to another agent, drawn in the topology view."""
    emit("edge", to=str(to), kind=kind)


def artifact(name: str, path: str | None = None) -> None:
    emit("artifact", name=name, path=path)


def label(text: str) -> None:
    """Set the short caption shown on this agent's cell."""
    emit("label", text=text)


def task() -> Any:
    """This agent's slice of work, as provided by the spec."""
    raw = os.environ.get("SWARMDECK_TASK", "")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def agent_index() -> int:
    return int(os.environ.get("SWARMDECK_AGENT_INDEX", "0"))


def agent_id() -> str:
    return os.environ.get("SWARMDECK_AGENT_ID", "a00000")


def swarm_id() -> str:
    return os.environ.get("SWARMDECK_SWARM_ID", "")
