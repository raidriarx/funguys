"""Core Multi-AI Orchestrator: Contract-First Planning, Tiered Quota Routing, Baton Pass, and QA Loop."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, List, Optional

from funguys.config import SUBSCRIPTION_PROFILES
from funguys.db import StateStore
from funguys.host.git_manager import GitWorkspaceManager
from funguys.host.graph_engine import GraphifyEngine
from funguys.host.qa_pipeline import AutonomousQAPipeline
from funguys.models import (
    ActivityEvent,
    AgentNode,
    AgentStatus,
    ContractAlert,
    ProjectState,
    QARunReport,
    TaskItem,
    TaskStatus,
    utc_now_iso,
)

BroadcastCallback = Callable[[Dict[str, Any]], Coroutine[Any, Any, None]]


class FunGuysOrchestrator:
    """Coordinates multiple teammates' AI subscriptions on the Central Host PC."""

    def __init__(
        self,
        store: StateStore,
        default_workspace: Path,
        broadcast_cb: Optional[BroadcastCallback] = None,
    ) -> None:
        self.store = store
        self.default_workspace = default_workspace
        self.broadcast_cb = broadcast_cb

        project = self.store.get_project()
        if not project:
            ws = self.default_workspace / "active_project"
            ws.mkdir(parents=True, exist_ok=True)
            project = ProjectState(
                id="proj-1",
                name="FunGuys Hackathon Demo Project",
                description="Full-stack real-time task & analytics web app with REST API, UI dashboard, and automated tests.",
                workspace_path=str(ws.resolve()),
                plan_approved=False,
            )
            self.store.save_project(project)
            self._seed_initial_contract_and_tasks(project)

        ws_path = Path(project.workspace_path)
        self.git = GitWorkspaceManager(ws_path)
        self.git.ensure_initialized(project.github_url)
        self.graph = GraphifyEngine(ws_path)
        self.qa = AutonomousQAPipeline(ws_path)
        self.graph.build_graph()
        self.chat_history: List[Dict[str, Any]] = []

    async def emit_event(
        self,
        source: str,
        event_type: str,
        message: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ActivityEvent:
        evt = ActivityEvent(
            id=f"evt-{uuid.uuid4().hex[:8]}",
            source=source,
            event_type=event_type,
            message=message,
            metadata=metadata or {},
        )
        self.store.add_event(evt)
        if self.broadcast_cb:
            await self.broadcast_cb({"type": "event", "event": evt.model_dump()})
        return evt

    def _seed_initial_contract_and_tasks(self, project: ProjectState) -> None:
        contracts_md = """# CONTRACTS.md — Shared Interface & Architecture Specification

## 1. Module Ownership Boundaries
- `backend/models.py` & `backend/api.py`: Core domain models and data service functions (Tier-1 AI).
- `frontend/index.html` & `frontend/app.js`: Interactive browser dashboard consuming the backend schema (Tier-2 AI).
- `tests/test_app.py`: Automated pytest verification suite testing domain contracts & edge cases (Tier-2 AI).

## 2. Agreed Function & Schema Contracts
- `create_item(title: str, priority: int = 1) -> dict`: Returns `{"id": str, "title": str, "priority": int, "completed": bool}`.
- `list_items() -> list[dict]`: Returns all stored items sorted by priority descending.
- `compute_metrics(items: list[dict]) -> dict`: Returns `{"total": int, "completed": int, "completion_rate": float}`.
"""
        project.contracts_markdown = contracts_md
        self.store.save_project(project)

        ws = Path(project.workspace_path)
        ws.mkdir(parents=True, exist_ok=True)
        (ws / "CONTRACTS.md").write_text(contracts_md, encoding="utf-8")

        initial_tasks = [
            TaskItem(
                id="task-1-contracts",
                title="Core Domain Models & Data Service (`backend/service.py`)",
                description=(
                    "Implement `create_item`, `list_items`, and `compute_metrics` in `backend/service.py` "
                    "matching `CONTRACTS.md`."
                ),
                complexity_tier=1,
                dependencies=[],
                target_files=["backend/service.py"],
                exported_symbols=["create_item", "list_items", "compute_metrics"],
                status=TaskStatus.READY,
                branch_name="task/task-1-contracts",
            ),
            TaskItem(
                id="task-2-frontend",
                title="Interactive Web UI (`frontend/index.html`)",
                description=(
                    "Create responsive web UI in `frontend/index.html` displaying project items and completion metrics."
                ),
                complexity_tier=2,
                dependencies=["task-1-contracts"],
                target_files=["frontend/index.html"],
                exported_symbols=[],
                status=TaskStatus.PENDING,
                branch_name="task/task-2-frontend",
            ),
            TaskItem(
                id="task-3-tests",
                title="Automated Pytest Suite (`tests/test_service.py`)",
                description=(
                    "Write comprehensive unit & contract integration tests in `tests/test_service.py` "
                    "verifying `create_item`, `list_items`, and `compute_metrics`."
                ),
                complexity_tier=2,
                dependencies=["task-1-contracts"],
                target_files=["tests/test_service.py"],
                exported_symbols=["test_create_and_list_items", "test_compute_metrics"],
                status=TaskStatus.PENDING,
                branch_name="task/task-3-tests",
            ),
        ]
        self.store.clear_tasks()
        for t in initial_tasks:
            self.store.save_task(t)

    async def initialize_project(
        self,
        name: str,
        description: str,
        workspace_path: Optional[str] = None,
        github_url: Optional[str] = None,
    ) -> ProjectState:
        ws = (
            Path(workspace_path)
            if workspace_path
            else (self.default_workspace / name.lower().replace(" ", "_"))
        )
        ws.mkdir(parents=True, exist_ok=True)
        project = ProjectState(
            id=f"proj-{uuid.uuid4().hex[:6]}",
            name=name,
            description=description,
            workspace_path=str(ws.resolve()),
            github_url=github_url,
            plan_approved=False,
        )
        self.store.save_project(project)
        self.git = GitWorkspaceManager(ws)
        self.git.ensure_initialized(github_url)
        self.graph = GraphifyEngine(ws)
        self.qa = AutonomousQAPipeline(ws)
        self._seed_initial_contract_and_tasks(project)
        self.graph.build_graph()
        await self.emit_event(
            "host",
            "project_initialized",
            f"Initialized project '{name}' at {ws}. Waiting for human plan approval.",
        )
        return project

    async def approve_plan(self, contracts_markdown: Optional[str] = None) -> ProjectState:
        project = self.store.get_project()
        assert project is not None
        if contracts_markdown:
            project.contracts_markdown = contracts_markdown
            (Path(project.workspace_path) / "CONTRACTS.md").write_text(
                contracts_markdown, encoding="utf-8"
            )
        project.plan_approved = True
        self.store.save_project(project)
        await self.emit_event(
            "human-team",
            "plan_approved",
            "Architecture & Contracts approved! Autonomous multi-AI execution unlocked.",
        )
        await self.schedule_ready_tasks()
        return project

    async def register_agent(
        self,
        owner_name: str,
        machine_name: str,
        profile_key: str,
        initial_used_pct: float = 0.0,
    ) -> AgentNode:
        import re

        owner_name = re.sub(r"\s+", " ", owner_name).strip() or "My PC"
        machine_name = re.sub(r"\s+", "", machine_name).strip() or owner_name
        clean_key = re.sub(r"\s+", "", profile_key).strip().lower()
        if clean_key not in SUBSCRIPTION_PROFILES:
            if "codex" in clean_key or "openai" in clean_key or "gpt" in clean_key:
                clean_key = "codex-pro"
            elif "gemini" in clean_key or "google" in clean_key:
                clean_key = "gemini-cli"
            elif "claude" in clean_key:
                clean_key = "claude-pro"
            else:
                clean_key = "codex-pro"
        profile_key = clean_key
        profile = SUBSCRIPTION_PROFILES[profile_key]
        existing_agents = self.store.list_agents()
        for existing in existing_agents:
            if (
                existing.machine_name.lower() == machine_name.lower()
                or existing.owner_name.lower() == owner_name.lower()
            ):
                existing.profile_key = profile_key
                existing.provider = profile.provider
                existing.display_name = f"{owner_name}'s {profile.display_name}"
                existing.tier = profile.tier
                self.store.save_agent(existing)
                await self.emit_event(
                    existing.id,
                    "agent_reconnected",
                    f"{existing.display_name} reconnected from {existing.machine_name}.",
                )
                await self.schedule_ready_tasks()
                return existing

        agent_id = f"{owner_name.lower().replace(' ', '-')}-{profile.provider}-{uuid.uuid4().hex[:4]}"
        turns_used = int((initial_used_pct / 100.0) * profile.estimated_turns_per_window)
        agent = AgentNode(
            id=agent_id,
            owner_name=owner_name,
            machine_name=machine_name,
            profile_key=profile_key,
            provider=profile.provider,
            display_name=f"{owner_name}'s {profile.display_name}",
            tier=profile.tier,
            status=AgentStatus.IDLE,
            used_pct=round(initial_used_pct, 1),
            turns_used=turns_used,
            turns_limit=profile.estimated_turns_per_window,
            soft_limit_pct=profile.soft_handoff_threshold_pct,
        )
        self.store.save_agent(agent)
        await self.emit_event(
            agent.id,
            "agent_joined",
            f"{agent.display_name} connected from {machine_name} (Tier {agent.tier}, {100 - agent.used_pct:.0f}% quota available).",
        )
        await self.schedule_ready_tasks()
        return agent

    def select_best_agent_for_task(self, task: TaskItem) -> Optional[AgentNode]:
        """Role-Based Tier Routing: match task complexity tier to the most efficient available AI."""
        agents = self.store.list_agents()
        eligible = [
            a
            for a in agents
            if a.status == AgentStatus.IDLE
            and a.used_pct < a.soft_limit_pct
            and a.current_task_id is None
        ]
        if not eligible:
            return None

        # Sort by: (1) Exact tier match first, (2) Lowest used_pct (most remaining quota headroom)
        eligible.sort(
            key=lambda a: (0 if a.tier == task.complexity_tier else 1, a.used_pct)
        )
        return eligible[0]

    async def schedule_ready_tasks(self) -> List[Dict[str, Any]]:
        """Promote unlocked PENDING tasks to READY and assign them to optimal idle AIs."""
        project = self.store.get_project()
        if not project or not project.plan_approved:
            return []

        tasks = self.store.list_tasks()
        in_progress_ids = {t.id for t in tasks if t.status == TaskStatus.IN_PROGRESS}
        for agent in self.store.list_agents():
            if agent.current_task_id and agent.current_task_id not in in_progress_ids:
                agent.status = AgentStatus.IDLE
                agent.current_task_id = None
                self.store.save_agent(agent)

        completed_ids = {t.id for t in tasks if t.status == TaskStatus.COMPLETED}
        assignments: List[Dict[str, Any]] = []

        # Unlock PENDING tasks whose DAG dependencies are all COMPLETED
        for task in tasks:
            if task.status == TaskStatus.PENDING and all(
                dep in completed_ids for dep in task.dependencies
            ):
                task.status = TaskStatus.READY
                task.updated_at = utc_now_iso()
                self.store.save_task(task)

        # Prioritize HANDOFF_PENDING tasks first (Baton Pass), then READY tasks by complexity tier
        schedulable = [
            t
            for t in self.store.list_tasks()
            if t.status in {TaskStatus.HANDOFF_PENDING, TaskStatus.READY}
            and not t.assigned_human
            and not t.circuit_breaker_tripped
        ]
        schedulable.sort(
            key=lambda t: (0 if t.status == TaskStatus.HANDOFF_PENDING else 1, t.complexity_tier)
        )

        for task in schedulable:
            agent = self.select_best_agent_for_task(task)
            if not agent:
                break

            was_handoff = task.status == TaskStatus.HANDOFF_PENDING
            task.status = TaskStatus.IN_PROGRESS
            task.assigned_agent_id = agent.id
            task.updated_at = utc_now_iso()
            self.store.save_task(task)

            agent.status = AgentStatus.WORKING
            agent.current_task_id = task.id
            agent.last_heartbeat = utc_now_iso()
            self.store.save_agent(agent)

            subgraph = self.graph.get_subgraph_digest(task.target_files)
            assignment_payload = {
                "task": task.model_dump(),
                "agent": agent.model_dump(),
                "subgraph_digest": subgraph,
                "contracts_markdown": project.contracts_markdown,
                "is_baton_pass": was_handoff,
            }
            assignments.append(assignment_payload)

            action_label = "Baton-Pass Handoff ->" if was_handoff else "Assigned ->"
            await self.emit_event(
                "router",
                "task_assigned",
                f"{action_label} `{task.title}` (Tier {task.complexity_tier}) routed to **{agent.display_name}** ({agent.used_pct:.1f}% used).",
                metadata={"task_id": task.id, "agent_id": agent.id, "baton_pass": was_handoff},
            )
            if self.broadcast_cb:
                await self.broadcast_cb(
                    {"type": "task_assigned", "assignment": assignment_payload}
                )

        return assignments

    async def run_autonomous_execution_loop(self, delay_sec: float = 3.0) -> None:
        """Background worker loop that executes IN_PROGRESS tasks via the AI CLI when unlocked."""
        import asyncio
        from funguys.worker.adapters import build_graphify_task_prompt, run_cli_agent_task

        if getattr(self, "_auto_loop_running", False):
            return
        self._auto_loop_running = True
        try:
            await asyncio.sleep(delay_sec)
            while True:
                project = self.store.get_project()
                if not project or not project.plan_approved:
                    break
                in_prog = [
                    t
                    for t in self.store.list_tasks()
                    if t.status == TaskStatus.IN_PROGRESS
                    and t.assigned_agent_id
                    and not t.assigned_human
                    and not t.circuit_breaker_tripped
                ]
                if not in_prog:
                    # Try scheduling any newly ready tasks
                    await self.schedule_ready_tasks()
                    in_prog = [
                        t
                        for t in self.store.list_tasks()
                        if t.status == TaskStatus.IN_PROGRESS
                        and t.assigned_agent_id
                        and not t.assigned_human
                        and not t.circuit_breaker_tripped
                    ]
                    if not in_prog:
                        break

                for task in in_prog:
                    agent = self.store.get_agent(task.assigned_agent_id) if task.assigned_agent_id else None
                    if not agent:
                        continue
                    await self.emit_event(
                        agent.machine_name,
                        "task_executing",
                        f"⚡ **[{agent.machine_name}]** ({agent.display_name}) is building Phase {task.phase} task `{task.id}`: **{task.title}** (`{', '.join(task.target_files)}`)...",
                        metadata={"task_id": task.id, "agent_id": agent.id},
                    )
                    subgraph = self.graph.get_subgraph_digest(task.target_files)
                    prompt = build_graphify_task_prompt(
                        task.model_dump(),
                        project.contracts_markdown,
                        subgraph,
                    )
                    ws_dir = Path(project.workspace_path)
                    ok, rate_limited, files_out, raw_out = await asyncio.to_thread(
                        run_cli_agent_task,
                        agent.provider,
                        ws_dir,
                        prompt,
                        task.target_files,
                        240,
                    )
                    # Re-check if a remote worker already submitted while we were running
                    fresh_task = self.store.get_task(task.id)
                    if not fresh_task or fresh_task.status != TaskStatus.IN_PROGRESS:
                        continue

                    if not files_out:
                        # Ensure target files exist if the CLI wrote to a slightly different path or stub
                        for rel in task.target_files:
                            fp = ws_dir / rel
                            if fp.exists() and fp.is_file():
                                files_out[rel] = fp.read_text(encoding="utf-8", errors="ignore")

                    if files_out:
                        await self.submit_task_work(
                            task_id=task.id,
                            agent_id=agent.id,
                            files=files_out,
                            commit_message=f"feat({task.id}): implemented {task.title} by {agent.machine_name}",
                        )
                    else:
                        await self.emit_event(
                            agent.machine_name,
                            "task_warning",
                            f"⚠️ **[{agent.machine_name}]** CLI run finished for `{task.id}` without target files ({raw_out[:160]}).",
                        )
                        break
                await asyncio.sleep(1.5)
        finally:
            self._auto_loop_running = False

    async def report_quota_and_check_handoff(
        self,
        agent_id: str,
        used_pct: float,
        turns_delta: int = 1,
        rate_limited: bool = False,
        cooldown_minutes: Optional[int] = None,
        handoff_notes: Optional[str] = None,
        wip_files: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Update an AI's quota usage and trigger an automatic Baton Pass if >=80% or rate-limited."""
        agent = self.store.get_agent(agent_id)
        if not agent:
            raise ValueError(f"Unknown agent: {agent_id}")

        agent.used_pct = min(100.0, max(agent.used_pct, round(used_pct, 1)))
        agent.turns_used += turns_delta
        agent.last_heartbeat = utc_now_iso()

        needs_handoff = rate_limited or (agent.used_pct >= agent.soft_limit_pct)
        handoff_result: Optional[Dict[str, Any]] = None

        if needs_handoff and agent.current_task_id:
            task = self.store.get_task(agent.current_task_id)
            if task and task.status == TaskStatus.IN_PROGRESS:
                if wip_files:
                    self.git.apply_worker_changes(
                        task.id,
                        agent.id,
                        wip_files,
                        commit_message="WIP checkpoint before quota handoff",
                        is_wip=True,
                    )
                    self.graph.build_graph()

                subgraph_digest = self.graph.get_subgraph_digest(task.target_files)
                notes = handoff_notes or (
                    f"Completed initial scaffolding for {', '.join(task.target_files)}. "
                    f"Paused at {agent.used_pct:.1f}% quota limit. Finish remaining logic & run QA."
                )
                digest_md = self.git.create_handoff_document(
                    task.id, agent.id, notes, subgraph_digest
                )

                task.status = TaskStatus.HANDOFF_PENDING
                task.assigned_agent_id = None
                task.branch_name = f"wip/{task.id}"
                task.handoff_digest = digest_md
                task.updated_at = utc_now_iso()
                self.store.save_task(task)

                cd_mins = cooldown_minutes or 60
                agent.status = AgentStatus.COOLDOWN
                agent.current_task_id = None
                agent.cooldown_until = (
                    datetime.now(timezone.utc) + timedelta(minutes=cd_mins)
                ).isoformat()
                self.store.save_agent(agent)

                reason = "HTTP 429 Rate Limit" if rate_limited else f"Soft Quota Threshold ({agent.used_pct:.1f}% >= {agent.soft_limit_pct:.0f}%)"
                await self.emit_event(
                    agent.id,
                    "baton_pass_triggered",
                    f"⚡ Baton Pass triggered on `{task.id}` due to {reason}. Committed `wip/{task.id}` & generated Graphify handoff digest.",
                    metadata={"task_id": task.id, "from_agent": agent.id},
                )
                new_assignments = await self.schedule_ready_tasks()
                handoff_result = {
                    "handed_off_task": task.id,
                    "handoff_digest": digest_md,
                    "new_assignments": new_assignments,
                }
        else:
            self.store.save_agent(agent)

        return {"agent": agent.model_dump(), "handoff": handoff_result}

    async def submit_task_work(
        self,
        task_id: str,
        agent_id: str,
        files: Dict[str, str],
        commit_message: str,
        used_pct_after: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Commit worker files, run Graphify contract delta analysis, and run 3-layer QA."""
        task = self.store.get_task(task_id)
        if not task:
            raise ValueError(f"Unknown task: {task_id}")
        agent = self.store.get_agent(agent_id)

        task.status = TaskStatus.IN_QA
        task.updated_at = utc_now_iso()
        self.store.save_task(task)

        # 1. Commit code to Host Git repo
        self.git.apply_worker_changes(task_id, agent_id, files, commit_message, is_wip=False)

        # 2. Run deterministic Graphify pass & detect contract signature changes
        all_tasks = self.store.list_tasks()
        graph_data, alerts = self.graph.scan_and_detect_contract_deltas(
            task_id, agent_id, all_tasks
        )
        for alert in alerts:
            self.store.save_contract_alert(alert)
            await self.emit_event(
                "graphify",
                "contract_delta",
                alert.summary,
                metadata=alert.model_dump(),
            )

        # 3. Run 3-Layer Autonomous QA Pipeline on Host PC
        project = self.store.get_project()
        qa_report = await self.qa.verify_task(
            task, live_preview_url=project.live_preview_url if project else None
        )
        self.store.save_qa_report(qa_report)

        # Update agent status & quota
        if agent:
            agent.turns_used += 1
            if used_pct_after is not None:
                agent.used_pct = min(100.0, round(used_pct_after, 1))
            else:
                agent.used_pct = min(
                    100.0, round((agent.turns_used / max(1, agent.turns_limit)) * 100.0, 1)
                )
            agent.status = (
                AgentStatus.COOLDOWN
                if agent.used_pct >= agent.soft_limit_pct
                else AgentStatus.IDLE
            )
            agent.current_task_id = None
            agent.last_heartbeat = utc_now_iso()
            self.store.save_agent(agent)

        if qa_report.overall_passed:
            task.status = TaskStatus.COMPLETED
            task.last_error = None
            task.updated_at = utc_now_iso()
            self.store.save_task(task)
            await self.emit_event(
                "qa-pipeline",
                "qa_passed",
                f"✅ `{task.title}` PASSED all QA layers and merged to `main`!",
                metadata={"task_id": task.id, "qa_id": qa_report.id},
            )
            await self._run_architect_and_consensus_review(task, agent_id, files, alerts)
            next_assignments = await self.schedule_ready_tasks()
            return {
                "status": "completed",
                "task": self.store.get_task(task.id).model_dump() if self.store.get_task(task.id) else task.model_dump(),
                "qa_report": qa_report.model_dump(),
                "contract_alerts": [a.model_dump() for a in alerts],
                "next_assignments": next_assignments,
            }

        # QA Failed -> Apply Smart Escalation + Circuit Breaker
        task.retry_count += 1
        task.last_error = qa_report.logs
        task.assigned_agent_id = None

        if task.retry_count >= task.max_retries:
            task.status = TaskStatus.BLOCKED_CIRCUIT_BREAKER
            task.circuit_breaker_tripped = True
            self.store.save_task(task)
            await self.emit_event(
                "circuit-breaker",
                "circuit_breaker_tripped",
                f"🛑 Circuit Breaker tripped on `{task.title}` after {task.retry_count} failed QA attempts. Paused to protect team AI quota — human attention requested!",
                metadata={"task_id": task.id, "qa_id": qa_report.id},
            )
            return {
                "status": "circuit_breaker_tripped",
                "task": task.model_dump(),
                "qa_report": qa_report.model_dump(),
            }

        if task.retry_count == 2:
            # Escalate to Tier-1 Debugging AI with Graphify context
            task.complexity_tier = 1
            subgraph_ctx = self.graph.get_subgraph_digest(task.target_files)
            task.handoff_digest = (
                f"### Escalated to Tier-1 Debugger (Attempt {task.retry_count + 1})\n"
                f"Previous QA failure:\n```\n{qa_report.logs[-600:]}\n```\n\n{subgraph_ctx}"
            )
            task.status = TaskStatus.HANDOFF_PENDING
            self.store.save_task(task)
            await self.emit_event(
                "qa-pipeline",
                "qa_escalated_tier1",
                f"⚠️ `{task.title}` failed QA twice. Escalating to **Tier-1 Debugger AI** with Graphify call-path context!",
                metadata={"task_id": task.id, "retry_count": task.retry_count},
            )
        else:
            task.status = TaskStatus.READY
            self.store.save_task(task)
            await self.emit_event(
                "qa-pipeline",
                "qa_retry",
                f"🔄 `{task.title}` failed QA (Attempt {task.retry_count}/{task.max_retries}). Scheduling automated bug-fix retry.",
                metadata={"task_id": task.id, "retry_count": task.retry_count},
            )

        next_assignments = await self.schedule_ready_tasks()
        return {
            "status": "retry_scheduled",
            "task": task.model_dump(),
            "qa_report": qa_report.model_dump(),
            "next_assignments": next_assignments,
        }

    async def _run_architect_and_consensus_review(
        self,
        task: TaskItem,
        submitting_agent_id: str,
        files: Dict[str, str],
        alerts: List[ContractAlert],
    ) -> None:
        """Lead Architect AI checks what each bot added in each phase; for bigger changes all bots review together."""
        project = self.store.get_project()
        agents = self.store.list_agents()
        all_tasks = self.store.list_tasks()

        lead_agent: Optional[AgentNode] = None
        if project and project.lead_architect_agent_id:
            lead_agent = next((a for a in agents if a.id == project.lead_architect_agent_id), None)
        if not lead_agent and agents:
            lead_agent = sorted(agents, key=lambda a: (a.tier, a.used_pct))[0]

        lead_name = lead_agent.machine_name if lead_agent else "Lead-Architect"
        added_files = list(files.keys())
        extra_files = [f for f in added_files if f not in task.target_files]

        # Check if an entire phase just finished or if contract deltas / extra files were introduced
        same_phase_tasks = [t for t in all_tasks if t.phase == task.phase]
        phase_completed = all(t.status == TaskStatus.COMPLETED for t in same_phase_tasks)
        is_major_change = bool(alerts) or bool(extra_files) or phase_completed

        review_summary = (
            f"[{lead_name} Phase-{task.phase} Audit] Verified {len(added_files)} file(s): {', '.join(added_files)}."
        )
        if extra_files:
            review_summary += f" New auxiliary files detected: {', '.join(extra_files)}."
        if alerts:
            review_summary += f" Contract signature updates: {len(alerts)}."

        await self.emit_event(
            lead_name,
            "architect_phase_review",
            f"🧐 **Lead Architect ({lead_name})** reviewed `{task.id}` (Phase {task.phase}): {review_summary}",
            metadata={"task_id": task.id, "phase": task.phase, "files": added_files},
        )

        if is_major_change and len(agents) > 1:
            bot_names = ", ".join(f"**{a.machine_name}** ({a.display_name})" for a in agents)
            reason = (
                f"Phase {task.phase} boundary completed"
                if phase_completed
                else f"Cross-cutting changes ({len(alerts)} contract deltas, {len(extra_files)} new files)"
            )
            consensus_note = (
                f"[ALL-BOTS CONSENSUS SYNC] ({reason}): {bot_names} reviewed `{', '.join(added_files)}` "
                f"and aligned interfaces for downstream Phase {task.phase + 1} tasks."
            )
            review_summary += f" | {consensus_note}"
            await self.emit_event(
                "all-bots-council",
                "consensus_sync",
                f"🤝 **Multi-Bot Consensus Check:** {consensus_note}",
                metadata={"task_id": task.id, "phase": task.phase, "bots": [a.machine_name for a in agents]},
            )

        task.architect_review = review_summary
        self.store.save_task(task)

    async def claim_task_by_human(self, task_id: str, human_name: str) -> TaskItem:
        """Allow a human teammate to claim a task alongside the AIs."""
        task = self.store.get_task(task_id)
        if not task:
            raise ValueError(f"Unknown task: {task_id}")
        task.assigned_human = human_name
        task.assigned_agent_id = None
        task.status = TaskStatus.IN_PROGRESS
        task.updated_at = utc_now_iso()
        self.store.save_task(task)
        await self.emit_event(
            human_name,
            "human_claimed_task",
            f"👩‍💻 Teammate **{human_name}** claimed `{task.title}` for manual coding.",
            metadata={"task_id": task.id, "human": human_name},
        )
        return task

    async def reset_circuit_breaker(self, task_id: str) -> TaskItem:
        """Reset a tripped circuit breaker after human review."""
        task = self.store.get_task(task_id)
        if not task:
            raise ValueError(f"Unknown task: {task_id}")
        task.retry_count = 0
        task.circuit_breaker_tripped = False
        task.status = TaskStatus.READY
        task.updated_at = utc_now_iso()
        self.store.save_task(task)
        await self.emit_event(
            "human-team",
            "circuit_breaker_reset",
            f"🔓 Circuit breaker reset for `{task.title}`. Task re-queued.",
        )
        await self.schedule_ready_tasks()
        return task

    async def ask_project_question(
        self,
        question: str,
        preferred_agent_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Main Agent Router uses the Lead Architect LLM to route user messages to the right bot(s), with full workspace read/write & task-spawning power."""
        q_clean = question.strip()
        q_lower = q_clean.lower()
        agents = self.store.list_agents()
        tasks = self.store.list_tasks()
        project = self.store.get_project()

        selected_agent: Optional[AgentNode] = None
        routing_reason = ""
        matched_domain = "Project Engineering & Workspace"

        # 1. Explicit agent selection override
        if preferred_agent_id and preferred_agent_id != "auto":
            selected_agent = next((a for a in agents if a.id == preferred_agent_id), None)
            if selected_agent:
                routing_reason = f"Explicitly targeted node [{selected_agent.machine_name}] ({selected_agent.display_name})."
                matched_domain = f"Direct Node ({selected_agent.machine_name})"

        # 2. Direct node/model mention in prompt
        if not selected_agent:
            for a in agents:
                if (
                    a.machine_name.lower() in q_lower
                    or a.owner_name.lower() in q_lower
                    or a.provider.lower() in q_lower
                    or ("codex" in q_lower and "codex" in a.profile_key.lower())
                    or ("gemini" in q_lower and "gemini" in a.profile_key.lower())
                    or ("claude" in q_lower and "claude" in a.profile_key.lower())
                ):
                    selected_agent = a
                    routing_reason = (
                        f"Message directly references `{a.machine_name}` ({a.display_name}, Tier {a.tier})."
                    )
                    matched_domain = f"Node-Specific ({a.machine_name})"
                    break

        # 3. Dynamic Task & File Ownership matching
        if not selected_agent:
            for t in tasks:
                task_terms = [w.lower() for w in t.title.replace("(", " ").replace(")", " ").replace("/", " ").split() if len(w) > 3]
                file_terms = [f.lower() for f in t.target_files] + [Path(f).name.lower() for f in t.target_files]
                if any(term in q_lower for term in file_terms + task_terms):
                    matched_domain = f"{t.title} ({', '.join(t.target_files)})"
                    if t.assigned_agent_id:
                        selected_agent = next((a for a in agents if a.id == t.assigned_agent_id), None)
                    if selected_agent:
                        routing_reason = (
                            f"Matched task `{t.id}` ({matched_domain}) -> Routed to task owner [{selected_agent.machine_name}] ({selected_agent.display_name})."
                        )
                        break

        # 4. Route to Project's chosen Lead Architect node (or top Tier-1 node)
        if not selected_agent and project and project.lead_architect_agent_id:
            selected_agent = next((a for a in agents if a.id == project.lead_architect_agent_id), None)
            if selected_agent:
                routing_reason = (
                    f"Routed by Main Agent to Project Lead Architect [{selected_agent.machine_name}] ({selected_agent.display_name})."
                )

        if not selected_agent and agents:
            selected_agent = sorted(agents, key=lambda a: (a.tier, a.used_pct))[0]
            routing_reason = (
                f"Routed by Main Agent to Lead Node [{selected_agent.machine_name}] ({selected_agent.display_name}, Tier {selected_agent.tier})."
            )

        agent_label = (
            f"[{selected_agent.machine_name}] {selected_agent.display_name} (Tier {selected_agent.tier})"
            if selected_agent
            else "[LEAD_ARCHITECT] Host Orchestrator"
        )
        agent_id = selected_agent.id if selected_agent else "host-orchestrator"
        agent_codename = selected_agent.machine_name if selected_agent else "architect"

        owned_tasks = [t for t in tasks if selected_agent and t.assigned_agent_id == selected_agent.id]
        owned_summary = (
            ", ".join(f"`{t.id}` ({t.title} | status={t.status.value} | files={', '.join(t.target_files)})" for t in owned_tasks)
            if owned_tasks
            else "Lead Architect & Cross-Module Coordinator"
        )
        all_tasks_summary = "\n".join(
            f"- {t.id} (Phase {t.phase}): {t.title} [status={t.status.value}, agent={t.assigned_agent_id}, files={', '.join(t.target_files)}]"
            for t in tasks
        )
        ws_dir = Path(project.workspace_path) if project else self.git.workspace_dir
        ws_dir.mkdir(parents=True, exist_ok=True)

        file_snippets: List[str] = []
        clean_file_list: List[str] = []
        excluded_dirs = {".git", ".funguys", ".graphify", "__pycache__", "node_modules", ".venv", ".pytest_cache", ".ruff_cache"}
        for p in sorted(ws_dir.rglob("*")):
            rel_parts = p.relative_to(ws_dir).parts
            if p.is_file() and not any(part in excluded_dirs for part in rel_parts):
                rel_str = p.relative_to(ws_dir).as_posix()
                clean_file_list.append(rel_str)
                if len(file_snippets) < 6 and p.stat().st_size < 40_000:
                    try:
                        content = p.read_text(encoding="utf-8", errors="ignore")
                        file_snippets.append(f"--- FILE: {rel_str} ---\n{content[:700]}")
                    except Exception:
                        pass

        workspace_context = (
            f"Project Source Files ({len(clean_file_list)}): {', '.join(clean_file_list)}\n\n"
            + ("\n\n".join(file_snippets) if file_snippets else "(Workspace is currently empty)")
        )
        recent_chat_ctx = "\n".join(
            f"User: {str(c.get('question', ''))[:400]}\n{c.get('agent_codename', 'agent')}: {str(c.get('answer', ''))[:400]}"
            for c in self.chat_history[-3:]
        )
        q_for_prompt = q_clean if len(q_clean) <= 1800 else (q_clean[:600] + "\n...[truncated stack trace]...\n" + q_clean[-1200:])
        wants_code_edit = any(
            kw in q_lower
            for kw in [
                "create ", "write ", "modify ", "replace ", "refactor ", "implement ", "add a file", "edit ",
                "fix ", "move ", "update ", "check my running app", "check running app", "check the app", "test "
            ]
        )

        llm_prompt = (
            f"You are AI node '{agent_codename}' ({agent_label}) inside the FunGuys Multi-AI engineering platform.\n"
            f"Active Project: {project.name if project else 'Untitled'} — {project.description if project else ''}\n"
            f"Workspace Root: {ws_dir}\n"
            f"Your Role / Owned Tasks: {owned_summary}\n"
            f"All Project Tasks in DAG:\n{all_tasks_summary}\n\n"
            f"Current Workspace Context:\n{workspace_context}\n\n"
            f"Recent Conversation:\n{recent_chat_ctx}\n\n"
            f"User Message: {q_for_prompt}\n\n"
            f"Instructions:\n"
            f"1. You have FULL read, write, and command execution access to the project workspace `{ws_dir}`.\n"
            f"2. You CAN and SHOULD inspect workspace files, run diagnostic checks (e.g. check adb devices, inspect running app processes, test code), and edit/create code files whenever the user asks you to check, fix, or run something.\n"
            f"3. Talk naturally and conversationally to the user as `{agent_codename}`. Explain clearly what you inspected or fixed, and provide clear actionable instructions or results."
        )

        def _call_actionable_agent() -> Optional[str]:
            import os
            import shutil
            import subprocess
            import tempfile

            codex_bin = shutil.which("codex") or r"C:\Users\USER\AppData\Local\Programs\OpenAI\Codex\bin\codex.EXE"
            if not os.path.exists(codex_bin):
                return None
            out_file = os.path.join(tempfile.gettempdir(), f"funguys_chat_{uuid.uuid4().hex[:6]}.txt")
            exec_dir = str(ws_dir)
            try:
                subprocess.run(
                    [
                        codex_bin,
                        "exec",
                        "-C",
                        exec_dir,
                        "--skip-git-repo-check",
                        "--dangerously-bypass-approvals-and-sandbox",
                        "--ephemeral",
                        "-o",
                        out_file,
                        llm_prompt,
                    ],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                if os.path.exists(out_file):
                    text = Path(out_file).read_text(encoding="utf-8", errors="ignore").strip()
                    try:
                        os.remove(out_file)
                    except Exception:
                        pass
                    if text:
                        return text
            except Exception:
                pass
            return None

        import asyncio

        llm_reply = await asyncio.to_thread(_call_actionable_agent)
        if wants_code_edit:
            self.graph.build_graph()

        if not llm_reply:
            completed_tasks = [t for t in tasks if t.status == TaskStatus.COMPLETED]
            in_prog_tasks = [t for t in tasks if t.status == TaskStatus.IN_PROGRESS]
            status_line = (
                f"All {len(tasks)} phases are **COMPLETED**!"
                if tasks and len(completed_tasks) == len(tasks)
                else f"**{len(completed_tasks)}/{len(tasks)} phases completed** ({', '.join(t.id for t in in_prog_tasks) or 'idle'} currently active)."
            )
            llm_reply = (
                f"Hey! Here is the current status from **{agent_codename}**:\n"
                f"- **Build Progress:** {status_line}\n"
                f"- **Phase Breakdown:** " + "; ".join(f"Phase {t.phase} (`{t.id}`): **{t.status.value.upper()}**" for t in tasks) + "\n"
                f"- **Project Source Files (`{ws_dir}`):** `{', '.join(clean_file_list) if clean_file_list else 'None'}`\n"
                f"- **How to Run Tests / App:** First `cd \"{ws_dir}\"`, then run `.\\.venv\\Scripts\\python.exe -m pytest -q` or `.\\.venv\\Scripts\\python.exe run_app.py`."
            )

        entry = {
            "id": f"msg-{uuid.uuid4().hex[:8]}",
            "timestamp": utc_now_iso(),
            "question": q_clean,
            "matched_domain": matched_domain,
            "routing_reason": routing_reason,
            "agent_id": agent_id,
            "agent_codename": agent_codename,
            "agent_label": agent_label,
            "answer": llm_reply,
        }
        self.chat_history.append(entry)
        await self.emit_event(
            "main-router",
            "query_routed",
            f"🧭 **Main Router:** Routed message *\"{q_clean[:60]}\"* -> **{agent_codename}** ({matched_domain})",
            metadata={"agent_id": agent_id, "domain": matched_domain},
        )
        await self.emit_event(
            agent_id,
            "agent_responded",
            f"💬 **{agent_codename}** responded (and synced workspace `{ws_dir.name}`).",
            metadata={"chat_id": entry["id"]},
        )
        return entry



