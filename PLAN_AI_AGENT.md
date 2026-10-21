# Gaper Bingzoid (FunGuys) — AI-Executable Implementation Plan (`PLAN_AI_AGENT.md`)

> **DIRECTIVE FOR CODING AGENT**:
> Follow Steps 1 through 6 in exact sequential order.
> **Critical Invariants to Preserve**:
> 1. `funguys/worker/runtime.py` is served standalone via `GET /worker.py` to remote machines that do **not** have the `funguys` package installed. Therefore, `funguys/worker/runtime.py` MUST ONLY import Python standard-library modules at the top level. Define protocol/compression helpers inside `runtime.py` and re-export them in `funguys/protocol.py`.
> 2. Preserve existing test-facing symbols (`generate_task_code_artifact` in `adapters.py`, `simulate_demo` in `demo.py`, `public_destination` and `fetch_public_page` in `mcp_bridge/server.py`) so all 55 existing tests in `tests/` continue to pass without regression.
> 3. Keep `"gemini-cli"` as `tier=2` in `SUBSCRIPTION_PROFILES` so `test_plan_approval_gate_and_role_based_tier_routing` passes, while updating `select_best_agent_for_task` to fall back to Tier 2 when no Tier 1 agent is registered.

---

## STEP 1: CONFIG, MODELS & SQLITE PERSISTENCE (`config.py`, `models.py`, `db.py`)

### 1.1 Edit `funguys/config.py`
Update `SUBSCRIPTION_PROFILES` to calibrate turn windows to Gemini Pro / Claude Pro / ChatGPT Plus limits while preserving existing profile keys:
```python
SUBSCRIPTION_PROFILES: Dict[str, SubscriptionProfile] = {
    "claude-max": SubscriptionProfile(
        provider="claude",
        cli_command="claude",
        display_name="Claude Code (Max 5x/20x)",
        tier=1,
        window_hours=5.0,
        estimated_turns_per_window=225,
        soft_handoff_threshold_pct=80.0,
        strengths=["controller", "architecture", "complex-backend", "root-cause-debugging", "contracts"],
    ),
    "claude-pro": SubscriptionProfile(
        provider="claude",
        cli_command="claude",
        display_name="Claude Code (Pro)",
        tier=1,
        window_hours=5.0,
        estimated_turns_per_window=45,
        soft_handoff_threshold_pct=80.0,
        strengths=["controller", "architecture", "complex-backend", "root-cause-debugging"],
    ),
    "codex-pro": SubscriptionProfile(
        provider="codex",
        cli_command="codex",
        display_name="OpenAI Codex / ChatGPT CLI (Plus/Pro)",
        tier=1,
        window_hours=5.0,
        estimated_turns_per_window=60,
        soft_handoff_threshold_pct=80.0,
        strengths=["controller", "algorithms", "refactoring", "backend", "api-integration"],
    ),
    "chatgpt-plus": SubscriptionProfile(
        provider="codex",
        cli_command="codex",
        display_name="ChatGPT (Plus/Pro via Codex/MCP)",
        tier=1,
        window_hours=5.0,
        estimated_turns_per_window=60,
        soft_handoff_threshold_pct=80.0,
        strengths=["controller", "algorithms", "refactoring", "backend"],
    ),
    "gemini-pro": SubscriptionProfile(
        provider="gemini",
        cli_command="gemini",
        display_name="Google Gemini Pro (Controller/Architect)",
        tier=1,
        window_hours=5.0,
        estimated_turns_per_window=60,
        soft_handoff_threshold_pct=80.0,
        strengths=["controller", "architecture", "fullstack", "debugging"],
    ),
    "gemini-cli": SubscriptionProfile(
        provider="gemini",
        cli_command="gemini",
        display_name="Google Gemini CLI (Pro/Flash)",
        tier=2,  # Must stay tier=2 for test_plan_approval_gate_and_role_based_tier_routing
        window_hours=5.0,
        estimated_turns_per_window=60,
        soft_handoff_threshold_pct=85.0,
        strengths=["frontend-ui", "unit-tests", "playwright-e2e", "boilerplate", "docs"],
    ),
    "mcp-ide": SubscriptionProfile(
        provider="mcp",
        cli_command="mcp",
        display_name="IDE / Desktop Agent (Cursor, Windsurf, Antigravity, Claude/ChatGPT App)",
        tier=1,
        window_hours=5.0,
        estimated_turns_per_window=60,
        soft_handoff_threshold_pct=80.0,
        strengths=["interactive-coding", "architecture", "ui", "refactoring"],
    ),
    "api-fallback": SubscriptionProfile(
        provider="api",
        cli_command="api",
        display_name="Fallback API / Local Model",
        tier=2,
        window_hours=24.0,
        estimated_turns_per_window=500,
        soft_handoff_threshold_pct=90.0,
        strengths=["tests", "boilerplate", "formatting", "docs"],
    ),
}
```

### 1.2 Edit `funguys/models.py`
1. Add coordination enums and models:
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


class AgentSignal(BaseModel):
    op: SignalOp
    source_agent_id: str
    target: Optional[str] = None
    task_id: Optional[str] = None
    payload: str = Field(max_length=1000)


class PromptQueueItem(BaseModel):
    id: str = Field(default_factory=lambda: "prm-" + uuid.uuid4().hex[:10])
    project_id: str
    from_agent_id: str
    target_agent_id: str
    task_id: Optional[str] = None
    op: SignalOp = SignalOp.STEER
    prompt: str = Field(max_length=4000)
    priority: int = Field(default=1, ge=1, le=5)
    status: Literal["queued", "delivered", "done"] = "queued"
    created_at: str = Field(default_factory=utc_now_iso)


class BlackboardEntry(BaseModel):
    id: str = Field(default_factory=lambda: "bb-" + uuid.uuid4().hex[:8])
    project_id: str
    agent_id: str
    task_id: Optional[str] = None
    kind: Literal["FACT", "TRIPWIRE", "LOCK"] = "FACT"
    scope: str = "*"
    key: str = Field(max_length=120)
    value: str = Field(max_length=500)
    created_at: str = Field(default_factory=utc_now_iso)
```
2. Add optional fields to `TaskItem`:
   - `held: bool = False`
   - `hold_reason: Optional[str] = None`
3. Update `AgentNode`:
   - Change `executor: Literal['remote', 'local', 'mcp_ide'] = 'remote'`
   - Add `is_controller: bool = False`
   - Add `estimated_tokens_used: int = 0`
4. Update `RegisterAgentRequest`:
   - Change `executor: Literal['remote', 'local', 'mcp_ide'] = 'remote'`

### 1.3 Edit `funguys/db.py`
1. In `StateStore._init_schema()`, add:
```sql
CREATE TABLE IF NOT EXISTS prompt_queue (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    target_agent_id TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS blackboard (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    scope TEXT NOT NULL,
    key TEXT NOT NULL,
    payload TEXT NOT NULL
);
```
2. Add methods to `StateStore`:
   - `enqueue_prompt(self, item: PromptQueueItem) -> None`
   - `pop_pending_prompts(self, agent_id: str, tier: int, project_id: str) -> List[PromptQueueItem]`:
     - Selects rows from `prompt_queue` where `project_id = ? AND status = 'queued' AND target_agent_id IN (?, ?, '*')` (matching `agent_id`, `f'role:tier{tier}'`, or `'*'`), ordered by `created_at ASC`.
     - Updates their `status = 'delivered'` and `payload` in the same transaction, returning `[PromptQueueItem.model_validate_json(r['payload']) ...]`.
   - `list_prompts(self, project_id: str) -> List[PromptQueueItem]`
   - `put_blackboard(self, entry: BlackboardEntry) -> None`:
     - Deletes existing row with matching `(project_id, kind, key)` and inserts `entry`.
   - `remove_blackboard_locks(self, project_id: str, agent_id: str, keys: Optional[List[str]] = None) -> None`
   - `list_blackboard(self, project_id: str, scopes: Optional[List[str]] = None) -> List[BlackboardEntry]`

---

## STEP 2: STANDALONE PROTOCOL, TOKEN SLICING & MAC/WIN FIXES (`runtime.py` & `protocol.py`)

### 2.1 Edit `funguys/worker/runtime.py`
Remember: `runtime.py` MUST remain 100% self-contained with only Python standard library imports.

1. **Add Cross-Platform CLI Path Resolution (`resolve_cli_binary`)**:
   ```python
   def resolve_cli_binary(provider: str) -> str | None:
       names = [provider, 'agy'] if provider == 'gemini' else [provider]
       for name in names:
           found = shutil.which(name)
           if found:
               return found
       if os.name != 'nt':
           home = Path.home()
           extra_dirs = [
               home / '.local' / 'bin',
               Path('/opt/homebrew/bin'),
               Path('/usr/local/bin'),
               home / '.npm-global' / 'bin',
           ]
           nvm_dir = home / '.nvm' / 'versions' / 'node'
           if nvm_dir.is_dir():
               extra_dirs.extend(sorted(nvm_dir.glob('*/bin'), reverse=True))
           path_env = os.environ.get('PATH', '')
           search_path = os.pathsep.join([*(str(d) for d in extra_dirs if d.is_dir()), path_env])
           for name in names:
               found = shutil.which(name, path=search_path)
               if found:
                   return found
       return None
   ```
   Update `cli_command(provider, root, prompt)` to call `binary = resolve_cli_binary(provider)` instead of `shutil.which`.
   *(Note: Keep `shutil.which(provider)` called first inside `resolve_cli_binary` so `test_no_provider_substitution_or_generic_artifact`, which monkeypatches `runtime.shutil.which`, continues to work identically!)*

2. **Fix macOS Process Group Termination (`stop_process`)**:
   ```python
   def stop_process(proc):
       if proc.poll() is not None:
           return
       if os.name == 'nt':
           subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'], capture_output=True, timeout=10)
       else:
           try:
               os.killpg(proc.pid, signal.SIGKILL)
           except (ProcessLookupError, PermissionError, OSError):
               try:
                   proc.kill()
               except OSError:
                   pass
       try:
           proc.wait(timeout=10)
       except subprocess.TimeoutExpired:
           pass
   ```

3. **Add Compact Signal Parser, Error Compressor, and Contract Slicer inside `runtime.py`**:
   ```python
   SIGNAL_PATTERN = re.compile(
       r'^@FG:(CLAIM|RELEASE|FACT|TRIPWIRE|TRIGGER|STEER|HOLD|GO|VETO)'
       r'(?:\s+TARGET=([^\s]+))?(?:\s+TASK=([^\s]+))?\s*::\s*(.+)$',
       re.MULTILINE
   )

   def parse_raw_signals(text: str, source_agent_id: str = '') -> list[dict]:
       signals = []
       for match in SIGNAL_PATTERN.finditer(text or ''):
           op, target, task_id, payload = match.groups()
           signals.append({
               'op': op,
               'source_agent_id': source_agent_id,
               'target': target,
               'task_id': task_id,
               'payload': payload.strip()[:1000],
           })
       return signals

   def compress_error_log(raw_log: str, max_chars: int = 1200) -> str:
       if not raw_log:
           return ''
       clean = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', raw_log)
       if len(clean) <= max_chars:
           return clean
       lines = [line for line in clean.splitlines() if line.strip()]
       key_lines = [
           line for line in lines
           if any(k in line for k in ('FAILED', 'ERROR', 'Error', 'Exception', 'AssertionError', 'SyntaxError', 'File "', 'E   '))
       ]
       tail = lines[-12:]
       combined = '\n'.join([*key_lines[:15], '...', *tail])
       return combined[-max_chars:]

   def slice_contract_for_task(contracts_markdown: str, task: dict, max_chars: int = 1200) -> str:
       if not contracts_markdown or len(contracts_markdown) <= max_chars:
           return contracts_markdown or ''
       sections = re.split(r'(?m)(?=^#{1,3}\s+)', contracts_markdown)
       preamble = sections[0][:350]
       keywords = {
           task.get('id', '').lower(),
           *(p.lower() for p in task.get('target_files', [])),
           *(Path(p).stem.lower() for p in task.get('target_files', [])),
           *(s.lower() for s in task.get('exported_symbols', [])),
       } - {''}
       matched = [s for s in sections[1:] if any(k in s.lower() for k in keywords)]
       result = (preamble + '\n' + '\n'.join(matched)).strip() if matched else contracts_markdown[:max_chars]
       return result[:max_chars]
   ```

4. **Upgrade `task_prompt(assignment)` in `runtime.py` to Use Compact Slicing & Blackboard/Inbox**:
   ```python
   def task_prompt(assignment):
       task = assignment['task']
       contract_slice = assignment.get('contract_slice') or slice_contract_for_task(assignment.get('contracts_markdown', ''), task)
       err_digest = compress_error_log(task.get('last_error') or '')
       bb_items = assignment.get('blackboard') or []
       bb_text = '\n'.join(f"- [{b['kind']}] {b['scope']}:{b['key']}={b['value']}" for b in bb_items[:15])
       inbox_items = assignment.get('inbox') or []
       inbox_text = '\n'.join(f"- [{m.get('op','STEER')} from {m.get('from_agent_id','controller')}]: {m.get('prompt','')}" for m in inbox_items)
       parts = [
           f"Implement {task['id']}: {task['title']}",
           task['description'],
           f"Target files: {', '.join(task['target_files'])}",
           f"Contracts:\n{contract_slice}",
           assignment.get('subgraph_digest', ''),
       ]
       if bb_text:
           parts.append(f"Shared Blackboard (Facts/Locks/Tripwires):\n{bb_text}")
       if inbox_text:
           parts.append(f"Controller / Peer Steering Inbox:\n{inbox_text}")
       if task.get('handoff_digest'):
           parts.append(f"Handoff / interface updates:\n{task['handoff_digest'][-1000:]}")
       if err_digest:
           parts.append(f"QA failure to fix:\n{err_digest}")
       parts.append(
           "Read existing source before editing. Make focused changes. Do not read credentials or files outside this workspace. "
           "If steering notes arrive in .funguys_inbox.md, apply them. "
           "To emit signals to the Controller or other agents, append lines matching:\n"
           "@FG:FACT :: scope:key=value\n"
           "@FG:TRIPWIRE :: scope:warning\n"
           "@FG:TRIGGER TARGET=<agent_id|role:tier1|role:tier2> TASK=<task_id> :: <prompt>\n"
           "@FG:STEER TARGET=<agent_id> TASK=<task_id> :: <instruction>"
       )
       return '\n'.join(p for p in parts if p)
   ```

5. **Fix `WorkerRuntime.heartbeat_loop` and `_perform_assignment` in `runtime.py` (Bug #2 Fix)**:
   - Add `self.active_scratch: Path | None = None` to `WorkerRuntime.__init__`.
   - In `heartbeat_loop`:
     - If `result.get('inbox')` is non-empty and `self.active_scratch` exists: append incoming prompts to `self.active_scratch / '.funguys_inbox.md'`.
     - Only set `self.cancelled.set()` if `current and not result.get('active')` (do **not** cancel when `handoff_digest` changes mid-run; instead write updated `handoff_digest` to `self.active_scratch / '.funguys_inbox.md'`!).
   - In `_perform_assignment`:
     - Set `self.active_scratch = root` inside the `with tempfile.TemporaryDirectory(...)` block and reset `self.active_scratch = None` in `finally`.
     - Exclude `.funguys_inbox.md` in `EXCLUDED` (`funguys/files.py` and `funguys/worker/runtime.py`) so inbox files are never committed to the target project!
     - If `self.cancelled.is_set()` after `execute_cli`: if `any(changes.values())`, call `self.deliver(f'/api/agents/{self.agent_id}/quota', {'used_pct': float(self.config.get('used_pct', 0)), 'turns_delta': 1, 'assignment_id': task['assignment_id'], 'handoff_notes': 'Assignment interrupted; WIP saved.', 'wip_files': changes['files'], 'wip_binary_files': changes['binary_files'], 'deleted_files': changes['deleted_files']})` before returning.
     - Parse `signals = parse_raw_signals(output, self.agent_id)` and include `'signals': signals` in the `/api/tasks/{id}/submit` payload.

### 2.2 Create `funguys/protocol.py`
Re-export `SIGNAL_PATTERN`, `parse_raw_signals`, `compress_error_log`, `slice_contract_for_task`, and provide typed `parse_agent_signals(text: str, source_agent_id: str) -> List[AgentSignal]`.

---

## STEP 3: CROSS-PLATFORM EXECUTION, KEEP-AWAKE & QA FIXES (`execution.py`, `qa_pipeline.py`, `tunnel.py`, `adapters.py`, `files.py`)

### 3.1 Edit `funguys/files.py`
1. Add `'.funguys_inbox.md'` to `EXCLUDED` in both `funguys/files.py` and `funguys/worker/runtime.py`.
2. Line 110: Change `'Workspace file exceeds the 2 MB worker transfer limit: '` to `f'Workspace file exceeds the {MAX_FILE // 1_000_000} MB worker transfer limit: '`.

### 3.2 Edit `funguys/host/execution.py`
1. In `SandboxRunner.run`: expand environment variable allowlist so macOS (`HOME`, `TMPDIR`), Linux (`XDG_CACHE_HOME`), and Windows (`USERPROFILE`, `LOCALAPPDATA`, `APPDATA`) tools work:
   ```python
   ALLOWED_ENV = {
       'PATH', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'TEMP', 'TMP', 'TMPDIR', 'PATHEXT', 'LANG',
       'HOME', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'XDG_CACHE_HOME', 'XDG_CONFIG_HOME',
       'PLAYWRIGHT_BROWSERS_PATH', 'SSL_CERT_FILE', 'NODE_PATH',
   }
   ```
2. Add `LaptopKeepAwake` class (uses `ctypes.windll.kernel32.SetThreadExecutionState(0x80000001)` on Windows and `caffeinate -di -w <pid>` on macOS).

### 3.3 Edit `funguys/host/qa_pipeline.py`
In `_run_playwright_check(self, task, attempt, live_preview_url)`:
- If `not live_preview_url and not task.require_browser`:
  - Check if any file in `task.target_files` ends with `('.html', '.htm', '.js', '.jsx', '.ts', '.tsx', '.css')`.
  - If not, immediately return `(True, 'SKIPPED: no browser target for this task', None)` without scanning the workspace or booting Chromium.

### 3.4 Edit `funguys/worker/adapters.py`
In `run_text_agent(provider, prompt, timeout=90)`:
1. Fix truncation: if `len(prompt) > 8000`, keep the first `2000` chars (system context) and last `4500` chars (recent history + question + output schema instructions).
2. Fix Claude `--tools` flag:
   ```python
   elif provider == 'claude':
       if '--tools' in command:
           idx = command.index('--tools')
           del command[idx:idx + 2]
   ```

### 3.5 Edit `funguys/host/tunnel.py`
In `get_lan_ip()`: add a standard-library fallback using `subprocess.run(['ifconfig' if os.name != 'nt' else 'ipconfig'], ...)` with regex `r'\b(100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3})\b'` to detect Tailscale/ZeroTier IPs on macOS and Windows even when `psutil` is not installed.

---

## STEP 4: SERVERLESS GITHUB `fg-c2` RENDEZVOUS (`funguys/host/git_c2.py`)

Create `funguys/host/git_c2.py`:
- Implements `GitC2Sync(git_manager: GitWorkspaceManager)`:
  - `sync_to_github(beacon_payload: dict) -> bool`:
    - Checks if `origin` remote exists via `git remote`. If not, returns `False`.
    - Serializes `beacon_payload` (`host_url`, `public_url`, `lan_ip`, `updated_at`, `blackboard`, `tasks`) to JSON bytes.
    - Uses `git hash-object -w --stdin`, `git mktree`, `git commit-tree`, and `git update-ref refs/heads/fg-c2 <commit>` (0 working-tree impact), then runs `git push -f origin refs/heads/fg-c2:refs/heads/fg-c2` with a 15-second cooldown.
  - Also add `discover_host_from_git(git_remote_url: str) -> Optional[str]` in `funguys/worker/runtime.py` so workers can recover the Host's latest tunnel URL if the Host laptop restarts or changes networks.

---

## STEP 5: MAIN CONTROLLER AGENT & ORCHESTRATOR REWORK (`funguys/host/orchestrator.py`)

### 5.1 Tier Fallback in `select_best_agent_for_task`
Update `select_best_agent_for_task(self, task, available=None)`:
```python
def select_best_agent_for_task(self, task, available=None):
    pool = [
        a for a in (available if available is not None else self.store.list_agents())
        if a.status == AgentStatus.IDLE
        and a.id not in self._consulting_agents
        and a.current_task_id is None
        and a.used_pct < a.soft_limit_pct
    ]
    if not pool:
        return None
    tier_matched = [a for a in pool if task.complexity_tier != 1 or a.tier == 1]
    candidates = tier_matched if tier_matched else pool
    return min(candidates, key=lambda a: (a.tier != task.complexity_tier, a.used_pct, a.id))
```
*(When both `claude-max` (Tier 1) and `gemini-cli` (Tier 2) are connected, `tier_matched` is non-empty so `claude-max` is selected, passing `test_plan_approval_gate_and_role_based_tier_routing`. When ONLY `gemini-cli` is connected, it falls back to `pool` instead of deadlocking!)*

### 5.2 Add Signal Processing, Blackboard, and Prompt Queue Methods
Add to `FunGuysOrchestrator`:
1. **`async def designate_controller(self, agent_id: str) -> AgentNode`**:
   - Sets `is_controller = (a.id == agent_id)` across all registered agents and updates `project.lead_architect_agent_id = agent_id`.
2. **`async def process_signals(self, source_agent_id: str, raw_signals: list[dict]) -> list[dict]`**:
   - Validates each signal as `AgentSignal`.
   - **`CLAIM`**: Checks `store.list_blackboard(project.id)` for `LOCK` conflicts on requested files. If locked by another agent, enqueues a `HOLD` prompt to `source_agent_id`. Otherwise writes `LOCK` entries.
   - **`RELEASE`**: Calls `store.remove_blackboard_locks(project.id, source_agent_id, files)`.
   - **`FACT` / `TRIPWIRE`**: Parses `scope:key=value` (or `key=value` with `scope='*'`) and calls `store.put_blackboard(...)`.
   - **`HOLD` / `GO`**: Sets `task.held = (op == 'HOLD')`, `task.hold_reason = sig.payload`, saves task, and triggers `schedule_ready_tasks()` on `GO`.
   - **`STEER` / `TRIGGER`**:
     - Enqueues `PromptQueueItem` via `store.enqueue_prompt(...)`.
     - If `op == SignalOp.TRIGGER` and `sig.task_id`: if task is `PENDING` or `HANDOFF_PENDING` and dependencies are met, marks `task.status = TaskStatus.READY` and calls `schedule_ready_tasks()`.
   - **`VETO`**: Marks target task `HANDOFF_PENDING`, appends `[VETO by {source_agent_id}]: {sig.payload}` to `task.handoff_digest`, and logs event.
3. **Update `assignment_payload(self, task, agent)`**:
   - Include:
     - `'contract_slice': slice_contract_for_task(project.contracts_markdown, task.model_dump())`
     - `'blackboard': [b.model_dump() for b in self.store.list_blackboard(project.id, task.target_files)]`
     - `'inbox': [p.model_dump() for p in self.store.pop_pending_prompts(agent.id, agent.tier, project.id)]`
4. **Update `heartbeat(self, agent_id, assignment_id=None, signals=None)`**:
   - If `signals`: call `await self.process_signals(agent_id, signals)`.
   - Pop pending prompts for `agent_id`: `inbox = [p.model_dump() for p in self.store.pop_pending_prompts(agent.id, agent.tier, project.id)]`.
   - Return `{'active': active, 'assignment': self.assigned_work(agent_id), 'inbox': inbox}`.
5. **Update `submit_task_work`**:
   - Accept optional `signals: list[dict] | None = None`.
   - If `signals`: call `await self.process_signals(agent_id or 'human', signals)`.
   - Automatically release file locks held by `agent_id` for `task_id` on completion.
6. **Update `ask_project_question`**:
   - When `is_btw` or targeting a remote/working agent: always enqueue a `PromptQueueItem` into `store.enqueue_prompt(...)` so the live worker receives it via heartbeat inbox.
   - Wrap `await asyncio.to_thread(run_text_agent, selected.provider, prompt)` in `try ... except Exception:`: if the host does not have the remote worker's CLI installed locally (or trusted execution is off), return `f"Queued steering directive to {selected.display_name} ({selected.machine_name}) inbox."` instead of raising an error. (If `run_text_agent` is monkeypatched in tests, it executes and returns normally).

---

## STEP 6: MCP IDE SERVER, API ENDPOINTS & DASHBOARD (`mcp_bridge/server.py`, `app.py`, `cli.py`, `dashboard.html`, `dashboard.js`)

### 6.1 Edit `funguys/mcp_bridge/server.py`
Keep `public_destination`, `PinnedHTTPSConnection`, `fetch_public_page`, `fetch_docs_markdown`, and `web_search` intact so existing tests pass.
Append `MCPWorkerServer`:
- Standard-library JSON-RPC 2.0 loop over `sys.stdin` / `sys.stdout`.
- Connects to Host using `WorkerRuntime` methods (`register`, `request`, `deliver`).
- Implements MCP tools:
  - `fg_listen(timeout_sec: int = 300)`: Polls `/api/worker/assignment` and `/api/agents/{id}/heartbeat` every 3s until an assignment or `inbox` message arrives; returns compact prompt built with `task_prompt()`.
  - `fg_signal(op: str, payload: str, target: str = "", task_id: str = "")`: Sends signal to `POST /api/controller/signal`.
  - `fg_check_inbox()`: Calls `/api/agents/{id}/heartbeat` and returns any pending `inbox` prompts and `blackboard` entries.
  - `fg_submit(task_id: str, summary: str = "Completed via MCP")`: Collects changed files in workspace and submits to `/api/tasks/{task_id}/submit`.

### 6.2 Edit `funguys/cli.py`
Add `mcp-worker` command:
```python
@app.command('mcp-worker')
def mcp_worker_command(
    host_url: str = typer.Option('http://127.0.0.1:8420', '--host'),
    token: str = typer.Option('', '--token', envvar='FUNGUYS_WORKER_TOKEN'),
    name: str = typer.Option('IDE-Agent', '--name'),
    codename: str = typer.Option('ide-mcp', '--codename'),
    workspace: Path = typer.Option(Path('.'), '--workspace'),
):
    """Run a stdio MCP server so Cursor, Windsurf, Antigravity, Claude Desktop, or ChatGPT App can join as a worker."""
    from funguys.mcp_bridge.server import run_mcp_stdio_worker
    run_mcp_stdio_worker(host_url=host_url, token=token, name=name, codename=codename, workspace=workspace.resolve())
```

### 6.3 Edit `funguys/host/app.py`
1. **Update `SubmitTaskRequest` and `HeartbeatRequest`**:
   - Add `signals: list[dict] = Field(default_factory=list)` to both `SubmitTaskRequest` and `HeartbeatRequest`.
2. **Add Request Models**:
   - `SignalBatchRequest(BaseModel)`: `signals: list[dict] = Field(default_factory=list, max_length=50)`
   - `SteerAgentRequest(BaseModel)`: `prompt: str = Field(min_length=1, max_length=4000)`, `op: Literal['STEER', 'TRIGGER', 'HOLD', 'GO'] = 'STEER'`, `task_id: Optional[str] = None`
3. **Update `authorize` Middleware (Line 228)**:
   - Allow `identity['role'] == 'worker'` to access `path in ('/api/worker/assignment', '/api/workspace/snapshot', '/api/controller/signal', '/api/blackboard')`.
4. **Add Endpoints**:
   - `POST /api/controller/signal`: Calls `orch.process_signals(agent_id, req.signals)`.
   - `POST /api/agents/{agent_id}/steer`: Enqueues prompt via `orch.steer_or_trigger_agent(agent_id, req.prompt, req.op, req.task_id)` and triggers `sync()`.
   - `POST /api/agents/{agent_id}/controller`: Calls `orch.designate_controller(agent_id)` and triggers `sync()`.
   - `GET /api/blackboard`: Returns `{'blackboard': [b.model_dump() for b in store.list_blackboard(store.get_project().id)], 'prompt_queue': [p.model_dump() for p in store.list_prompts(store.get_project().id)]}`.
5. **Update `state()`**:
   - Add `'blackboard': [b.model_dump() for b in store.list_blackboard(project.id)]` and `'prompt_queue': [p.model_dump() for p in store.list_prompts(project.id)]`.
6. **Fix `restart_host()` (Line 273) & `BodyLimit` (Line 103)**:
   - In `restart_host()`: preserve current `os.environ` without forcing `FUNGUYS_TRUSTED_EXECUTION = '1'`, and pass `['--host', bind_host]`.
   - In `BodyLimit`: change `'Request exceeds 30 MB limit'` to `f'Request exceeds {limit // 1_000_000} MB limit'`.
7. **Integrate `LaptopKeepAwake`**:
   - Start `LaptopKeepAwake()` inside `lifespan(app)` and stop it in the `finally:` block.

### 6.4 Edit `funguys/host/dashboard.html` & `funguys/host/dashboard.js`
- Keep all existing element IDs (`#login`, `#app`, `#connection`, `#zero-workers-container`, `#invite-form`, `#contracts`, `#nav-worker-count`, `#nav-btn-workers`, `#screen-workers`, `#agents`, `#btn-back-to-project`, `#screen-project`, `#approve`, `#project-status`, `#pause`, `#sign-out`) untouched so `tests/test_browser_dashboard.py` passes 100%.
- In `renderAgents(agents)` (`dashboard.js`): add **"Set Controller"** and **"Steer / Queue Prompt"** buttons on each agent card.
- In `dashboard.html`: add a **Shared Blackboard & Prompt Queue** display section (`#blackboard-list` and `#queue-list`) rendered in `render(data)`.
- In `invite-result` (`dashboard.html` & `dashboard.js`): add a read-only textarea `#mcp-command` showing the IDE MCP JSON config (`funguys mcp-worker --host ... --token ...`) so IDE agents (Cursor, Windsurf, Antigravity, Claude Desktop) can be connected alongside Windows/Mac CLI workers.

---

## STEP 7: FINAL VERIFICATION COMMANDS

Run the full test suite to verify zero regressions and validate the new coordination features:
```powershell
uv run pytest -q
```
