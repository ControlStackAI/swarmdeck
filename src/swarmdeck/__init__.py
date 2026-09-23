"""swarmdeck -- launch, control, and visualize agent swarms."""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = [
    "AgentSpec",
    "AgentState",
    "Event",
    "SpecError",
    "Status",
    "Supervisor",
    "SwarmSpec",
    "SwarmState",
    "__version__",
    "load_spec",
    "parse_spec",
]


def __getattr__(name: str):
    # Lazy so that `import swarmdeck` inside an agent process stays cheap and
    # does not drag in the server stack.
    if name in ("AgentSpec", "SwarmSpec", "SpecError", "load_spec", "parse_spec"):
        from . import spec as _spec

        return getattr(_spec, name)
    if name in ("AgentState", "SwarmState", "Status"):
        from . import state as _state

        return getattr(_state, name)
    if name == "Event":
        from .events import Event

        return Event
    if name == "Supervisor":
        from .supervisor import Supervisor

        return Supervisor
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
