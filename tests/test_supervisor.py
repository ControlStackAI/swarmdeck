"""Integration tests. These launch real processes on purpose -- the whole
point of the supervisor is behaviour under real process semantics, and a
mocked subprocess would test none of it."""

import asyncio
import sys

import pytest

from swarmdeck.spec import parse_spec
from swarmdeck.supervisor import Supervisor

pytestmark = pytest.mark.asyncio

PY = sys.executable


def spec_for(code: str, tmp_path, **overrides):
    raw = {
        "name": "t",
        "command": [PY, "-u", "-c", code],
        "tasks": overrides.pop("tasks", 4),
        "concurrency": overrides.pop("concurrency", 4),
        **overrides,
    }
    return parse_spec(raw, base=tmp_path)


async def run(spec, tmp_path, **kwargs):
    sup = Supervisor(spec, run_dir=tmp_path / "run", **kwargs)
    await sup.start()
    await asyncio.wait_for(sup.wait(), timeout=60)
    await sup.logs.close()
    return sup


async def test_all_agents_succeed(tmp_path):
    sup = await run(spec_for("print('ok')", tmp_path), tmp_path)
    assert sup.state.agg.as_dict(0)["by_status"] == {"succeeded": 4}
    assert all(a.exit_code == 0 for a in sup.state.agents)


async def test_nonzero_exit_is_a_failure(tmp_path):
    sup = await run(spec_for("import sys; sys.exit(3)", tmp_path), tmp_path)
    assert sup.state.agg.as_dict(0)["by_status"] == {"failed": 4}
    assert sup.state.agents[0].exit_code == 3
    assert "3" in (sup.state.agents[0].error or "")


async def test_agent_reporting_failure_fails_even_on_exit_zero(tmp_path):
    code = "import json; print(json.dumps({'t':'result','ok':False,'error':'nope'}))"
    sup = await run(spec_for(code, tmp_path, tasks=2), tmp_path)
    assert sup.state.agg.as_dict(0)["by_status"] == {"failed": 2}
    assert sup.state.agents[0].error == "nope"


async def test_events_reach_state(tmp_path):
    code = (
        "import json;"
        "print(json.dumps({'t':'metric','tokens_in':10,'cost_usd':0.25}));"
        "print(json.dumps({'t':'progress','pct':1.0,'step':'done'}));"
        "print(json.dumps({'t':'result','ok':True,'data':{'v':1}}))"
    )
    sup = await run(spec_for(code, tmp_path, tasks=3), tmp_path)
    agg = sup.state.agg.as_dict(0)
    assert agg["tokens_in"] == 30
    assert agg["cost_usd"] == pytest.approx(0.75)
    assert sup.state.agents[0].result == {"v": 1}


async def test_plain_stdout_is_captured_as_logs(tmp_path):
    sup = await run(spec_for("print('hello from the agent')", tmp_path), tmp_path)
    lines = sup.logs.tail(0)
    assert any("hello from the agent" in line["text"] for line in lines)


async def test_stderr_is_captured_and_marked(tmp_path):
    code = "import sys; print('to stderr', file=sys.stderr)"
    sup = await run(spec_for(code, tmp_path, tasks=1), tmp_path)
    lines = sup.logs.tail(0)
    assert any(line["level"] == "warn" and "to stderr" in line["text"] for line in lines)


async def test_retry_then_succeed(tmp_path):
    # Fails on attempt 0, succeeds on attempt 1.
    code = "import os,sys;sys.exit(0 if os.environ['SWARMDECK_ATTEMPT'] != '0' else 1)"
    spec = spec_for(code, tmp_path, tasks=3, retries=1, retry_backoff=0.01)
    sup = await run(spec, tmp_path)
    assert sup.state.agg.as_dict(0)["by_status"] == {"succeeded": 3}
    assert all(a.attempt == 1 for a in sup.state.agents)


async def test_retries_are_exhausted_then_the_agent_fails(tmp_path):
    spec = spec_for("import sys; sys.exit(1)", tmp_path, tasks=2, retries=2, retry_backoff=0.01)
    sup = await run(spec, tmp_path)
    assert sup.state.agg.as_dict(0)["by_status"] == {"failed": 2}
    assert sup.state.agents[0].attempt == 2


async def test_timeout_terminates_the_agent(tmp_path):
    spec = spec_for("import time; time.sleep(30)", tmp_path, tasks=2, timeout=1.0)
    sup = await run(spec, tmp_path)
    assert sup.state.agg.as_dict(0)["by_status"] == {"timeout": 2}
    assert all(a.duration() < 15 for a in sup.state.agents)


async def test_concurrency_is_respected(tmp_path):
    spec = spec_for("import time; time.sleep(0.4)", tmp_path, tasks=12, concurrency=3)
    sup = await run(spec, tmp_path)
    assert sup.state.agg.peak_running <= 3
    assert sup.state.agg.as_dict(0)["by_status"] == {"succeeded": 12}


async def test_cancel_stops_running_agents(tmp_path):
    spec = spec_for("import time; time.sleep(30)", tmp_path, tasks=4, concurrency=4)
    sup = Supervisor(spec, run_dir=tmp_path / "run")
    await sup.start()
    await asyncio.sleep(1.0)
    affected = await sup.cancel()
    await asyncio.wait_for(sup.wait(), timeout=30)
    await sup.logs.close()
    assert affected == 4
    assert sup.state.agg.as_dict(0)["by_status"] == {"cancelled": 4}


async def test_cancel_does_not_count_already_finished_agents(tmp_path):
    spec = spec_for("print('fast')", tmp_path, tasks=3, concurrency=3)
    sup = await run(spec, tmp_path)
    assert await sup.cancel() == 0


@pytest.mark.skipif(not hasattr(__import__("signal"), "SIGSTOP"), reason="POSIX only")
async def test_pause_and_resume(tmp_path):
    spec = spec_for("import time; time.sleep(4)", tmp_path, tasks=2, concurrency=2)
    sup = Supervisor(spec, run_dir=tmp_path / "run")
    await sup.start()
    await asyncio.sleep(0.8)

    assert sup.pause() == 2
    assert sup.state.agg.as_dict(0)["by_status"].get("paused") == 2
    assert sup.resume() == 2
    assert sup.state.agg.as_dict(0)["by_status"].get("running") == 2

    await asyncio.wait_for(sup.wait(), timeout=30)
    await sup.logs.close()


async def test_retry_requeues_failed_agents(tmp_path):
    spec = spec_for("import sys; sys.exit(1)", tmp_path, tasks=3)
    sup = await run(spec, tmp_path)
    assert sup.retry({"status": "failed"}) == 3
    await asyncio.wait_for(sup.wait(), timeout=30)
    await sup.logs.close()
    # They fail again -- but they genuinely re-ran, which is the contract.
    assert sup.state.agg.as_dict(0)["by_status"] == {"failed": 3}


async def test_launch_failure_is_reported_not_raised(tmp_path):
    spec = parse_spec({"command": ["/definitely/not/a/real/binary"], "tasks": 2}, base=tmp_path)
    sup = await run(spec, tmp_path)
    assert sup.state.agg.as_dict(0)["by_status"] == {"failed": 2}
    assert "launch failed" in (sup.state.agents[0].error or "")


async def test_task_reaches_the_agent_via_environment(tmp_path):
    code = "import os; print(os.environ['SWARMDECK_TASK'])"
    spec = parse_spec(
        {
            "command": [PY, "-u", "-c", code],
            "tasks": {"inline": [{"topic": "finches"}]},
        },
        base=tmp_path,
    )
    sup = await run(spec, tmp_path)
    assert any("finches" in line["text"] for line in sup.logs.tail(0))


async def test_template_substitution_in_argv(tmp_path):
    spec = parse_spec(
        {
            "command": [PY, "-u", "-c", "import sys; print(sys.argv[1])", "{{ index }}"],
            "tasks": 3,
        },
        base=tmp_path,
    )
    sup = await run(spec, tmp_path)
    assert any(line["text"] == "2" for line in sup.logs.tail(2))


async def test_results_are_written(tmp_path):
    sup = await run(spec_for("print('ok')", tmp_path, tasks=3), tmp_path)
    path = sup.write_results()
    assert path.exists()
    assert len(path.read_text().strip().splitlines()) == 3


async def test_shutdown_is_idempotent(tmp_path):
    spec = spec_for("import time; time.sleep(20)", tmp_path, tasks=2)
    sup = Supervisor(spec, run_dir=tmp_path / "run")
    await sup.start()
    await asyncio.sleep(0.5)
    await sup.shutdown()
    await sup.shutdown()
    assert sup.state.phase == "finished"
