"""FastAPI + WebSocket Host Server for FunGuys."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from pydantic import BaseModel, Field

from funguys.config import SUBSCRIPTION_PROFILES
from funguys.db import StateStore
from funguys.host.orchestrator import FunGuysOrchestrator
from funguys.host.tunnel import get_lan_ip, get_public_tunnel_url
from funguys.mcp_bridge.server import fetch_docs_markdown, web_search
from funguys.models import (
    ApprovePlanRequest,
    ClaimTaskHumanRequest,
    CreateProjectRequest,
    RegisterAgentRequest,
    ReportQuotaRequest,
    TaskItem,
    TaskStatus,
)
from funguys.worker.adapters import generate_task_code_artifact


class SubmitTaskRequest(BaseModel):
    agent_id: str
    files: Dict[str, str]
    commit_message: str = "Complete task implementation"
    used_pct_after: Optional[float] = None


class CreateTaskRequest(BaseModel):
    id: str
    title: str
    description: str
    complexity_tier: int = 2
    dependencies: List[str] = Field(default_factory=list)
    target_files: List[str] = Field(default_factory=list)


class GraphQueryRequest(BaseModel):
    query: str
    target: Optional[str] = None


class WebSearchRequest(BaseModel):
    query: str
    max_results: int = 5


class FetchDocsRequest(BaseModel):
    url: str


class StartGrillRequest(BaseModel):
    project_name: str
    project_idea: str
    lead_architect_agent_id: Optional[str] = None
    workspace_path: Optional[str] = None
    continue_existing: bool = False


class AnswerGrillRequest(BaseModel):
    answer: str


class AskAgentRequest(BaseModel):
    question: str
    preferred_agent_id: Optional[str] = "auto"


def create_host_app(base_dir: Optional[Path] = None) -> FastAPI:
    from funguys.host.architect import GrillArchitectEngine

    root_dir = base_dir or Path.cwd()
    state_dir = root_dir / ".funguys"
    state_dir.mkdir(parents=True, exist_ok=True)

    app = FastAPI(
        title="FunGuys Host Server",
        description="Multiplayer, Usage-Aware Multi-AI Collaborative Engineering Platform",
        version="0.1.0",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    ws_clients: Set[WebSocket] = set()

    async def broadcast(payload: Dict[str, Any]) -> None:
        dead: List[WebSocket] = []
        for ws in list(ws_clients):
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            ws_clients.discard(ws)

    store = StateStore(state_dir / "state.db")
    orchestrator = FunGuysOrchestrator(
        store=store,
        default_workspace=state_dir / "workspaces",
        broadcast_cb=broadcast,
    )
    grill_engine = GrillArchitectEngine()
    grill_session_file = state_dir / "grill_session.json"
    if grill_session_file.exists():
        try:
            from funguys.host.architect import GrillSession

            grill_engine.session = GrillSession.model_validate_json(
                grill_session_file.read_text(encoding="utf-8")
            )
        except Exception:
            pass
    app.state.store = store
    app.state.orchestrator = orchestrator
    app.state.grill_engine = grill_engine

    def discover_local_workspaces() -> List[Dict[str, Any]]:
        discovered: List[Dict[str, Any]] = []
        ws_root = state_dir / "workspaces"
        candidate_dirs: List[Path] = []
        if ws_root.exists():
            for d in sorted(ws_root.iterdir()):
                if d.is_dir():
                    candidate_dirs.append(d.resolve())
        curr_proj = store.get_project()
        if curr_proj and curr_proj.workspace_path:
            cp = Path(curr_proj.workspace_path).resolve()
            if cp.exists() and cp not in candidate_dirs:
                candidate_dirs.insert(0, cp)

        for d in candidate_dirs:
            files_list: List[str] = []
            for p in sorted(d.rglob("*")):
                rel_parts = p.relative_to(d).parts
                if p.is_file() and not any(part in {".git", ".funguys", "__pycache__", "node_modules", ".venv", ".pytest_cache", ".ruff_cache"} for part in rel_parts):
                    files_list.append(p.relative_to(d).as_posix())
            proj_name = d.name.replace("_", " ").title()
            proj_idea = ""
            contracts_file = d / "CONTRACTS.md"
            if contracts_file.exists():
                try:
                    c_text = contracts_file.read_text(encoding="utf-8", errors="ignore")
                    for line in c_text.splitlines():
                        if line.startswith("# CONTRACTS.md"):
                            parts = line.split("—", 1) if "—" in line else line.split("-", 1)
                            if len(parts) == 2 and parts[1].strip():
                                proj_name = parts[1].strip()
                        elif "**Core Concept:**" in line:
                            proj_idea = line.split("**Core Concept:**", 1)[1].strip()
                except Exception:
                    pass
            discovered.append(
                {
                    "path": str(d),
                    "folder_name": d.name,
                    "project_name": proj_name,
                    "project_idea": proj_idea,
                    "file_count": len(files_list),
                    "files": files_list[:20],
                }
            )
        return discovered

    def build_full_state() -> Dict[str, Any]:
        snap = store.snapshot()
        graph_json_path = orchestrator.graph.graph_dir / "graph.json"
        if not graph_json_path.exists():
            graph_data = orchestrator.graph.build_graph()
        else:
            import json

            graph_data = json.loads(graph_json_path.read_text(encoding="utf-8"))

        report_md_path = orchestrator.graph.graph_dir / "GRAPH_REPORT.md"
        graph_report_md = (
            report_md_path.read_text(encoding="utf-8") if report_md_path.exists() else ""
        )

        try:
            grill_session_file.write_text(
                grill_engine.session.model_dump_json(indent=2), encoding="utf-8"
            )
        except Exception:
            pass

        return {
            "lan_ip": get_lan_ip(),
            "public_url": get_public_tunnel_url(),
            "grill": grill_engine.session.model_dump(),
            "available_workspaces": discover_local_workspaces(),
            "chat_history": list(orchestrator.chat_history),
            "project": snap["project"].model_dump() if snap["project"] else None,
            "tasks": [t.model_dump() for t in snap["tasks"]],
            "agents": [a.model_dump() for a in snap["agents"]],
            "contract_alerts": [c.model_dump() for c in snap["contract_alerts"]],
            "qa_reports": [q.model_dump() for q in snap["qa_reports"]],
            "events": [e.model_dump() for e in snap["events"]],
            "graph": graph_data,
            "graph_report_markdown": graph_report_md,
            "profiles": {
                k: {
                    "provider": v.provider,
                    "display_name": v.display_name,
                    "tier": v.tier,
                    "window_hours": v.window_hours,
                    "soft_handoff_threshold_pct": v.soft_handoff_threshold_pct,
                    "strengths": list(v.strengths),
                }
                for k, v in SUBSCRIPTION_PROFILES.items()
            },
        }

    @app.get("/api/state")
    async def get_state() -> Dict[str, Any]:
        return build_full_state()

    @app.get("/api/workspace/snapshot")
    async def get_workspace_snapshot() -> Dict[str, Any]:
        ws_dir = orchestrator.git.workspace_dir
        files_map: Dict[str, str] = {}
        if ws_dir.exists():
            for p in ws_dir.rglob("*"):
                rel_parts = p.relative_to(ws_dir).parts
                if p.is_file() and not any(part in {".git", ".funguys", "__pycache__", "node_modules", ".venv", ".pytest_cache", ".ruff_cache"} for part in rel_parts):
                    rel = p.relative_to(ws_dir).as_posix()
                    try:
                        if p.stat().st_size <= 250_000:
                            files_map[rel] = p.read_text(encoding="utf-8")
                    except Exception:
                        pass
        return {"workspace_path": str(ws_dir), "files": files_map}

    @app.post("/api/grill/start")
    async def start_grill(req: StartGrillRequest) -> Dict[str, Any]:
        import asyncio
        from funguys.host.git_manager import GitWorkspaceManager
        from funguys.host.graph_engine import GraphifyEngine
        from funguys.host.qa_pipeline import AutonomousQAPipeline

        loop = asyncio.get_running_loop()

        def progress_cb(stage: str, detail: str, error: Optional[str] = None) -> None:
            payload = {
                "type": "grill_phase",
                "stage": stage,
                "detail": detail,
                "error": error,
                "grill": grill_engine.session.model_dump(),
            }
            asyncio.run_coroutine_threadsafe(broadcast(payload), loop)

        agents = store.list_agents()
        lead_agent = None
        if req.lead_architect_agent_id:
            lead_agent = next((a for a in agents if a.id == req.lead_architect_agent_id), None)
        if not lead_agent and agents:
            lead_agent = sorted(agents, key=lambda a: (a.tier, a.used_pct))[0]

        lead_id = lead_agent.id if lead_agent else None
        lead_label = f"[{lead_agent.machine_name}] {lead_agent.display_name}" if lead_agent else "Lead Architect AI"

        project = store.get_project()
        target_ws = req.workspace_path.strip() if req.workspace_path else (project.workspace_path if project and req.continue_existing else "")
        if target_ws:
            ws_path = Path(target_ws).resolve()
            ws_path.mkdir(parents=True, exist_ok=True)
            target_ws = str(ws_path)
            if project:
                project.workspace_path = target_ws
                store.save_project(project)
            orchestrator.git = GitWorkspaceManager(ws_path)
            orchestrator.git.ensure_initialized(project.github_url if project else None)
            orchestrator.graph = GraphifyEngine(ws_path)
            orchestrator.qa = AutonomousQAPipeline(ws_path)
            orchestrator.graph.build_graph()

        existing_tasks = store.list_tasks() if (req.continue_existing or target_ws) else []
        session = await asyncio.to_thread(
            grill_engine.start_interview,
            req.project_name,
            req.project_idea,
            lead_id,
            lead_label,
            target_ws or None,
            req.continue_existing,
            existing_tasks,
            progress_cb,
        )
        mode_tag = f" [CONTINUING WORKSPACE: {session.existing_files_count} existing files]" if session.continue_existing else ""
        await orchestrator.emit_event(
            lead_agent.machine_name if lead_agent else "architect-ai",
            "grill_started",
            f"🔥 **{lead_label}** started Grill-Me Architect Interview{mode_tag} for **{session.project_name}**: \"{session.project_idea}\"",
        )
        state = build_full_state()
        await broadcast({"type": "state_sync", "state": state})
        return state

    @app.post("/api/grill/answer")
    async def answer_grill(req: AnswerGrillRequest) -> Dict[str, Any]:
        import asyncio
        from funguys.host.architect import GrillQA

        loop = asyncio.get_running_loop()

        def progress_cb(stage: str, detail: str, error: Optional[str] = None) -> None:
            payload = {
                "type": "grill_phase",
                "stage": stage,
                "detail": detail,
                "error": error,
                "grill": grill_engine.session.model_dump(),
            }
            asyncio.run_coroutine_threadsafe(broadcast(payload), loop)

        ans_lower = req.answer.strip().lower()
        curr_q = grill_engine.session.current_question
        q_text_lower = (curr_q.question.lower() if curr_q else "")
        should_auto_compile = (
            any(kw in ans_lower for kw in ["start building", "build the complete", "start now", "compile plan", "stop asking"])
            or (curr_q and curr_q.ready_to_compile_hint and any(kw in ans_lower for kw in ["build", "proceed", "start", "recommended", "finalize"]))
            or ("specification is complete" in q_text_lower)
        )

        if should_auto_compile:
            if curr_q:
                grill_engine.session.qa_history.append(
                    GrillQA(question=curr_q.question, answer=req.answer.strip())
                )
            await compile_grill_plan()
            await orchestrator.approve_plan()
            asyncio.create_task(orchestrator.run_autonomous_execution_loop(delay_sec=3.0))
            state = build_full_state()
            await broadcast({"type": "state_sync", "state": state})
            return state

        await asyncio.to_thread(grill_engine.submit_answer, req.answer, progress_cb)
        state = build_full_state()
        await broadcast({"type": "state_sync", "state": state})
        return state

    @app.post("/api/grill/compile")
    async def compile_grill_plan() -> Dict[str, Any]:
        import asyncio

        loop = asyncio.get_running_loop()

        def progress_cb(stage: str, detail: str, error: Optional[str] = None) -> None:
            payload = {
                "type": "grill_phase",
                "stage": stage,
                "detail": detail,
                "error": error,
                "grill": grill_engine.session.model_dump(),
            }
            asyncio.run_coroutine_threadsafe(broadcast(payload), loop)

        agents_dump = [a.model_dump() for a in store.list_agents()]
        existing_tasks = store.list_tasks()
        draft = await asyncio.to_thread(
            grill_engine.compile_plan_and_tasks,
            agents_dump,
            existing_tasks,
            progress_cb,
        )
        session = grill_engine.session
        project = store.get_project()
        if project:
            project.name = session.project_name
            project.description = session.project_idea
            project.lead_architect_agent_id = session.lead_architect_agent_id
            if session.workspace_path:
                project.workspace_path = session.workspace_path
            project.contracts_markdown = draft["contracts_markdown"]
            project.plan_approved = False
            store.save_project(project)
            ws_dir = Path(project.workspace_path)
            ws_dir.mkdir(parents=True, exist_ok=True)
            (ws_dir / "CONTRACTS.md").write_text(
                draft["contracts_markdown"], encoding="utf-8"
            )
            orchestrator.graph.build_graph()

        if session.continue_existing:
            # Keep already completed tasks from the previous phase(s) and add new continuation tasks
            completed_previous = [t for t in existing_tasks if t.status == TaskStatus.COMPLETED]
            new_ids = {t.id for t in draft["tasks"]}
            store.clear_tasks()
            for ct in completed_previous:
                if ct.id not in new_ids:
                    store.save_task(ct)
            for t in draft["tasks"]:
                store.save_task(t)
        else:
            store.clear_tasks()
            for t in draft["tasks"]:
                store.save_task(t)

        await orchestrator.emit_event(
            session.lead_architect_label,
            "plan_drafted",
            f"📐 **{session.lead_architect_label}** compiled `CONTRACTS.md` and {len(draft['tasks'])} {'continuation ' if session.continue_existing else ''}task(s) for **{session.project_name}**. Review & click `[ APPROVE_PLAN_&_UNLOCK_AIS ]`!",
        )
        state = build_full_state()
        await broadcast({"type": "state_sync", "state": state})
        return state

    @app.post("/api/project/init")
    async def init_project(req: CreateProjectRequest) -> Dict[str, Any]:
        await orchestrator.initialize_project(
            name=req.name,
            description=req.description,
            workspace_path=req.workspace_path,
            github_url=req.github_url,
        )
        state = build_full_state()
        await broadcast({"type": "state_sync", "state": state})
        return state

    @app.post("/api/project/approve")
    async def approve_project_plan(req: ApprovePlanRequest) -> Dict[str, Any]:
        await orchestrator.approve_plan(req.contracts_markdown)
        asyncio.create_task(orchestrator.run_autonomous_execution_loop(delay_sec=3.0))
        state = build_full_state()
        await broadcast({"type": "state_sync", "state": state})
        return state

    @app.post("/api/agents/register")
    async def register_agent(req: RegisterAgentRequest) -> Dict[str, Any]:
        agent = await orchestrator.register_agent(
            owner_name=req.owner_name,
            machine_name=req.machine_name,
            profile_key=req.profile_key,
            initial_used_pct=req.initial_used_pct,
        )
        await broadcast({"type": "state_sync", "state": build_full_state()})
        return agent.model_dump()

    @app.post("/api/agents/{agent_id}/quota")
    async def report_agent_quota(agent_id: str, req: ReportQuotaRequest) -> Dict[str, Any]:
        try:
            res = await orchestrator.report_quota_and_check_handoff(
                agent_id=agent_id,
                used_pct=req.used_pct,
                turns_delta=req.turns_delta,
                rate_limited=req.rate_limited,
                cooldown_minutes=req.cooldown_minutes,
                handoff_notes=req.handoff_notes,
            )
            await broadcast({"type": "state_sync", "state": build_full_state()})
            return res
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @app.post("/api/agents/{agent_id}/rename")
    async def rename_agent(agent_id: str, body: Dict[str, str]) -> Dict[str, Any]:
        agent = store.get_agent(agent_id)
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")
        codename = (body.get("codename") or "").strip()
        owner_name = (body.get("owner_name") or "").strip()
        if codename:
            agent.machine_name = codename
        if owner_name:
            agent.owner_name = owner_name
            profile = SUBSCRIPTION_PROFILES.get(agent.profile_key, SUBSCRIPTION_PROFILES["claude-pro"])
            agent.display_name = f"{owner_name}'s {profile.display_name}"
        elif codename:
            profile = SUBSCRIPTION_PROFILES.get(agent.profile_key, SUBSCRIPTION_PROFILES["claude-pro"])
            agent.display_name = f"[{codename}] {profile.display_name}"
        store.save_agent(agent)
        await orchestrator.emit_event(
            agent.id,
            "agent_renamed",
            f"Renamed node `{agent_id}` to Code Name: **{agent.machine_name}** ({agent.display_name})",
        )
        await broadcast({"type": "state_sync", "state": build_full_state()})
        return agent.model_dump()

    @app.delete("/api/agents/{agent_id}")
    async def delete_agent(agent_id: str) -> Dict[str, str]:
        with store._connect() as conn:
            conn.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
        await broadcast({"type": "state_sync", "state": build_full_state()})
        return {"deleted": agent_id}

    @app.post("/api/tasks")
    async def create_task(req: CreateTaskRequest) -> Dict[str, Any]:
        task = TaskItem(
            id=req.id,
            title=req.title,
            description=req.description,
            complexity_tier=req.complexity_tier,
            dependencies=req.dependencies,
            target_files=req.target_files,
            status=TaskStatus.PENDING if req.dependencies else TaskStatus.READY,
            branch_name=f"task/{req.id}",
        )
        store.save_task(task)
        await orchestrator.schedule_ready_tasks()
        await broadcast({"type": "state_sync", "state": build_full_state()})
        return task.model_dump()

    @app.delete("/api/tasks")
    async def clear_all_tasks() -> Dict[str, Any]:
        with store._connect() as conn:
            conn.execute("DELETE FROM tasks")
            conn.execute("DELETE FROM contract_alerts")
            conn.execute("DELETE FROM qa_reports")
        for agent in store.list_agents():
            agent.current_task_id = None
            if agent.status.value == "working":
                from funguys.models import AgentStatus
                agent.status = AgentStatus.IDLE
            store.save_agent(agent)
        await orchestrator.emit_event("human-team", "tasks_cleared", "Cleared task board for a fresh start.")
        state = build_full_state()
        await broadcast({"type": "state_sync", "state": state})
        return state

    @app.post("/api/tasks/{task_id}/submit")
    async def submit_task(task_id: str, req: SubmitTaskRequest) -> Dict[str, Any]:
        try:
            files_to_use = dict(req.files or {})
            has_escaped_newlines = any("`n" in str(v) for v in files_to_use.values())
            if not files_to_use or has_escaped_newlines:
                task_obj = store.get_task(task_id)
                if task_obj:
                    if has_escaped_newlines and task_obj.retry_count > 0:
                        task_obj.retry_count = 0
                        store.save_task(task_obj)
                    import asyncio
                    from funguys.worker.adapters import build_graphify_task_prompt, run_cli_agent_task

                    project_obj = store.get_project()
                    contracts_md = project_obj.contracts_markdown if project_obj else ""
                    subgraph_ctx = orchestrator.graph.get_subgraph_digest(task_obj.target_files)
                    prompt = build_graphify_task_prompt(task_obj.model_dump(), contracts_md, subgraph_ctx)
                    ok, _rl, cli_files, _out = await asyncio.to_thread(
                        run_cli_agent_task,
                        "codex",
                        orchestrator.git.workspace_dir,
                        prompt,
                        task_obj.target_files,
                        90,
                    )
                    if ok and cli_files:
                        files_to_use = cli_files
                    else:
                        files_to_use = generate_task_code_artifact(task_obj.model_dump())

            res = await orchestrator.submit_task_work(
                task_id=task_id,
                agent_id=req.agent_id,
                files=files_to_use,
                commit_message=req.commit_message,
                used_pct_after=req.used_pct_after,
            )
            await broadcast({"type": "state_sync", "state": build_full_state()})
            return res
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @app.post("/api/tasks/{task_id}/claim-human")
    async def claim_task_human(task_id: str, req: ClaimTaskHumanRequest) -> Dict[str, Any]:
        try:
            task = await orchestrator.claim_task_by_human(task_id, req.human_name)
            await broadcast({"type": "state_sync", "state": build_full_state()})
            return task.model_dump()
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @app.post("/api/tasks/{task_id}/reset-circuit-breaker")
    async def reset_cb(task_id: str) -> Dict[str, Any]:
        try:
            task = await orchestrator.reset_circuit_breaker(task_id)
            await broadcast({"type": "state_sync", "state": build_full_state()})
            return task.model_dump()
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @app.post("/api/chat/ask")
    async def ask_agent_question(req: AskAgentRequest) -> Dict[str, Any]:
        entry = await orchestrator.ask_project_question(
            question=req.question,
            preferred_agent_id=req.preferred_agent_id,
        )
        await broadcast({"type": "state_sync", "state": build_full_state()})
        return entry

    @app.delete("/api/chat")
    async def clear_chat_history() -> Dict[str, Any]:
        orchestrator.chat_history.clear()
        await broadcast({"type": "state_sync", "state": build_full_state()})
        return {"cleared": True}

    @app.post("/api/graphify/query")
    async def graphify_query(req: GraphQueryRequest) -> Dict[str, str]:
        return {"result": orchestrator.graph.query(req.query)}

    @app.post("/api/graphify/path")
    async def graphify_path(req: GraphQueryRequest) -> Dict[str, str]:
        return {"result": orchestrator.graph.path_between(req.query, req.target or "")}

    @app.post("/api/graphify/explain")
    async def graphify_explain(req: GraphQueryRequest) -> Dict[str, str]:
        return {"result": orchestrator.graph.explain_node(req.query)}

    @app.post("/api/tools/web-search")
    async def tool_web_search(req: WebSearchRequest) -> Dict[str, Any]:
        results = await web_search(req.query, req.max_results)
        return {"results": results}

    @app.post("/api/tools/fetch-docs")
    async def tool_fetch_docs(req: FetchDocsRequest) -> Dict[str, str]:
        md = await fetch_docs_markdown(req.url)
        return {"markdown": md}

    @app.post("/api/simulate/hackathon")
    async def simulate_hackathon_run() -> Dict[str, Any]:
        """Run a full multiplayer simulation showing Tiered Routing, Graphify Deltas, 80% Baton Pass, and QA Bug Auto-Fix."""
        # 1. Reset clean demo project
        await orchestrator.initialize_project(
            name="FunGuys Hackathon Live Demo",
            description="Collaborative real-time full-stack app built by 3 friends' pooled AI subscriptions.",
        )
        # Clear previous agents for clean simulation
        with store._connect() as conn:
            conn.execute("DELETE FROM agents")

        # 2. Connect 3 friends' laptops with different subscription tiers & current usage
        alex_claude = await orchestrator.register_agent(
            owner_name="Alex",
            machine_name="alex-macbook",
            profile_key="claude-max",
            initial_used_pct=68.0,  # Close to 80% soft limit!
        )
        jordan_codex = await orchestrator.register_agent(
            owner_name="Jordan",
            machine_name="jordan-thinkpad",
            profile_key="codex-pro",
            initial_used_pct=25.0,
        )
        sam_gemini = await orchestrator.register_agent(
            owner_name="Sam",
            machine_name="sam-zephyrus",
            profile_key="gemini-cli",
            initial_used_pct=10.0,
        )

        # 3. Human team approves the Contract-First Plan -> assigns `task-1-contracts` (Tier 1) to Jordan's Codex (lowest used_pct Tier-1)
        await orchestrator.approve_plan()
        t1 = store.get_task("task-1-contracts")
        assert t1 is not None and t1.assigned_agent_id is not None
        first_tier1_agent = t1.assigned_agent_id

        # 4. Simulate `first_tier1_agent` hitting 82% quota mid-task -> Automated Baton Pass to the other Tier-1 AI!
        await orchestrator.report_quota_and_check_handoff(
            agent_id=first_tier1_agent,
            used_pct=82.5,
            turns_delta=12,
            rate_limited=False,
            handoff_notes="Defined module skeleton for `backend/service.py`; reached 82.5% soft quota limit. Passing baton via Graphify subgraph.",
            wip_files={"backend/__init__.py": "", "backend/service.py": "# WIP scaffold\n"},
        )

        # Now the other Tier-1 AI has automatically received `task-1-contracts` via Baton Pass!
        t1_after_handoff = store.get_task("task-1-contracts")
        assert t1_after_handoff is not None and t1_after_handoff.assigned_agent_id is not None
        second_tier1_agent = t1_after_handoff.assigned_agent_id

        # 5. Second Tier-1 AI finishes `task-1-contracts`, triggering Graphify Contract Deltas & unlocking `task-2-frontend` and `task-3-tests`
        t1_files = generate_task_code_artifact(t1_after_handoff.model_dump())
        await orchestrator.submit_task_work(
            task_id="task-1-contracts",
            agent_id=second_tier1_agent,
            files=t1_files,
            commit_message="feat(contracts): implement create_item, list_items, compute_metrics",
            used_pct_after=74.0,
        )

        # 6. `task-2-frontend` (Tier 2) and `task-3-tests` (Tier 2) are now unlocked!
        t2 = store.get_task("task-2-frontend")
        if t2 and t2.assigned_agent_id:
            t2_files = generate_task_code_artifact(t2.model_dump())
            await orchestrator.submit_task_work(
                task_id="task-2-frontend",
                agent_id=t2.assigned_agent_id,
                files=t2_files,
                commit_message="feat(ui): build responsive dashboard index.html",
            )

        # 7. `task-3-tests` (Tier 2) runs: first submission has a bug caught by Layer-2 Pytest QA, then auto-fixed on retry!
        t3 = store.get_task("task-3-tests")
        if t3 and t3.assigned_agent_id:
            buggy_files = generate_task_code_artifact(
                t3.model_dump(), inject_bug_on_first_try=True
            )
            await orchestrator.submit_task_work(
                task_id="task-3-tests",
                agent_id=t3.assigned_agent_id,
                files=buggy_files,
                commit_message="test(service): initial unit tests",
            )

            # Now `task-3-tests` is automatically re-scheduled with retry_count=1! Submit the fixed version:
            t3_retry = store.get_task("task-3-tests")
            if t3_retry and t3_retry.assigned_agent_id:
                fixed_files = generate_task_code_artifact(
                    t3_retry.model_dump(), inject_bug_on_first_try=False
                )
                await orchestrator.submit_task_work(
                    task_id="task-3-tests",
                    agent_id=t3_retry.assigned_agent_id,
                    files=fixed_files,
                    commit_message="fix(tests): fix completion_rate assertion in test_compute_metrics",
                )

        state = build_full_state()
        await broadcast({"type": "state_sync", "state": state})
        return state

    @app.get("/screenshots/{filename}")
    async def get_screenshot(filename: str) -> FileResponse:
        path = orchestrator.qa.screenshots_dir / filename
        if not path.exists():
            raise HTTPException(status_code=404, detail="Screenshot not found")
        media_type = "image/svg+xml" if filename.endswith(".svg") else "image/png"
        return FileResponse(path, media_type=media_type)

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket) -> None:
        await ws.accept()
        ws_clients.add(ws)
        try:
            await ws.send_json({"type": "state_sync", "state": build_full_state()})
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            ws_clients.discard(ws)
        except Exception:
            ws_clients.discard(ws)

    @app.get("/join.ps1", response_class=PlainTextResponse)
    async def serve_powershell_join_script(
        request: Request,
        name: str = "My PC",
        codename: str = "",
        profile: str = "codex-pro",
        used_pct: float = 15.0,
    ) -> PlainTextResponse:
        import re

        name = re.sub(r"\s+", " ", name).strip() or "My PC"
        codename = re.sub(r"\s+", "", codename).strip()
        profile = re.sub(r"\s+", "", profile).strip().lower()
        if "codex" in profile or profile not in SUBSCRIPTION_PROFILES:
            if "gemini" in profile:
                profile = "gemini-cli"
            elif "claude" in profile:
                profile = "claude-pro"
            else:
                profile = "codex-pro"
        base_url = str(request.base_url).rstrip("/")
        if request.headers.get("x-forwarded-proto") == "https" and base_url.startswith("http://"):
            base_url = "https://" + base_url[len("http://") :]
        ps_script = f"""# FunGuys Zero-Install Remote Worker Script for Windows PowerShell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force -ErrorAction SilentlyContinue
$ErrorActionPreference = "Stop"
$HostUrl = "{base_url}"
$PreName = "{name}"
$PreCode = "{codename}"
$PreProfile = "{profile}"
$UsedPct = {used_pct}

Clear-Host
Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "   FUNGUYS MULTI-AI COLLABORATIVE PLATFORM - NODE JOIN    " -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "Host Mission Control: $HostUrl`n" -ForegroundColor DarkGray

# 1. Detect which AI CLI tools / models are installed on this machine
Write-Host "[1/3] Scanning local environment for AI CLI engines..." -ForegroundColor Yellow
$InstalledOptions = @()

$CodexCmd = Get-Command codex.cmd -ErrorAction SilentlyContinue
if (-not $CodexCmd) {{ $CodexCmd = Get-Command codex -ErrorAction SilentlyContinue }}
if ($CodexCmd) {{
    $InstalledOptions += [PSCustomObject]@{{ Index = ($InstalledOptions.Count + 1); Key = "codex-pro"; Name = "OpenAI Codex CLI (Plus/Pro / GPT-4o / o-series)"; Cmd = $CodexCmd }}
    Write-Host "  [+] FOUND: OpenAI Codex CLI ($($CodexCmd.Source))" -ForegroundColor Green
}}

$GeminiCmd = Get-Command gemini.cmd -ErrorAction SilentlyContinue
if (-not $GeminiCmd) {{ $GeminiCmd = Get-Command gemini -ErrorAction SilentlyContinue }}
if ($GeminiCmd) {{
    $InstalledOptions += [PSCustomObject]@{{ Index = ($InstalledOptions.Count + 1); Key = "gemini-cli"; Name = "Google Gemini CLI (Pro / Flash)"; Cmd = $GeminiCmd }}
    Write-Host "  [+] FOUND: Google Gemini CLI ($($GeminiCmd.Source))" -ForegroundColor Green
}}

$ClaudeCmd = Get-Command claude.cmd -ErrorAction SilentlyContinue
if (-not $ClaudeCmd) {{ $ClaudeCmd = Get-Command claude -ErrorAction SilentlyContinue }}
if ($ClaudeCmd) {{
    $InstalledOptions += [PSCustomObject]@{{ Index = ($InstalledOptions.Count + 1); Key = "claude-pro"; Name = "Anthropic Claude Code (Sonnet / Opus)"; Cmd = $ClaudeCmd }}
    Write-Host "  [+] FOUND: Claude Code CLI ($($ClaudeCmd.Source))" -ForegroundColor Green
}}

# Add standard fallback profiles
$AllProfiles = @(
    [PSCustomObject]@{{ Key = "codex-pro"; Name = "OpenAI Codex CLI (Plus/Pro)" }},
    [PSCustomObject]@{{ Key = "gemini-cli"; Name = "Google Gemini CLI (Pro/Flash)" }},
    [PSCustomObject]@{{ Key = "claude-pro"; Name = "Claude Code (Pro)" }},
    [PSCustomObject]@{{ Key = "claude-max"; Name = "Claude Code (Max)" }},
    [PSCustomObject]@{{ Key = "api-fallback"; Name = "API Direct Fallback" }}
)

Write-Host "`n[2/3] Select AI Model / Profile to contribute from this machine:" -ForegroundColor Yellow
if ($InstalledOptions.Count -gt 0) {{
    Write-Host "  -- Detected Installed Tools (Recommended) --" -ForegroundColor Cyan
    foreach ($opt in $InstalledOptions) {{
        Write-Host "   [$($opt.Index)] $($opt.Name)" -ForegroundColor White
    }}
    Write-Host "  -- Other Profiles --" -ForegroundColor DarkGray
    $otherIdx = $InstalledOptions.Count + 1
    $OtherList = @()
    foreach ($prof in $AllProfiles) {{
        if (-not ($InstalledOptions | Where-Object {{ $_.Key -eq $prof.Key }})) {{
            Write-Host "   [$otherIdx] $($prof.Name)" -ForegroundColor DarkGray
            $OtherList += [PSCustomObject]@{{ Index = $otherIdx; Key = $prof.Key; Name = $prof.Name }}
            $otherIdx++
        }}
    }}
    $DefaultChoice = "1"
    $ChoiceInput = Read-Host "`nEnter selection number (press Enter for [$DefaultChoice]: $($InstalledOptions[0].Name))"
    $SelectedChoice = if ([string]::IsNullOrWhiteSpace($ChoiceInput)) {{ $DefaultChoice }} else {{ $ChoiceInput.Trim() }}
    $Chosen = ($InstalledOptions + $OtherList) | Where-Object {{ [string]$_.Index -eq $SelectedChoice }} | Select-Object -First 1
    if ($Chosen) {{
        $ProfileKey = $Chosen.Key
        $ProfileName = $Chosen.Name
    }} else {{
        $ProfileKey = $InstalledOptions[0].Key
        $ProfileName = $InstalledOptions[0].Name
    }}
}} else {{
    Write-Host "  No AI CLIs detected on PATH yet. Choose subscription profile:" -ForegroundColor DarkYellow
    for ($i = 0; $i -lt $AllProfiles.Count; $i++) {{
        Write-Host "   [$($i + 1)] $($AllProfiles[$i].Name)" -ForegroundColor White
    }}
    $ChoiceInput = Read-Host "`nEnter selection number (press Enter for [1]: OpenAI Codex CLI)"
    $SelectedIdx = if ([string]::IsNullOrWhiteSpace($ChoiceInput)) {{ 0 }} else {{ [int]$ChoiceInput - 1 }}
    if ($SelectedIdx -ge 0 -and $SelectedIdx -lt $AllProfiles.Count) {{
        $ProfileKey = $AllProfiles[$SelectedIdx].Key
        $ProfileName = $AllProfiles[$SelectedIdx].Name
    }} else {{
        $ProfileKey = "codex-pro"
        $ProfileName = "OpenAI Codex CLI (Plus/Pro)"
    }}
}}

Write-Host "`n[3/3] Choose Machine & Node Identity:" -ForegroundColor Yellow
$DefaultCodeName = if ($PreCode -ne "") {{ $PreCode }} elseif ($env:COMPUTERNAME) {{ $env:COMPUTERNAME }} else {{ "worker-node" }}
$InputCodeName = Read-Host "Enter a Code Name for this machine (press Enter for '$DefaultCodeName')"
$MachineName = if ([string]::IsNullOrWhiteSpace($InputCodeName)) {{ $DefaultCodeName }} else {{ $InputCodeName.Trim() }}

$DefaultOwner = if ($PreName -ne "" -and $PreName -ne "My PC") {{ $PreName }} else {{ $MachineName }}
$OwnerName = $DefaultOwner

Write-Host "`nConnecting as [$MachineName] using [$ProfileName]..." -ForegroundColor Cyan

$RegBody = @{{
    owner_name = $OwnerName
    machine_name = $MachineName
    profile_key = $ProfileKey
    initial_used_pct = $UsedPct
}} | ConvertTo-Json

$Agent = Invoke-RestMethod -Uri "$HostUrl/api/agents/register" -Method Post -ContentType "application/json" -Body $RegBody
$AgentId = $Agent.id
Write-Host "==========================================================" -ForegroundColor Green
Write-Host " [CONNECTED] Successfully registered as $($Agent.display_name)!" -ForegroundColor Green
Write-Host " Agent ID: $AgentId | Machine: $MachineName" -ForegroundColor Green
Write-Host "==========================================================" -ForegroundColor Green
Write-Host "Waiting for task assignments from Host Mission Control... (Press Ctrl+C to stop)" -ForegroundColor Cyan

while ($true) {{
    try {{
        $State = Invoke-RestMethod -Uri "$HostUrl/api/state" -Method Get
        foreach ($Task in $State.tasks) {{
            if ($Task.status -eq "in_progress" -and $Task.assigned_agent_id -eq $AgentId) {{
                Write-Host "`n[TASK ASSIGNED] $($Task.id): $($Task.title)" -ForegroundColor Yellow
                $WorkDir = Join-Path $env:TEMP "funguys_worker_$($Task.id)"
                if (Test-Path $WorkDir) {{ Remove-Item -Recurse -Force $WorkDir -ErrorAction SilentlyContinue }}
                New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null

                # 1. Pull full workspace snapshot from Host so AI has complete repo & contract context
                try {{
                    $Snap = Invoke-RestMethod -Uri "$HostUrl/api/workspace/snapshot" -Method Get
                    if ($Snap.files) {{
                        foreach ($Prop in $Snap.files.PSObject.Properties) {{
                            $DestPath = Join-Path $WorkDir ($Prop.Name -replace '/', '\\')
                            $DestDir = Split-Path -Parent $DestPath
                            if (-not (Test-Path $DestDir)) {{ New-Item -ItemType Directory -Force -Path $DestDir | Out-Null }}
                            Set-Content -Path $DestPath -Value $Prop.Value -Encoding UTF8 -NoNewline
                        }}
                    }}
                }} catch {{
                    Write-Host "[WARN] Workspace snapshot sync notice: $_" -ForegroundColor DarkGray
                }}

                $FilesPayload = @{{}}
                $Prompt = "Implement task $($Task.id) (Phase $($Task.phase)): $($Task.title). $($Task.description). Target files: $($Task.target_files -join ', '). Read CONTRACTS.md in the workspace first and write complete, production-ready code."
                Push-Location $WorkDir
                try {{
                    if ($ProfileKey -like "codex*" -and $CodexCmd) {{
                        $null | & $CodexCmd.Source exec -C $WorkDir --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox $Prompt
                    }} elseif ($ProfileKey -like "gemini*" -and $GeminiCmd) {{
                        $null | & $GeminiCmd.Source -p $Prompt -y
                    }} elseif ($ProfileKey -like "claude*" -and $ClaudeCmd) {{
                        $null | & $ClaudeCmd.Source -p $Prompt --dangerously-skip-permissions
                    }} elseif ($CodexCmd) {{
                        $null | & $CodexCmd.Source exec -C $WorkDir --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox $Prompt
                    }}
                }} catch {{
                    Write-Host "[INFO] AI CLI execution notice: $_" -ForegroundColor DarkYellow
                }} finally {{
                    Pop-Location
                }}
                # 2. Upload ALL newly created or modified files in $WorkDir
                $AllFiles = Get-ChildItem -Path $WorkDir -Recurse -File
                foreach ($F in $AllFiles) {{
                    $RelPath = $F.FullName.Substring($WorkDir.Length).TrimStart('\', '/') -replace '\\', '/'
                    if ($RelPath -notmatch '^(\.git|\.funguys|__pycache__|node_modules)') {{
                        $FilesPayload[$RelPath] = Get-Content $F.FullName -Raw -Encoding UTF8
                    }}
                }}

                $SubmitBody = @{{
                    agent_id = $AgentId
                    files = $FilesPayload
                    commit_message = "feat($($Task.id)): completed by $MachineName ($ProfileKey)"
                }} | ConvertTo-Json -Depth 6

                $Res = Invoke-RestMethod -Uri "$HostUrl/api/tasks/$($Task.id)/submit" -Method Post -ContentType "application/json" -Body $SubmitBody
                Write-Host "[SUBMITTED] Task $($Task.id) -> QA Status: $($Res.status)" -ForegroundColor Green
            }}
        }}
    }} catch {{
        Write-Host "[WARN] Poll error: $_" -ForegroundColor DarkYellow
    }}
    Start-Sleep -Seconds 3
}}
"""
        return PlainTextResponse(ps_script)

    # Serve the Mission Control Web Dashboard at `/`
    dashboard_html_path = Path(__file__).parent / "dashboard.html"

    @app.get("/", response_class=HTMLResponse)
    async def serve_dashboard() -> HTMLResponse:
        if dashboard_html_path.exists():
            return HTMLResponse(dashboard_html_path.read_text(encoding="utf-8"))
        return HTMLResponse("<h1>FunGuys Mission Control Loading...</h1>")

    return app
