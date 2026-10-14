import json

import pytest
import yaml

from swarmdeck.spec import SpecError, load_spec, parse_spec, render, render_argv


def write(tmp_path, name, data):
    path = tmp_path / name
    path.write_text(yaml.safe_dump(data) if isinstance(data, dict) else data)
    return path


def test_flat_and_nested_agent_forms_are_equivalent(tmp_path):
    flat = parse_spec({"command": ["echo", "hi"], "tasks": 3}, base=tmp_path)
    nested = parse_spec({"agent": {"command": ["echo", "hi"]}, "tasks": 3}, base=tmp_path)
    assert flat.agent.command == nested.agent.command
    assert flat.size == nested.size == 3


def test_command_string_is_split_without_a_shell(tmp_path):
    spec = parse_spec({"command": "echo 'hello world'", "tasks": 1}, base=tmp_path)
    assert spec.agent.command == ["echo", "hello world"]


def test_concurrency_never_exceeds_agent_count(tmp_path):
    spec = parse_spec({"command": ["true"], "tasks": 4, "concurrency": 99}, base=tmp_path)
    assert spec.concurrency == 4


@pytest.mark.parametrize(
    "tasks",
    [
        {"count": 100_001},
        {"range": {"stop": 100_001}},
        {"range": {"start": 0, "stop": 10, "step": 0}},
        {"count": 2, "template": "not-a-mapping"},
    ],
)
def test_task_expansion_rejects_invalid_or_oversized_inputs_before_materializing(tasks):
    with pytest.raises(SpecError):
        parse_spec({"command": ["true"], "tasks": tasks})


@pytest.mark.parametrize(
    ("tasks", "size"),
    [
        (5, 5),
        ({"count": 7}, 7),
        ({"range": {"start": 2, "stop": 8}}, 6),
        ({"inline": [{"a": 1}, {"a": 2}]}, 2),
    ],
)
def test_task_expansion_forms(tmp_path, tasks, size):
    assert parse_spec({"command": ["true"], "tasks": tasks}, base=tmp_path).size == size


def test_tasks_from_jsonl(tmp_path):
    (tmp_path / "t.jsonl").write_text('{"topic":"a"}\n\n# comment\n{"topic":"b"}\n')
    spec = parse_spec({"command": ["true"], "tasks": {"file": "t.jsonl"}}, base=tmp_path)
    assert [t["topic"] for t in spec.tasks] == ["a", "b"]


def test_tasks_from_txt_one_per_line(tmp_path):
    (tmp_path / "u.txt").write_text("https://a\nhttps://b\n\n")
    spec = parse_spec({"command": ["true"], "tasks": {"file": "u.txt"}}, base=tmp_path)
    assert [t["value"] for t in spec.tasks] == ["https://a", "https://b"]


def test_bad_jsonl_names_the_line(tmp_path):
    (tmp_path / "t.jsonl").write_text('{"ok":1}\nnot json\n')
    with pytest.raises(SpecError, match=r"t\.jsonl:2"):
        parse_spec({"command": ["true"], "tasks": {"file": "t.jsonl"}}, base=tmp_path)


@pytest.mark.parametrize(
    "raw",
    [
        {"tasks": 1},  # no command
        {"command": ["true"]},  # no tasks
        {"command": ["true"], "tasks": {"inline": []}},  # zero agents
        {"command": ["true"], "tasks": 1, "concurrency": 0},
    ],
)
def test_invalid_specs_are_rejected_with_a_message(tmp_path, raw):
    with pytest.raises(SpecError):
        parse_spec(raw, base=tmp_path)


def test_render_substitutes_scope():
    out = render(
        "{{ task.topic }}-{{ index }}-{{ attempt }}",
        task={"topic": "birds"},
        index=7,
        agent_id="a00007",
        attempt=2,
    )
    assert out == "birds-7-2"


def test_render_serialises_nested_structures():
    out = render("{{ task }}", task={"a": 1}, index=0, agent_id="x", attempt=0)
    assert json.loads(out) == {"a": 1}


def test_render_reports_a_missing_key_instead_of_silently_emptying():
    with pytest.raises(SpecError, match="nope"):
        render("{{ task.nope }}", task={}, index=0, agent_id="x", attempt=0)


def test_spec_dir_is_available_to_templates():
    argv = render_argv(
        ["python", "{{ spec_dir }}/w.py"],
        task={},
        index=0,
        agent_id="x",
        attempt=0,
        spec_dir="/opt/specs",
    )
    assert argv == ["python", "/opt/specs/w.py"]


def test_load_spec_round_trip(tmp_path):
    path = write(
        tmp_path,
        "demo.swarm.yaml",
        {"name": "demo", "command": ["echo", "{{ index }}"], "tasks": {"count": 2}},
    )
    spec = load_spec(path)
    assert spec.name == "demo"
    assert spec.source_path == path
    assert spec.size == 2
