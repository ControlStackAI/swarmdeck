import sys

import pytest
import yaml
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from swarmdeck.server import ServerConfig, create_app

pytestmark = pytest.mark.asyncio


def make_client(tmp_path, **cfg):
    specs = tmp_path / "specs"
    specs.mkdir(exist_ok=True)
    (specs / "quick.swarm.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "quick",
                "command": [sys.executable, "-u", "-c", "print('ok')"],
                "tasks": 3,
                "concurrency": 2,
            }
        )
    )
    config = ServerConfig(root=tmp_path / "root", spec_dir=specs, tick=0.02, **cfg)
    return TestClient(create_app(config)), specs


async def test_health_reports_the_wire_contract(tmp_path):
    client, _ = make_client(tmp_path)
    with client:
        body = client.get("/api/health").json()
    assert body["ok"] is True
    # The UI reads columns and statuses from here rather than hardcoding them.
    assert body["columns"][0] == "i"
    assert body["statuses"]["3"] == "succeeded"
    assert "process" in body["runtimes"]


async def test_specs_are_listed_with_parsed_detail(tmp_path):
    client, _ = make_client(tmp_path)
    with client:
        specs = client.get("/api/specs").json()
    assert len(specs) == 1
    assert specs[0]["name"] == "quick"
    assert specs[0]["agents"] == 3


async def test_launch_run_and_inspect(tmp_path):
    client, specs = make_client(tmp_path)
    with client:
        res = client.post("/api/swarms", json={"spec_path": str(specs / "quick.swarm.yaml")})
        assert res.status_code == 201
        swarm_id = res.json()["swarm_id"]

        snapshot = client.get(f"/api/swarms/{swarm_id}").json()
        assert snapshot["type"] == "snapshot"
        assert len(snapshot["rows"]) == 3

        agent = client.get(f"/api/swarms/{swarm_id}/agents/0").json()
        assert agent["id"] == "a00000"
        assert "logs" in agent

        assert client.get(f"/api/swarms/{swarm_id}/agents/99").status_code == 404
        assert len(client.get(f"/api/swarms/{swarm_id}/results").json()) == 3


async def test_launch_is_confined_to_the_spec_directory(tmp_path):
    """Otherwise the endpoint reads and executes arbitrary files on the host."""
    client, _ = make_client(tmp_path)
    outside = tmp_path / "outside.swarm.yaml"
    outside.write_text(yaml.safe_dump({"command": ["echo", "hi"], "tasks": 1}))
    with client:
        res = client.post("/api/swarms", json={"spec_path": str(outside)})
    assert res.status_code == 400
    assert "must live under" in res.json()["detail"]


async def test_launch_can_be_disabled_entirely(tmp_path):
    client, specs = make_client(tmp_path, allow_launch=False)
    with client:
        res = client.post("/api/swarms", json={"spec_path": str(specs / "quick.swarm.yaml")})
    assert res.status_code == 403


async def test_token_is_required_when_configured(tmp_path):
    client, _ = make_client(tmp_path, token="s3cret")
    with client:
        assert client.get("/api/swarms").status_code == 401
        assert client.get("/api/health").status_code == 200  # health stays open
        ok = client.get("/api/swarms", headers={"Authorization": "Bearer s3cret"})
        assert ok.status_code == 200
        assert client.get("/api/swarms?token=s3cret").status_code == 200
        assert client.get("/api/swarms?token=wrong").status_code == 401


async def test_unknown_swarm_and_action_are_rejected(tmp_path):
    client, specs = make_client(tmp_path)
    with client:
        assert client.get("/api/swarms/nope").status_code == 404
        swarm_id = client.post(
            "/api/swarms", json={"spec_path": str(specs / "quick.swarm.yaml")}
        ).json()["swarm_id"]
        res = client.post(f"/api/swarms/{swarm_id}/control", json={"action": "explode"})
        assert res.status_code == 400


async def test_concurrency_control_requires_a_value(tmp_path):
    client, specs = make_client(tmp_path)
    with client:
        swarm_id = client.post(
            "/api/swarms", json={"spec_path": str(specs / "quick.swarm.yaml")}
        ).json()["swarm_id"]
        assert (
            client.post(
                f"/api/swarms/{swarm_id}/control", json={"action": "concurrency"}
            ).status_code
            == 400
        )
        res = client.post(
            f"/api/swarms/{swarm_id}/control",
            json={"action": "concurrency", "value": 2},
        )
        assert res.json()["concurrency"] == 2


async def test_websocket_sends_hello_then_snapshot(tmp_path):
    client, specs = make_client(tmp_path)
    with client:
        swarm_id = client.post(
            "/api/swarms", json={"spec_path": str(specs / "quick.swarm.yaml")}
        ).json()["swarm_id"]

        with client.websocket_connect(f"/ws/swarms/{swarm_id}") as ws:
            hello = ws.receive_json()
            assert hello["type"] == "hello"
            assert hello["total"] == 3
            assert hello["cols"][0] == "i"

            snapshot = ws.receive_json()
            assert snapshot["type"] == "snapshot"
            assert len(snapshot["rows"]) == 3
            # Rows must match the declared column count exactly.
            assert all(len(r) == len(hello["cols"]) for r in snapshot["rows"])


async def test_websocket_rejects_a_bad_token(tmp_path):
    client, specs = make_client(tmp_path, token="s3cret")
    with client:
        swarm_id = client.post(
            "/api/swarms",
            json={"spec_path": str(specs / "quick.swarm.yaml")},
            headers={"Authorization": "Bearer s3cret"},
        ).json()["swarm_id"]
        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect(f"/ws/swarms/{swarm_id}?token=wrong"),
        ):
            pass


async def test_websocket_rejects_cross_origin_browser_clients(tmp_path):
    client, specs = make_client(tmp_path)
    with client:
        swarm_id = client.post(
            "/api/swarms", json={"spec_path": str(specs / "quick.swarm.yaml")}
        ).json()["swarm_id"]
        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect(
                f"/ws/swarms/{swarm_id}", headers={"origin": "https://evil.example"}
            ),
        ):
            pass


async def test_delete_removes_the_swarm(tmp_path):
    client, specs = make_client(tmp_path)
    with client:
        swarm_id = client.post(
            "/api/swarms", json={"spec_path": str(specs / "quick.swarm.yaml")}
        ).json()["swarm_id"]
        assert client.delete(f"/api/swarms/{swarm_id}").json()["ok"] is True
        assert client.get(f"/api/swarms/{swarm_id}").status_code == 404
