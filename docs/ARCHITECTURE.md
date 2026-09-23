# Architecture

```
  spec.yaml ──► Supervisor ──► Runtime ──► N agent processes
                    │        (process | docker)          │
                    │                                    │ stdout: NDJSON
                    ▼                                    ▼
               SwarmState ◄─────────────────────── event parser
                    │                                    │
                    │ deltas @ 10Hz                      │ log lines
                    ▼                                    ▼
              WebSocket ──► canvas dashboard        logs.jsonl
                                                   (+ per-agent ring)
```

Everything below is a consequence of one constraint: the interesting range is
100–1000+ concurrent agents, and every naive choice breaks somewhere inside it.

## Components

| Module | Responsibility |
|---|---|
| `spec.py` | Parse the YAML spec, expand `tasks:` into one entry per agent, render `{{ }}` templates |
| `supervisor.py` | Schedule, launch, supervise, retry, and steer. The control plane |
| `state.py` | Authoritative in-memory state and the delta protocol |
| `events.py` | Decode one line of agent output. The trust boundary |
| `logstore.py` | Durable log capture with a bounded memory and fd footprint |
| `runtimes/` | How one agent actually executes |
| `server/app.py` | REST + WebSocket |
| `ui/` | Canvas dashboard |

## The four decisions that carry the scale

### 1. A worker pool, not a task per agent

`concurrency` worker coroutines pull indices off a queue. One thousand parked
coroutines is survivable but wasteful; more importantly, a pool makes live
concurrency changes trivial — `set_concurrency(n)` moves a target and workers
exit or spawn to match.

This matters because the single most useful control at this scale is "slow
down, I am burning money", and it needs to work without restarting the run.

### 2. Deltas, never the firehose

The obvious design — forward every agent event to the browser — collapses
around fifty agents. A thousand agents each emitting ten events a second is
ten thousand messages per second per connected tab.

So the dashboard channel carries **only state deltas**, at a fixed 10Hz tick:

- Each agent has a revision; the swarm has a version counter.
- A delta contains only agents whose revision moved since the client's last
  frame, as **column-oriented arrays** rather than objects.
- Logs are never on this channel. They are fetched per agent, on demand, when
  a human opens the inspector.

Measured on a 500-agent run: a full snapshot is 39 KB, and steady-state deltas
average 1.3 rows — about **4.7 KB/s**.

Backpressure falls out for free. The send is awaited, so a slow client simply
receives fewer, larger frames; the next delta is computed from the last
*successfully sent* version, so frames coalesce rather than queue.

### 3. One log file, not one per agent

Each live agent already costs two file descriptors for its pipes. Adding a log
file per agent takes it to three, and `RLIMIT_NOFILE` — 1024 by default on
many systems — caps you around 340 concurrent agents. That is squarely inside
the range this tool exists to serve.

So all agents write through a single batched writer to one `logs.jsonl`, and
each agent keeps a 200-line in-memory ring for instant inspector reads. The
process runtime raises the soft fd limit toward the hard limit at launch and
reports the achievable ceiling in preflight rather than failing at agent 400.

Log writes are shed under queue pressure rather than applying backpressure to
the agent's stdout pipe. A blocked reader would stall the agent itself, which
turns an observability problem into a correctness problem.

### 4. Process groups

Agents shell out — to model CLIs, to tools, to other frameworks. Every agent
gets its own session, and cancellation signals the whole group. Killing only
the direct child leaves the expensive part running and still billing.

Pause is `SIGSTOP` on the group, resume is `SIGCONT`. The timeout watchdog
subtracts paused time, so pausing a swarm to look at it does not cause a mass
timeout on resume.

## Rendering

The hive draws to a canvas. A thousand DOM nodes each transitioning a
background colour ten times a second is the other half of why naive versions
of this view lock up; a canvas draws the same thousand cells in well under a
millisecond and scales to ten thousand without changing approach.

Agent state lives in typed arrays outside React. React renders the chrome at
6Hz; the canvas runs its own `requestAnimationFrame` loop keyed on a store
epoch and repaints only when something changed.

### Colour

Status is encoded with four hues — gray (inert), blue (in flight), green
(succeeded), red (failed) — validated against protanopia, deuteranopia, and
tritanopia simulation. Sub-states share their parent hue and are separated by
lightness or a diagonal hatch.

Two pairings were measured and rejected rather than shipped: violet "paused"
against blue "running" measures ΔE 1.6 under deuteranopia, and amber "timeout"
against red "failed" measures 14.1 even with normal colour vision. Hue does not
carry either distinction; texture and the legend do. Details in `ui/src/status.ts`.

## What is deliberately not here

- **No database.** Run state is in memory; durable output is JSONL on disk.
  A swarm run is a batch job, not a system of record.
- **No agent framework.** swarmdeck does not know what an agent is. That is
  the point.
- **No trace storage.** Point your agents at Langfuse, Phoenix, or any OTLP
  endpoint. swarmdeck answers "what is this fleet doing right now", which is
  a different question from "what happened in that trace".

## Extending it

A new execution backend implements `Runtime` and `Handle` in
`runtimes/base.py` — launch, stream lines, wait, terminate, pause, resume —
and registers itself with `@register`. Nothing else in the system needs to
change. Kubernetes, SSH, and Ray backends are all wanted.
