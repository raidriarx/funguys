# Gaper Bingzoid (FunGuys) — AI-Executable Implementation Plan (`PLAN_AI_AGENT.md`)

> **DIRECTIVE FOR CODING AGENT**:
> Execute Steps 1 through 6 in exact sequential order to implement **Model A ("Player-Coach" Controller)** with **Feature #9 (Mid-Work Task Drop & Baton Pass on Controller Promotion / Arbitration)**, **Feature #2 (Explicit Controller Arbitration Instructions & `@FG:DISPUTE` Protocol)**, and **Feature #4 (On-Demand Context Window Reset & Targeted Handoff Selection)**.
>
> **Critical Invariants to Preserve**:
> 1. `funguys/worker/runtime.py` is served standalone via `GET /worker.py` to remote machines that do **not** have the `funguys` package installed. Therefore, `funguys/worker/runtime.py` MUST ONLY import Python standard-library modules at the top level. Define `SIGNAL_PATTERN`, `controller_arbitration_prompt`, and `task_prompt` inside `runtime.py` and re-export them in `funguys/protocol.py`.
> 2. Preserve all existing test-facing symbols and endpoints so all 63 existing tests in `tests/` continue to pass without regression.
> 3. Preserve the **Cross-Provider Planning Priority (`claude opus:high` $\to$ `gemini argon:medium` $\to$ `codex astra:low`)** and **Work-Importance Routing (`important_coding` vs. `gruntwork` / `testing`)**.

---

## STEP 1: EXPAND SIGNAL & REQUEST MODELS (`funguys/models.py`)

### 1.1 Update `SignalOp` and `PromptQueueItem` in `funguys/models.py`
1. Add `DISPUTE = "DISPUTE"` and `HANDOFF = "HANDOFF"` to `SignalOp`:
```python
class SignalOp(str, Enum):
    CLAIM = "CLAIM"
    RELEASE = "RELEASE"
    FACT = "FACT"
    TRIPWIRE = "TRIPWIRE"
    TRIGGER = "TRIGGER"
    STEER = "STEER"
    HOLD = "HOLD"
    GO = "GO"
    VETO = "VETO"
    DISPUTE = "DISPUTE"
    HANDOFF = "HANDOFF"
```
2. Update `PromptQueueItem.mode` to include `"handoff"`:
```python
class PromptQueueItem(BaseModel):
    id: str = Field(default_factory=lambda: f"pq-{uuid.uuid4().hex[:8]}")
    target_agent_id: str
    sender_agent_id: str
    mode: Literal["queue", "steer", "interrupt", "handoff"] = "queue"
    prompt: str = Field(min_length=1, max_length=4000)
    priority: int = Field(default=10, ge=0, le=100)
    delivered: bool = False
    created_at: str = Field(default_factory=utc_now_iso)
```
3. Add `HandoffAgentRequest` and `ArbitrateDisputeRequest` at the bottom of `funguys/models.py`:
```python
class HandoffAgentRequest(BaseModel):
    target_agent_id: Optional[str] = None
    handoff_notes: str = Field(default="Context window reset & baton pass handoff.", max_length=4000)
    assignment_id: Optional[str] = None
    wip_files: Dict[str, str] = Field(default_factory=dict)
    wip_binary_files: Dict[str, str] = Field(default_factory=dict)
    deleted_files: List[str] = Field(default_factory=list)
    reset_window: bool = False


class ArbitrateDisputeRequest(BaseModel):
    sender_agent_id: str = "worker"
    target: str = Field(min_length=1, max_length=256)
    reason: str = Field(min_length=1, max_length=4000)
    task_id: Optional[str] = None
```

---

## STEP 2: UPDATE STANDALONE RUNTIME & PROTOCOL (`funguys/worker/runtime.py` & `funguys/protocol.py`)

### 2.1 Update `SIGNAL_PATTERN` in `funguys/worker/runtime.py`
Update `SIGNAL_PATTERN` (around line 55) to recognize `DISPUTE` and `HANDOFF`:
```python
SIGNAL_PATTERN = re.compile(
    r'@FG:(CLAIM|RELEASE|FACT|TRIPWIRE|TRIGGER|STEER|HOLD|GO|VETO|DISPUTE|HANDOFF)\s+([^\s\r\n]+)(?:[ \t]+([^\r\n]+))?',
    re.IGNORECASE,
)
```

### 2.2 Add `controller_arbitration_prompt` and Update `task_prompt` in `funguys/worker/runtime.py`
1. Add `controller_arbitration_prompt` directly above `task_prompt(assignment)`:
```python
def controller_arbitration_prompt(dispute, project_name, contracts_markdown, blackboard_digest, tasks_summary, code_excerpts=""):
    """Build the explicit instruction prompt for the Main Controller Agent when arbitrating a swarm dispute."""
    return (
        f"You are the MAIN CONTROLLER & LEAD ARCHITECT AI for project '{project_name}'.\n"
        f"Your role is high-level swarm arbitration, contract enforcement, and task routing (NO direct file editing in this turn).\n\n"
        f"=== ACTIVE SWARM DISPUTE / ESCALATION ===\n"
        f"- Raised By Agent: {dispute.get('sender_agent_id', 'worker')}\n"
        f"- Target File / Module / Task: {dispute.get('target', '')}\n"
        f"- Problem Description: {dispute.get('reason', '')}\n\n"
        f"=== AGREED INTERFACE CONTRACTS (Excerpt) ===\n"
        f"{(contracts_markdown or '')[:2000]}\n\n"
        f"=== ACTIVE TASKS & ASSIGNMENTS ===\n"
        f"{tasks_summary}\n\n"
        f"{blackboard_digest}\n"
        f"{('=== RELEVANT WORKSPACE CODE EXCERPTS ===' + chr(10) + code_excerpts) if code_excerpts else ''}\n\n"
        f"=== YOUR CONTROLLER DUTIES ===\n"
        f"1. Diagnose the root cause of the conflict (e.g., signature mismatch against CONTRACTS.md, file lock contention, or missing export).\n"
        f"2. Provide a concise, binding architectural ruling.\n"
        f"3. Emit 1-line C2 coordination signals (one per line) to enforce your ruling across the worker swarm:\n"
        f"   - `@FG:STEER <agent_id_or_task_id> <directive>` (streams live into the worker's `.funguys_inbox.md`)\n"
        f"   - `@FG:STEER <task_id> ROLE=important_coding <directive>` (re-classifies task tier if needed)\n"
        f"   - `@FG:FACT <key> <value>` (records binding architectural decision on the shared Blackboard)\n"
        f"   - `@FG:HOLD <task_id> <reason>` or `@FG:GO <task_id>` (pauses or unblocks dependent tasks)\n"
        f"   - `@FG:VETO <task_id> <reason>` (cancels a broken implementation and forces rework)\n"
        f"   - `@FG:HANDOFF <agent_id> <target_agent_id> <reason>` (resets an agent's context window and hands off its WIP)\n"
        f"Do not call file-editing tools in this response."
    )
```
2. Update `task_prompt(assignment)` in `funguys/worker/runtime.py` so every worker knows:
   - The 4 problem-detection feedback loops (CLI test/import errors, `CONTRACTS.md` mismatches, Blackboard `[LOCK]`/`[TRIPWIRE]` notices, and QA failures outside `target_files`).
   - How to escalate disputes via `@FG:DISPUTE <file_or_task> <reason>` or request a context-window handoff via `@FG:HANDOFF <self_or_target> <reason>`:
```python
def task_prompt(assignment):
    task = assignment['task']
    contract_slice = slice_contract_for_task(
        assignment.get('contracts_markdown', ''),
        task.get('target_files', []),
        task.get('exported_symbols', []),
    )
    compact_err = compress_error_log(task.get('last_error') or '')
    blackboard_digest = assignment.get('blackboard_digest', '')
    queued_prompts = assignment.get('queued_prompts', [])
    steer_block = ''
    if queued_prompts:
        joined_prompts = '\n'.join(f"- [{p.get('mode','steer').upper()}] {p.get('prompt','')}" for p in queued_prompts)
        steer_block = f"\nController Steering Directives (HIGHEST PRIORITY):\n{joined_prompts}\n"
    return (
        f"You are an authenticated FunGuys Worker Agent assigned to implement {task['id']}: {task['title']}\n"
        f"{task['description']}\n"
        f"Target files: {', '.join(task['target_files'])}\n"
        f"{steer_block}"
        f"Contracts:\n{contract_slice}\n{assignment.get('subgraph_digest','')}\n"
        f"{blackboard_digest}\n"
        f"Handoff / interface updates:\n{task.get('handoff_digest') or ''}\n"
        f"QA failure to fix:\n{compact_err}\n"
        "If a live steering note appears in `.funguys_inbox.md`, follow it immediately.\n"
        "Problem Detection & Dispute Escalation Rules:\n"
        "- If you detect a contradiction between existing code and CONTRACTS.md, a `[LOCK]`/`[TRIPWIRE]` collision on the Blackboard, "
        "or a test/import error caused by a file outside your assigned `target_files`, DO NOT modify unassigned files or invent hacks.\n"
        "- Immediately emit `@FG:DISPUTE <file_or_task_id> <explanation>` in stdout so the Main Controller arbitrates.\n"
        "- If your context window is saturated or you need to pass WIP to another worker, emit `@FG:HANDOFF <target_agent_id_or_self> <handoff_notes>`.\n"
        "Optional 1-line C2 signals you may print in stdout: "
        "`@FG:FACT <key> <value>`, `@FG:CLAIM <file>`, `@FG:RELEASE <file>`, `@FG:TRIPWIRE <file> <reason>`, "
        "`@FG:DISPUTE <target> <reason>`, `@FG:HANDOFF <target> <notes>`, "
        "`@FG:TRIGGER <task_id> <agent_id>`, `@FG:STEER <agent_id> <msg>`.\n"
        "Read existing source before editing. Make focused changes. Do not read credentials or files outside this workspace. Do not claim success without implementing the task."
    )
```

### 2.3 Update `WorkerRuntime.heartbeat_loop` and `_perform_assignment` in `funguys/worker/runtime.py`
1. In `WorkerRuntime.heartbeat_loop`, trigger `self.cancelled.set()` when `mode in ('interrupt', 'handoff')`:
```python
                if any(p.get('mode') in ('interrupt', 'handoff') for p in prompts):
                    self.cancelled.set()
```
2. In `WorkerRuntime._perform_assignment`, when `self.cancelled.is_set()` is triggered, checkpoint WIP files via `POST /api/agents/{self.agent_id}/handoff` (falling back to `/quota` only if `/handoff` is unavailable) so the agent's context window is reset and WIP is saved **without** putting the agent into a 60-minute rate-limit cooldown:
```python
                if self.cancelled.is_set():
                    if any(changes.values()):
                        try:
                            self.request('POST', f'/api/agents/{self.agent_id}/handoff', {
                                'assignment_id': task['assignment_id'],
                                'handoff_notes': 'Interrupted for Controller promotion, arbitration, or live steering; WIP checkpointed.',
                                'wip_files': changes['files'],
                                'wip_binary_files': changes['binary_files'],
                                'deleted_files': changes['deleted_files'],
                            }, timeout=20)
                        except Exception:
                            pass
                    return
```
And apply the same `/handoff` call in the `except Exception as exc:` block when `self.cancelled.is_set()`.

### 2.4 Re-export `controller_arbitration_prompt` in `funguys/protocol.py`
Import `controller_arbitration_prompt` from `funguys.worker.runtime` and include it in `__all__` in `funguys/protocol.py`.

---

## STEP 3: IMPLEMENT FEATURES #9, #4, AND #2 IN `funguys/host/orchestrator.py`

### 3.1 Implement Feature #4: `handoff_agent_task` (On-Demand Context Window Reset & Handoff Selection)
Add `handoff_agent_task` to `FunGuysOrchestrator` in `funguys/host/orchestrator.py`:
```python
    async def handoff_agent_task(
        self,
        agent_id: str,
        target_agent_id: Optional[str] = None,
        handoff_notes: str = "Context window reset & baton pass handoff.",
        assignment_id: Optional[str] = None,
        wip_files: Optional[Dict[str, str]] = None,
        wip_binary_files: Optional[Dict[str, str]] = None,
        deleted_files: Optional[List[str]] = None,
        reset_window: bool = False,
        preserve_self_if_busy: bool = False,
    ) -> Dict[str, object]:
        """Reset an agent's active task context window, checkpoint WIP to Git, and hand off to a target (or idle) agent without cooldown."""
        handoff_task_id = None
        async with self._state_lock:
            agent = self.store.get_agent(agent_id)
            if not agent:
                raise ValueError("Unknown worker")
            if reset_window:
                agent.turns_used = 0
                agent.high_tier_turns_used = 0
                agent.used_pct = 0.0
                agent.high_tier_used_pct = 0.0
                agent.quota_window_started_at = utc_now_iso()
                agent.high_tier_window_started_at = utc_now_iso()
            agent.last_heartbeat = utc_now_iso()
            task = self.store.get_task(agent.current_task_id) if agent.current_task_id else None
            if task and task.status == TaskStatus.IN_PROGRESS:
                if assignment_id and assignment_id != task.assignment_id:
                    raise ConflictError("Handoff references a stale assignment")
                if wip_files or wip_binary_files or deleted_files:
                    task.candidate_revision = await asyncio.to_thread(
                        self.git.checkpoint,
                        task,
                        decode_files(wip_files or {}, wip_binary_files),
                        deleted_files or [],
                    )
                self._cancel_assignment(task)
                if agent.executor in ("remote", "mcp", "mcp_ide"):
                    self.store.enqueue_prompt(PromptQueueItem(
                        target_agent_id=agent.id,
                        sender_agent_id="controller",
                        mode="handoff",
                        prompt=f"Context reset / handoff triggered: {handoff_notes}",
                        priority=90,
                    ))
                digest = self.git.create_handoff_document(
                    task.id,
                    agent.id,
                    handoff_notes or "Context window reset; resume from checkpoint.",
                    self.graph.get_subgraph_digest(task.target_files),
                )
                task.handoff_digest = ((task.handoff_digest + "\n\n" + digest) if task.handoff_digest else digest).strip()
                if target_agent_id and target_agent_id != "auto":
                    task.preferred_agent_id = target_agent_id
                elif preserve_self_if_busy:
                    other_idle = [
                        a for a in self.store.list_agents()
                        if a.id != agent.id
                        and a.status == AgentStatus.IDLE
                        and a.current_task_id is None
                        and a.used_pct < a.soft_limit_pct
                    ]
                    if not other_idle:
                        task.preferred_agent_id = agent.id
                task.status = TaskStatus.HANDOFF_PENDING
                task.assigned_agent_id = None
                task.assignment_id = None
                self.store.save_task(task)
                handoff_task_id = task.id
            agent.current_task_id = None
            if agent.status == AgentStatus.WORKING:
                agent.status = AgentStatus.IDLE
            self.store.save_agent(agent)
        if handoff_task_id:
            await self.emit_event(
                agent_id,
                "context_reset_handoff",
                f"Context window reset & baton pass for {handoff_task_id} (target={target_agent_id or 'auto'})",
                {"task_id": handoff_task_id, "from_agent_id": agent_id, "target_agent_id": target_agent_id},
            )
        assignments = await self.schedule_ready_tasks()
        return {
            "agent": self.public_agent(self.store.get_agent(agent_id)),
            "handoff": handoff_task_id,
            "new_assignments": assignments,
        }
```

### 3.2 Implement Feature #9: `drop_task_for_controller` & Wire into Controller Promotion / Election
1. Add `drop_task_for_controller(self, agent_id: str, reason: str = ...)` in `FunGuysOrchestrator`:
```python
    async def drop_task_for_controller(
        self,
        agent_id: str,
        reason: str = "Called upon to act as Main Controller; dropping active coding task with WIP handoff.",
        schedule_immediately: bool = True,
    ) -> Optional[str]:
        """Feature #9: Cleanly drop and checkpoint any active coding task when an agent becomes or acts as Main Controller."""
        dropped_task_id = None
        async with self._state_lock:
            agent = self.store.get_agent(agent_id)
            if not agent or not agent.current_task_id:
                return None
            task = self.store.get_task(agent.current_task_id)
            if task and task.status == TaskStatus.IN_PROGRESS:
                if task.candidate_path and Path(task.candidate_path).exists():
                    try:
                        task.candidate_revision = await asyncio.to_thread(self.git.checkpoint, task, {}, [])
                    except Exception:
                        pass
                self._cancel_assignment(task)
                if agent.executor in ("remote", "mcp", "mcp_ide"):
                    self.store.enqueue_prompt(PromptQueueItem(
                        target_agent_id=agent.id,
                        sender_agent_id="controller",
                        mode="handoff",
                        prompt=reason,
                        priority=95,
                    ))
                digest = self.git.create_handoff_document(
                    task.id,
                    agent.id,
                    reason,
                    self.graph.get_subgraph_digest(task.target_files),
                )
                task.handoff_digest = ((task.handoff_digest + "\n\n" + digest) if task.handoff_digest else digest).strip()
                # If all other workers are busy, keep preferred_agent_id = agent.id so Controller resumes its own task once done!
                other_idle = [
                    a for a in self.store.list_agents()
                    if a.id != agent.id
                    and a.status == AgentStatus.IDLE
                    and a.current_task_id is None
                    and a.used_pct < a.soft_limit_pct
                ]
                if not other_idle:
                    task.preferred_agent_id = agent.id
                task.status = TaskStatus.HANDOFF_PENDING
                task.assigned_agent_id = None
                task.assignment_id = None
                self.store.save_task(task)
                dropped_task_id = task.id
            agent.current_task_id = None
            if agent.status == AgentStatus.WORKING:
                agent.status = AgentStatus.IDLE
            self.store.save_agent(agent)
        if dropped_task_id:
            await self.emit_event(
                agent_id,
                "controller_task_dropped",
                f"Controller {agent.display_name} dropped {dropped_task_id} (WIP saved) to assume Controller duties.",
                {"task_id": dropped_task_id, "agent_id": agent_id},
            )
            if schedule_immediately:
                await self.schedule_ready_tasks()
        return dropped_task_id
```
2. Update `designate_controller(self, agent_id: str)` in `funguys/host/orchestrator.py`:
   - If the newly designated controller has an active coding task (`target.current_task_id`), call `await self.drop_task_for_controller(agent_id, reason="Promoted to Main Controller Agent; WIP checkpointed for handoff.")` so it immediately drops its coding task, checkpoints WIP, hands off to any idle worker (or holds preference for itself if all workers are busy), and becomes `IDLE`.

### 3.3 Implement Feature #2: `arbitrate_dispute` & Wire `@FG:DISPUTE` and `@FG:HANDOFF` in `process_signals`
1. Add `arbitrate_dispute` to `FunGuysOrchestrator`:
```python
    async def arbitrate_dispute(
        self,
        sender_agent_id: str,
        target: str,
        reason: str,
        task_id: Optional[str] = None,
    ) -> Dict[str, object]:
        """Feature #2 & #9: Free the Main Controller (dropping its coding task if busy), run an arbitration turn, and steer workers."""
        from funguys.worker.adapters import run_text_agent
        from funguys.protocol import controller_arbitration_prompt

        async with self._state_lock:
            ctrl = self._ensure_controller_elected()
            if not ctrl:
                raise ConflictError("No controller agent available to arbitrate dispute")
            ctrl_id = ctrl.id
            was_coding_task_id = ctrl.current_task_id

        # Feature #9: If the Controller is currently coding a task, drop & checkpoint it before arbitrating
        dropped_task_id = None
        if was_coding_task_id:
            dropped_task_id = await self.drop_task_for_controller(
                ctrl_id,
                reason=f"Paused coding on {was_coding_task_id} to arbitrate swarm dispute on {target}: {reason}",
                schedule_immediately=False,
            )

        # Record the dispute on the Blackboard as a tripwire/fact so all agents have visibility
        self.store.put_blackboard(BlackboardEntry(
            key=f"dispute:{target}",
            kind="tripwire",
            value=f"[{sender_agent_id}] {reason}"[:1000],
            agent_id=sender_agent_id,
            task_id=task_id,
        ))

        async with self._state_lock:
            ctrl = self.store.get_agent(ctrl_id)
            self._consulting_agents.add(ctrl_id)
            project = self.store.get_project()
            tasks = self.store.list_tasks()
            tasks_summary = "\n".join(
                f"- {t.id} [{t.status.value}] (assigned={t.assigned_agent_id or 'none'}, files={','.join(t.target_files)}): {t.title}"
                for t in tasks
            )
            bb_digest = self._blackboard_digest_for_task(tasks[0]) if tasks else ""

        ruling_text = ""
        emitted_signals = []
        try:
            files = await asyncio.to_thread(bundle, self.git.workspace_path)
            excerpts = []
            for fname, content in files.get("files", {}).items():
                if target in fname or fname in target:
                    excerpts.append(f"FILE {fname}:\n{content[:1000]}")
            prompt = controller_arbitration_prompt(
                {"sender_agent_id": sender_agent_id, "target": target, "reason": reason},
                project.name,
                project.contracts_markdown,
                bb_digest,
                tasks_summary,
                "\n\n".join(excerpts[:3]),
            )
            try:
                ruling_text = await asyncio.to_thread(run_text_agent, ctrl.provider, prompt, role="planning")
            except Exception as exc:
                ruling_text = f"Controller arbitration recorded dispute on {target} ({exc}). @FG:STEER {sender_agent_id} Follow CONTRACTS.md strictly for {target}"
            parsed = parse_agent_signals(ctrl_id, ruling_text)
            if parsed:
                emitted_signals = await self.process_signals(
                    ctrl_id,
                    [{"op": s.op.value, "target": s.target, "payload": s.payload} for s in parsed],
                )
            # Always notify the disputing worker in its .funguys_inbox.md
            if sender_agent_id and self.store.get_agent(sender_agent_id):
                self.store.enqueue_prompt(PromptQueueItem(
                    target_agent_id=sender_agent_id,
                    sender_agent_id=ctrl_id,
                    mode="steer",
                    prompt=f"[Controller Arbitration Ruling on {target}]: {ruling_text[:600]}",
                    priority=85,
                ))
        finally:
            async with self._state_lock:
                self._consulting_agents.discard(ctrl_id)
                ctrl_after = self.store.get_agent(ctrl_id)
                if ctrl_after and not ctrl_after.current_task_id:
                    ctrl_after.status = AgentStatus.COOLDOWN if ctrl_after.cooldown_until else AgentStatus.IDLE
                    self.store.save_agent(ctrl_after)

        await self.emit_event(
            ctrl_id,
            "dispute_arbitrated",
            f"Controller arbitrated dispute from {sender_agent_id} on {target}.",
            {"sender_agent_id": sender_agent_id, "target": target, "dropped_task_id": dropped_task_id, "signals": emitted_signals},
        )
        # Re-schedule ready/handoff tasks: if all other workers were busy, Controller now re-claims its own dropped task!
        next_assignments = await self.schedule_ready_tasks()
        return {
            "controller_agent_id": ctrl_id,
            "dropped_task_id": dropped_task_id,
            "ruling": ruling_text,
            "signals_processed": emitted_signals,
            "next_assignments": next_assignments,
        }
```
2. In `process_signals(self, sender_agent_id, raw_signals)`:
   - Handle `op == SignalOp.DISPUTE.value`: queue dispute details into a post-lock list `disputes_to_arbitrate` and call `await self.arbitrate_dispute(sender_agent_id, target, payload)` after releasing `self._state_lock`.
   - Handle `op == SignalOp.HANDOFF.value`: parse optional `target_agent_id` from `payload.split()[0]` (if it matches a known `agent-...` ID) and call `await self.handoff_agent_task(agent_to_handoff, target_agent_id=..., handoff_notes=...)` after releasing `self._state_lock`.

---

## STEP 4: EXPOSE API ENDPOINTS & UPDATE ARCHITECT SELECTION (`funguys/host/app.py`)

1. In `authorize()` middleware (`funguys/host/app.py:250-262`), allow workers to call:
   - `/api/controller/arbitrate`
   - `f"/api/agents/{identity['agent_id']}/handoff"` (or any `/api/agents/{id}/handoff` when `is_controller` is True).
2. Add endpoints in `funguys/host/app.py`:
   - `POST /api/agents/{agent_id}/handoff`: calls `await orch.handoff_agent_task(agent_id, **req.model_dump())`
   - `POST /api/controller/arbitrate`: calls `await orch.arbitrate_dispute(**req.model_dump())`
3. Update `select_architect(agent_id)` in `funguys/host/app.py`:
   - If the selected local Controller agent is currently `WORKING` on a coding task (`selected.current_task_id is not None`), do not fail when starting a Grill-Me interview; allow `start_grill` / `compile_grill` to call `await orch.drop_task_for_controller(selected.id, reason="Controller needed for Lead Architect planning")`.

---

## STEP 5: ADD MCP IDE TOOLS (`funguys/mcp_bridge/server.py`)

1. Add `fg_dispute` and `fg_handoff` to `MCP_TOOLS` in `funguys/mcp_bridge/server.py`:
   - `fg_dispute`: `{"target": str, "reason": str, "task_id": Optional[str]}` $\to$ calls `POST /api/controller/arbitrate`
   - `fg_handoff`: `{"agent_id": Optional[str], "target_agent_id": Optional[str], "handoff_notes": Optional[str]}` $\to$ calls `POST /api/agents/{target}/handoff`
2. Handle both tool names in `MCPWorkerServer.handle_call_tool()`.

---

## STEP 6: VERIFICATION & AUTOMATED TESTS (`tests/test_c2_controller_and_mcp.py`)

Add tests in `tests/test_c2_controller_and_mcp.py` verifying:
1. **`test_feature_9_controller_drops_task_on_promotion_and_arbitration`**:
   - Register Agent A (Gemini, working on `task-1-contracts`) and Agent B (Claude).
   - Promote Agent A to Controller (`POST /api/agents/{id}/controller`) while it is working on `task-1-contracts` $\to$ verify `task-1-contracts` checkpoints WIP and transitions to `HANDOFF_PENDING` (or reassigns cleanly), and Agent A becomes `IDLE`.
2. **`test_feature_9_controller_resumes_own_task_when_all_workers_busy`**:
   - With Controller coding `task-1-contracts` and all other workers busy, trigger a worker dispute (`@FG:DISPUTE` / `POST /api/controller/arbitrate`).
   - Verify Controller drops `task-1-contracts`, arbitrates the dispute, emits steering signals to the worker's inbox, and **automatically re-claims `task-1-contracts`** afterwards because all other workers are busy.
3. **`test_feature_4_on_demand_context_window_reset_and_targeted_handoff`**:
   - Call `POST /api/agents/{agent_a_id}/handoff` with `target_agent_id=agent_b_id` and `wip_files={"backend/service.py": "# wip\n"}`.
   - Verify Agent A is reset to `IDLE` (NOT `COOLDOWN`), WIP is preserved in the candidate worktree, and Agent B receives the assignment with the Baton Pass `handoff_digest`.

Run the full test suite:
```powershell
uv run pytest -q
```
All 66+ tests must pass with zero regressions.
