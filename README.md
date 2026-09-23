<h1 align="center">swarmdeck</h1>

<p align="center">
  <strong>Launch, control, and visualize agent swarms at 100&ndash;1000+ scale.</strong>
</p>

<p align="center">
  <a href="LICENSE"><img alt="License" src="https://img.shields.io/badge/license-Apache--2.0-blue.svg"></a>
  <img alt="Python" src="https://img.shields.io/badge/python-3.10%2B-blue.svg">
  <img alt="Status" src="https://img.shields.io/badge/status-alpha-orange.svg">
</p>

---

Running one agent is a solved problem. Running nine hundred of them is not.

At that scale the questions stop being "what did the model say" and start being
operational: which three hundred are still running, why did that cohort fail at
the same minute, what has this cost me so far, and can I stop the half that is
clearly stuck without killing the half that is working.

swarmdeck is the control plane for that. It launches agents, supervises them,
and renders the whole swarm as one live picture you can act on.

```bash
pip install swarmdeck
swarmdeck demo --agents 1000 --concurrency 200
```

No API keys, no accounts, no config. The demo runs a thousand simulated agents
with realistic timings, costs, and failure modes.

## What makes it different

**It does not care what your agents are made of.** An agent is a process. If it
runs, it works — a Python script, a Go binary, `curl`, a LangGraph app, a Claude
Code invocation. There is no adapter to write and no framework to adopt.

**Structured reporting is opt-in, not required.** Any process works immediately;
stdout becomes log lines and the exit code becomes pass/fail. Print a JSON line
and you get progress bars, token counts, cost tracking, and topology edges:

```python
print('{"t": "progress", "pct": 0.4}', flush=True)
print('{"t": "metric", "tokens_in": 812, "cost_usd": 0.0031}', flush=True)
```

That is the entire integration surface. [`PROTOCOL.md`](docs/PROTOCOL.md) is the
whole spec and it fits on one page.

**It is built for the scale it claims.** Most agent dashboards are a websocket
that forwards every event to the browser. That collapses somewhere around fifty
agents. swarmdeck separates the firehose from the picture: the dashboard
receives only state deltas at a fixed tick, logs are fetched per agent on
demand, and the swarm renders to canvas rather than to a thousand DOM nodes.
See [Architecture](#architecture) for why each of those is load-bearing.

**Control, not just observation.** Pause, resume, cancel, retry, and re-throttle
— on one agent, on a selection, or on the whole swarm. `retry status=failed`
after a provider outage beats clicking three hundred cells.

## Quickstart

```bash
pip install swarmdeck

# Terminal: run a swarm to completion with a live summary
swarmdeck demo --agents 500

# Dashboard: launch and steer swarms from the browser
swarmdeck serve --open
```

Write a spec:

```yaml
# research.swarm.yaml
name: research
concurrency: 64
command: ["python", "-u", "agent.py", "--topic", "{{ task.topic }}"]
timeout: 300
retries: 2

tasks:
  file: topics.jsonl     # one JSON object per agent
```

```bash
swarmdeck validate research.swarm.yaml   # check before you spend money
swarmdeck run research.swarm.yaml -c 128 # or launch it from the dashboard
```

Every run writes `results.jsonl`, `logs.jsonl`, and a `manifest.json` under
`.swarmdeck/runs/`, so a swarm is reproducible and auditable after the fact.

## The dashboard

| View | What it answers |
|---|---|
| **Hive** | One cell per agent on a canvas grid. Where is the swarm right now, and which cells are stuck? |
| **Metrics** | Throughput, concurrency, cost, and token burn over time. Is this run worth continuing? |
| **Topology** | Delegation edges between agents, drawn only from relationships agents declare. Who handed work to whom? |
| **Inspector** | One agent's logs, events, timing, result, and task. Why did *this* one fail? |

Select agents by index, status, or tag, then act on the selection.

## Writing an agent

Nothing is required. This is a valid agent:

```bash
command: ["curl", "--fail", "-sS", "{{ task.value }}"]
```

For richer reporting, print JSON lines — from any language. Python users can use
the optional helper:

```python
from swarmdeck import sdk

task = sdk.task()                          # this agent's slice of work
sdk.progress(0.25, "searching")
sdk.metric(tokens_in=812, tokens_out=140, cost_usd=0.0031)
sdk.edge(to="a00042", kind="delegate")     # draws a topology edge
sdk.result(ok=True, data={"answer": 42})
```

Agents receive their work through the environment: `SWARMDECK_TASK`,
`SWARMDECK_AGENT_INDEX`, `SWARMDECK_AGENT_ID`, `SWARMDECK_ATTEMPT`,
`SWARMDECK_TOTAL`, `SWARMDECK_RUN_DIR`.

## Architecture

```
  spec.yaml ──► Supervisor ──► Runtime ──► N agent processes
                    │             (process | docker)      │
                    │                                     │ stdout: NDJSON
                    ▼                                     ▼
               SwarmState ◄──────────────────────── event parser
                    │                                     │
                    │ deltas @ 10Hz                       │ log lines
                    ▼                                     ▼
              WebSocket ──► canvas dashboard         logs.jsonl
                                                    (+ per-agent ring)
```

Four decisions carry the scale:

1. **A worker pool, not a task per agent.** Concurrency is the pool size, so
   re-throttling a live swarm is changing one number.
2. **Deltas, never the firehose.** The browser receives only agents whose
   revision moved, as column-oriented arrays, at a fixed tick. A slow client
   naturally gets fewer, larger frames — coalescing is the backpressure.
3. **One log file, not one per agent.** A file descriptor per live agent is what
   actually breaks first; `RLIMIT_NOFILE` is checked at launch and reported.
4. **Process groups.** Agents shell out. Cancelling only the direct child leaks
   the expensive part, so signals go to the whole group.

Longer version in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Where it fits

swarmdeck is a control plane, not a framework and not a tracing backend.

- **Agent frameworks** (LangGraph, CrewAI, Agency Swarm, OpenAI Agents SDK)
  define how one agent reasons. swarmdeck runs a thousand of whatever you built
  and gives you a handle on them. Use both.
- **LLM observability** (Langfuse, Phoenix, AgentOps) answers "what happened in
  that trace", after the fact, per request. swarmdeck answers "what is happening
  across this fleet, right now, and what do I do about it". Also use both —
  point your agents at your tracer and swarmdeck will not get in the way.

If you only ever run ten agents, you do not need this.

## Status

Alpha. The event protocol and spec format are settling but not yet frozen;
breaking changes will be called out in release notes until 1.0.

Contributions welcome — see [`CONTRIBUTING.md`](CONTRIBUTING.md). Good first
issues are labelled. Particularly wanted: additional runtimes (Kubernetes, SSH,
Ray), and adapters that translate popular frameworks' native callbacks into the
event protocol.

## Security

The server starts processes described by whoever can reach it. That is remote
code execution by design, the same way a CI runner is. It binds loopback by
default and requires an explicit token to bind anywhere else. Read
[`SECURITY.md`](SECURITY.md) before exposing it.

## License

[Apache-2.0](LICENSE) © ControlStack AI
