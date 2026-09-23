# The agent event protocol

An agent is a process. It reports by writing newline-delimited JSON to stdout.
That is the whole protocol, and this page is the whole specification.

Nothing here is required. A process that prints nothing and exits 0 is a
successful agent. Every event below is an optional upgrade.

## Rules

1. One JSON object per line, on **stdout**, flushed.
2. The object has a `t` field naming the event type.
3. Any line that is not a JSON object with a known `t` becomes a **log line**.
   That includes plain text, malformed JSON, and JSON from some other logger.
   Agents are never punished for printing.
4. stderr is captured too, as log lines at `warn` level.
5. Lines over 64 KB are truncated, not dropped.
6. Exit code 0 means success, unless the agent said otherwise with a `result`
   event.

Rule 3 is the load-bearing one. It means you can point swarmdeck at an
existing script and get a working dashboard immediately, then add structure
where it pays.

## Events

### `progress`

```json
{"t": "progress", "pct": 0.4, "step": "searching"}
```

`pct` accepts `0.0-1.0` or `0-100` — both are understood, because agents get
this wrong constantly. `step` is a short caption shown on the agent's cell.

### `metric`

```json
{"t": "metric", "tokens_in": 812, "tokens_out": 140, "cost_usd": 0.0031}
```

Values **accumulate** by default. Send `"set": true` to replace instead of add.

`tokens_in`, `tokens_out`, and `cost_usd` roll up into the swarm totals and
drive the cost chart. `latency_ms`, `tool_calls`, and `errors` are also
recognised. Any other numeric key is stored and shown in the inspector.
Non-numeric values, `NaN`, and infinities are ignored rather than allowed to
poison the aggregates.

### `result`

```json
{"t": "result", "ok": true, "data": {"answer": 42}}
{"t": "result", "ok": false, "error": "rate limited by upstream"}
```

`data` lands in `results.jsonl`. An agent that exits 0 after reporting
`"ok": false` is still recorded as failed — the agent's own verdict wins.

### `log`

```json
{"t": "log", "level": "warn", "msg": "retrying after 429"}
```

Levels: `debug`, `info`, `warn`, `error`. Unknown levels become `info`.
`warn` and `error` also surface in the swarm-wide activity feed.

### `edge`

```json
{"t": "edge", "to": "a00042", "kind": "delegate"}
```

Declares a relationship to another agent, drawn in the topology view. `to` is
an agent id (`a00042`) or a bare index (`42`).

### `status`, `label`

```json
{"t": "label", "text": "drafting section 3"}
```

Sets the caption on the agent's cell. `status` with a `state` field does the
same thing and exists for agents that already emit lifecycle events.

### `artifact`

```json
{"t": "artifact", "name": "report.md", "path": "/tmp/out/report.md"}
```

Recorded against the agent and listed in the inspector.

## What the agent receives

| Variable | Meaning |
|---|---|
| `SWARMDECK_TASK` | This agent's task, JSON-encoded |
| `SWARMDECK_AGENT_INDEX` | `0`-based position in the swarm |
| `SWARMDECK_AGENT_ID` | `a00042` |
| `SWARMDECK_ATTEMPT` | `0` on the first try, `1` on the first retry |
| `SWARMDECK_TOTAL` | Total agents in the swarm |
| `SWARMDECK_SWARM_ID` | Identifier for this run |
| `SWARMDECK_RUN_DIR` | Where this run's logs and results are written |

The task also reaches the command line through `{{ task.field }}` templating in
the spec, which is usually more convenient.

## Backpressure

Under load, `log` events are shed before anything else and the dropped count is
reported rather than hidden. Lifecycle events — `status`, `progress`, `metric`,
`result`, `edge` — are never dropped, because losing one would strand an agent
in the dashboard forever.

If you see a non-zero dropped count, your agents are logging faster than the
disk can absorb. That is a signal about your agents, not a defect to paper
over.

## Examples

Bash:

```bash
echo '{"t":"progress","pct":0.5}'
echo '{"t":"result","ok":true,"data":{"rows":128}}'
```

Python, with the optional helper:

```python
from swarmdeck import sdk

sdk.progress(0.5, "calling model")
sdk.metric(tokens_in=812, cost_usd=0.0031)
sdk.result(ok=True, data={"rows": 128})
```

Node:

```js
const emit = (o) => process.stdout.write(JSON.stringify(o) + "\n");
emit({ t: "progress", pct: 0.5 });
emit({ t: "result", ok: true });
```
