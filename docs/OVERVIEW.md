# Swarmdeck

A runtime-agnostic control plane for launching, supervising, and inspecting agent processes.

**Status:** Alpha.

## Bring your process

Run existing scripts and executables. Add structured reporting only when you need it.

## Keep the controls close

Pause, resume, cancel, retry, and adjust concurrency across a run or a selection.

## Inspect what happened

Follow state deltas in the dashboard and inspect per-agent logs and persisted run artifacts.

## How it fits together

Task spec → Supervisor → Agent processes → Live dashboard.

## Current boundaries

Alpha software. Large-swarm capacity is a design goal, not a published benchmark. The server launches commands: keep it on loopback unless you have configured and reviewed authentication.

## Get started

See the [README](../README.md) for prerequisites, setup, configuration, and development checks.

## Further reading

- [Architecture](../docs/ARCHITECTURE.md)
- [Event protocol](../docs/PROTOCOL.md)
- [Security](../SECURITY.md)

## Landing page

The companion [static landing page](site/index.html) is self-contained HTML/CSS with local assets and no analytics, external fonts, or JavaScript dependencies. Serve `docs/site/` from a static host to render it. GitHub file view shows its source. No hosting or repository-visibility change is implied by committing these files.
