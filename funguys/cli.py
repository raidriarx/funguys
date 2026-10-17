"""Command-Line Interface (`funguys host`, `funguys join`, `funguys simulate`, `funguys graph`)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Optional

import typer
import uvicorn
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from funguys.host.app import create_host_app
from funguys.host.graph_engine import GraphifyEngine
from funguys.host.tunnel import get_lan_ip, get_public_tunnel_url, start_cloudflare_tunnel
from funguys.worker.adapters import detect_available_clis
from funguys.worker.node import TeammateWorkerNode

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

app = typer.Typer(
    name="funguys",
    help="FunGuys — Multiplayer, Usage-Aware Multi-AI Collaborative Engineering Platform",
    add_completion=False,
)
console = Console(legacy_windows=False)


@app.command("host")
def host_command(
    port: int = typer.Option(8420, "--port", "-p", help="Port for the Host PC server"),
    host: str = typer.Option("0.0.0.0", "--host", help="Bind address"),
    tunnel: bool = typer.Option(
        False, "--tunnel", "-t", help="Open a public Cloudflare tunnel for remote friends"
    ),
) -> None:
    """Start the Central Host PC Server, Graphify Engine, 3-Layer QA Pipeline, and Web Dashboard."""
    lan_ip = get_lan_ip()
    public_url: Optional[str] = get_public_tunnel_url()
    tunnel_proc = None

    if tunnel and not public_url:
        tunnel_proc, public_url = start_cloudflare_tunnel(port)

    join_target = public_url or f"http://{lan_ip}:{port}"
    console.print(
        Panel.fit(
            f"[bold green][FunGuys] Central Host PC Online![/bold green]\n\n"
            f"* [bold]Mission Control Dashboard:[/bold] [cyan]http://localhost:{port}[/cyan]\n"
            f"* [bold]LAN Dashboard / Join URL:[/bold]  [cyan]http://{lan_ip}:{port}[/cyan]\n"
            + (
                f"* [bold]Public Tunnel URL:[/bold]         [magenta]{public_url}[/magenta]\n"
                if public_url
                else ""
            )
            + f"\n[bold yellow]Teammate Join Command:[/bold yellow]\n"
            f"  [white]uv run funguys join {join_target} --name \"Alex\" --profile claude-max[/white]",
            title="FunGuys Host Server",
            border_style="green",
        )
    )

    fastapi_app = create_host_app(Path.cwd())
    try:
        uvicorn.run(fastapi_app, host=host, port=port, log_level="info")
    finally:
        if tunnel_proc:
            tunnel_proc.terminate()


@app.command("join")
def join_command(
    host_url: str = typer.Argument(..., help="Host PC URL (e.g. http://192.168.1.50:8420)"),
    name: str = typer.Option("Teammate", "--name", "-n", help="Your name"),
    profile: Optional[str] = typer.Option(
        None,
        "--profile",
        "-p",
        help="Subscription profile: claude-max, claude-pro, codex-pro, gemini-cli, api-fallback",
    ),
    used_pct: float = typer.Option(
        15.0, "--used-pct", help="Current estimated % of your subscription window already used"
    ),
    real_cli: bool = typer.Option(
        False, "--real-cli", help="Invoke installed local CLI binary (claude/gemini/codex)"
    ),
) -> None:
    """Connect your laptop's AI subscription to a friend's FunGuys Host PC."""
    detected = detect_available_clis()
    chosen_profile = profile or (detected[0] if detected else "claude-pro")
    console.print(
        f"[bold cyan][FunGuys] Joining Host at {host_url}...[/bold cyan]\n"
        f"* Teammate: [bold]{name}[/bold]\n"
        f"* Detected Local CLIs: [yellow]{', '.join(detected) or 'none (using built-in adapter)'}[/yellow]\n"
        f"* Active Profile: [green]{chosen_profile}[/green] ({used_pct}% window used)"
    )
    node = TeammateWorkerNode(
        host_url=host_url,
        owner_name=name,
        profile_key=chosen_profile,
        initial_used_pct=used_pct,
        use_real_cli=real_cli,
    )
    asyncio.run(node.listen_forever())


@app.command("graph")
def graph_command(
    workspace: Path = typer.Argument(Path("."), help="Target project workspace directory"),
    query: Optional[str] = typer.Option(None, "--query", "-q", help="Run a Graphify query"),
) -> None:
    """Compile the deterministic Graphify AST knowledge graph (`GRAPH_REPORT.md` & `graph.json`)."""
    engine = GraphifyEngine(workspace.resolve())
    data = engine.build_graph()
    console.print(
        f"[bold green][OK] Graphify AST Pass Complete![/bold green] "
        f"Nodes: [cyan]{data['node_count']}[/cyan] | Edges: [cyan]{data['edge_count']}[/cyan] | "
        f"Clusters: [cyan]{len(data['clusters'])}[/cyan]"
    )
    if query:
        console.print(engine.query(query))


@app.command("simulate")
def simulate_command() -> None:
    """Run an end-to-end 3-friend multi-AI hackathon simulation locally and print results."""
    from fastapi.testclient import TestClient

    fastapi_app = create_host_app(Path.cwd())
    client = TestClient(fastapi_app)
    resp = client.post("/api/simulate/hackathon")
    data = resp.json()

    table = Table(title="FunGuys 3-Friend Multi-AI Hackathon Simulation Results")
    table.add_column("Task ID", style="cyan")
    table.add_column("Title", style="white")
    table.add_column("Tier", style="yellow")
    table.add_column("Status", style="green")
    table.add_column("QA Retries", style="magenta")

    for t in data["tasks"]:
        table.add_row(
            t["id"],
            t["title"],
            f"Tier {t['complexity_tier']}",
            t["status"].upper(),
            str(t["retry_count"]),
        )
    console.print(table)
    console.print(
        f"\n[bold green][OK] Graphify AST Nodes Mapped:[/bold green] {data['graph']['node_count']} nodes, "
        f"{len(data['contract_alerts'])} Contract Deltas broadcasted, "
        f"{len(data['qa_reports'])} Autonomous 3-Layer QA runs completed!"
    )


if __name__ == "__main__":
    app()
