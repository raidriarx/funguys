"""Teammate Worker Node (`funguys join`) connecting a friend's laptop to the Host PC."""

from __future__ import annotations

import asyncio
import json
import platform
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
import websockets
from rich.console import Console

from funguys.worker.adapters import (
    build_graphify_task_prompt,
    generate_task_code_artifact,
    run_cli_agent_task,
)

console = Console()


class TeammateWorkerNode:
    """Connects a friend's AI subscription to the Host PC over WebSockets + REST."""

    def __init__(
        self,
        host_url: str,
        owner_name: str,
        profile_key: str = "claude-pro",
        initial_used_pct: float = 15.0,
        work_dir: Optional[Path] = None,
        use_real_cli: bool = False,
    ) -> None:
        self.host_url = host_url.rstrip("/")
        self.ws_url = self.host_url.replace("http://", "ws://").replace("https://", "wss://") + "/ws"
        self.owner_name = owner_name
        self.machine_name = platform.node() or "teammate-laptop"
        self.profile_key = profile_key
        self.initial_used_pct = initial_used_pct
        self.work_dir = work_dir or (Path.cwd() / ".funguys" / "worker_workspace")
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.use_real_cli = use_real_cli
        self.agent_info: Optional[Dict[str, Any]] = None

    async def register(self) -> Dict[str, Any]:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{self.host_url}/api/agents/register",
                json={
                    "owner_name": self.owner_name,
                    "machine_name": self.machine_name,
                    "profile_key": self.profile_key,
                    "initial_used_pct": self.initial_used_pct,
                },
            )
            resp.raise_for_status()
            self.agent_info = resp.json()
            return self.agent_info

    async def execute_assignment(self, assignment: Dict[str, Any]) -> Dict[str, Any]:
        task = assignment["task"]
        agent = assignment["agent"]
        subgraph_digest = assignment.get("subgraph_digest", "")
        contracts_md = assignment.get("contracts_markdown", "")

        console.print(
            f"[bold cyan]⚡ Executing Task:[/bold cyan] {task['title']} "
            f"on [green]{agent['display_name']}[/green]"
        )

        prompt = build_graphify_task_prompt(task, contracts_md, subgraph_digest)
        files_out: Dict[str, str] = {}
        rate_limited = False

        if self.use_real_cli:
            ok, rate_limited, files_out, _ = run_cli_agent_task(
                agent["provider"],
                self.work_dir,
                prompt,
                task.get("target_files", []),
            )

        if rate_limited:
            async with httpx.AsyncClient(timeout=15.0) as client:
                res = await client.post(
                    f"{self.host_url}/api/agents/{agent['id']}/quota",
                    json={
                        "used_pct": 100.0,
                        "turns_delta": 1,
                        "rate_limited": True,
                        "cooldown_minutes": 60,
                        "handoff_notes": "CLI hit subscription rate limit mid-task; handing off via Graphify.",
                    },
                )
                return res.json()

        if not files_out:
            files_out = generate_task_code_artifact(task)

        async with httpx.AsyncClient(timeout=30.0) as client:
            res = await client.post(
                f"{self.host_url}/api/tasks/{task['id']}/submit",
                json={
                    "agent_id": agent["id"],
                    "files": files_out,
                    "commit_message": f"feat({task['id']}): complete {task['title']}",
                },
            )
            res.raise_for_status()
            return res.json()

    async def listen_forever(self) -> None:
        info = await self.register()
        agent_id = info["id"]
        console.print(
            f"[bold green]✔ Connected to FunGuys Host![/bold green] Agent ID: [yellow]{agent_id}[/yellow]"
        )
        async with websockets.connect(self.ws_url) as ws:
            async for raw_msg in ws:
                data = json.loads(raw_msg)
                if data.get("type") == "task_assigned":
                    assignment = data["assignment"]
                    if assignment["agent"]["id"] == agent_id:
                        await self.execute_assignment(assignment)
