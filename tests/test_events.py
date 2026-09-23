"""The parser is the trust boundary between agent output and the control
plane, so it gets the most adversarial tests in the suite."""

import json

import pytest

from swarmdeck.events import MAX_MSG_CHARS, EventType, parse_line


def test_plain_text_becomes_a_log_line():
    ev = parse_line("hello world", 3)
    assert ev.type == EventType.LOG
    assert ev.msg == "hello world"
    assert ev.agent_index == 3


def test_structured_event_is_decoded():
    ev = parse_line('{"t":"progress","pct":0.5,"step":"searching"}', 0)
    assert ev.type == EventType.PROGRESS
    assert ev.pct == 0.5
    assert ev.step == "searching"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(0.5, 0.5), (50, 0.5), (100, 1.0), (-3, 0.0), (250, 1.0)],
)
def test_progress_accepts_fractions_and_percentages(raw, expected):
    assert parse_line(json.dumps({"t": "progress", "pct": raw}), 0).pct == expected


def test_malformed_json_degrades_to_a_log_line_rather_than_raising():
    ev = parse_line('{"t":"progress", "pct":', 0)
    assert ev.type == EventType.LOG
    assert "pct" in ev.msg


def test_foreign_json_is_kept_as_a_log_line():
    # An agent emitting its own structured logs should stay readable.
    ev = parse_line('{"level":"info","message":"from another logger"}', 0)
    assert ev.type == EventType.LOG
    assert "another logger" in ev.msg


def test_json_that_is_not_an_object_is_a_log_line():
    assert parse_line("[1, 2, 3]", 0).type == EventType.LOG
    assert parse_line('"bare string"', 0).type == EventType.LOG


def test_metrics_split_known_from_custom():
    ev = parse_line('{"t":"metric","tokens_in":100,"cost_usd":0.5,"note":"hi","retries":2}', 0)
    assert ev.metrics == {"tokens_in": 100.0, "cost_usd": 0.5, "retries": 2.0}
    assert ev.extra == {"note": "hi"}


def test_non_finite_metrics_are_discarded():
    # NaN would poison every downstream aggregate.
    ev = parse_line('{"t":"metric","cost_usd":NaN,"tokens_in":5}', 0)
    assert "cost_usd" not in ev.metrics
    assert ev.metrics["tokens_in"] == 5.0


def test_booleans_are_not_treated_as_numbers():
    ev = parse_line('{"t":"metric","tokens_in":true}', 0)
    assert "tokens_in" not in ev.metrics


def test_long_lines_are_clipped_not_dropped():
    ev = parse_line("x" * 500_000, 0)
    assert ev.type == EventType.LOG
    assert len(ev.msg) <= MAX_MSG_CHARS


def test_result_carries_failure():
    ev = parse_line('{"t":"result","ok":false,"error":"rate limited"}', 0)
    assert ev.ok is False
    assert ev.error == "rate limited"


def test_log_events_are_droppable_but_lifecycle_events_are_not():
    assert parse_line('{"t":"log","msg":"x"}', 0).droppable
    assert not parse_line('{"t":"result","ok":true}', 0).droppable
    assert not parse_line('{"t":"progress","pct":1}', 0).droppable


def test_unknown_log_level_falls_back_to_info():
    assert parse_line('{"t":"log","level":"screaming","msg":"x"}', 0).level == "info"
