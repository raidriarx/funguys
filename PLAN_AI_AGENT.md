# Gaper Bingzoid (FunGuys) — AI-Executable Implementation Plan (`PLAN_AI_AGENT.md`)

> **DIRECTIVE FOR CODING AGENT**:
> Follow Steps 1 through 7 in exact sequential order.
> **Critical Invariants to Preserve**:
> 1. `funguys/worker/runtime.py` is served standalone via `GET /worker.py` to remote machines that do **not** have the `funguys` package installed. Therefore, `funguys/worker/runtime.py` MUST ONLY import Python standard-library modules at the top level. Define model-tier resolution, protocol parsing, and compression helpers inside `runtime.py` and re-export them in `funguys/protocol.py`.
> 2. Preserve existing test-facing symbols (`generate_task_code_artifact` in `adapters.py`, `simulate_demo` in `demo.py`, `public_destination` and `fetch_public_page` in `mcp_bridge/server.py`, and legacy profile keys `'claude-max'`, `'claude-pro'`, `'codex-pro'`, `'gemini-cli'`) so all 55 existing tests in `tests/` continue to pass without regression.
> 3. Enforce the **Strict Provider Model & Effort Routing Matrix** and **Separate Usage-Bucket Conservation** defined in Step 1 & Step 2 below.

---

## STEP 1: MODEL & EFFORT ROUTING MATRIX, CONFIG & MODELS (`config.py`, `models.py`, `db.py`)

### 1.1 Exact Model & Reasoning-Effort Policy Across Providers
Every task or consultation is classified into one of 4 execution roles:
- `"planning"` — Architect interview, DAG compilation, and Main Controller arbitration (**NO file editing**).
- `"hard_coding"` — `complexity_tier == 1` and not a testing task.
- `"easy_coding"` — `complexity_tier == 2` and not a testing task.
- `"testing"` — `require_tests == True` or all `target_files` are test files (`test_*` / `*_test.*` / `tests/*`).

Each provider maps these 4 roles to exact `(model, effort)` pairs and **never drops below the hard floor**:

| Provider | `"planning"` (No Coding) | `"hard_coding"` (`tier=1`) | `"easy_coding"` (`tier=2`) | `"testing"` (Tests/QA) | Hard Floor (Never Lower) |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **`gemini`** (`gemini` / `agy`) | `("argon", "medium")` | `("argon", "low")` *(fallback: `("flash", "high")`)* | `("argon", "low")` *(shifts to `("flash", "high")` when `argon_used_pct >= 60.0`)* | `("flash", "high")` | `("flash", "high")` |
| **`codex`** (ChatGPT / OpenAI) | `("astra", "medium")` | `("sol", "high")` | `("sol", "high")` | `("sol", "high")` | `("sol", "high")` |
| **`claude`** (Anthropic) | `("opus", "high")` | `("opus", "high")` | `("opus", "medium")` | `("opus", "medium")` | `("opus", "medium")` |

**Separate Usage Rate Conservation Rules**:
1. **Google (`gemini` / `agy`)**:
   - `argon` (`argon-medium` and `argon-low`) runs on a separate usage rate from `flash` (`flash-high`).
   - `argon-medium` is **never** used for coding.
   - Track `high_tier_used_pct` (Argon bucket) separately from `used_pct` (Flash / general bucket) on `AgentNode`.
   - When `high_tier_used_pct >= 60.0`, `"easy_coding"` automatically uses `("flash", "high")` to save Argon.
   - When `high_tier_used_pct >= 85.0` (or when `argon-low` hits a rate limit), **all** coding tasks automatically fall back to `("flash", "high")`, reserving the last 15% of Argon quota exclusively for `("argon", "medium")` planning/controller turns.
2. **ChatGPT / OpenAI (`codex`)**:
   - `("astra", "medium")` is strictly reserved for `"planning"` / Controller turns (tracked in `high_tier_used_pct`).
   - `("sol", "high")` executes 100% of `"hard_coding"`, `"easy_coding"`, and `"testing"` (tracked in `used_pct`), so Astra never runs out during coding.
3. **Claude (`claude`)**:
   - `("opus", "high")` executes `"planning"` and `"hard_coding"`.
   - `("opus", "medium")` executes `"easy_coding"` and `"testing"`.

### 1.2 Edit `funguys/config.py`
Update `SUBSCRIPTION_PROFILES` to include both the new tiered profiles and the legacy profile keys required by tests:
```python
@dataclass(frozen=True)
class SubscriptionProfile:
    provider: str
    cli_command: str
    display_name: str
    tier: int  # 1 = High-Reasoning/Architect/Hard-Coding, 2 = Builder/Easy-Coding/Testing
    window_hours: float
    estimated_turns_per_window: int
    soft_handoff_threshold_pct: float = 80.0
    high_tier_turns_per_window: int = 25  # Separate rate limit bucket for Argon / Astra / Opus-High
    strengths: List[str] = ()


MODEL_ROUTING_POLICY: Dict[str, Dict[str, tuple[str, str]]] = {
    "gemini": {
        "planning": ("argon", "medium"),
        "hard_coding": ("argon", "low"),
        "easy_coding": ("argon", "low"),
        "testing": ("flash", "high"),
        "fallback": ("flash", "high"),
    },
    "codex": {
        "planning": ("astra", "medium"),
        "hard_coding": ("sol", "high"),
        "easy_coding": ("sol", "high"),
        "testing": ("sol", "high"),
        "fallback": ("sol", "high"),
    },
    "claude": {
        "planning": ("opus", "high"),
        "hard_coding": ("opus", "high"),
        "easy_coding": ("opus", "medium"),
        "testing": ("opus", "medium"),
        "fallback": ("opus", "medium"),
    },
}
```
Keep keys `"claude-max"`, `"claude-pro"`, `"codex-pro"`, `"gemini-cli"` (with `"gemini-cli"` keeping `tier=2` so `test_plan_approval_gate_and_role_based_tier_routing` passes), and add:
- `"gemini-argon-flash"`: `provider="gemini"`, `display_name="Gemini (Argon Med/Low + Flash High)"`, `tier=1`, `window_hours=5.0`, `estimated_turns_per_window=80`, `high_tier_turns_per_window=25`.
- `"chatgpt-astra-sol"`: `provider="codex"`, `display_name="ChatGPT (Astra Med + Sol High)"`, `tier=1`, `window_hours=5.0`, `estimated_turns_per_window=80`, `high_tier_turns_per_window=25`.
- `"claude-opus"`: `provider="claude"`, `display_name="Claude (Opus High + Opus Med)"`, `tier=1`, `window_hours=5.0`, `estimated_turns_per_window=60`, `high_tier_turns_per_window=25`.
- `"mcp-ide"`: `provider="mcp"`, `display_name="IDE Agent (MCP Bridge)"`, `tier=1`, `window_hours=5.0`, `estimated_turns_per_window=60`, `high_tier_turns_per_window=25`.

### 1.3 Edit `funguys/models.py`
1. Add `TaskRole = Literal["planning", "hard_coding", "easy_coding", "testing"]`.
2. Add coordination models:
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
3. Add fields to `TaskItem`:
   - `held: bool = False`
   - `hold_reason: Optional[str] = None`
   - `task_role: Optional[str] = None`  # `"hard_coding"`, `"easy_coding"`, or `"testing"`
   - `assigned_model: Optional[str] = None`  # e.g. `"argon:low"`, `"flash:high"`, `"sol:high"`, `"opus:medium"`
4. Add fields to `AgentNode`:
   - Change `executor: Literal['remote', 'local', 'mcp_ide'] = 'remote'`
   - `is_controller: bool = False`
   - `high_tier_used_pct: float = 0.0`  # Tracks Argon / Astra / Opus-High separate rate limit bucket
   - `high_tier_turns_used: int = 0`
   - `high_tier_turns_limit: int = 25`
   - `active_model_tier: str = ""`
5. Update `RegisterAgentRequest`:
   - Change `executor: Literal['remote', 'local', 'mcp_ide'] = 'remote'`

### 1.4 Edit `funguys/db.py`
Add `prompt_queue` and `blackboard` tables to `StateStore._init_schema()` and add methods:
- `enqueue_prompt(self, item: PromptQueueItem) -> None`
- `pop_pending_prompts(self, agent_id: str, tier: int, project_id: str) -> List[PromptQueueItem]`
- `list_prompts(self, project_id: str) -> List[PromptQueueItem]`
- `put_blackboard(self, entry: BlackboardEntry) -> None`
- `remove_blackboard_locks(self, project_id: str, agent_id: str, keys: Optional[List[str]] = None) -> None`
- `list_blackboard(self, project_id: str, scopes: Optional[List[str]] = None) -> List[BlackboardEntry]`

---

## STEP 2: STANDALONE PROTOCOL, MODEL-TIER SELECTION & MAC/WIN FIXES (`runtime.py` & `protocol.py`)

### 2.1 Edit `funguys/worker/runtime.py`
Remember: `runtime.py` MUST only import Python standard library modules at the top level.

1. **Add Role & Model-Effort Selector (`resolve_task_role` & `resolve_model_and_effort`)**:
   ```python
   MODEL_POLICY = {
       'gemini': {
           'planning': ('argon', 'medium'),
           'hard_coding': ('argon', 'low'),
           'easy_coding': ('argon', 'low'),
           'testing': ('flash', 'high'),
           'fallback': ('flash', 'high'),
       },
       'codex': {
           'planning': ('astra', 'medium'),
           'hard_coding': ('sol', 'high'),
           'easy_coding': ('sol', 'high'),
           'testing': ('sol', 'high'),
           'fallback': ('sol', 'high'),
       },
       'claude': {
           'planning': ('opus', 'high'),
           'hard_coding': ('opus', 'high'),
           'easy_coding': ('opus', 'medium'),
           'testing': ('opus', 'medium'),
           'fallback': ('opus', 'medium'),
       },
   }

   def classify_task_role(task: dict) -> str:
       if task.get('task_role') in ('planning', 'hard_coding', 'easy_coding', 'testing'):
           return task['task_role']
       files = [str(f).lower() for f in (task.get('target_files') or [])]
       title_lower = str(task.get('title', '')).lower()
       if (
           task.get('require_tests')
           or (files and all('test' in f or f.startswith('tests/') for f in files))
           or ('test' in title_lower and any('test' in f for f in files))
       ):
           return 'testing'
       return 'hard_coding' if int(task.get('complexity_tier', 2)) == 1 else 'easy_coding'

   def select_model_and_effort(provider: str, role: str, high_tier_used_pct: float = 0.0, force_fallback: bool = False) -> tuple[str, str]:
       policy = MODEL_POLICY.get(provider, MODEL_POLICY['codex'])
       if force_fallback:
           return policy['fallback']
       if provider == 'gemini':
           if role == 'planning':
               return ('argon', 'medium')
           if role == 'testing' or high_tier_used_pct >= 85.0:
               return ('flash', 'high')
           if role == 'easy_coding' and high_tier_used_pct >= 60.0:
               return ('flash', 'high')
           return ('argon', 'low')
       if provider == 'codex':
           return ('astra', 'medium') if role == 'planning' else ('sol', 'high')
       if provider == 'claude':
           return ('opus', 'high') if role in ('planning', 'hard_coding') else ('opus', 'medium')
       return policy.get(role, policy['fallback'])
   ```

2. **Add Cross-Platform CLI Binary Discovery (`resolve_cli_binary`)**:
   - First check `shutil.which(provider)` (and `shutil.which('agy')` for `'gemini'`) so tests that monkeypatch `runtime.shutil.which` continue to pass.
   - If `None` and `os.name != 'nt'`, search `~/.local/bin`, `/opt/homebrew/bin`, `/usr/local/bin`, `~/.npm-global/bin`, and `~/.nvm/versions/node/*/bin`.

3. **Pass Model & Effort Environment / CLI Flags in `execute_cli`**:
   - Keep `cli_command(provider, root, prompt)` signature backwards-compatible (`def cli_command(provider, root, prompt, model=None, effort=None):`) so `test_no_provider_substitution_or_generic_artifact` (which calls `runtime.cli_command('claude', tmp_path, 'question')`) works without changes.
   - Inside `execute_cli(provider, root, prompt, cancelled=None, timeout=600, role='easy_coding', high_tier_used_pct=0.0)`:
     - Compute `model, effort = select_model_and_effort(provider, role, high_tier_used_pct)`.
     - Set environment variables for the child CLI process (`FUNGUYS_MODEL=model`, `FUNGUYS_REASONING_EFFORT=effort`) and pass provider flags when configured:
       - If `provider == 'gemini'` and `limited` (rate-limit hit while running `argon-low`): **automatically retry once in-place with `select_model_and_effort('gemini', role, force_fallback=True)` (`flash-high`)** before reporting rate-limit failure! This ensures a mid-task Argon rate limit seamlessly finishes on `flash-high` without losing work!

4. **Fix macOS Process Group Termination (`stop_process`)**:
   - Wrap `os.killpg(proc.pid, signal.SIGKILL)` in `try ... except (ProcessLookupError, PermissionError, OSError): proc.kill()`.

5. **Add Compact Signal Parser, Error Compressor, and Contract Slicer inside `runtime.py`**:
   - Implement `SIGNAL_PATTERN`, `parse_raw_signals(text, source_agent_id)`, `compress_error_log(raw_log, max_chars=1200)`, and `slice_contract_for_task(contracts_markdown, task, max_chars=1200)`.
   - Update `task_prompt(assignment)` to inject `contract_slice`, `blackboard`, `inbox`, `compress_error_log(last_error)`, and `@FG:` signal syntax instructions.

6. **Fix `WorkerRuntime.heartbeat_loop` and `_perform_assignment` (Non-Destructive Steering Inbox)**:
   - Write incoming `inbox` prompts and `handoff_digest` updates to `self.active_scratch / '.funguys_inbox.md'` instead of calling `self.cancelled.set()`.
   - Only call `self.cancelled.set()` if `current and not result.get('active')`.
   - When cancelled, if `any(changes.values())`, upload WIP files to `/api/agents/{self.agent_id}/quota` before returning.
   - Include `'signals': parse_raw_signals(output, self.agent_id)` and `'model_used': f'{model}:{effort}'` in `/api/tasks/{id}/submit`.

### 2.2 Create `funguys/protocol.py`
Re-export `SIGNAL_PATTERN`, `parse_raw_signals`, `compress_error_log`, `slice_contract_for_task`, `classify_task_role`, `select_model_and_effort`, and `parse_agent_signals`.

---

## STEP 3: PLANNER WORK-PARSING FOR ARGON/ASTRA CONSERVATION (`architect.py`)

Edit `funguys/host/architect.py`:
1. **Strict Planning-Only Model Selection**:
   - In `_call_architect_llm_json(self, prompt, timeout_sec=90, progress_cb=None)`:
     - Pass `role='planning'` to `run_text_agent(self.provider, ..., role='planning')` so the Architect **always** runs on:
       - `argon` (`medium`) for Google (`gemini`)
       - `astra` (`medium`) for ChatGPT/OpenAI (`codex`)
       - `opus` (`high`) for Claude (`claude`)
2. **Parse Out Work into Atomic Coding vs. Testing Tasks**:
   - In `compile_plan_and_tasks()` prompt instructions (lines 328–347):
     - Instruct the Architect to:
       1. Break implementation into small, focused tasks (1–2 target files per task) so coding agents (`argon-low` / `sol-high` / `opus-high`) complete each task in a single fast pass without exhausting Argon/Opus rate limits.
       2. Explicitly separate **Core Architecture/Hard Coding** (`complexity_tier: 1`, `task_role: "hard_coding"`), **Standard/UI Wiring** (`complexity_tier: 2`, `task_role: "easy_coding"`), and **Testing/Verification** (`complexity_tier: 2`, `require_tests: true`, `task_role: "testing"`, targeting `tests/...`) into distinct tasks so all testing work is automatically offloaded to `flash-high`, `sol-high`, and `opus-medium`.

---

## STEP 4: CROSS-PLATFORM EXECUTION, KEEP-AWAKE, QA & GITHUB C2 (`execution.py`, `qa_pipeline.py`, `tunnel.py`, `adapters.py`, `files.py`, `git_c2.py`)

1. **`funguys/files.py`**:
   - Add `'.funguys_inbox.md'` to `EXCLUDED` in `files.py` and `runtime.py`.
   - Fix error string on line 110 to report `{MAX_FILE // 1_000_000} MB`.
2. **`funguys/host/execution.py`**:
   - Preserve `HOME`, `USERPROFILE`, `APPDATA`, `LOCALAPPDATA`, `TMPDIR`, `XDG_CACHE_HOME`, `XDG_CONFIG_HOME`, `PLAYWRIGHT_BROWSERS_PATH`, `SSL_CERT_FILE`, `NODE_PATH` in `SandboxRunner.run`.
   - Add `LaptopKeepAwake` class (`caffeinate -di -w <pid>` on macOS, `SetThreadExecutionState` on Windows).
3. **`funguys/host/qa_pipeline.py`**:
   - In `_run_playwright_check`: if `not live_preview_url and not task.require_browser` and no file in `task.target_files` ends with `('.html', '.htm', '.js', '.jsx', '.ts', '.tsx', '.css')`, return `(True, 'SKIPPED: no browser target for this task', None)`.
4. **`funguys/worker/adapters.py`**:
   - Update `run_text_agent(provider, prompt, timeout=90, role='planning')`:
     - Preserve first 2,000 chars and last 4,500 chars when truncating long prompts.
     - For `claude`, remove `'--tools'` and its next argument from `command` instead of setting `''`.
5. **`funguys/host/tunnel.py`**:
   - Add stdlib `ifconfig`/`ipconfig` fallback in `get_lan_ip()` when `psutil` is not installed.
6. **Create `funguys/host/git_c2.py`**:
   - Implement `GitC2Sync` using fast Git plumbing (`hash-object`, `mktree`, `commit-tree`, `update-ref refs/heads/fg-c2`) to sync the live host URL beacon and blackboard state to `origin/fg-c2` on GitHub so laptop workers can reconnect without a static server.

---

## STEP 5: MAIN CONTROLLER & DUAL-BUCKET ORCHESTRATOR (`funguys/host/orchestrator.py`)

1. **Dual-Bucket Quota & Model-Role Routing in `select_best_agent_for_task` and `schedule_ready_tasks`**:
   - Classify each task's role via `role = classify_task_role(task.model_dump())`.
   - Set `task.task_role = role`.
   - In `select_best_agent_for_task(self, task, available=None)`:
     ```python
     def select_best_agent_for_task(self, task, available=None):
         role = classify_task_role(task.model_dump())
         pool = [
             a for a in (available if available is not None else self.store.list_agents())
             if a.status == AgentStatus.IDLE
             and a.id not in self._consulting_agents
             and a.current_task_id is None
             and a.used_pct < a.soft_limit_pct
         ]
         if not pool:
             return None
         if role == 'testing':
             # Prefer agents with lowest Flash/Sol/Opus-Med bucket usage; never require tier 1 for testing
             return min(pool, key=lambda a: (a.used_pct, a.tier == 1, a.id))
         tier_matched = [a for a in pool if task.complexity_tier != 1 or a.tier == 1]
         candidates = tier_matched if tier_matched else pool
         return min(candidates, key=lambda a: (a.tier != task.complexity_tier, a.high_tier_used_pct if a.provider == 'gemini' else a.used_pct, a.used_pct, a.id))
     ```
   - When assigning `task` to `agent`, record `model, effort = select_model_and_effort(agent.provider, role, agent.high_tier_used_pct)` and set `task.assigned_model = f"{model}:{effort}"` and `agent.active_model_tier = f"{model}:{effort}"`.
2. **Separate Bucket Accounting in `_finish_agent`**:
   - If `task.assigned_model` used a high-tier bucket (`argon:*`, `astra:*`, or `opus:high`):
     - Increment `agent.high_tier_turns_used += 1` and update `agent.high_tier_used_pct = min(100.0, round(agent.high_tier_turns_used / max(1, agent.high_tier_turns_limit) * 100, 1))`.
     - For `gemini` and `codex`, do **not** mark the entire agent `COOLDOWN` when `high_tier_used_pct >= 85%` if `used_pct < soft_limit_pct`—because that worker can still run `flash:high` or `sol:high` coding and testing tasks!
   - If `task.assigned_model` used the standard/fallback bucket (`flash:high`, `sol:high`, `opus:medium`):
     - Increment `agent.turns_used += 1` and update `agent.used_pct` without touching `high_tier_used_pct`!
3. **Add `process_signals`, `designate_controller`, and `steer_or_trigger_agent`**:
   - Process `@FG:` signals (`CLAIM`, `RELEASE`, `FACT`, `TRIPWIRE`, `HOLD`, `GO`, `STEER`, `TRIGGER`, `VETO`).
   - Update `heartbeat()`, `assignment_payload()`, `submit_task_work()`, and `ask_project_question()` as specified to deliver `inbox` prompts and `blackboard` entries.

---

## STEP 6: MCP IDE SERVER, API ENDPOINTS & DASHBOARD (`mcp_bridge/server.py`, `app.py`, `cli.py`, `dashboard.html`, `dashboard.js`)

1. **`funguys/mcp_bridge/server.py` & `funguys/cli.py`**:
   - Keep existing `public_destination` and `fetch_public_page` functions in `server.py`.
   - Add `MCPWorkerServer` and `run_mcp_stdio_worker` (`fg_listen`, `fg_signal`, `fg_check_inbox`, `fg_submit`) and register `@app.command('mcp-worker')` in `funguys/cli.py`.
2. **`funguys/host/app.py`**:
   - Add `/api/controller/signal` and `/api/blackboard` to the worker path allowlist in `authorize()`.
   - Add `POST /api/controller/signal`, `POST /api/agents/{agent_id}/steer`, `POST /api/agents/{agent_id}/controller`, and `GET /api/blackboard`.
   - Expose `'blackboard'`, `'prompt_queue'`, and `'model_policy': MODEL_ROUTING_POLICY` in `state()`.
   - Fix `restart_host()` and `BodyLimit` error message, and wire `LaptopKeepAwake` into `lifespan()`.
3. **`funguys/host/dashboard.html` & `funguys/host/dashboard.js`**:
   - Preserve all existing DOM IDs tested by `tests/test_browser_dashboard.py`.
   - Display each worker's active model/effort tier (`argon:medium`, `argon:low`, `flash:high`, `astra:medium`, `sol:high`, `opus:high`, `opus:medium`) and both quota buckets (High-Tier Bucket % + Standard Bucket %).
   - Add **Set Controller** and **Steer / Queue Prompt** controls on agent cards, plus the **Shared Blackboard & Prompt Queue** panel.

---

## STEP 7: FINAL VERIFICATION

Run the full test suite and verify all 55+ tests pass:
```powershell
uv run pytest -q
```
