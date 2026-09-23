"""Runtime abstraction: how a single agent actually gets executed.

Swapping local processes for containers or remote workers should not touch
the supervisor, the state model, or the UI. Everything a runtime must
provide is here.
"""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator
from dataclasses import dataclass


@dataclass(slots=True)
class LaunchRequest:
    argv: list[str]
    env: dict[str, str]
    cwd: str | None
    merge_stderr: bool
    shutdown_grace: float
    image: str | None = None


@dataclass(slots=True)
class OutputLine:
    text: str
    stderr: bool


class Handle(abc.ABC):
    """A running agent."""

    @property
    @abc.abstractmethod
    def pid(self) -> int | None: ...

    @abc.abstractmethod
    def lines(self) -> AsyncIterator[OutputLine]:
        """Yield output lines until the agent's streams close."""

    @abc.abstractmethod
    async def wait(self) -> int:
        """Return the exit code."""

    @abc.abstractmethod
    async def terminate(self, grace: float) -> None:
        """Ask nicely, then insist."""

    @abc.abstractmethod
    def pause(self) -> bool: ...

    @abc.abstractmethod
    def resume(self) -> bool: ...


class Runtime(abc.ABC):
    name: str

    @abc.abstractmethod
    async def launch(self, request: LaunchRequest) -> Handle: ...

    async def preflight(self, concurrency: int) -> list[str]:
        """Return human-readable warnings before a run starts.

        Catching "you asked for 1000 agents but your fd limit is 1024" here
        is much kinder than discovering it 400 agents in.
        """
        return []


_REGISTRY: dict[str, type[Runtime]] = {}


def register(cls: type[Runtime]) -> type[Runtime]:
    _REGISTRY[cls.name] = cls
    return cls


def get_runtime(name: str) -> Runtime:
    try:
        cls = _REGISTRY[name]
    except KeyError:
        known = ", ".join(sorted(_REGISTRY)) or "none"
        raise ValueError(f"unknown runtime {name!r} (available: {known})") from None
    return cls()


def available() -> list[str]:
    return sorted(_REGISTRY)
