"""Command line interface."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import sys
import webbrowser
from pathlib import Path

import typer
from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.text import Text

from . import __version__
from .background import spawn
from .spec import SpecError, load_spec
from .state import SwarmState
from .supervisor import Supervisor

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Launch, control, and visualize agent swarms.",
)
console = Console()
err = Console(stderr=True)

#: Shipped inside the package so `swarmdeck demo` works from a wheel, not
#: just from a source checkout.
EXAMPLES = Path(__file__).resolve().parent / "examples"

_STATUS_STYLE = {
    "pending": "dim",
    "starting": "cyan",
    "running": "bold cyan",
    "succeeded": "green",
    "failed": "bold red",
    "cancelled": "yellow",
    "timeout": "magenta",
    "paused": "blue",
    "retrying": "yellow",
}


def _fail(message: str) -> None:
    err.print(f"[bold red]error[/] {message}")
    raise typer.Exit(1)


@app.command()
def version() -> None:
    """Print the version."""
    console.print(f"swarmdeck {__version__}")


@app.command()
def validate(spec_path: Path = typer.Argument(..., help="Path to a swarm spec")) -> None:
    """Check a spec and show what it would launch."""
    try:
        spec = load_spec(spec_path)
    except SpecError as exc:
        _fail(str(exc))
        return

    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_row("name", spec.name)
    if spec.description:
        table.add_row("description", spec.description)
    table.add_row("agents", str(spec.size))
    table.add_row("concurrency", str(spec.concurrency))
    table.add_row("runtime", spec.agent.runtime)
    table.add_row("command", " ".join(spec.agent.command))
    table.add_row("timeout", f"{spec.agent.timeout}s" if spec.agent.timeout else "none")
    table.add_row("retries", str(spec.agent.retries))
    console.print(table)

    sample = spec.tasks[0]
    console.print(f"\n[dim]first task:[/] {json.dumps(sample, default=str)[:200]}")
    console.print("[green]spec is valid[/]")


@app.command()
def run(
    spec_path: Path = typer.Argument(..., help="Path to a swarm spec"),
    concurrency: int = typer.Option(None, "--concurrency", "-c", help="Override concurrency"),
    limit: int = typer.Option(None, "--limit", "-n", help="Run only the first N tasks"),
    no_logs: bool = typer.Option(False, "--no-logs", help="Skip writing logs to disk"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="No live display"),
    results: Path = typer.Option(None, "--results", help="Write results JSONL here"),
) -> None:
    """Run a swarm to completion in the terminal."""
    try:
        spec = load_spec(spec_path)
    except SpecError as exc:
        _fail(str(exc))
        return

    if limit:
        spec.tasks = spec.tasks[:limit]
    if concurrency:
        spec.concurrency = concurrency
    spec.concurrency = max(1, min(spec.concurrency, len(spec.tasks)))

    code = asyncio.run(_run(spec, capture_logs=not no_logs, quiet=quiet, results=results))
    raise typer.Exit(code)


async def _run(spec, *, capture_logs: bool, quiet: bool, results: Path | None) -> int:
    sup = Supervisor(spec, capture_logs=capture_logs)
    stopping = False

    def request_stop() -> None:
        nonlocal stopping
        if stopping:
            # Second Ctrl-C: the operator means it.
            os._exit(130)
        stopping = True
        err.print("\n[yellow]stopping -- terminating agents (Ctrl-C again to force)[/]")
        spawn(sup.shutdown(), name="cli-shutdown")

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, AttributeError):
            loop.add_signal_handler(sig, request_stop)

    await sup.start()
    for warning in sup.warnings:
        err.print(f"[yellow]warning[/] {warning}")
    if not quiet:
        console.print(
            f"[bold]{spec.name}[/]  {spec.size} agents  "
            f"concurrency {spec.concurrency}  -> [dim]{sup.run_dir}[/]\n"
        )

    waiter = asyncio.ensure_future(sup.wait())
    if quiet:
        await waiter
    else:
        with Live(_render(sup.state), console=console, refresh_per_second=8) as live:
            while not waiter.done():
                await asyncio.sleep(0.12)
                live.update(_render(sup.state))
            live.update(_render(sup.state))

    await sup.logs.close()
    path = sup.write_results(results)

    agg = sup.state.agg.as_dict(0)
    failed = sum(agg["by_status"].get(name, 0) for name in ("failed", "timeout", "cancelled"))
    console.print(
        f"\n[bold]done[/] {agg['by_status'].get('succeeded', 0)}/{agg['total']} succeeded"
        f"  ·  {agg['elapsed']}s"
        f"  ·  {agg['throughput']}/s"
        + (f"  ·  ${agg['cost_usd']:.4f}" if agg["cost_usd"] else "")
    )
    console.print(f"[dim]results: {path}[/]")
    if sup.logs.dropped:
        err.print(f"[yellow]{sup.logs.dropped} log lines dropped under load[/]")
    return 1 if failed else 0


def _render(state: SwarmState) -> Table:
    agg = state.agg.as_dict(0)
    total = max(1, agg["total"])
    done = agg["done"]

    bar_width = 44
    filled = int(bar_width * done / total)
    counts = agg["by_status"]
    ok = counts.get("succeeded", 0)
    bad = counts.get("failed", 0) + counts.get("timeout", 0)
    ok_w = int(bar_width * ok / total)
    bad_w = int(bar_width * bad / total)
    run_w = max(0, filled - ok_w - bad_w)

    bar = Text()
    bar.append("█" * ok_w, style="green")
    bar.append("█" * bad_w, style="red")
    bar.append("█" * run_w, style="yellow")
    bar.append("░" * (bar_width - ok_w - bad_w - run_w), style="dim")

    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_row(bar, f"{done}/{total}  [cyan]{agg['running']} running[/]")

    parts = Text()
    for name, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        if not count or name == "pending":
            continue
        parts.append(f"{count} {name}  ", style=_STATUS_STYLE.get(name, ""))
    table.add_row(parts, "")

    stats = (
        f"[dim]{agg['elapsed']}s · {agg['throughput']}/s · "
        f"mean {agg['mean_duration']}s · peak {agg['peak_running']}"
    )
    if agg["tokens_in"] or agg["tokens_out"]:
        stats += f" · {int(agg['tokens_in'])}in/{int(agg['tokens_out'])}out tok"
    if agg["cost_usd"]:
        stats += f" · ${agg['cost_usd']:.4f}"
    table.add_row(stats + "[/]", "")

    for entry in state.feed[-4:]:
        who = f"a{entry['index']:05d}" if entry["index"] >= 0 else "swarm"
        style = "red" if entry["level"] == "error" else "yellow"
        table.add_row(Text(f"  {who}  {entry['msg'][:96]}", style=style), "")

    return table


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host", help="Bind address"),
    port: int = typer.Option(8787, "--port", "-p"),
    specs: Path = typer.Option(
        Path("examples"), "--specs", help="Directory the dashboard can launch specs from"
    ),
    root: Path = typer.Option(Path(".swarmdeck"), "--root", help="Run data directory"),
    token: str = typer.Option(None, "--token", help="Require this bearer token"),
    no_launch: bool = typer.Option(
        False, "--no-launch", help="Serve read-only; refuse API launches"
    ),
    tick: float = typer.Option(0.1, "--tick", help="Dashboard update interval (s)"),
    open_browser: bool = typer.Option(False, "--open", help="Open the dashboard"),
) -> None:
    """Start the dashboard and control API."""
    import uvicorn

    from .server import ServerConfig, create_app, default_token

    loopback = host in ("127.0.0.1", "localhost", "::1")
    if not loopback and not token:
        token = default_token()
        err.print(
            "[bold yellow]This server can start processes on this host.[/]\n"
            f"Binding {host} without a token would expose that to your network, "
            "so one was generated:\n\n"
            f"    [bold]{token}[/]\n\n"
            f"Dashboard: http://{host}:{port}/?token={token}\n"
        )

    config = ServerConfig(
        root=root.resolve(),
        spec_dir=specs.resolve(),
        token=token,
        allow_launch=not no_launch,
        tick=tick,
    )
    application = create_app(config)

    url = f"http://{'127.0.0.1' if loopback else host}:{port}/"
    if token:
        url += f"?token={token}"
    console.print(f"[bold]swarmdeck[/] {__version__}  ->  [link={url}]{url}[/]")
    console.print(f"[dim]specs: {config.spec_dir}  ·  runs: {config.root}[/]")
    if open_browser:
        with contextlib.suppress(Exception):
            webbrowser.open(url)

    uvicorn.run(application, host=host, port=port, log_level="warning", access_log=False)


@app.command()
def demo(
    agents: int = typer.Option(500, "--agents", "-n", help="How many agents to simulate"),
    concurrency: int = typer.Option(100, "--concurrency", "-c"),
    serve_ui: bool = typer.Option(False, "--serve", help="Run it in the dashboard"),
) -> None:
    """Run the bundled simulated swarm -- no API keys, no dependencies."""
    spec_path = EXAMPLES / "demo.swarm.yaml"
    if not spec_path.is_file():
        _fail(f"demo spec not found at {spec_path} (are you running from a source checkout?)")

    if serve_ui:
        console.print("[dim]Start the server, then launch 'demo' from the UI.[/]")
        serve(  # type: ignore[call-arg]
            host="127.0.0.1",
            port=8787,
            specs=EXAMPLES,
            root=Path(".swarmdeck"),
            token=None,
            no_launch=False,
            tick=0.1,
            open_browser=True,
        )
        return

    spec = load_spec(spec_path)
    spec.tasks = [{"index": i} for i in range(agents)]
    spec.concurrency = max(1, min(concurrency, agents))
    raise typer.Exit(asyncio.run(_run(spec, capture_logs=True, quiet=False, results=None)))


def main() -> None:
    try:
        app()
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
