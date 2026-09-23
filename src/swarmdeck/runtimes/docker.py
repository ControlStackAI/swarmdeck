"""Docker runtime -- one container per agent, via the `docker` CLI.

Shelling out to `docker` rather than depending on the SDK keeps the core
dependency-free and works identically with podman (set
``SWARMDECK_DOCKER_BIN=podman``). Container startup is ~200ms, so this trades
launch latency for isolation; use it when agents run untrusted code.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import uuid
from collections.abc import AsyncIterator

from ..background import spawn
from .base import Handle, LaunchRequest, OutputLine, Runtime, register
from .process import ProcessHandle, ProcessRuntime

_BIN = os.environ.get("SWARMDECK_DOCKER_BIN", "docker")


class ContainerHandle(Handle):
    """Wraps the `docker run` client process and the container it controls."""

    __slots__ = ("_container", "_inner", "_paused")

    def __init__(self, inner: ProcessHandle, container: str) -> None:
        self._inner = inner
        self._container = container
        self._paused = False

    @property
    def pid(self) -> int | None:
        return self._inner.pid

    def lines(self) -> AsyncIterator[OutputLine]:
        return self._inner.lines()

    async def wait(self) -> int:
        return await self._inner.wait()

    async def _docker(self, *args: str) -> bool:
        try:
            proc = await asyncio.create_subprocess_exec(
                _BIN,
                *args,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            return await proc.wait() == 0
        except OSError:
            return False

    async def terminate(self, grace: float) -> None:
        # Killing the client leaves the container running, so the container
        # has to be stopped explicitly.
        await self._docker("stop", "-t", str(int(grace)), self._container)
        await self._inner.terminate(grace)

    def pause(self) -> bool:
        if self._paused:
            return False
        self._paused = True
        spawn(self._docker("pause", self._container), name="docker-pause")
        return True

    def resume(self) -> bool:
        if not self._paused:
            return False
        self._paused = False
        spawn(self._docker("unpause", self._container), name="docker-unpause")
        return True


@register
class DockerRuntime(Runtime):
    name = "docker"

    async def launch(self, request: LaunchRequest) -> Handle:
        if not request.image:
            raise ValueError("docker runtime requires an 'image:' in the agent spec")

        container = f"swarmdeck-{uuid.uuid4().hex[:12]}"
        argv = [
            _BIN,
            "run",
            "--rm",
            "--name",
            container,
            "--init",
            "-a",
            "stdout",
            "-a",
            "stderr",
        ]
        for key, value in request.env.items():
            argv += ["-e", f"{key}={value}"]
        if request.cwd:
            argv += ["-w", request.cwd]
        argv.append(request.image)
        argv += request.argv

        inner = await ProcessRuntime().launch(
            LaunchRequest(
                argv=argv,
                env={},
                cwd=None,
                merge_stderr=request.merge_stderr,
                shutdown_grace=request.shutdown_grace,
            )
        )
        assert isinstance(inner, ProcessHandle)
        return ContainerHandle(inner, container)

    async def preflight(self, concurrency: int) -> list[str]:
        warnings = await ProcessRuntime().preflight(concurrency)
        if shutil.which(_BIN) is None:
            warnings.append(f"{_BIN!r} not found on PATH")
        if concurrency > 200:
            warnings.append(
                f"{concurrency} concurrent containers is a lot of daemon "
                f"pressure; the process runtime scales further if you do not "
                f"need isolation"
            )
        return warnings
