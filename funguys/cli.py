"""FunGuys command-line host, worker, graph and isolated demo commands."""
from __future__ import annotations
import asyncio
import sys
from pathlib import Path
from typing import Optional
import typer
import uvicorn
from rich.console import Console
from funguys.host.app import create_host_app
from funguys.host.graph_engine import GraphifyEngine
from funguys.host.tunnel import start_cloudflare_tunnel, stop_cloudflare_tunnel,get_public_tunnel_url
from funguys.worker.adapters import detect_available_clis
from funguys.worker.node import TeammateWorkerNode

app = typer.Typer(name='funguys',help='FunGuys collaborative engineering workflow',add_completion=False)
console = Console(legacy_windows=False)

@app.command('host')
def host_command(port:int=typer.Option(8420,'--port','-p'),host:str=typer.Option('127.0.0.1','--host'),tunnel:bool=typer.Option(False,'--tunnel/--no-tunnel','-t')):
    """Start locally by default; public tunneling is explicit and authenticated."""
    public_url = None
    if tunnel:
        _,public_url = start_cloudflare_tunnel(port)
        if not public_url:
            console.print('Tunnel unavailable or startup timed out. Starting local host; install/configure cloudflared to enable public invites.')
    try:
        server = create_host_app(Path.cwd(),public_url=public_url,bind_host=host,tunnel_url_provider=get_public_tunnel_url if tunnel else None)
        console.print(f'Mission Control: http://localhost:{port}')
        console.print('Administrator token file: ' + str(Path.cwd()/'.funguys'/'admin_token'))
        console.print('Sign in using that token. Generate short-lived worker invites in Mission Control.')
        if public_url:
            console.print(f'Public dashboard: {public_url}')
        uvicorn.run(server,host=host,port=port,log_level='info')
    finally:
        if tunnel:
            stop_cloudflare_tunnel()

@app.command('join')
def join_command(host_url:str,name:str=typer.Option('Teammate','--name','-n'),profile:Optional[str]=typer.Option(None,'--profile','-p'),used_pct:float=typer.Option(0,'--used-pct',min=0,max=100),token:str=typer.Option('', '--token',envvar='FUNGUYS_WORKER_TOKEN'),codename:str=typer.Option('','--codename')):
    """Run a real local CLI worker with a dashboard-issued invite or saved credential."""
    detected = detect_available_clis()
    chosen = profile or (detected[0] if detected else 'codex-pro')
    node = TeammateWorkerNode(host_url,name,chosen,used_pct,token=token,machine_name=codename)
    asyncio.run(node.listen_forever())

@app.command('graph')
def graph_command(workspace:Path=typer.Argument(Path('.')),query:Optional[str]=typer.Option(None,'--query','-q')):
    engine = GraphifyEngine(workspace.resolve())
    data = engine.build_graph()
    console.print(f"Nodes: {data['node_count']}; edges: {data['edge_count']}")
    if query:
        console.print(engine.query(query))

@app.command('simulate')
def simulate_command():
    """Run a disposable demo without replacing the active project or agent registry."""
    from funguys.host.demo import simulate_demo
    data = asyncio.run(simulate_demo(Path.cwd()/'.funguys'/'simulations'))
    for task in data['tasks']:
        console.print(f"{task['id']}: {task['status']} (QA retries: {task['retry_count']})")
    console.print(data['note'])

if __name__=='__main__':
    app()
