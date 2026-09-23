# Contributing

Thanks for looking. This project is young enough that most contributions are
genuinely shaping it rather than filling in gaps.

## Setup

```bash
git clone https://github.com/ControlStackAI/swarmdeck.git
cd swarmdeck

python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

npm --prefix ui install
npm --prefix ui run build      # writes into src/swarmdeck/ui/
```

Then:

```bash
make test        # pytest
make lint        # ruff check + format --check
make ui          # rebuild the dashboard
make demo        # 500 simulated agents in the terminal
make serve       # dashboard on http://127.0.0.1:8787
```

For UI work, run the backend and Vite separately — Vite proxies `/api` and
`/ws` to port 8787, so you get hot reload against a live swarm:

```bash
swarmdeck serve &          # terminal 1
npm --prefix ui run dev    # terminal 2, http://localhost:5173
```

## What is most wanted

- **Runtimes.** Kubernetes, SSH, and Ray backends. Implement `Runtime` and
  `Handle` in `src/swarmdeck/runtimes/base.py` and register with `@register`;
  nothing else in the system needs to change.
- **Framework adapters.** Small shims that translate a framework's native
  callbacks (LangGraph, CrewAI, the OpenAI Agents SDK) into protocol events,
  so their users get a dashboard without writing print statements.
- **Scale reports.** If you run this at 2000+ agents, or on macOS, or on a
  host where the fd preflight was wrong, an issue describing what happened is
  worth a lot.

## House style

The code is written to be read by someone deciding whether to trust it, so:

- **Comment the decision, not the mechanics.** `# raise the fd soft limit`
  adds nothing above the line that does it. Why 1024 is the number that breaks
  a 400-agent run is the thing worth writing down.
- **Match the surrounding code.** Same naming, same density, same idiom.
- Line length 96, `ruff` for both linting and formatting.
- Type annotations on anything public.

## Tests

`make test` must pass. The supervisor tests launch real subprocesses on
purpose — the whole point of that module is behaviour under real process
semantics, and a mocked subprocess would test none of it.

New behaviour needs a test. Bug fixes need a test that fails before the fix;
three of the bugs found while building this were caught exactly that way.

## Pull requests

- One concern per PR.
- Describe what changes for a user, not just what you edited.
- If you changed the event protocol or the spec format, update
  `docs/PROTOCOL.md` in the same PR. Those two are the public contract.
- Breaking changes are acceptable before 1.0, but say so explicitly.

## Code of conduct

By participating you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).

## Licence

Contributions are licensed under [Apache-2.0](LICENSE), matching the project.
