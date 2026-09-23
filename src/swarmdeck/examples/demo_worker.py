#!/usr/bin/env python3
"""A fake agent, so the dashboard has something to show without API keys.

It imitates the shape of real LLM agent work -- variable duration, streaming
progress, token and cost accounting, occasional failures and delegations --
using nothing but the standard library. Read it as the worked example of the
event protocol.
"""

from __future__ import annotations

import json
import os
import random
import sys
import time

STEPS = [
    "reading task",
    "planning",
    "searching",
    "calling model",
    "evaluating",
    "calling tool",
    "drafting",
    "verifying",
]


def emit(**payload) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def main() -> int:
    index = int(os.environ.get("SWARMDECK_AGENT_INDEX", "0"))
    total = int(os.environ.get("SWARMDECK_TOTAL", "1"))

    # Deterministic per (agent, attempt): a rerun of the demo looks the same,
    # but a retry is a genuinely different roll -- otherwise every retry
    # reproduces the same failure and the retry path looks broken.
    attempt = int(os.environ.get("SWARMDECK_ATTEMPT", "0"))
    rng = random.Random((index * 2654435761 + attempt * 40503) % (2**32))

    fail_rate = float(os.environ.get("DEMO_FAIL_RATE", "0.06"))
    slow_rate = float(os.environ.get("DEMO_SLOW_RATE", "0.04"))
    speed = float(os.environ.get("DEMO_SPEED", "1.0"))

    duration = rng.uniform(1.5, 6.0)
    if rng.random() < slow_rate:
        duration *= rng.uniform(4, 10)  # the stragglers that skew every p99
    duration /= max(speed, 0.01)

    steps = rng.randint(4, len(STEPS))
    emit(t="label", text=STEPS[0])

    tokens_in = 0
    tokens_out = 0
    cost = 0.0
    fails_at = rng.random() < fail_rate
    fail_step = rng.randint(1, steps) if fails_at else -1

    for step in range(steps):
        time.sleep(duration / steps)

        name = STEPS[step % len(STEPS)]
        emit(t="progress", pct=(step + 1) / steps, step=name)

        ti = rng.randint(150, 2400)
        to = rng.randint(40, 700)
        step_cost = ti * 3e-6 + to * 1.5e-5
        tokens_in += ti
        tokens_out += to
        cost += step_cost
        emit(
            t="metric",
            tokens_in=ti,
            tokens_out=to,
            cost_usd=round(step_cost, 6),
            tool_calls=1,
        )

        if rng.random() < 0.10:
            emit(t="log", level="info", msg=f"{name}: {rng.randint(2, 40)} candidates")

        # A tenth of agents hand work to a neighbour, which gives the
        # topology view something real to draw.
        if step == 1 and rng.random() < 0.10 and total > 1:
            peer = rng.randrange(total)
            if peer != index:
                emit(t="edge", to=f"a{peer:05d}", kind="delegate")
                emit(t="log", level="info", msg=f"delegated subtask to a{peer:05d}")

        if step == fail_step:
            reason = rng.choice(
                [
                    "rate limited by upstream provider",
                    "context length exceeded",
                    "tool call returned malformed JSON",
                    "connection reset",
                ]
            )
            emit(t="log", level="error", msg=reason)
            emit(t="result", ok=False, error=reason)
            return 1

    emit(
        t="result",
        ok=True,
        data={
            "index": index,
            "steps": steps,
            "tokens": tokens_in + tokens_out,
            "cost_usd": round(cost, 6),
            "summary": f"agent {index} finished {steps} steps",
        },
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
