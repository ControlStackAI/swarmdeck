"""The delta protocol is what keeps 1000 agents affordable, so its
"only changed rows" promise is tested directly rather than assumed."""

from swarmdeck.events import parse_line
from swarmdeck.state import AGENT_COLUMNS, AgentState, Status, SwarmState


def make(n: int = 5) -> SwarmState:
    return SwarmState("s1", "test", [AgentState(index=i, id=f"a{i:05d}") for i in range(n)])


def test_starts_all_pending():
    state = make(4)
    assert state.agg.as_dict(0)["by_status"] == {"pending": 4}
    assert state.agg.total == 4


def test_status_transitions_keep_aggregates_consistent():
    state = make(3)
    state.set_status(state.agents[0], Status.RUNNING)
    state.set_status(state.agents[1], Status.RUNNING)
    state.set_status(state.agents[0], Status.SUCCEEDED)
    agg = state.agg.as_dict(0)
    assert agg["by_status"] == {"pending": 1, "running": 1, "succeeded": 1}
    assert agg["running"] == 1
    assert agg["done"] == 1
    assert agg["pending"] == 1


def test_delta_returns_only_changed_rows():
    state = make(100)
    cursor = state.version
    state.set_status(state.agents[7], Status.RUNNING)
    state.set_status(state.agents[9], Status.RUNNING)

    delta = state.delta(cursor)
    assert [row[0] for row in delta["rows"]] == [7, 9]
    assert len(delta["rows"]) == 2

    # Nothing changed since: an empty delta, not a repeat of the same rows.
    assert state.delta(delta["v"])["rows"] == []


def test_snapshot_row_shape_matches_declared_columns():
    state = make(2)
    snapshot = state.snapshot()
    assert snapshot["cols"] == list(AGENT_COLUMNS)
    assert all(len(row) == len(AGENT_COLUMNS) for row in snapshot["rows"])


def test_metrics_accumulate_by_default_and_replace_when_absolute():
    state = make(1)
    agent = state.agents[0]
    state.apply(parse_line('{"t":"metric","tokens_in":100,"cost_usd":0.5}', 0))
    state.apply(parse_line('{"t":"metric","tokens_in":50}', 0))
    assert agent.tokens_in == 150
    assert state.agg.tokens_in == 150

    state.apply(parse_line('{"t":"metric","tokens_in":10,"set":true}', 0))
    assert agent.tokens_in == 10
    # The swarm rollup must follow the correction, not drift from it.
    assert state.agg.tokens_in == 10


def test_succeeding_forces_progress_to_complete():
    state = make(1)
    state.apply(parse_line('{"t":"progress","pct":0.3}', 0))
    state.set_status(state.agents[0], Status.SUCCEEDED)
    assert state.agents[0].progress == 1.0


def test_peak_running_is_retained_after_agents_finish():
    state = make(3)
    for agent in state.agents:
        state.set_status(agent, Status.RUNNING)
    for agent in state.agents:
        state.set_status(agent, Status.SUCCEEDED)
    assert state.agg.as_dict(0)["peak_running"] == 3


def test_error_logs_surface_in_the_feed_but_info_logs_do_not():
    state = make(1)
    state.apply(parse_line('{"t":"log","level":"info","msg":"routine"}', 0))
    assert state.feed == []
    state.apply(parse_line('{"t":"log","level":"error","msg":"boom"}', 0))
    assert len(state.feed) == 1
    assert state.feed[0]["msg"] == "boom"


def test_feed_is_bounded():
    state = make(1)
    for i in range(1000):
        state.push_feed("warn", 0, f"line {i}")
    assert len(state.feed) <= 300
    assert state.feed[-1]["msg"] == "line 999"


def test_selectors_resolve_by_status_and_index():
    state = make(6)
    for i in (1, 3, 5):
        state.set_status(state.agents[i], Status.FAILED)
    assert [a.index for a in state.select(status="failed")] == [1, 3, 5]
    assert [a.index for a in state.select(indices=[0, 3])] == [0, 3]


def test_added_agents_join_the_aggregate():
    state = make(2)
    state.add_agent(AgentState(index=2, id="a00002"))
    assert state.agg.total == 3
    assert state.agg.as_dict(0)["by_status"]["pending"] == 3
    assert state.by_id("a00002") is not None
