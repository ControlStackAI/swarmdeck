# Security

## The thing to understand first

**swarmdeck runs processes described by whoever can reach its API.** That is
not a vulnerability, it is the product — the same way a CI runner or a job
scheduler executes what you give it. But it means the server is a privileged
surface and must be treated like one.

Anyone who can `POST /api/swarms` can execute commands as the user running the
server.

## Defaults

- The server binds `127.0.0.1` unless told otherwise.
- Binding a non-loopback address **without** `--token` generates a token and
  prints it; the server will not silently expose an unauthenticated control
  plane to your network.
- API-driven launches are confined to the `--specs` directory. A spec path
  outside it is rejected, so the endpoint cannot be used to read and execute
  arbitrary files on the host.
- `--no-launch` serves the dashboard read-only, for when you want visibility
  without a launch surface.
- Commands are argv lists, executed without a shell. There is no string
  interpolation into `/bin/sh`, so a task value cannot inject a pipeline.

## If you expose it

Do not put this on a public address. If you need remote access:

1. Prefer an SSH tunnel: `ssh -L 8787:127.0.0.1:8787 host`.
2. If that is not possible, use `--token` **and** terminate TLS in front of it.
   The token is sent as a bearer header or a query parameter; over plain HTTP
   it is readable in transit and in proxy logs.
3. Restrict `--specs` to a directory containing only reviewed specs.
4. Run it as a dedicated unprivileged user. Agents inherit the server's
   environment, credentials, and filesystem access.

## Secrets

swarmdeck does not manage credentials and does not want to. Agents inherit the
environment of the process that launched them.

- **Never put API keys in a spec file.** Specs are meant to be committed.
  Reference the environment instead: the agent process sees the server's
  environment, so export secrets where you start `swarmdeck serve`.
- Agent output is written to `logs.jsonl` in plain text. An agent that prints
  a key persists that key to disk. Redact in the agent, not after the fact.
- `results.jsonl` contains whatever agents put in their `result` events. Treat
  a run directory with the same care as the data the agents handled.
- This repository contains no credentials of any kind. CI runs a secret scan
  on every push and pull request.

## The untrusted-agent case

If your agents run code you did not write — generated code, third-party tools,
anything from a model — the `process` runtime is not an isolation boundary. It
gives each agent its own process group, and nothing else.

Use the `docker` runtime instead:

```yaml
runtime: docker
image: python:3.12-slim
command: ["python", "-c", "..."]
```

One container per agent, at roughly 200ms of startup cost. Add your own
`--network none`, resource limits, and read-only mounts by extending the
runtime; the defaults are deliberately plain rather than pretending to be a
sandbox.

## Reporting a vulnerability

Open a [security advisory](https://github.com/ControlStackAI/swarmdeck/security/advisories/new)
rather than a public issue. Include the version, a reproduction, and what an
attacker gains. We aim to acknowledge within a week.

Out of scope, because they are documented behaviour rather than defects:

- The API executes commands (that is the product).
- Agents inherit the server's environment (documented above).
- An agent can consume unbounded CPU, memory, or spend (use `timeout`, the
  concurrency control, and the docker runtime).
