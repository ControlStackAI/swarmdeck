"""HTTP + WebSocket API.

Security posture, stated plainly: this server starts processes described by
whoever can reach it. That is remote code execution by design, the same way
a CI runner is. So it binds loopback by default, and binding anywhere else
requires an explicit flag *and* a token. See SECURITY.md.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.websockets import WebSocketDisconnect

from .. import __version__
from ..registry import Registry
from ..runtimes import available as available_runtimes
from ..spec import SpecError, SwarmSpec, load_spec, parse_spec
from ..state import AGENT_COLUMNS, Status

log = logging.getLogger("swarmdeck.server")

#: Dashboard refresh rate. 10Hz reads as live to a human while keeping the
#: delta small enough that 1000 agents is a few KB per frame.
DEFAULT_TICK = 0.1

UI_DIR = Path(__file__).resolve().parent.parent / "ui"


class LaunchRequest(BaseModel):
    spec_path: str | None = None
    spec: dict[str, Any] | None = None
    concurrency: int | None = Field(default=None, ge=1)
    capture_logs: bool = True


class ControlRequest(BaseModel):
    action: str
    indices: list[int] | None = None
    status: str | None = None
    tag: str | None = None
    value: int | None = None


class ServerConfig:
    def __init__(
        self,
        *,
        root: Path,
        spec_dir: Path,
        token: str | None = None,
        allow_launch: bool = True,
        tick: float = DEFAULT_TICK,
    ) -> None:
        self.root = root
        self.spec_dir = spec_dir
        self.token = token
        self.allow_launch = allow_launch
        self.tick = tick


def create_app(config: ServerConfig) -> FastAPI:
    registry = Registry(config.root)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        # Never leave agent processes running after the server exits.
        await registry.shutdown_all()

    app = FastAPI(
        title="swarmdeck",
        version=__version__,
        description="Launch, control, and visualize agent swarms.",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    app.state.config = config
    app.state.registry = registry

    async def auth(request: Request) -> None:
        if not config.token:
            return
        header = request.headers.get("authorization", "")
        supplied = (
            header[7:]
            if header.lower().startswith("bearer ")
            else (request.query_params.get("token") or "")
        )
        if not secrets.compare_digest(supplied, config.token):
            raise HTTPException(status_code=401, detail="invalid or missing token")

    guard = [Depends(auth)]

    # -- meta ------------------------------------------------------------

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {
            "ok": True,
            "version": __version__,
            "runtimes": available_runtimes(),
            "auth_required": bool(config.token),
            "launch_enabled": config.allow_launch,
            "tick": config.tick,
            "columns": list(AGENT_COLUMNS),
            "statuses": Status.NAMES,
        }

    @app.get("/api/specs", dependencies=guard)
    async def list_specs() -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if config.spec_dir.is_dir():
            for path in sorted(config.spec_dir.rglob("*.y*ml")):
                entry: dict[str, Any] = {
                    "path": str(path),
                    "name": path.stem,
                    "size": path.stat().st_size,
                }
                try:
                    spec = load_spec(path)
                    entry.update(
                        name=spec.name,
                        description=spec.description,
                        agents=spec.size,
                        concurrency=spec.concurrency,
                        runtime=spec.agent.runtime,
                        command=" ".join(spec.agent.command),
                    )
                except SpecError as exc:
                    entry["error"] = str(exc)
                out.append(entry)
        return out

    # -- swarms ----------------------------------------------------------

    @app.get("/api/swarms", dependencies=guard)
    async def list_swarms() -> list[dict[str, Any]]:
        return registry.list()

    @app.post("/api/swarms", dependencies=guard, status_code=201)
    async def launch(body: LaunchRequest) -> dict[str, Any]:
        if not config.allow_launch:
            raise HTTPException(403, "launching over the API is disabled on this server")

        try:
            spec = _resolve_spec(body, config)
        except SpecError as exc:
            raise HTTPException(400, str(exc)) from exc

        if body.concurrency:
            spec.concurrency = min(body.concurrency, spec.size)

        sup = await registry.launch(spec, capture_logs=body.capture_logs)
        return {
            "swarm_id": sup.swarm_id,
            "name": spec.name,
            "agents": spec.size,
            "concurrency": spec.concurrency,
            "run_dir": str(sup.run_dir),
            "warnings": sup.warnings,
        }

    def _sup(swarm_id: str):
        sup = registry.get(swarm_id)
        if sup is None:
            raise HTTPException(404, f"no swarm {swarm_id!r}")
        return sup

    @app.get("/api/swarms/{swarm_id}", dependencies=guard)
    async def get_swarm(swarm_id: str) -> dict[str, Any]:
        sup = _sup(swarm_id)
        data = sup.state.snapshot()
        data["run_dir"] = str(sup.run_dir)
        data["warnings"] = sup.warnings
        data["concurrency"] = sup.spec.concurrency
        data["command"] = sup.spec.agent.command
        return data

    @app.get("/api/swarms/{swarm_id}/agents/{index}", dependencies=guard)
    async def get_agent(swarm_id: str, index: int) -> dict[str, Any]:
        detail = _sup(swarm_id).agent_detail(index)
        if detail is None:
            raise HTTPException(404, f"no agent {index}")
        return detail

    @app.get("/api/swarms/{swarm_id}/logs", dependencies=guard)
    async def get_logs(
        swarm_id: str, index: int = Query(...), limit: int = Query(200, le=5000)
    ) -> dict[str, Any]:
        sup = _sup(swarm_id)
        return {"index": index, "lines": sup.logs.tail(index, limit)}

    @app.get("/api/swarms/{swarm_id}/results", dependencies=guard)
    async def get_results(swarm_id: str) -> list[dict[str, Any]]:
        return list(_sup(swarm_id).results())

    @app.get("/api/swarms/{swarm_id}/graph", dependencies=guard)
    async def get_graph(swarm_id: str) -> dict[str, Any]:
        """Delegation topology, built only from edges agents actually declared."""
        sup = _sup(swarm_id)
        nodes, links = [], []
        for agent in sup.state.agents:
            if agent.edges or agent.status != Status.PENDING:
                nodes.append(
                    {
                        "id": agent.index,
                        "status": Status.name(agent.status),
                        "label": agent.label,
                    }
                )
            for target, kind in agent.edges:
                resolved = sup.state.by_id(target)
                tgt = resolved.index if resolved else _as_index(target)
                if tgt is not None:
                    links.append({"source": agent.index, "target": tgt, "kind": kind})
        return {"nodes": nodes, "links": links}

    @app.post("/api/swarms/{swarm_id}/control", dependencies=guard)
    async def control(swarm_id: str, body: ControlRequest) -> dict[str, Any]:
        sup = _sup(swarm_id)
        selector = {"indices": body.indices, "status": body.status, "tag": body.tag}
        selector = {k: v for k, v in selector.items() if v is not None} or None
        action = body.action

        if action == "pause":
            return {"affected": sup.pause(selector)}
        if action == "resume":
            return {"affected": sup.resume(selector)}
        if action == "cancel":
            return {"affected": await sup.cancel(selector)}
        if action == "retry":
            return {"affected": sup.retry(selector)}
        if action == "concurrency":
            if body.value is None:
                raise HTTPException(400, "concurrency requires 'value'")
            return {"concurrency": sup.set_concurrency(body.value)}
        raise HTTPException(400, f"unknown action {action!r}")

    @app.delete("/api/swarms/{swarm_id}", dependencies=guard)
    async def delete_swarm(swarm_id: str) -> dict[str, Any]:
        if not await registry.remove(swarm_id):
            raise HTTPException(404, f"no swarm {swarm_id!r}")
        return {"ok": True}

    # -- live stream -------------------------------------------------------

    @app.websocket("/ws/swarms/{swarm_id}")
    async def stream(websocket: WebSocket, swarm_id: str) -> None:
        # Browsers do not apply CORS to WebSockets. Without an explicit
        # same-origin check, a hostile website could read a loopback
        # dashboard's live agent state in the default tokenless mode.
        origin = websocket.headers.get("origin")
        if origin:
            parsed = urlsplit(origin)
            if parsed.scheme not in ("http", "https") or parsed.netloc != websocket.headers.get(
                "host", ""
            ):
                await websocket.close(code=4403, reason="origin not allowed")
                return
        if config.token:
            supplied = websocket.query_params.get("token", "")
            if not secrets.compare_digest(supplied, config.token):
                await websocket.close(code=4401, reason="invalid token")
                return

        sup = registry.get(swarm_id)
        if sup is None:
            await websocket.close(code=4404, reason="no such swarm")
            return

        await websocket.accept()
        state = sup.state
        try:
            await websocket.send_text(
                json.dumps(
                    {
                        "type": "hello",
                        "cols": list(AGENT_COLUMNS),
                        "statuses": Status.NAMES,
                        "tick": config.tick,
                        "swarm_id": swarm_id,
                        "name": state.name,
                        "total": state.agg.total,
                    }
                )
            )
            await websocket.send_text(json.dumps(state.snapshot()))
            last = state.version
            last_total = state.agg.total
            last_heartbeat = time.monotonic()

            while True:
                await asyncio.sleep(config.tick)
                # If agents were appended mid-run the client's row array is
                # the wrong shape, so resend everything rather than a delta
                # it cannot index.
                if state.agg.total != last_total:
                    last_total = state.agg.total
                    last = state.version
                    await websocket.send_text(json.dumps(state.snapshot()))
                    last_heartbeat = time.monotonic()
                    continue

                if state.changed_since(last):
                    payload = state.delta(last)
                    # Capture the version *before* awaiting the send: anything
                    # that changes mid-flight is simply included next tick.
                    last = state.version
                    await websocket.send_text(json.dumps(payload))
                    last_heartbeat = time.monotonic()
                elif time.monotonic() - last_heartbeat > 5.0:
                    await websocket.send_text(
                        json.dumps({"type": "heartbeat", "v": last, "phase": state.phase})
                    )
                    last_heartbeat = time.monotonic()
        except (WebSocketDisconnect, ConnectionError, RuntimeError):
            pass
        except asyncio.CancelledError:
            raise
        finally:
            with contextlib.suppress(Exception):
                await websocket.close()

    # -- static UI ---------------------------------------------------------

    index = UI_DIR / "index.html"
    if index.is_file():
        app.mount("/assets", StaticFiles(directory=UI_DIR / "assets"), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        async def spa(full_path: str) -> Any:
            candidate = (UI_DIR / full_path).resolve()
            if full_path and UI_DIR in candidate.parents and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(index)

    else:

        @app.get("/", include_in_schema=False)
        async def no_ui() -> JSONResponse:
            return JSONResponse(
                {
                    "error": "dashboard assets are not built",
                    "hint": "run `npm --prefix ui install && npm --prefix ui run build`, "
                    "or install the published wheel which ships them",
                    "api": "/api/docs",
                },
                status_code=503,
            )

    return app


def _as_index(value: str) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _resolve_spec(body: LaunchRequest, config: ServerConfig) -> SwarmSpec:
    if body.spec_path:
        path = Path(body.spec_path).expanduser().resolve()
        # Confine API-driven launches to the spec directory; otherwise the
        # endpoint reads (and runs) arbitrary files on the host.
        root = config.spec_dir.resolve()
        if root not in path.parents and path != root:
            raise SpecError(f"spec must live under {root}")
        return load_spec(path)
    if body.spec:
        return parse_spec(body.spec, base=config.spec_dir)
    raise SpecError("provide either 'spec_path' or 'spec'")


def default_token() -> str:
    return os.environ.get("SWARMDECK_TOKEN") or secrets.token_urlsafe(24)
