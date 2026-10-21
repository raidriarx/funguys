"""Versioned, leased orchestration with verification before merge."""
from __future__ import annotations
import asyncio
import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional
from funguys.config import PLANNER_PROVIDER_PRIORITY, SUBSCRIPTION_PROFILES, get_profile, resolve_profile_key
from funguys.files import bundle, decode_files, validate_dag, validate_relative_path
from funguys.host.git_c2 import GitC2Sync
from funguys.host.git_manager import GitWorkspaceManager
from funguys.host.graph_engine import GraphifyEngine
from funguys.host.qa_pipeline import AutonomousQAPipeline
from funguys.models import (
    ActivityEvent,
    AgentNode,
    AgentSignal,
    AgentStatus,
    BlackboardEntry,
    ProjectState,
    PromptQueueItem,
    SignalOp,
    TaskItem,
    TaskStatus,
    utc_now_iso,
)
from funguys.protocol import (
    classify_task_role,
    compress_error_log,
    parse_agent_signals,
    select_model_and_effort,
    slice_contract_for_task,
)


class ConflictError(ValueError):
    pass


class FunGuysOrchestrator:
    def __init__(self, store, default_workspace, broadcast_cb=None):
        self.store, self.default_workspace, self.broadcast_cb = store, default_workspace, broadcast_cb
        from funguys.host.workflow_log import WorkflowLogger
        db_path = getattr(store, 'db_path', None)
        self.wlog = WorkflowLogger(Path(db_path).parent / 'logs' if db_path else None)
        self.wlog.log('host_start', 'Orchestrator started', default_workspace=str(default_workspace))
        self._state_lock = asyncio.Lock()
        self._submission_lock = asyncio.Lock()
        self._local_running = set()
        self._local_cancellations = {}
        self._consulting_agents = set()
        self._planning = False
        self.btw_session = None
        self.lease_seconds = 90
        self.chat_history = store.list_chat()
        project = store.get_project()
        if not project:
            ws = default_workspace / 'demo_project'
            project = ProjectState(id='proj-' + uuid.uuid4().hex, name='FunGuys Demo Project', description='Demo tasks; create a project for real work.', workspace_path=str(ws.resolve()))
            store.save_project(project)
            self._seed_initial_contract_and_tasks(project)
        self.store.save_project(project)
        self._activate(project)
        for task in store.list_tasks():
            if task.status == TaskStatus.IN_QA or (task.status == TaskStatus.IN_PROGRESS and not task.assignment_id):
                task.status = TaskStatus.BLOCKED_ENVIRONMENT
                task.blocked_reason = 'Host restarted during verification. Review and reset this task.'
                task.assignment_id = None
                store.save_task(task)

    def _activate(self, project):
        self.git, self.graph, self.qa = self._prepare_workspace(project)
        self.git_c2 = GitC2Sync(Path(project.workspace_path))

    def _prepare_workspace(self,project):
        manager = GitWorkspaceManager(Path(project.workspace_path))
        manager.ensure_initialized(project.github_url)
        graph = GraphifyEngine(manager.workspace_path)
        qa = AutonomousQAPipeline(manager.workspace_path)
        graph.build_graph()
        return manager,graph,qa

    def _seed_initial_contract_and_tasks(self, project):
        project.contracts_markdown = '# Demo contracts\n\nImplement create_item(title, priority=1), list_items(), compute_metrics(items) in backend/service.py.\nMetrics contain total, completed, completion_rate (percent).\n'
        ws = Path(project.workspace_path)
        ws.mkdir(parents=True, exist_ok=True)
        (ws / 'CONTRACTS.md').write_text(project.contracts_markdown, encoding='utf-8')
        tasks = [TaskItem(id='task-1-contracts', title='Core domain service', description='Implement create_item, list_items and compute_metrics.', complexity_tier=1, work_role='important_coding', target_files=['backend/service.py'], status=TaskStatus.READY), TaskItem(id='task-2-frontend', title='Interactive Web UI', description='Build frontend/index.html.', dependencies=['task-1-contracts'], work_role='gruntwork', target_files=['frontend/index.html'], require_browser=True), TaskItem(id='task-3-tests', title='Automated service tests', description='Test the service contracts.', dependencies=['task-1-contracts'], work_role='testing', target_files=['tests/test_service.py'], require_tests=True)]
        self.store.replace_tasks(tasks, project)

    async def emit_event(self, source, event_type, message, metadata=None):
        evt = ActivityEvent(id='evt-' + uuid.uuid4().hex, source=source, event_type=event_type, message=message, metadata=metadata or {})
        self.store.add_event(evt)
        self.wlog.log('event:' + event_type, message, source=source, metadata=metadata or {})
        if self.broadcast_cb:
            await self.broadcast_cb({'type': 'event', 'event': evt.model_dump()})
        return evt

    def ensure_quiescent(self):
        if self._local_running or self._submission_lock.locked() or any(t.status in (TaskStatus.IN_PROGRESS, TaskStatus.IN_QA) for t in self.store.list_tasks()):
            raise ConflictError('Pause active work and wait for execution/verification to stop before replacing project state')

    async def initialize_project(self, name, description, workspace_path=None, github_url=None):
        async with self._state_lock:
            self.ensure_quiescent()
            ws = Path(workspace_path).resolve() if workspace_path else (self.default_workspace / ('project-' + uuid.uuid4().hex[:12])).resolve()
            project = ProjectState(id='proj-' + uuid.uuid4().hex, name=name.strip() or 'New Project', description=description, workspace_path=str(ws), github_url=github_url)
            prepared = await asyncio.to_thread(self._prepare_workspace, project)
            self.store.replace_tasks([], project)
            self.git, self.graph, self.qa = prepared
            self.git_c2 = GitC2Sync(Path(project.workspace_path))
            self._release_agents()
        await self.emit_event('host', 'project_initialized', f'Initialized {project.name}. Compile and explicitly approve a plan to begin.')
        return project

    def _release_agents(self):
        for agent in self.store.list_agents():
            agent.current_task_id = None
            if agent.status == AgentStatus.WORKING:
                agent.status = AgentStatus.IDLE
            self.store.save_agent(agent)

    async def install_plan(self, tasks, contracts, name=None, description=None, lead_id=None):
        validate_dag(tasks)
        async with self._state_lock:
            self.ensure_quiescent()
            project = self.store.get_project()
            project.plan_approved = False
            project.plan_version = uuid.uuid4().hex
            project.contracts_markdown = contracts
            project.name = name or project.name
            project.description = description or project.description
            project.lead_architect_agent_id = lead_id
            for task in tasks:
                task.project_id, task.plan_version = project.id, project.plan_version
                task.assignment_id = task.assigned_agent_id = task.candidate_path = task.candidate_revision = None
                if not task.work_role and not task.task_role:
                    task.work_role = classify_task_role(task)
                elif task.task_role and not task.work_role:
                    task.work_role = task.task_role
                task.task_role = task.work_role
            self.store.replace_tasks(tasks, project)
            self._release_agents()
        self.wlog.log('plan_installed', f'{len(tasks)} task(s) installed', project_id=project.id, plan_version=project.plan_version, contracts_markdown=contracts, tasks=[t.model_dump(include={'id','title','phase','complexity_tier','work_role','dependencies','target_files','verification_commands','require_tests','require_browser','status'}) for t in tasks])
        await self.emit_event('planner', 'plan_drafted', 'Plan compiled. Review contracts and explicitly approve this plan version.')
        return project

    async def approve_plan(self, contracts_markdown=None, plan_version=None):
        async with self._state_lock:
            if self._planning:
                raise ConflictError('Planning is still in progress')
            project = self.store.get_project()
            if plan_version != project.plan_version:
                raise ConflictError('The plan changed; review the current version before approval')
            if project.plan_approved:
                return project
            validate_dag(self.store.list_tasks())
            if not self.store.list_tasks():
                raise ValueError('Compile or create tasks before approving the plan')
            if contracts_markdown is not None and contracts_markdown != project.contracts_markdown:
                project.plan_version = uuid.uuid4().hex
            project.contracts_markdown = contracts_markdown if contracts_markdown is not None else project.contracts_markdown
            await asyncio.to_thread(self.git.commit_contracts, project.contracts_markdown)
            project.plan_approved = True
            self.store.save_project(project)
        await self.emit_event('human-team', 'plan_approved', 'Reviewed plan version approved; task execution unlocked.')
        await self.schedule_ready_tasks()
        return project

    async def pause_project(self):
        async with self._state_lock:
            project = self.store.get_project()
            project.plan_approved = False
            self.store.save_project(project)
            for task in self.store.list_tasks():
                self._cancel_assignment(task)
                if task.status == TaskStatus.IN_QA:
                    task.status = TaskStatus.BLOCKED_ENVIRONMENT
                    task.blocked_reason = 'Verification cancelled by project pause; reset after review'
                elif task.status == TaskStatus.IN_PROGRESS:
                    task.status = TaskStatus.HANDOFF_PENDING
                else:
                    continue
                task.assignment_id = task.assigned_agent_id = task.assigned_human = None
                self.store.save_task(task)
            self._release_agents()
        await self.emit_event('human-team', 'project_paused', 'Project paused; outstanding assignments invalidated.')

    def _cancel_assignment(self, task):
        cancelled = self._local_cancellations.get(task.assignment_id)
        if cancelled:
            cancelled.set()

    def _ensure_controller_elected(self):
        agents = self.store.list_agents()
        if not agents:
            return None
        current_ctrl = next((a for a in agents if a.is_controller and a.status != AgentStatus.OFFLINE), None)
        if current_ctrl:
            return current_ctrl
        # Elect best connected agent by planning usage rate priority:
        # 1st: Claude (Opus High) -> 2nd: Gemini (Argon Medium) -> 3rd: OpenAI/Codex (Astra Low)
        online = [a for a in agents if a.status != AgentStatus.OFFLINE] or agents
        best = min(
            online,
            key=lambda a: (
                PLANNER_PROVIDER_PRIORITY.get(a.provider, 9),
                a.high_tier_used_pct,
                a.used_pct,
                a.id,
            ),
        )
        for a in agents:
            should_be = (a.id == best.id)
            if a.is_controller != should_be:
                a.is_controller = should_be
                self.store.save_agent(a)
        project = self.store.get_project()
        if project and not project.lead_architect_agent_id:
            project.lead_architect_agent_id = best.id
            self.store.save_project(project)
        return best

    async def designate_controller(self, agent_id: str) -> AgentNode:
        async with self._state_lock:
            target = self.store.get_agent(agent_id)
            if not target:
                raise ValueError('Unknown agent')
            for a in self.store.list_agents():
                a.is_controller = (a.id == agent_id)
                self.store.save_agent(a)
            project = self.store.get_project()
            if project:
                project.lead_architect_agent_id = agent_id
                self.store.save_project(project)
        await self.emit_event('controller', 'controller_elected', f'{target.display_name} designated as Main Controller Agent.', {'agent_id': agent_id})
        return self.store.get_agent(agent_id)

    async def register_agent(self, owner_name, machine_name, profile_key, initial_used_pct=0, worker_id=None, executor='remote', existing_agent_id=None, is_controller=False):
        canonical_key = resolve_profile_key(profile_key)
        if canonical_key not in SUBSCRIPTION_PROFILES or not worker_id:
            raise ValueError('A known subscription profile and persistent worker_id are required')
        profile = SUBSCRIPTION_PROFILES[canonical_key]
        profile_key = canonical_key
        async with self._state_lock:
            existing = next((a for a in self.store.list_agents() if a.worker_id == worker_id), None)
            if existing and existing.id != existing_agent_id:
                raise ConflictError('Worker already registered; reconnect with its worker credential')
            if existing:
                if existing.current_task_id and profile_key != existing.profile_key:
                    raise ConflictError('Release the active task before changing provider')
                agent = existing
                agent.owner_name, agent.machine_name = owner_name.strip(), machine_name.strip()
            else:
                model, effort = select_model_and_effort(profile.provider, 'gruntwork', 0.0, False)
                agent = AgentNode(
                    id='agent-' + uuid.uuid4().hex,
                    owner_name=owner_name.strip() or 'Teammate',
                    machine_name=machine_name.strip() or 'worker',
                    profile_key=profile_key,
                    provider=profile.provider,
                    display_name=profile.display_name,
                    worker_id=worker_id,
                    executor=executor,
                    used_pct=initial_used_pct,
                    high_tier_turns_limit=profile.high_tier_turns_per_window,
                    high_tier_used_pct=initial_used_pct,
                    active_model=model,
                    active_effort=effort,
                    active_model_tier=f"{model}:{effort}",
                    is_controller=bool(is_controller),
                )
                agent.turns_used = int(initial_used_pct * profile.estimated_turns_per_window / 100)
                agent.high_tier_turns_used = int(initial_used_pct * profile.high_tier_turns_per_window / 100)
                if initial_used_pct >= profile.soft_handoff_threshold_pct:
                    agent.status = AgentStatus.COOLDOWN
                    agent.cooldown_until = (datetime.now(timezone.utc)+timedelta(hours=profile.window_hours)).isoformat()
            agent.profile_key, agent.provider, agent.tier = profile_key, profile.provider, profile.tier
            agent.display_name = f'{agent.owner_name}: {profile.display_name}'
            agent.turns_limit, agent.soft_limit_pct = profile.estimated_turns_per_window, profile.soft_handoff_threshold_pct
            agent.high_tier_turns_limit = profile.high_tier_turns_per_window
            agent.last_heartbeat = utc_now_iso()
            if agent.status == AgentStatus.OFFLINE:
                agent.status = AgentStatus.IDLE
            self.store.save_agent(agent)
            self._ensure_controller_elected()
        await self.emit_event(agent.id, 'agent_joined', f'{agent.display_name} connected on {agent.machine_name} ({agent.executor}).')
        await self.schedule_ready_tasks()
        return self.store.get_agent(agent.id)

    def _refresh_agents(self,only_agent_id=None):
        now = datetime.now(timezone.utc)
        agents = [self.store.get_agent(only_agent_id)] if only_agent_id else self.store.list_agents()
        for agent in agents:
            if not agent:
                continue
            before = agent.model_dump_json()
            profile = SUBSCRIPTION_PROFILES.get(resolve_profile_key(agent.profile_key)) or SUBSCRIPTION_PROFILES['chatgpt-astra-sol']
            window = profile.window_hours
            if (now - datetime.fromisoformat(agent.quota_window_started_at)).total_seconds() >= window * 3600:
                agent.used_pct, agent.turns_used, agent.cooldown_until = 0, 0, None
                agent.quota_window_started_at = utc_now_iso()
                if agent.status == AgentStatus.COOLDOWN:
                    agent.status = AgentStatus.IDLE
            if (now - datetime.fromisoformat(agent.high_tier_window_started_at)).total_seconds() >= profile.high_tier_window_hours * 3600:
                agent.high_tier_turns_used = 0
                agent.high_tier_used_pct = 0.0
                agent.high_tier_window_started_at = utc_now_iso()
            if agent.cooldown_until and datetime.fromisoformat(agent.cooldown_until) <= now:
                agent.used_pct, agent.turns_used, agent.cooldown_until = 0, 0, None
                agent.quota_window_started_at = utc_now_iso()
                agent.status = AgentStatus.IDLE
            if agent.executor in ('remote', 'mcp', 'mcp_ide') and (now - datetime.fromisoformat(agent.last_heartbeat)).total_seconds() > self.lease_seconds:
                agent.status = AgentStatus.OFFLINE
            task = self.store.get_task(agent.current_task_id) if agent.current_task_id else None
            if task and task.status == TaskStatus.IN_PROGRESS and agent.status == AgentStatus.OFFLINE:
                task.status = TaskStatus.HANDOFF_PENDING
                task.last_error = 'Worker heartbeat expired; previous assignment invalidated'
                task.assignment_id = task.assigned_agent_id = None
                self.store.save_task(task)
                agent.current_task_id = None
            elif agent.current_task_id and (not task or task.status not in (TaskStatus.IN_PROGRESS, TaskStatus.IN_QA)):
                agent.current_task_id = None
                if agent.status == AgentStatus.WORKING:
                    agent.status = AgentStatus.IDLE
            if agent.model_dump_json()!=before:
                self.store.save_agent(agent)

    def select_best_agent_for_task(self, task, available=None):
        all_agents = available if available is not None else self.store.list_agents()
        role = classify_task_role(task)
        idle_agents = [
            a for a in all_agents
            if a.status == AgentStatus.IDLE
            and a.id not in self._consulting_agents
            and a.current_task_id is None
            and a.used_pct < a.soft_limit_pct
        ]
        if not idle_agents:
            return None

        # 1. Explicit controller trigger override (`@FG:TRIGGER <task_id> <agent_id>`)
        if task.preferred_agent_id:
            pref = next((a for a in idle_agents if a.id == task.preferred_agent_id), None)
            if pref:
                return pref

        # 2. Strict tier filtering for Tier-1 important work when Tier-1 agents are available;
        #    also allow Gemini (Argon Low) when no Tier-1 node is idle and Argon bucket has headroom.
        tier_candidates = [a for a in idle_agents if (task.complexity_tier != 1 or a.tier == 1)]
        if not tier_candidates and task.complexity_tier == 1:
            tier_candidates = [
                a for a in idle_agents
                if a.provider == 'gemini' and a.high_tier_used_pct < 85.0
            ]
        if not tier_candidates:
            return None

        def _score(agent: AgentNode):
            tier_mismatch = (agent.tier != task.complexity_tier)
            # For testing / gruntwork, prefer Flash High / Sol High / Opus Medium (standard bucket)
            if role in ('testing', 'gruntwork'):
                role_pref = 0 if agent.tier == 2 else 1
                return (tier_mismatch, role_pref, agent.used_pct, agent.id)
            # For important_coding, prefer healthy high_tier bucket (Opus High / Argon Low / Sol High)
            high_exhausted = 1 if agent.high_tier_used_pct >= 85.0 else 0
            return (tier_mismatch, high_exhausted, agent.used_pct, agent.id)

        return min(tier_candidates, key=_score)

    def _blackboard_digest_for_task(self, task: TaskItem) -> str:
        entries = self.store.list_blackboard(limit=30)
        if not entries:
            return ''
        lines = []
        for e in entries[:15]:
            if e.kind == 'lock':
                lines.append(f"[LOCK] {e.key} (by {e.agent_id})")
            elif e.kind == 'tripwire':
                lines.append(f"[TRIPWIRE] {e.key}: {e.value}")
            else:
                lines.append(f"[FACT] {e.key}: {e.value}")
        return "Shared Blackboard Facts & Locks:\n" + "\n".join(lines)

    async def process_signals(self, sender_agent_id: str, raw_signals: List[Dict[str, str]]) -> List[Dict[str, str]]:
        """Process 1-line C2 coordination signals (`@FG:CLAIM`, `FACT`, `TRIPWIRE`, `TRIGGER`, `STEER`, `HOLD`, `GO`, `VETO`)."""
        processed = []
        trigger_schedule = False
        async with self._state_lock:
            sender = self.store.get_agent(sender_agent_id)
            is_ctrl = bool(sender and sender.is_controller) or sender_agent_id in ('controller', 'human-team')
            for sig in raw_signals:
                op = str(sig.get('op', '')).upper()
                target = str(sig.get('target', '')).strip()
                payload = str(sig.get('payload', '')).strip()[:500]
                if not op or not target:
                    continue
                if op == SignalOp.CLAIM.value:
                    self.store.put_blackboard(BlackboardEntry(key=target, kind='lock', value=payload or 'claimed', agent_id=sender_agent_id))
                    processed.append({'op': op, 'target': target})
                elif op == SignalOp.RELEASE.value:
                    self.store.remove_blackboard_locks(sender_agent_id, key=target)
                    processed.append({'op': op, 'target': target})
                elif op == SignalOp.FACT.value:
                    self.store.put_blackboard(BlackboardEntry(key=target, kind='fact', value=payload, agent_id=sender_agent_id))
                    processed.append({'op': op, 'target': target, 'payload': payload})
                elif op == SignalOp.TRIPWIRE.value:
                    self.store.put_blackboard(BlackboardEntry(key=target, kind='tripwire', value=payload, agent_id=sender_agent_id))
                    for other in self.store.list_tasks():
                        if target in (other.target_files or []) and other.status in (TaskStatus.READY, TaskStatus.IN_PROGRESS):
                            other.handoff_digest = ((other.handoff_digest or '') + f"\n[TRIPWIRE {target}]: {payload}").strip()
                            self.store.save_task(other)
                    processed.append({'op': op, 'target': target, 'payload': payload})
                elif op == SignalOp.STEER.value:
                    import re as _re
                    role_match = _re.search(r'\bROLE\s*=\s*(important_coding|gruntwork|testing)\b', payload, _re.IGNORECASE)
                    reclassified_role = role_match.group(1).lower() if role_match else None
                    # Target can be a task_id or an agent_id
                    target_task = self.store.get_task(target)
                    target_agent = self.store.get_agent(target)
                    if not target_task and target_agent and target_agent.current_task_id:
                        target_task = self.store.get_task(target_agent.current_task_id)
                    if reclassified_role and target_task and is_ctrl:
                        target_task.task_role = reclassified_role
                        target_task.work_role = reclassified_role
                        target_task.complexity_tier = 1 if reclassified_role == 'important_coding' else 2
                        self.store.save_task(target_task)
                    mode = 'interrupt' if (is_ctrl and payload.upper().startswith('!')) else 'steer'
                    clean_payload = payload[1:].strip() if mode == 'interrupt' else payload
                    if clean_payload:
                        target_agent_id_for_queue = target_agent.id if target_agent else (target_task.assigned_agent_id if target_task and target_task.assigned_agent_id else target)
                        item = PromptQueueItem(
                            target_agent_id=target_agent_id_for_queue,
                            sender_agent_id=sender_agent_id,
                            mode=mode,
                            prompt=clean_payload,
                            priority=50 if is_ctrl else 10,
                        )
                        self.store.enqueue_prompt(item)
                        if target_task:
                            target_task.handoff_digest = ((target_task.handoff_digest or '') + f"\n[Controller Steer]: {clean_payload}").strip()
                            self.store.save_task(target_task)
                        processed.append({'op': op, 'target': target, 'mode': mode, 'role': reclassified_role or ''})
                elif op == SignalOp.TRIGGER.value:
                    task = self.store.get_task(target)
                    if task:
                        if payload:
                            task.preferred_agent_id = payload.split()[0]
                        task.held = False
                        task.held_by_controller = False
                        task.hold_reason = None
                        if task.status == TaskStatus.PENDING:
                            completed = {t.id for t in self.store.list_tasks() if t.status == TaskStatus.COMPLETED}
                            if all(d in completed for d in task.dependencies):
                                task.status = TaskStatus.READY
                        self.store.save_task(task)
                        trigger_schedule = True
                        processed.append({'op': op, 'target': target, 'preferred_agent_id': task.preferred_agent_id or ''})
                elif op == SignalOp.HOLD.value and is_ctrl:
                    task = self.store.get_task(target)
                    if task:
                        task.held = True
                        task.held_by_controller = True
                        task.hold_reason = payload or 'Held by Main Controller (@FG:HOLD)'
                        self.store.save_task(task)
                        processed.append({'op': op, 'target': target})
                elif op == SignalOp.GO.value and is_ctrl:
                    task = self.store.get_task(target)
                    if task:
                        task.held = False
                        task.held_by_controller = False
                        task.hold_reason = None
                        self.store.save_task(task)
                        trigger_schedule = True
                        processed.append({'op': op, 'target': target})
                elif op == SignalOp.VETO.value and is_ctrl:
                    task = self.store.get_task(target)
                    if task:
                        self._cancel_assignment(task)
                        task.status = TaskStatus.READY
                        task.last_error = f"Vetoed by Controller ({sender_agent_id}): {payload or 'Rework required'}"
                        task.assignment_id = task.assigned_agent_id = None
                        self.store.save_task(task)
                        trigger_schedule = True
                        processed.append({'op': op, 'target': target})
        if processed:
            await self.emit_event(sender_agent_id, 'c2_signals_processed', f"Processed {len(processed)} C2 signal(s): {', '.join(p['op'] + ':' + p['target'] for p in processed)}", {'signals': processed})
        if trigger_schedule:
            await self.schedule_ready_tasks()
        return processed

    async def steer_or_trigger_agent(
        self,
        target_agent_id: str,
        prompt: str,
        mode: str = "steer",
        task_id: Optional[str] = None,
        sender_agent_id: str = "controller",
    ) -> Dict[str, object]:
        """Allow the Main Controller or Human to queue a prompt, live-steer, or immediately trigger an agent."""
        clean_mode = mode if mode in ("queue", "steer", "interrupt") else "steer"
        async with self._state_lock:
            agent = self.store.get_agent(target_agent_id)
            if not agent:
                raise ValueError("Target agent not found")
            item = PromptQueueItem(
                target_agent_id=target_agent_id,
                sender_agent_id=sender_agent_id,
                mode=clean_mode,
                prompt=prompt.strip(),
                priority=80 if clean_mode == "interrupt" else 40,
            )
            self.store.enqueue_prompt(item)
            active_tid = task_id or agent.current_task_id
            if active_tid:
                task = self.store.get_task(active_tid)
                if task:
                    prefix = f"{task.handoff_digest}\n" if task.handoff_digest else ""
                    task.handoff_digest = f"{prefix}[Controller {clean_mode.upper()}]: {prompt.strip()}"
                    if not agent.current_task_id and task.status in (TaskStatus.READY, TaskStatus.PENDING):
                        task.preferred_agent_id = target_agent_id
                        task.held = False
                        task.held_by_controller = False
                        task.hold_reason = None
                    if clean_mode == "interrupt" and task.status == TaskStatus.IN_PROGRESS:
                        self._cancel_assignment(task)
                    self.store.save_task(task)
        await self.emit_event(sender_agent_id, "agent_steered", f"Queued [{clean_mode.upper()}] directive for {agent.display_name}", {"target_agent_id": target_agent_id, "mode": clean_mode})
        assignments = await self.schedule_ready_tasks()
        return {"queued": item.model_dump(), "assignments": assignments}

    async def schedule_ready_tasks(self):
        assignments = []
        async with self._state_lock:
            self._refresh_agents()
            self._ensure_controller_elected()
            project = self.store.get_project()
            if not project.plan_approved or self._planning:
                return []
            completed = {t.id for t in self.store.list_tasks() if t.status == TaskStatus.COMPLETED}
            available = self.store.list_agents()
            active_locks = {e.key: e.agent_id for e in self.store.list_blackboard(kind='lock')}
            tasks = sorted(self.store.list_tasks(), key=lambda t: (t.status != TaskStatus.HANDOFF_PENDING, not bool(t.preferred_agent_id), t.phase, t.complexity_tier, t.id))
            for task in tasks:
                if len(assignments)>=8:
                    break
                if task.status == TaskStatus.PENDING and all(dep in completed for dep in task.dependencies):
                    task.status = TaskStatus.READY
                if task.status not in (TaskStatus.READY, TaskStatus.HANDOFF_PENDING) or task.assigned_human:
                    continue
                if task.held or task.held_by_controller:
                    task.blocked_reason = task.hold_reason or 'Held by Main Controller (@FG:HOLD); waiting for @FG:GO'
                    self.store.save_task(task)
                    continue
                agent = self.select_best_agent_for_task(task,available)
                if not agent:
                    reason = 'Waiting for a connected worker with sufficient quota and the required tier'
                    if task.blocked_reason != reason:
                        self.wlog.log('schedule_waiting', reason, task_id=task.id, complexity_tier=task.complexity_tier, agents=[{'id':a.id,'status':a.status.value,'tier':a.tier,'used_pct':a.used_pct,'executor':a.executor} for a in available])
                    task.blocked_reason = reason
                    self.store.save_task(task)
                    continue
                conflicting_lock = next((f for f in (task.target_files or []) if f in active_locks and active_locks[f] != agent.id), None)
                if conflicting_lock:
                    task.blocked_reason = f'Target file {conflicting_lock} is claimed on blackboard by {active_locks[conflicting_lock]}'
                    self.store.save_task(task)
                    continue
                assignment_id = uuid.uuid4().hex
                try:
                    previous_candidate = task.candidate_path
                    candidate = await asyncio.to_thread(self.git.prepare_candidate, assignment_id, task.candidate_revision)
                    await asyncio.to_thread(self.git.integrate_latest, candidate)
                    base = await asyncio.to_thread(self.git.git, 'rev-parse', 'HEAD', cwd=candidate)
                except (ValueError, RuntimeError) as exc:
                    self.wlog.exception('assignment_prepare_failed', 'Could not prepare candidate worktree', exc, task_id=task.id)
                    task.status, task.blocked_reason = TaskStatus.BLOCKED_ENVIRONMENT, str(exc)
                    self.store.save_task(task)
                    continue
                role = classify_task_role(task)
                model, effort = select_model_and_effort(agent.provider, role, agent.high_tier_used_pct)
                task.task_role = role
                task.work_role = role
                task.assigned_model = f"{model}:{effort}"
                task.status, task.assignment_id = TaskStatus.IN_PROGRESS, assignment_id
                task.project_id, task.plan_version = project.id, project.plan_version
                task.base_revision, task.candidate_path = base, str(candidate)
                task.candidate_revision = None
                task.assigned_agent_id, task.blocked_reason = agent.id, None
                task.updated_at = utc_now_iso()
                task.lease_expires_at = (datetime.now(timezone.utc) + timedelta(seconds=self.lease_seconds)).isoformat()
                agent.status, agent.current_task_id = AgentStatus.WORKING, task.id
                agent.active_model, agent.active_effort = model, effort
                agent.active_model_tier = f"{model}:{effort}"
                self.store.save_task(task)
                self.store.save_agent(agent)
                assignments.append(self.assignment_payload(task, agent))
                if previous_candidate and Path(previous_candidate) != candidate:
                    try:
                        await asyncio.to_thread(self.git.remove_candidate,Path(previous_candidate))
                    except (ValueError,RuntimeError,OSError):
                        pass  # Keep a recoverable artifact if a host filesystem lock prevents cleanup.
        for assignment in assignments:
            await self.emit_event('router', 'task_assigned', f"Assigned {assignment['task']['title']} to {assignment['agent']['display_name']} [{assignment['recommended_model']}-{assignment['recommended_effort']}]", {'task_id': assignment['task']['id']})
        return assignments

    def public_agent(self, agent):
        return agent.model_dump(exclude={'credential_hash'})

    def public_task(self, task):
        return task.model_dump(exclude={'candidate_path'})

    def assignment_payload(self, task, agent):
        role = classify_task_role(task)
        model, effort = select_model_and_effort(agent.provider, role, agent.high_tier_used_pct)
        full_contracts = self.store.get_project().contracts_markdown
        sliced_contracts = slice_contract_for_task(full_contracts, task.target_files, task.exported_symbols)
        pending_prompts = [p.model_dump() for p in self.store.pop_pending_prompts(agent.id)]
        return {
            'task': self.public_task(task),
            'agent': self.public_agent(agent),
            'task_role': role,
            'work_role': role,
            'assigned_model': f"{model}:{effort}",
            'recommended_model': model,
            'recommended_effort': effort,
            'contracts_markdown': sliced_contracts,
            'subgraph_digest': self.graph.get_subgraph_digest(task.target_files),
            'blackboard_digest': self._blackboard_digest_for_task(task),
            'queued_prompts': pending_prompts,
        }

    def assigned_work(self, agent_id):
        agent = self.store.get_agent(agent_id)
        task = self.store.get_task(agent.current_task_id) if agent and agent.current_task_id else None
        if not task or task.status != TaskStatus.IN_PROGRESS or task.assigned_agent_id != agent_id or not self.store.get_project().plan_approved:
            return None
        return self.assignment_payload(task, agent)

    async def heartbeat(self, agent_id, assignment_id=None):
        async with self._state_lock:
            self._refresh_agents(agent_id)
            agent = self.store.get_agent(agent_id)
            if not agent:
                raise ValueError('Unknown worker')
            agent.last_heartbeat = utc_now_iso()
            if agent.status == AgentStatus.OFFLINE:
                agent.status = AgentStatus.COOLDOWN if agent.cooldown_until else AgentStatus.IDLE
            self.store.save_agent(agent)
            task = self.store.get_task(agent.current_task_id) if agent.current_task_id else None
            active = bool(task and assignment_id == task.assignment_id and task.status in (TaskStatus.IN_PROGRESS, TaskStatus.IN_QA))
            if active:
                task.lease_expires_at = (datetime.now(timezone.utc) + timedelta(seconds=self.lease_seconds)).isoformat()
                self.store.save_task(task)
            queued = [p.model_dump() for p in self.store.pop_pending_prompts(agent_id)]
        return {'active': active, 'assignment': self.assigned_work(agent_id), 'queued_prompts': queued}

    def validate_assignment(self, task_id, agent_id, assignment_id, project_id, plan_version, base_revision=None, human=False):
        task, project = self.store.get_task(task_id), self.store.get_project()
        if not task:
            raise ValueError('Unknown task')
        if not project.plan_approved or task.project_id != project.id or task.plan_version != project.plan_version:
            raise ConflictError('Project or plan is no longer approved')
        if task.status != TaskStatus.IN_PROGRESS or task.assignment_id != assignment_id or task.project_id != project_id or task.plan_version != plan_version:
            raise ConflictError('Stale or inactive assignment; reload current work')
        if human:
            if not task.assigned_human:
                raise ConflictError('A human must claim this task first')
        elif not agent_id or task.assigned_agent_id != agent_id or not self.store.get_agent(agent_id):
            raise ConflictError('Submission does not belong to this worker')
        if not human:
            agent = self.store.get_agent(agent_id)
            if agent.executor in ('remote', 'mcp', 'mcp_ide') and (datetime.now(timezone.utc) - datetime.fromisoformat(agent.last_heartbeat)).total_seconds() > self.lease_seconds:
                raise ConflictError('Worker lease expired; reconnect and obtain a fresh assignment')
        if base_revision != task.base_revision:
            raise ConflictError('Submission base revision does not match this assignment')
        return task

    async def report_quota_and_check_handoff(self, agent_id, used_pct, turns_delta=1, rate_limited=False, cooldown_minutes=None, handoff_notes=None, wip_files=None, reset_window=False, assignment_id=None, wip_binary_files=None, deleted_files=None):
        async with self._state_lock:
            agent = self.store.get_agent(agent_id)
            if not agent:
                raise ValueError('Unknown worker')
            if reset_window:
                agent.turns_used = 0
                agent.high_tier_turns_used = 0
                agent.high_tier_used_pct = 0.0
                agent.quota_window_started_at = utc_now_iso()
                agent.high_tier_window_started_at = utc_now_iso()
            agent.used_pct = min(100, max(0, used_pct))
            agent.turns_used += turns_delta
            agent.last_heartbeat = utc_now_iso()
            handoff = None
            if rate_limited or used_pct >= agent.soft_limit_pct:
                task = self.store.get_task(agent.current_task_id) if agent.current_task_id else None
                if task and task.status == TaskStatus.IN_PROGRESS:
                    if assignment_id != task.assignment_id:
                        raise ConflictError('Quota handoff references a stale assignment')
                    if wip_files or wip_binary_files or deleted_files:
                        task.candidate_revision = await asyncio.to_thread(self.git.checkpoint, task, decode_files(wip_files or {},wip_binary_files), deleted_files or [])
                    self._cancel_assignment(task)
                    task.handoff_digest = self.git.create_handoff_document(task.id, agent.id, handoff_notes or 'Quota reached; resume incomplete work.', self.graph.get_subgraph_digest(task.target_files))
                    task.status, task.assigned_agent_id, task.assignment_id = TaskStatus.HANDOFF_PENDING, None, None
                    self.store.save_task(task)
                    handoff = task.id
                agent.status = AgentStatus.COOLDOWN
                if not task or task.status != TaskStatus.IN_QA:
                    agent.current_task_id = None
                agent.cooldown_until = (datetime.now(timezone.utc) + timedelta(minutes=max(1, min(cooldown_minutes or 60, 1440)))).isoformat()
            elif agent.status == AgentStatus.COOLDOWN:
                agent.status, agent.cooldown_until = AgentStatus.IDLE, None
            self.store.save_agent(agent)
            self._ensure_controller_elected()
        if handoff:
            await self.emit_event(agent.id, 'baton_pass_triggered', f'Quota handoff for {handoff}')
        assignments = await self.schedule_ready_tasks()
        return {'agent': self.public_agent(agent), 'handoff': handoff, 'new_assignments': assignments}

    async def delete_agent(self, agent_id):
        async with self._state_lock:
            for task in self.store.list_tasks():
                if task.assigned_agent_id == agent_id and task.status in (TaskStatus.IN_PROGRESS, TaskStatus.IN_QA):
                    self._cancel_assignment(task)
                    task.status, task.assignment_id, task.assigned_agent_id = TaskStatus.HANDOFF_PENDING, None, None
                    task.last_error = 'Assigned worker was removed'
                    self.store.save_task(task)
            self.store.remove_blackboard_locks(agent_id)
            self.store.delete_agent(agent_id)
            self._ensure_controller_elected()
        await self.schedule_ready_tasks()

    async def submit_task_work(self, task_id, agent_id, files, commit_message, used_pct_after=None, *, assignment_id, project_id, plan_version, base_revision, binary_files=None, deleted_files=None, submission_id=None, human=False, _verify_only=False, raw_output=None):
        decoded = decode_files(files, binary_files)
        deleted_files = [validate_relative_path(p) for p in (deleted_files or [])]
        if not decoded and not deleted_files and not _verify_only:
            raise ValueError('No changes submitted; CLI failure does not produce a synthetic implementation')
        if not submission_id:
            raise ValueError('A stable submission_id is required')
        if raw_output and agent_id:
            parsed_sigs = parse_agent_signals(agent_id, str(raw_output))
            if parsed_sigs:
                await self.process_signals(agent_id, [{'op': s.op.value, 'target': s.target, 'payload': s.payload} for s in parsed_sigs])
        payload_digest = hashlib.sha256(json.dumps({'files': {k: hashlib.sha256(v).hexdigest() for k,v in decoded.items()}, 'deleted': sorted(deleted_files)}, sort_keys=True).encode()).hexdigest()
        async with self._submission_lock:
            async with self._state_lock:
                previous = self.store.get_task(task_id)
                if previous and previous.submission_id == submission_id:
                    if previous.submitted_assignment_id != assignment_id or previous.submitted_agent_id != agent_id or previous.project_id != project_id or previous.plan_version != plan_version:
                        raise ConflictError('Submission receipt belongs to a different assignment or worker')
                    if previous.submission_digest != payload_digest:
                        raise ConflictError('Submission ID reused with different content')
                    return {'status': previous.submission_status, 'task': self.public_task(previous), 'replayed': True}
                task = self.validate_assignment(task_id, agent_id, assignment_id, project_id, plan_version, base_revision, human)
                task.status = TaskStatus.IN_QA
                self.store.save_task(task)
                manager, candidate, project = self.git, Path(task.candidate_path), self.store.get_project()
            try:
                if not _verify_only:
                    await asyncio.to_thread(manager.write_changes, candidate, decoded, deleted_files)
                    task.candidate_revision = await asyncio.to_thread(manager.commit_candidate, candidate, task_id, agent_id or task.assigned_human, commit_message)
                expected_head = await asyncio.to_thread(manager.integrate_latest, candidate)
                async with self._state_lock:
                    current = self.store.get_task(task_id)
                    if not current or current.assignment_id != assignment_id:
                        raise ConflictError('Assignment invalidated before QA; candidate was not merged')
                    current.candidate_revision = task.candidate_revision
                    self.store.save_task(current)
                qa = AutonomousQAPipeline(candidate)
                self.wlog.log('qa_started', f'Verifying {task_id}', task_id=task_id, agent_id=agent_id, human=human, changed_files=sorted(decoded), deleted_files=deleted_files, verify_only=_verify_only)
                report = await qa.verify_task(task, project.live_preview_url)
            except Exception as exc:
                self.wlog.exception('submission_failed', f'Submission/QA crashed for {task_id}', exc, task_id=task_id, agent_id=agent_id)
                async with self._state_lock:
                    current = self.store.get_task(task_id)
                    if current and current.assignment_id == assignment_id:
                        current.status, current.blocked_reason = TaskStatus.BLOCKED_ENVIRONMENT, str(exc)
                        current.candidate_revision, current.assignment_id = task.candidate_revision, None
                        self.store.save_task(current)
                        self._finish_agent(current, used_pct_after)
                    elif current and current.candidate_path == str(candidate) and current.status == TaskStatus.BLOCKED_ENVIRONMENT:
                        current.candidate_revision = task.candidate_revision
                        self.store.save_task(current)
                raise
            report.project_id,report.plan_version = project_id,plan_version
            self.store.save_qa_report(report)
            self.wlog.log('qa_report', f'{task_id}: passed={report.overall_passed}', task_id=task_id, attempt=report.attempt, checks=report.checks, environment_blocked=report.environment_blocked, logs=report.logs)
            if report.screenshot_path:
                import shutil
                origin = qa.screenshots_dir / Path(report.screenshot_path).name
                if origin.is_file() and not origin.is_symlink():
                    await asyncio.to_thread(shutil.copyfile, origin, self.qa.screenshots_dir / origin.name)
            async with self._state_lock:
                current = self.store.get_task(task_id)
                active_project = self.store.get_project()
                if not current or current.assignment_id != assignment_id or active_project.id != project_id or active_project.plan_version != plan_version or not active_project.plan_approved:
                    raise ConflictError('Assignment invalidated during verification; result was not merged')
                task.submission_id, task.submission_digest = submission_id, payload_digest
                task.submitted_assignment_id,task.submitted_agent_id = assignment_id,agent_id
                if report.overall_passed:
                    try:
                        task.candidate_revision = await asyncio.to_thread(manager.merge_verified, candidate, expected_head)
                    except (RuntimeError, ValueError) as exc:
                        task.status, task.blocked_reason = TaskStatus.BLOCKED_ENVIRONMENT, str(exc)
                    else:
                        task.status, task.last_error = TaskStatus.COMPLETED, None
                        task.preferred_agent_id = None
                        task.architect_review = 'Automated structural and configured QA checks completed. No model consensus review was performed.'
                        if agent_id:
                            self.store.remove_blackboard_locks(agent_id)
                elif report.environment_blocked:
                    task.status, task.blocked_reason, task.last_error = TaskStatus.BLOCKED_ENVIRONMENT, report.logs, compress_error_log(report.logs)
                else:
                    task.retry_count += 1
                    task.last_error = compress_error_log(report.logs)
                    if task.retry_count >= task.max_retries:
                        task.status, task.circuit_breaker_tripped = TaskStatus.BLOCKED_CIRCUIT_BREAKER, True
                    else:
                        task.status = TaskStatus.READY
                        if task.retry_count == 2:
                            task.complexity_tier = 1
                            task.work_role = 'important_coding'
                self._finish_agent(task, used_pct_after)
                if task.status != TaskStatus.COMPLETED:
                    task.assigned_agent_id, task.assignment_id = None, None
                if task.assigned_human and task.status == TaskStatus.READY:
                    task.blocked_reason = 'Human retry required; reclaim or release this task'
                task.updated_at = utc_now_iso()
                task.submission_status = task.status.value
                self.store.save_task(task)
                self.wlog.log('task_outcome', f'{task.id} -> {task.status.value}', task_id=task.id, status=task.status.value, retry_count=task.retry_count, complexity_tier=task.complexity_tier, blocked_reason=task.blocked_reason)
            if task.status == TaskStatus.COMPLETED:
                try:
                    _, alerts = await asyncio.to_thread(self.graph.scan_and_detect_contract_deltas, task.id, agent_id or 'human', self.store.list_tasks())
                except Exception as exc:
                    alerts = []
                    self.graph.last_error = str(exc)
                    self.graph.last_graph_data['warnings'] = ['Graph update failed: '+str(exc)]
                    await self.emit_event('graph','graph_update_failed',str(exc))
                for alert in alerts:
                    alert.project_id,alert.plan_version = project_id,plan_version
                    self.store.save_contract_alert(alert)
                async with self._state_lock:
                    for other in self.store.list_tasks():
                        notices = [a.summary for a in alerts if other.id in a.dependent_task_ids]
                        if notices:
                            other.handoff_digest = (other.handoff_digest or '') + '\nContract updates to reconcile:\n' + '\n'.join(notices)
                            if other.status == TaskStatus.IN_PROGRESS and other.assigned_agent_id:
                                self._cancel_assignment(other)
                                self._finish_agent(other)
                                other.status,other.assignment_id,other.assigned_agent_id = TaskStatus.HANDOFF_PENDING,None,None
                                other.blocked_reason = 'Contract changed; refreshing assignment context'
                            self.store.save_task(other)
                try:
                    await asyncio.to_thread(manager.remove_candidate,candidate)
                    task.candidate_path = None
                    self.store.save_task(task)
                except (ValueError,RuntimeError,OSError):
                    await self.emit_event('host','cleanup_pending',f'Verified worktree retained for {task.id}; cleanup can be retried after filesystem locks clear')
                if hasattr(self, 'git_c2') and self.git_c2:
                    await asyncio.to_thread(self.git_c2.write_local_checkpoint, self.store.snapshot())
            await self.emit_event('qa', 'qa_result', f'{task.title}: {task.status.value}. {report.checks}', {'task_id': task.id})
            assignments = await self.schedule_ready_tasks()
            return {'status': task.status.value, 'task': self.public_task(task), 'qa_report': report.model_dump(), 'next_assignments': assignments}

    async def retry_verification(self, task_id):
        async with self._state_lock:
            task = self.store.get_task(task_id)
            project = self.store.get_project()
            if not task or task.status != TaskStatus.BLOCKED_ENVIRONMENT or not task.candidate_revision or not task.candidate_path:
                raise ConflictError('No retained candidate is available for QA recheck')
            if not project.plan_approved or task.project_id != project.id or task.plan_version != project.plan_version:
                raise ConflictError('Approve the current plan before rechecking its candidate')
            previous_human = task.assigned_human
            task.assigned_human,task.assigned_agent_id = 'QA recheck',None
            task.status,task.assignment_id = TaskStatus.IN_PROGRESS,uuid.uuid4().hex
            task.blocked_reason = None
            self.store.save_task(task)
        try:
            return await self.submit_task_work(task.id,None,{},'Recheck retained candidate',assignment_id=task.assignment_id,project_id=task.project_id,plan_version=task.plan_version,base_revision=task.base_revision,submission_id=uuid.uuid4().hex,human=True,_verify_only=True)
        finally:
            async with self._state_lock:
                current = self.store.get_task(task_id)
                if current and current.assigned_human == 'QA recheck':
                    current.assigned_human = previous_human
                    self.store.save_task(current)
            await self.schedule_ready_tasks()

    def _finish_agent(self, task, used_pct_after=None):
        agent = self.store.get_agent(task.assigned_agent_id) if task.assigned_agent_id else None
        if not agent or agent.current_task_id != task.id:
            return
        agent.current_task_id = None
        agent.turns_used += 1
        role = classify_task_role(task)
        is_high_tier_model = (
            agent.active_model in ('argon', 'astra')
            or (agent.active_model == 'opus' and agent.active_effort == 'high')
        )
        if role in ('planning', 'important_coding') and is_high_tier_model:
            agent.high_tier_turns_used += 1
            agent.high_tier_used_pct = min(
                100.0,
                round(agent.high_tier_turns_used / max(1, agent.high_tier_turns_limit) * 100.0, 1),
            )
        if used_pct_after is not None:
            agent.used_pct = min(100, max(0, used_pct_after))
        else:
            agent.used_pct = min(100,max(agent.used_pct,round(agent.turns_used/max(1,agent.turns_limit)*100,1)))
        agent.status = AgentStatus.COOLDOWN if agent.used_pct >= agent.soft_limit_pct else AgentStatus.IDLE
        if agent.status == AgentStatus.COOLDOWN:
            if not agent.cooldown_until:
                profile = SUBSCRIPTION_PROFILES.get(resolve_profile_key(agent.profile_key)) or SUBSCRIPTION_PROFILES['chatgpt-astra-sol']
                agent.cooldown_until = (datetime.now(timezone.utc) + timedelta(hours=profile.window_hours)).isoformat()
        else:
            agent.cooldown_until = None
        self.store.save_agent(agent)

    async def fail_execution(self, task_id, agent_id, assignment_id, error):
        async with self._state_lock:
            task = self.store.get_task(task_id)
            if not task or task.assignment_id != assignment_id or task.assigned_agent_id != agent_id or task.status != TaskStatus.IN_PROGRESS:
                raise ConflictError('Stale execution failure')
            task.execution_failures += 1
            task.last_error = error[:10000]
            self.wlog.log('execution_failed', f'{task_id} execution failed', task_id=task_id, agent_id=agent_id, error=error)
            task.status, task.blocked_reason = TaskStatus.BLOCKED_ENVIRONMENT, 'Execution failed; fix the worker environment and explicitly reset this task'
            task.assignment_id = None
            self._finish_agent(task)
            task.assigned_agent_id = None
            self.store.save_task(task)
        await self.schedule_ready_tasks()
        return self.public_task(task)

    async def claim_task_by_human(self, task_id, human_name):
        async with self._state_lock:
            project, task = self.store.get_project(), self.store.get_task(task_id)
            if not task or not project.plan_approved:
                raise ConflictError('Approve the plan before claiming work')
            completed = {t.id for t in self.store.list_tasks() if t.status == TaskStatus.COMPLETED}
            if not all(d in completed for d in task.dependencies) or task.status in (TaskStatus.COMPLETED, TaskStatus.IN_QA):
                raise ConflictError('Task is completed, verifying, or waiting for dependencies')
            if task.status == TaskStatus.IN_PROGRESS and not task.assigned_human:
                self._cancel_assignment(task)
                self._finish_agent(task)
            if task.status in (TaskStatus.BLOCKED_ENVIRONMENT,TaskStatus.BLOCKED_CIRCUIT_BREAKER):
                task.circuit_breaker_tripped,task.retry_count,task.blocked_reason = False,0,None
            task.assignment_id = uuid.uuid4().hex
            candidate = await asyncio.to_thread(self.git.prepare_candidate, task.assignment_id, task.candidate_revision)
            await asyncio.to_thread(self.git.integrate_latest, candidate)
            task.base_revision = await asyncio.to_thread(self.git.git, 'rev-parse', 'HEAD', cwd=candidate)
            task.candidate_path = str(candidate)
            task.project_id, task.plan_version = project.id, project.plan_version
            task.assigned_human, task.assigned_agent_id = human_name.strip() or 'Human', None
            task.status = TaskStatus.IN_PROGRESS
            self.store.save_task(task)
        return task

    async def release_human(self, task_id):
        async with self._state_lock:
            task = self.store.get_task(task_id)
            if not task or not task.assigned_human or task.status == TaskStatus.IN_QA:
                raise ConflictError('No releasable human task')
            task.assigned_human, task.assignment_id = None, None
            if task.status not in (TaskStatus.BLOCKED_ENVIRONMENT,TaskStatus.BLOCKED_CIRCUIT_BREAKER):
                task.status = TaskStatus.READY
            self.store.save_task(task)
        await self.schedule_ready_tasks()
        return self.store.get_task(task_id)

    async def reset_circuit_breaker(self, task_id):
        async with self._state_lock:
            task = self.store.get_task(task_id)
            if not task or task.status not in (TaskStatus.BLOCKED_CIRCUIT_BREAKER, TaskStatus.BLOCKED_ENVIRONMENT):
                raise ConflictError('Only blocked tasks can be reset')
            task.retry_count, task.execution_failures = 0, 0
            if task.blocked_reason and 'conflict' in task.blocked_reason.lower():
                task.candidate_revision = None
            task.circuit_breaker_tripped, task.blocked_reason = False, None
            task.status, task.assignment_id, task.assigned_human = TaskStatus.READY, None, None
            self.store.save_task(task)
        await self.schedule_ready_tasks()
        return self.store.get_task(task_id)

    async def clear_tasks(self):
        async with self._state_lock:
            self.ensure_quiescent()
            project = self.store.get_project()
            project.plan_approved = False
            project.plan_version = uuid.uuid4().hex
            self.store.replace_tasks([], project)
            self._release_agents()

    async def add_task(self, task):
        async with self._state_lock:
            if self._planning or self.store.get_project().plan_approved:
                raise ConflictError('Pause the project before changing the reviewed task plan')
            self.ensure_quiescent()
            tasks = self.store.list_tasks()
            if any(t.id == task.id for t in tasks):
                raise ConflictError('Task ID already exists')
            validate_dag([*tasks, task])
            if not task.work_role and not task.task_role:
                task.work_role = classify_task_role(task)
            elif task.task_role and not task.work_role:
                task.work_role = task.task_role
            task.task_role = task.work_role
            task.status = TaskStatus.PENDING if task.dependencies else TaskStatus.READY
            self.store.save_task(task)
            project = self.store.get_project()
            project.plan_version = uuid.uuid4().hex
            self.store.save_project(project)
        await self.schedule_ready_tasks()
        return self.store.get_task(task.id)

    async def run_autonomous_execution_loop(self, delay_sec=0):
        """Only explicit local executors run here; remote assignments stay remote."""
        from funguys.worker.adapters import run_cli_agent_task
        from funguys.worker.runtime import write_snapshot, collect_changes, task_prompt
        import tempfile
        import threading
        if getattr(self, '_auto_loop_running', False):
            return
        self._auto_loop_running = True
        try:
            await asyncio.sleep(delay_sec)
            for task in self.store.list_tasks():
                agent = self.store.get_agent(task.assigned_agent_id) if task.assigned_agent_id else None
                if task.status != TaskStatus.IN_PROGRESS or not agent or agent.executor != 'local':
                    continue
                self._local_running.add(task.assignment_id)
                cancelled = threading.Event()
                self._local_cancellations[task.assignment_id] = cancelled
                try:
                    payload = self.assignment_payload(task, agent)
                    with tempfile.TemporaryDirectory(prefix='funguys-local-') as scratch:
                        snapshot = await asyncio.to_thread(bundle, Path(task.candidate_path))
                        await asyncio.to_thread(write_snapshot, Path(scratch), snapshot)
                        prompt = task_prompt(payload)
                        ok, limited, _, output = await asyncio.to_thread(
                            run_cli_agent_task,
                            agent.provider,
                            Path(scratch),
                            prompt,
                            task.target_files,
                            240,
                            cancelled,
                            payload.get('recommended_model'),
                            payload.get('recommended_effort'),
                        )
                        self.wlog.log('local_cli_finished', f'{agent.provider} finished {task.id}', task_id=task.id, agent_id=agent.id, ok=ok, rate_limited=limited, prompt=prompt, output=output)
                        fresh = self.store.get_task(task.id)
                        if not fresh or fresh.assignment_id != task.assignment_id or fresh.status != TaskStatus.IN_PROGRESS:
                            continue
                        if limited:
                            changes = await asyncio.to_thread(collect_changes, Path(scratch), snapshot)
                            await self.report_quota_and_check_handoff(agent.id, 100, rate_limited=True, assignment_id=task.assignment_id, handoff_notes=output[-1000:], wip_files=changes['files'],wip_binary_files=changes['binary_files'],deleted_files=changes['deleted_files'])
                        elif ok:
                            changes = await asyncio.to_thread(collect_changes, Path(scratch), snapshot)
                            await self.submit_task_work(task.id, agent.id, changes['files'], 'Local worker implementation', assignment_id=task.assignment_id, project_id=task.project_id, plan_version=task.plan_version, base_revision=task.base_revision, binary_files=changes['binary_files'], deleted_files=changes['deleted_files'], submission_id=uuid.uuid4().hex, raw_output=output[-4000:])
                        else:
                            await self.fail_execution(task.id, agent.id, task.assignment_id, output)
                except Exception as exc:
                    fresh = self.store.get_task(task.id)
                    if fresh and fresh.assignment_id == task.assignment_id and fresh.status == TaskStatus.IN_PROGRESS:
                        await self.fail_execution(task.id, agent.id, task.assignment_id, str(exc))
                finally:
                    cancelled.set()
                    self._local_cancellations.pop(task.assignment_id,None)
                    self._local_running.discard(task.assignment_id)
        finally:
            self._auto_loop_running = False

    def get_btw_session(self):
        if not self.btw_session:
            return None
        agent = self.store.get_agent(self.btw_session['agent_id'])
        if not agent:
            self.btw_session = None
            return None
        return {
            **self.btw_session,
            'active': True,
            'turns': len(self.btw_session.get('messages', [])) // 2,
            'agent_status': agent.status.value,
            'current_task_id': agent.current_task_id,
        }

    async def end_btw_session(self):
        async with self._state_lock:
            session = self.btw_session
            if not session:
                return {'ended': False, 'message': 'No active side conversation'}
            task_id = session.get('task_id')
            notes = session.get('notes', [])
            agent_label = session.get('agent_label', 'Agent')
            if task_id and notes:
                task = self.store.get_task(task_id)
                if task:
                    guidance = '\n'.join(f'- {n}' for n in notes)
                    prefix = f'{task.handoff_digest}\n\n' if task.handoff_digest else ''
                    task.handoff_digest = f'{prefix}### User /btw Side Conversation Guidance\n{guidance}'
                    self.store.save_task(task)
                    await self.emit_event(task_id, 'btw_guidance_committed', f'Side conversation with {agent_label} ended. Recorded {len(notes)} guidance note(s) on {task_id}.')
            self.btw_session = None
        return {'ended': True}

    async def ask_project_question(self, question, preferred_agent_id=None):
        from funguys.worker.adapters import run_text_agent
        clean_q = question.strip()
        is_btw = clean_q.lower().startswith('/btw')
        btw_note = clean_q[4:].strip() if is_btw else None

        async with self._state_lock:
            if self._planning:
                raise ConflictError('Architect consultation is already in progress')
            agents = self.store.list_agents()
            if not agents:
                raise ConflictError('No agents are currently registered or connected.')

            # 1. If an active side conversation is connected, route follow-ups to that agent
            selected = None
            if self.btw_session and self.btw_session.get('agent_id'):
                selected = next((a for a in agents if a.id == self.btw_session['agent_id']), None)

            # 2. If a specific preferred agent was requested
            if not selected and preferred_agent_id and preferred_agent_id != 'auto':
                selected = next((a for a in agents if a.id == preferred_agent_id), None)
                if not selected:
                    raise ValueError('Selected agent does not exist')

            # 3. Intelligent default routing (prefer Main Controller / Claude Opus High -> Gemini Argon Med -> OpenAI Astra Low)
            if not selected:
                if is_btw:
                    selected = next((a for a in agents if a.status == AgentStatus.WORKING and a.current_task_id), None)
                if not selected:
                    selected = min(
                        (a for a in agents if a.status == AgentStatus.IDLE and a.id not in self._consulting_agents),
                        key=lambda a: (not a.is_controller, PLANNER_PROVIDER_PRIORITY.get(a.provider, 9), a.tier),
                        default=None,
                    )
                if not selected:
                    selected = min(
                        agents,
                        key=lambda a: (not a.is_controller, PLANNER_PROVIDER_PRIORITY.get(a.provider, 9), a.tier),
                        default=None,
                    )

            if not selected:
                raise ConflictError('No agent is available to answer questions.')
            if selected.used_pct >= selected.soft_limit_pct:
                raise ConflictError('Selected agent has reached its soft quota limit.')

            is_side_convo = is_btw or bool(self.btw_session) or (selected.status == AgentStatus.WORKING)

            if is_side_convo:
                if not self.btw_session or self.btw_session.get('agent_id') != selected.id:
                    self.btw_session = {
                        'id': 'btw-' + uuid.uuid4().hex[:8],
                        'agent_id': selected.id,
                        'agent_codename': selected.machine_name,
                        'agent_label': selected.display_name,
                        'task_id': selected.current_task_id,
                        'messages': [],
                        'notes': [],
                    }
                if is_btw and btw_note:
                    self.btw_session['notes'].append(btw_note)
                    self.store.enqueue_prompt(PromptQueueItem(
                        target_agent_id=selected.id,
                        sender_agent_id='human-team',
                        mode='steer',
                        prompt=btw_note,
                        priority=50,
                    ))
                    if selected.current_task_id:
                        task = self.store.get_task(selected.current_task_id)
                        if task:
                            prefix = f'{task.handoff_digest}\n\n' if task.handoff_digest else ''
                            task.handoff_digest = f'{prefix}[User /btw guidance]: {btw_note}'
                            self.store.save_task(task)

            was_idle = (selected.status == AgentStatus.IDLE)
            if was_idle:
                self._consulting_agents.add(selected.id)
                selected.status = AgentStatus.WORKING
                self.store.save_agent(selected)

            project = self.store.get_project()
            current_task = self.store.get_task(selected.current_task_id) if selected.current_task_id else None

        try:
            files = await asyncio.to_thread(bundle, self.git.workspace_path)
            target_names = set(current_task.target_files if current_task else [])
            selected_items = [item for item in files['files'].items() if item[0] in target_names]
            if not selected_items:
                selected_items = list(files['files'].items())[:3]
            context = '\n'.join(f'FILE {name}\n{content[:800]}' for name, content in selected_items[:3])
            contracts_excerpt = (project.contracts_markdown or '')[:1500]

            task_info = ''
            if current_task:
                task_info = (
                    f"\nActive Assignment Context:\n"
                    f"You are currently working on task {current_task.id}: '{current_task.title}'\n"
                    f"Task Specification:\n{current_task.description}\n"
                    f"Target Files: {', '.join(current_task.target_files)}\n"
                    f"Current Task Status: {current_task.status.value}\n"
                    f"Known Failures to Address: {current_task.last_error or 'None'}\n"
                )

            history_info = ''
            if self.btw_session and self.btw_session.get('messages'):
                history_lines = [f"{m['role'].upper()}: {m['text']}" for m in self.btw_session['messages'][-4:]]
                history_info = '\nRecent side conversation history:\n' + '\n'.join(history_lines) + '\n'

            prompt = (
                f"You are {selected.display_name} on {selected.machine_name}.\n"
                f"You are participating in a conversation with the user about the project.\n"
                f"Project: {project.name}\nContracts excerpt:\n{contracts_excerpt}\n"
                f"{task_info}"
                f"{history_info}"
                f"Workspace Excerpts:\n{context}\n\n"
                f"User: {clean_q}\n\n"
                f"Answer the user's question directly, concisely, and helpfully based on the context above. "
                f"If the user gave a '/btw' note or guidance for your current task, acknowledge it and explain briefly how it applies. "
                f"If acting as the Main Controller, you may also emit 1-line C2 directives (`@FG:STEER <agent_id> <msg>` or `@FG:TRIGGER <task_id> <agent_id>`). "
                f"Do not call tools or attempt file editing in this response."
            )
            try:
                answer = await asyncio.to_thread(run_text_agent, selected.provider, prompt, role='planning')
            except TypeError:
                answer = await asyncio.to_thread(run_text_agent, selected.provider, prompt)
            c2_sigs = parse_agent_signals(selected.id, answer)
            if c2_sigs:
                await self.process_signals(selected.id, [{'op': s.op.value, 'target': s.target, 'payload': s.payload} for s in c2_sigs])

            async with self._state_lock:
                if self.btw_session:
                    self.btw_session['messages'].append({'role': 'user', 'text': clean_q, 'timestamp': utc_now_iso(), 'is_btw': is_btw})
                    self.btw_session['messages'].append({'role': 'agent', 'text': answer, 'timestamp': utc_now_iso()})

                entry = {
                    'id': 'msg-' + uuid.uuid4().hex,
                    'project_id': project.id,
                    'timestamp': utc_now_iso(),
                    'question': clean_q,
                    'answer': answer,
                    'agent_id': selected.id,
                    'agent_codename': selected.machine_name,
                    'agent_label': selected.display_name,
                    'routing_reason': f"Side conversation with {selected.machine_name}" if is_side_convo else f"Executed by {selected.display_name}",
                    'matched_domain': 'Task /btw Guidance' if is_btw else 'Project context',
                    'is_btw': is_btw,
                }
                self.store.save_chat(entry)
                self.chat_history = self.store.list_chat()
                return entry
        finally:
            if was_idle:
                async with self._state_lock:
                    self._consulting_agents.discard(selected.id)
                    agent = self.store.get_agent(selected.id)
                    if agent and not agent.current_task_id:
                        agent.status = AgentStatus.COOLDOWN if agent.cooldown_until else AgentStatus.IDLE
                        self.store.save_agent(agent)
