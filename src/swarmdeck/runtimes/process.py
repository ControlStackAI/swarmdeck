"""Local subprocess runtime -- the default, and the one tuned for scale.

Two details matter more than anything else here:

* **Process groups.** Every agent gets its own session, so cancelling one
  kills the whole tree it spawned. Agent frameworks shell out constantly;
  without this you leak orphaned model calls that keep billing.
* **File descriptors.** Each live agent costs 2-3 fds in the parent. The
  default soft ``RLIMIT_NOFILE`` of 1024 caps you somewhere around 400
  concurrent agents, which is exactly the range this tool targets, so we
  raise the soft limit toward the hard limit and report what is achievable.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
from collections.abc import AsyncIterator

from .base import Handle, LaunchRequest, OutputLine, Runtime, register

#: asyncio's StreamReader buffer per pipe. Large enough that a JSON event
#: with an embedded document does not blow up mid-line.
STREAM_LIMIT = 1 << 20

#: Reserved for the event loop, the HTTP server, SQLite, and the log writer.
FD_HEADROOM = 128

_POSIX = os.name == "posix"
_HAS_STOP = _POSIX and hasattr(signal, "SIGSTOP")


def raise_fd_limit() -> tuple[int, int]:
    """Raise the soft fd limit to the hard limit. Returns (soft, hard)."""
    if not _POSIX:
        return (0, 0)
    import resource

    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft < hard:
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (hard, hard))
            soft = hard
        except (ValueError, OSError):
            pass
    return (soft, hard)


def max_concurrency(fds_per_agent: int = 3) -> int:
    if not _POSIX:
        return 512
    soft, _ = raise_fd_limit()
    if soft in (-1, 0) or soft > 1 << 30:  # RLIM_INFINITY
        return 100_000
    return max(1, (soft - FD_HEADROOM) // fds_per_agent)


class ProcessHandle(Handle):
    __slots__ = ("_merge", "_paused", "_pgid", "_proc")

    def __init__(self, proc: asyncio.subprocess.Process, merge_stderr: bool) -> None:
        self._proc = proc
        self._merge = merge_stderr
        self._paused = False
        self._pgid: int | None = None
        if _POSIX:
            with contextlib.suppress(OSError, ProcessLookupError):
                self._pgid = os.getpgid(proc.pid)

    @property
    def pid(self) -> int | None:
        return self._proc.pid

    async def _pump(
        self, reader: asyncio.StreamReader, queue: asyncio.Queue, is_err: bool
    ) -> None:
        try:
            while True:
                try:
                    raw = await reader.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    # Line exceeded STREAM_LIMIT. Drain what we can and keep
                    # going rather than abandoning the agent's output.
                    raw = await reader.read(STREAM_LIMIT)
                    if raw:
                        await queue.put(
                            OutputLine(raw.decode("utf-8", "replace").rstrip("\n"), is_err)
                        )
                    continue
                if not raw:
                    return
                await queue.put(OutputLine(raw.decode("utf-8", "replace").rstrip("\n"), is_err))
        finally:
            await queue.put(None)

    async def lines(self) -> AsyncIterator[OutputLine]:
        streams: list[tuple[asyncio.StreamReader, bool]] = []
        if self._proc.stdout is not None:
            streams.append((self._proc.stdout, False))
        if not self._merge and self._proc.stderr is not None:
            streams.append((self._proc.stderr, True))
        if not streams:
            return

        queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        pumps = [
            asyncio.create_task(self._pump(reader, queue, is_err)) for reader, is_err in streams
        ]
        remaining = len(pumps)
        try:
            while remaining:
                item = await queue.get()
                if item is None:
                    remaining -= 1
                    continue
                yield item
        finally:
            for task in pumps:
                task.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)

    async def wait(self) -> int:
        return await self._proc.wait()

    def _signal(self, sig: int) -> bool:
        if self._proc.returncode is not None:
            return False
        try:
            if self._pgid is not None:
                os.killpg(self._pgid, sig)
            else:
                self._proc.send_signal(sig)
            return True
        except (ProcessLookupError, PermissionError, OSError):
            return False

    async def terminate(self, grace: float) -> None:
        if self._proc.returncode is not None:
            return
        # A stopped process cannot handle SIGTERM; wake it first or it will
        # sit in the grace period and then get SIGKILLed for no reason.
        if self._paused:
            self.resume()
        self._signal(signal.SIGTERM)
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=grace)
        except asyncio.TimeoutError:
            self._signal(signal.SIGKILL if _POSIX else signal.SIGTERM)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._proc.wait(), timeout=2.0)

    def pause(self) -> bool:
        if not _HAS_STOP or self._paused:
            return False
        if self._signal(signal.SIGSTOP):
            self._paused = True
            return True
        return False

    def resume(self) -> bool:
        if not _HAS_STOP or not self._paused:
            return False
        if self._signal(signal.SIGCONT):
            self._paused = False
            return True
        return False


@register
class ProcessRuntime(Runtime):
    name = "process"

    async def launch(self, request: LaunchRequest) -> Handle:
        env = {**os.environ, **request.env}
        # Unbuffered child output; without this a Python agent's events sit
        # in a 4KB pipe buffer and the dashboard shows nothing for minutes.
        env.setdefault("PYTHONUNBUFFERED", "1")

        kwargs: dict = {
            "stdout": asyncio.subprocess.PIPE,
            "stderr": (
                asyncio.subprocess.STDOUT if request.merge_stderr else asyncio.subprocess.PIPE
            ),
            "stdin": asyncio.subprocess.DEVNULL,
            "cwd": request.cwd,
            "env": env,
            "limit": STREAM_LIMIT,
        }
        if _POSIX:
            kwargs["start_new_session"] = True

        proc = await asyncio.create_subprocess_exec(*request.argv, **kwargs)
        return ProcessHandle(proc, request.merge_stderr)

    async def preflight(self, concurrency: int) -> list[str]:
        warnings: list[str] = []
        if not _POSIX:
            warnings.append(
                "non-POSIX host: pause/resume and process-group cancellation are unavailable"
            )
            return warnings

        soft, hard = raise_fd_limit()
        ceiling = max_concurrency()
        if concurrency > ceiling:
            warnings.append(
                f"concurrency {concurrency} exceeds what this host's file "
                f"descriptor limit supports (~{ceiling}; soft={soft}, hard={hard}). "
                f"Raise it with `ulimit -n` or lower concurrency."
            )

        if not _HAS_STOP:
            warnings.append("SIGSTOP unavailable: pause will be a no-op")

        if sys.platform == "linux":
            try:
                with open("/proc/sys/kernel/pid_max", encoding="ascii") as fh:
                    pid_max = int(fh.read().strip())
                if concurrency * 4 > pid_max:
                    warnings.append(
                        f"concurrency {concurrency} is close to kernel.pid_max "
                        f"({pid_max}); agents that fork may fail to spawn"
                    )
            except (OSError, ValueError):
                pass

        return warnings
