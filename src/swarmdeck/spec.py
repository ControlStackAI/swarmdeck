"""Swarm specs: the YAML file that describes what to launch and how many.

A spec is deliberately closer to a batch job description than to an agent
framework config. swarmdeck does not know what an agent *is* -- it knows how
to start a process, how many of them to run at once, and what to do when one
dies. Everything model-shaped lives inside the command you point it at.
"""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

#: ``{{ task.topic }}`` / ``{{ index }}``. Intentionally not Jinja: the
#: substitution surface stays small enough to audit, and specs stay
#: portable to any reimplementation of this protocol.
_TEMPLATE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_.]*)\s*\}\}")

MAX_AGENTS = 100_000


class SpecError(ValueError):
    """Raised for a spec a human needs to fix. Message is user-facing."""


@dataclass(slots=True)
class AgentSpec:
    command: list[str]
    runtime: str = "process"
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    timeout: float | None = None
    retries: int = 0
    retry_backoff: float = 2.0
    merge_stderr: bool = False
    image: str | None = None  # docker runtime
    shutdown_grace: float = 5.0


@dataclass(slots=True)
class SwarmSpec:
    name: str
    agent: AgentSpec
    tasks: list[Any]
    concurrency: int = 32
    tags: dict[str, list[str]] = field(default_factory=dict)
    source_path: Path | None = None
    description: str = ""

    @property
    def size(self) -> int:
        return len(self.tasks)


def render(
    template: str,
    *,
    task: Any,
    index: int,
    agent_id: str,
    attempt: int,
    spec_dir: str = "",
) -> str:
    """Substitute ``{{ ... }}`` placeholders in one argv element."""
    scope: dict[str, Any] = {
        "index": index,
        "id": agent_id,
        "attempt": attempt,
        "task": task,
        # Specs almost always sit next to the script they launch, and the
        # process inherits the operator's cwd, not the spec's.
        "spec_dir": spec_dir,
    }

    def lookup(match: re.Match[str]) -> str:
        parts = match.group(1).split(".")
        value: Any = scope
        for part in parts:
            if isinstance(value, dict) and part in value:
                value = value[part]
            elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
                value = value[int(part)]
            else:
                raise SpecError(
                    f"template {{{{ {match.group(1)} }}}} has no value (missing key {part!r})"
                )
        if isinstance(value, (dict, list)):
            return json.dumps(value, separators=(",", ":"))
        if value is None:
            return ""
        return str(value)

    return _TEMPLATE.sub(lookup, template)


def render_argv(
    argv: list[str],
    *,
    task: Any,
    index: int,
    agent_id: str,
    attempt: int,
    spec_dir: str = "",
) -> list[str]:
    return [
        render(
            arg,
            task=task,
            index=index,
            agent_id=agent_id,
            attempt=attempt,
            spec_dir=spec_dir,
        )
        for arg in argv
    ]


def _load_tasks(raw: Any, base: Path) -> list[Any]:
    """Expand the ``tasks:`` block into one entry per agent."""
    if raw is None:
        raise SpecError("spec needs a 'tasks:' block (count, inline, file, or range)")

    if isinstance(raw, int):
        return [{"index": i} for i in range(raw)]

    if isinstance(raw, list):
        return list(raw)

    if not isinstance(raw, dict):
        raise SpecError(
            f"'tasks:' must be an int, list, or mapping -- got {type(raw).__name__}"
        )

    if "count" in raw:
        count = int(raw["count"])
        if count < 1:
            raise SpecError("tasks.count must be >= 1")
        template = raw.get("template")
        if template is None:
            return [{"index": i} for i in range(count)]
        return [{**template, "index": i} for i in range(count)]

    if "inline" in raw:
        items = raw["inline"]
        if not isinstance(items, list):
            raise SpecError("tasks.inline must be a list")
        return list(items)

    if "range" in raw:
        r = raw["range"]
        start, stop = int(r.get("start", 0)), int(r["stop"])
        step = int(r.get("step", 1))
        return [{"index": i, "value": i} for i in range(start, stop, step)]

    if "file" in raw:
        path = (base / str(raw["file"])).resolve()
        if not path.is_file():
            raise SpecError(f"tasks.file not found: {path}")
        return _read_task_file(path)

    raise SpecError("'tasks:' needs one of: count, inline, range, file")


def _read_task_file(path: Path) -> list[Any]:
    suffix = path.suffix.lower()
    text = path.read_text(encoding="utf-8")
    if suffix in (".jsonl", ".ndjson"):
        out = []
        for lineno, line in enumerate(text.splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                out.append(json.loads(line))
            except ValueError as exc:
                raise SpecError(f"{path}:{lineno}: invalid JSON -- {exc}") from exc
        return out
    if suffix == ".json":
        data = json.loads(text)
        if not isinstance(data, list):
            raise SpecError(f"{path}: expected a JSON array of tasks")
        return data
    if suffix in (".yaml", ".yml"):
        data = yaml.safe_load(text)
        if not isinstance(data, list):
            raise SpecError(f"{path}: expected a YAML list of tasks")
        return data
    if suffix in (".txt", ".csv"):
        # One task per line -- the lowest-friction way to fan out over a
        # list of URLs, prompts, or file paths.
        return [
            {"index": i, "value": line}
            for i, line in enumerate(ln.strip() for ln in text.splitlines() if ln.strip())
        ]
    raise SpecError(f"{path}: unsupported task file type {suffix!r}")


def _as_argv(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(x) for x in raw]
    if isinstance(raw, str):
        # Split without a shell. Anything needing pipes or globs should call
        # a script; we never hand a string to /bin/sh.
        return shlex.split(raw)
    raise SpecError("'command:' must be a string or a list of arguments")


def load_spec(path: str | Path) -> SwarmSpec:
    path = Path(path).resolve()
    if not path.is_file():
        raise SpecError(f"spec not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise SpecError(f"{path}: invalid YAML -- {exc}") from exc
    if not isinstance(raw, dict):
        raise SpecError(f"{path}: spec must be a YAML mapping")
    return parse_spec(raw, base=path.parent, source_path=path)


def parse_spec(
    raw: dict[str, Any], *, base: Path | None = None, source_path: Path | None = None
) -> SwarmSpec:
    base = base or Path.cwd()

    agent_raw = raw.get("agent")
    if agent_raw is None:
        # Allow the flat form, which is what most specs end up wanting.
        agent_raw = {
            k: raw[k]
            for k in (
                "command",
                "runtime",
                "env",
                "cwd",
                "timeout",
                "retries",
                "retry_backoff",
                "merge_stderr",
                "image",
                "shutdown_grace",
            )
            if k in raw
        }
    if not isinstance(agent_raw, dict) or "command" not in agent_raw:
        raise SpecError("spec needs a 'command:' (top level or under 'agent:')")

    env = {str(k): str(v) for k, v in (agent_raw.get("env") or {}).items()}

    agent = AgentSpec(
        command=_as_argv(agent_raw["command"]),
        runtime=str(agent_raw.get("runtime", "process")),
        env=env,
        cwd=str(agent_raw["cwd"]) if agent_raw.get("cwd") else None,
        timeout=float(agent_raw["timeout"]) if agent_raw.get("timeout") else None,
        retries=int(agent_raw.get("retries", 0)),
        retry_backoff=float(agent_raw.get("retry_backoff", 2.0)),
        merge_stderr=bool(agent_raw.get("merge_stderr", False)),
        image=agent_raw.get("image"),
        shutdown_grace=float(agent_raw.get("shutdown_grace", 5.0)),
    )
    if not agent.command:
        raise SpecError("'command:' resolved to an empty argv")

    tasks = _load_tasks(raw.get("tasks"), base)
    if not tasks:
        raise SpecError("'tasks:' expanded to zero agents")
    if len(tasks) > MAX_AGENTS:
        raise SpecError(f"{len(tasks)} agents exceeds the {MAX_AGENTS} cap")

    concurrency = int(raw.get("concurrency", 32))
    if concurrency < 1:
        raise SpecError("concurrency must be >= 1")

    tags = {str(k): [str(x) for x in v] for k, v in (raw.get("tags") or {}).items()}

    return SwarmSpec(
        name=str(raw.get("name") or (source_path.stem if source_path else "swarm")),
        description=str(raw.get("description", "")),
        agent=agent,
        tasks=tasks,
        concurrency=min(concurrency, len(tasks)),
        tags=tags,
        source_path=source_path,
    )


def iter_tags(spec: SwarmSpec, index: int) -> Iterator[str]:
    for tag, members in spec.tags.items():
        for member in members:
            if member == "*" or str(index) == str(member):
                yield tag
                break
            if "-" in str(member):
                lo, _, hi = str(member).partition("-")
                if lo.isdigit() and hi.isdigit() and int(lo) <= index <= int(hi):
                    yield tag
                    break
