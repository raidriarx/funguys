# Gaper Bingzoid (FunGuys) — AI-Executable Implementation Plan (`PLAN_AI_AGENT.md`)

> **DIRECTIVE FOR CODING AGENT**:
> Follow Steps 1 through 7 in exact sequential order.
> **Critical Invariants to Preserve**:
> 1. `funguys/worker/runtime.py` is served standalone via `GET /worker.py` to remote machines that do **not** have the `funguys` package installed. Therefore, `funguys/worker/runtime.py` MUST ONLY import Python standard-library modules at the top level. Define model-tier resolution, token-weight scoring, protocol parsing, and compression helpers inside `runtime.py` and re-export them in `funguys/protocol.py`.
> 2. Preserve existing test-facing symbols (`generate_task_code_artifact` in `adapters.py`, `simulate_demo` in `demo.py`, `public_destination` and `fetch_public_page` in `mcp_bridge/server.py`, and legacy profile keys `'claude-max'`, `'claude-pro'`, `'codex-pro'`, `'gemini-cli'`) so all 55 existing tests in `tests/` continue to pass without regression.
> 3. Enforce the **Cross-Provider Planning Priority (`claude opus:high` $\to$ `codex astra:medium` $\to$ `gemini argon:medium`)** and **Token-Heavy Task Offloading to High-Usage-Rate Models** defined in Steps 1, 2, and 5 below.

---

## STEP 1: USAGE-RATE PRIORITY MATRIX, CONFIG & MODELS (`config.py`, `models.py`, `db.py`)

### 1.1 Exact Cross-Provider Priority & Model-Effort Policy

1. **Planning / Controller Priority Order (Highest Usage Rate to Lowest)**:
   - **Priority 1 (`rank=0`)**: `provider == "claude"` $\to$ **`("opus", "high")`**
   - **Priority 2 (`rank=1`)**: `provider == "codex"` $\to$ **`("astra", "medium")`**
   - **Priority 3 (`rank=2`)**: `provider == "gemini"` $\to$ **`("argon", "medium")`**
2. **Task Role & Token-Weight Classification**:
   - `"planning"` — Architect interview, DAG compilation, and Main Controller arbitration (**NO file editing**).
   - `"hard_coding"` — `complexity_tier == 1` and not a testing task.
   - `"token_heavy_coding"` — Any coding task where `is_token_heavy_task(task)` is `True` (e.g., `len(target_files) >= 3`, `len(description) > 1200`, `retry_count >= 1`, or existing target files exceed `12_000` bytes).
   - `"easy_coding"` — `complexity_tier == 2` and not a testing task.
   - `"testing"` — `require_tests == True` or target files are test files (`test_*` / `*_test.*` / `tests/*`).
3. **Provider-to-Model Mapping**:

| Provider | `"planning"` | `"hard_coding"` (Compact) | `"token_heavy_coding"` (High Token Load) | `"easy_coding"` | `"testing"` | Hard Floor |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **`claude`** *(Priority 1)* | `("opus", "high")` | `("opus", "high")` | `("opus", "high")` *(or `("opus", "medium")` if `high_tier_used_pct >= 75%`)* | `("opus", "medium")` | `("opus", "medium")` | `("opus", "medium")` |
| **`codex`** *(Priority 2)* | `("astra", "medium")` | `("sol", "high")` | `("sol", "high")` | `("sol", "high")` | `("sol", "high")` | `("sol", "high")` |
| **`gemini`** *(Priority 3)* | `("argon", "medium")` | `("argon", "low")` *(fallback: `("flash", "high")`)* | **`("flash", "high")`** *(Never waste Argon on token-heavy tasks)* | `("argon", "low")` *(or `("flash", "high")` if `high_tier_used_pct >= 60%`)* | **`("flash", "high")`** | `("flash", "high")` |

### 1.2 Edit `funguys/config.py`
Add `PLANNER_PROVIDER_PRIORITY = {"claude": 0, "codex": 1, "gemini": 2, "mcp": 1, "api": 3}` and `MODEL_ROUTING_POLICY`:
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
    high_tier_turns_per_window: int = 25  # Separate rate limit bucket for Opus-High / Astra / Argon
    usage_rate_rank: int = 1  # Lower = Higher usage rate capacity (0=Claude/Sol/Flash high-rate, 2=Argon tight rate)
    strengths: List[str] = ()

PLANNER_PROVIDER_PRIORITY: Dict[str, int] = {
    "claude": 0,  # 1st choice for planning: Claude Opus High (highest usage rate)
    "codex": 1,   # 2nd choice for planning: ChatGPT Astra Medium
    "gemini": 2,  # 3rd choice for planning: Gemini Argon Medium (lowest usage rate)
    "mcp": 1,
    "api": 3,
}

MODEL_ROUTING_POLICY: Dict[str, Dict[str, tuple[str, str]]] = {
    "claude": {
        "planning": ("opus", "high"),
        "hard_coding": ("opus", "high"),
        "token_heavy_coding": ("opus", "high"),
        "easy_coding": ("opus", "medium"),
        "testing": ("opus", "medium"),
        "fallback": ("opus", "medium"),
    },
    "codex": {
        "planning": ("astra", "medium"),
        "hard_coding": ("sol", "high"),
        "token_heavy_coding": ("sol", "high"),
        "easy_coding": ("sol", "high"),
        "testing": ("sol", "high"),
        "fallback": ("sol", "high"),
    },
    "gemini": {
        "planning": ("argon", "medium"),
        "hard_coding": ("argon", "low"),
        "token_heavy_coding": ("flash", "high"),
        "easy_coding": ("argon", "low"),
        "testing": ("flash", "high"),
        "fallback": ("flash", "high"),
    },
}
```
Keep legacy keys `"claude-max"`, `"claude-pro"`, `"codex-pro"`, `"gemini-cli"` (keeping `"gemini-cli"` as `tier=2` for test compatibility), and add `"claude-opus"`, `"chatgpt-astra-sol"`, `"gemini-argon-flash"`, and `"mcp-ide"`.

### 1.3 Edit `funguys/models.py`
1. Add `SignalOp`, `AgentSignal`, `PromptQueueItem`, and `BlackboardEntry` models.
2. Add fields to `TaskItem`:
   - `held: bool = False`
   - `hold_reason: Optional[str] = None`
   - `task_role: Optional[str] = None`
   - `token_heavy: bool = False`
   - `assigned_model: Optional[str] = None`
3. Add fields to `AgentNode`:
   - Change `executor: Literal['remote', 'local', 'mcp_ide'] = 'remote'`
   - `is_controller: bool = False`
   - `high_tier_used_pct: float = 0.0`
   - `high_tier_turns_used: int = 0`
   - `high_tier_turns_limit: int = 25`
   - `active_model_tier: str = ""`
4. Update `RegisterAgentRequest`:
   - Change `executor: Literal['remote', 'local', 'mcp_ide'] = 'remote'`

### 1.4 Edit `funguys/db.py`
Add `prompt_queue` and `blackboard` tables to `StateStore._init_schema()` and implement:
- `enqueue_prompt(self, item: PromptQueueItem) -> None`
- `pop_pending_prompts(self, agent_id: str, tier: int, project_id: str) -> List[PromptQueueItem]`
- `list_prompts(self, project_id: str) -> List[PromptQueueItem]`
- `put_blackboard(self, entry: BlackboardEntry) -> None`
- `remove_blackboard_locks(self, project_id: str, agent_id: str, keys: Optional[List[str]] = None) -> None`
- `list_blackboard(self, project_id: str, scopes: Optional[List[str]] = None) -> List[BlackboardEntry]`

---

## STEP 2: STANDALONE PROTOCOL, TOKEN-HEAVY DETECTOR & MODEL-TIER SELECTOR (`runtime.py` & `protocol.py`)

### 2.1 Edit `funguys/worker/runtime.py`
Remember: `runtime.py` MUST only import Python standard library modules at the top level.

1. **Add Token-Heavy Task Detector & Model-Effort Selector**:
   ```python
   PLANNER_PRIORITY = {'claude': 0, 'codex': 1, 'gemini': 2, 'mcp': 1, 'api': 3}

   MODEL_POLICY = {
       'claude': {
           'planning': ('opus', 'high'),
           'hard_coding': ('opus', 'high'),
           'token_heavy_coding': ('opus', 'high'),
           'easy_coding': ('opus', 'medium'),
           'testing': ('opus', 'medium'),
           'fallback': ('opus', 'medium'),
       },
       'codex': {
           'planning': ('astra', 'medium'),
           'hard_coding': ('sol', 'high'),
           'token_heavy_coding': ('sol', 'high'),
           'easy_coding': ('sol', 'high'),
           'testing': ('sol', 'high'),
           'fallback': ('sol', 'high'),
       },
       'gemini': {
           'planning': ('argon', 'medium'),
           'hard_coding': ('argon', 'low'),
           'token_heavy_coding': ('flash', 'high'),
           'easy_coding': ('argon', 'low'),
           'testing': ('flash', 'high'),
           'fallback': ('flash', 'high'),
       },
   }

   def is_token_heavy_task(task: dict, workspace_root: Path | None = None) -> bool:
       if task.get('token_heavy'):
           return True
       files = task.get('target_files') or []
       if len(files) >= 3 or int(task.get('retry_count', 0)) >= 1 or len(str(task.get('description', ''))) > 1200:
           return True
       if workspace_root and workspace_root.exists():
           total_size = sum((workspace_root / f).stat().st_size for f in files if (workspace_root / f).is_file())
           if total_size > 12_000:
               return True
       return False

   def classify_task_role(task: dict, workspace_root: Path | None = None) -> str:
       if task.get('task_role') in ('planning', 'hard_coding', 'token_heavy_coding', 'easy_coding', 'testing'):
           return task['task_role']
       files = [str(f).lower() for f in (task.get('target_files') or [])]
       title_lower = str(task.get('title', '')).lower()
       if (
           task.get('require_tests')
           or (files and all('test' in f or f.startswith('tests/') for f in files))
           or ('test' in title_lower and any('test' in f for f in files))
       ):
           return 'testing'
       if is_token_heavy_task(task, workspace_root):
           return 'token_heavy_coding'
       return 'hard_coding' if int(task.get('complexity_tier', 2)) == 1 else 'easy_coding'

   def select_model_and_effort(provider: str, role: str, high_tier_used_pct: float = 0.0, force_fallback: bool = False) -> tuple[str, str]:
       policy = MODEL_POLICY.get(provider, MODEL_POLICY['claude'])
       if force_fallback:
           return policy['fallback']
       if provider == 'gemini':
           if role == 'planning':
               return ('argon', 'medium')
           # Token-heavy tasks, testing, or high Argon usage always route to high-usage-rate flash:high
           if role in ('testing', 'token_heavy_coding') or high_tier_used_pct >= 85.0:
               return ('flash', 'high')
           if role == 'easy_coding' and high_tier_used_pct >= 60.0:
               return ('flash', 'high')
           return ('argon', 'low')
       if provider == 'codex':
           return ('astra', 'medium') if role == 'planning' else ('sol', 'high')
       if provider == 'claude':
           if role in ('planning', 'hard_coding'):
               return ('opus', 'high')
           if role == 'token_heavy_coding':
               return ('opus', 'medium') if high_tier_used_pct >= 75.0 else ('opus', 'high')
           return ('opus', 'medium')
       return policy.get(role, policy['fallback'])
   ```

2. **Add Cross-Platform CLI Binary Discovery (`resolve_cli_binary`)**:
   - Check `shutil.which(provider)` (and `shutil.which('agy')` for `'gemini'`) first.
   - On macOS/Linux (`os.name != 'nt'`), search `~/.local/bin`, `/opt/homebrew/bin`, `/usr/local/bin`, `~/.npm-global/bin`, and `~/.nvm/versions/node/*/bin`.

3. **Pass Model & Effort Flags and In-Place Fallback in `execute_cli`**:
   - Keep `cli_command(provider, root, prompt, model=None, effort=None)` signature backwards-compatible.
   - In `execute_cli(provider, root, prompt, cancelled=None, timeout=600, role='easy_coding', high_tier_used_pct=0.0)`:
     - Select `model, effort = select_model_and_effort(provider, role, high_tier_used_pct)`.
     - Pass `FUNGUYS_MODEL=model` and `FUNGUYS_REASONING_EFFORT=effort` in `env`.
     - If `provider == 'gemini'` and `model == 'argon'` and `limited` (rate-limit hit): immediately retry in-place with `('flash', 'high')` so the task finishes without failing.

4. **Fix macOS `stop_process(proc)`**:
   - Wrap `os.killpg(proc.pid, signal.SIGKILL)` in `try ... except (ProcessLookupError, PermissionError, OSError): proc.kill()`.

5. **Add Compact Signal Parser, Error Compressor, and Contract Slicer inside `runtime.py`**:
   - Implement `SIGNAL_PATTERN`, `parse_raw_signals`, `compress_error_log`, and `slice_contract_for_task`.
   - Update `task_prompt(assignment)` to use `contract_slice`, `blackboard`, `inbox`, `compress_error_log(last_error)`, and `@FG:` signal instructions.

6. **Fix `WorkerRuntime.heartbeat_loop` and `_perform_assignment`**:
   - Deliver incoming `inbox` prompts and `handoff_digest` updates to `self.active_scratch / '.funguys_inbox.md'` without cancelling the active CLI subprocess.
   - Checkpoint WIP files on cancel, and send parsed `signals` on task submission.

### 2.2 Create `funguys/protocol.py`
Re-export `PLANNER_PRIORITY`, `MODEL_POLICY`, `is_token_heavy_task`, `classify_task_role`, `select_model_and_effort`, `SIGNAL_PATTERN`, `parse_raw_signals`, `compress_error_log`, `slice_contract_for_task`, and `parse_agent_signals`.

---

## STEP 3: PLANNER PRIORITY (`OPUS HIGH` $\to$ `ASTRA MEDIUM` $\to$ `ARGON MEDIUM`) & WORK PARSING (`architect.py`, `adapters.py`, `app.py`)

1. **Edit `funguys/worker/adapters.py` (`detect_available_clis`)**:
   - Order CLI detection by usage rate priority:
     1. `claude` (`claude-pro`)
     2. `codex` (`codex-pro`)
     3. `gemini` / `agy` (`gemini-cli`)
2. **Edit `funguys/host/app.py` (`select_architect`)**:
   - When `agent_id` is not explicitly specified (`Auto: idle local architect`), sort idle local architects by `(PLANNER_PROVIDER_PRIORITY.get(a.provider, 99), a.high_tier_used_pct, a.used_pct, a.tier)` so **Claude (`opus:high`) is ALWAYS selected first**, followed by **ChatGPT (`astra:medium`)**, and finally **Google (`argon:medium`)**:
     ```python
     def select_architect(agent_id):
         from funguys.config import PLANNER_PROVIDER_PRIORITY
         agents = store.list_agents()
         selected = (
             next((a for a in agents if a.id == agent_id), None)
             if agent_id
             else min(
                 (a for a in agents if a.executor == 'local' and a.status.value == 'idle' and a.id not in orch._consulting_agents),
                 key=lambda a: (PLANNER_PROVIDER_PRIORITY.get(a.provider, 99), a.high_tier_used_pct, a.used_pct, a.tier),
                 default=None,
             )
         )
     ```
3. **Edit `funguys/host/architect.py`**:
   - Default `self.provider = "claude"` (falling back to `"codex"` then `"gemini"` based on `select_architect`).
   - Pass `role='planning'` to `run_text_agent(self.provider, ..., role='planning')` so planning always uses `opus:high`, `astra:medium`, or `argon:medium`.
   - In `compile_plan_and_tasks()`, instruct the planner to parse work into atomic 1–2 file tasks and separate `hard_coding`, `easy_coding`, and `testing` tasks so token-heavy and testing work is offloaded to high-usage-rate models (`sol:high`, `flash:high`, `opus:medium`).

---

## STEP 4: CROSS-PLATFORM EXECUTION, KEEP-AWAKE, QA & GITHUB C2 (`execution.py`, `qa_pipeline.py`, `tunnel.py`, `files.py`, `git_c2.py`)

1. **`funguys/files.py`**: Add `'.funguys_inbox.md'` to `EXCLUDED` and fix error string on line 110.
2. **`funguys/host/execution.py`**: Preserve `HOME`, `USERPROFILE`, `APPDATA`, `LOCALAPPDATA`, `TMPDIR`, `XDG_CACHE_HOME`, `XDG_CONFIG_HOME`, `PLAYWRIGHT_BROWSERS_PATH`, `SSL_CERT_FILE`, `NODE_PATH` in `SandboxRunner.run`, and add `LaptopKeepAwake`.
3. **`funguys/host/qa_pipeline.py`**: Skip Playwright when `not live_preview_url and not task.require_browser` and no target file is a frontend/HTML file.
4. **`funguys/host/tunnel.py`**: Add stdlib `ifconfig`/`ipconfig` fallback in `get_lan_ip()`.
5. **Create `funguys/host/git_c2.py`**: Implement `GitC2Sync` to publish host beacon and C2 state to `refs/heads/fg-c2` on GitHub.

---

## STEP 5: MAIN CONTROLLER & USAGE-RATE-AWARE SCHEDULER (`funguys/host/orchestrator.py`)

1. **Default Controller Selection Priority (`claude` $\to$ `codex` $\to$ `gemini`)**:
   - When selecting or auto-assigning the Main Controller Agent (`designate_controller` / `_get_controller_agent`), rank candidates by `(PLANNER_PROVIDER_PRIORITY.get(a.provider, 99), a.high_tier_used_pct, a.used_pct)`.
2. **Route Token-Heavy Tasks & Hard Coding by Usage Rate in `select_best_agent_for_task`**:
   ```python
   def select_best_agent_for_task(self, task, available=None):
       from funguys.config import PLANNER_PROVIDER_PRIORITY
       role = classify_task_role(task.model_dump(), self.git.workspace_path if hasattr(self, 'git') else None)
       heavy = is_token_heavy_task(task.model_dump(), self.git.workspace_path if hasattr(self, 'git') else None)
       pool = [
           a for a in (available if available is not None else self.store.list_agents())
           if a.status == AgentStatus.IDLE
           and a.id not in self._consulting_agents
           and a.current_task_id is None
           and a.used_pct < a.soft_limit_pct
       ]
       if not pool:
           return None
       if role == 'testing' or heavy:
           # Offload testing and token-heavy tasks to high-usage-rate models (sol:high, flash:high, opus:medium/high)
           # Avoid draining Argon bucket on token-heavy tasks
           return min(
               pool,
               key=lambda a: (
                   a.tier != task.complexity_tier if not (role == 'testing' or heavy) else False,
                   a.used_pct,
                   PLANNER_PROVIDER_PRIORITY.get(a.provider, 99),
                   a.id,
               ),
           )
       tier_matched = [a for a in pool if task.complexity_tier != 1 or a.tier == 1]
       candidates = tier_matched if tier_matched else pool
       # For Tier 1 hard coding, prefer Claude (opus:high) and ChatGPT (sol:high) before Gemini (argon:low)
       # so Argon's lower usage rate is conserved!
       return min(
           candidates,
           key=lambda a: (
               a.tier != task.complexity_tier,
               PLANNER_PROVIDER_PRIORITY.get(a.provider, 99),
               a.high_tier_used_pct if a.provider == 'gemini' else a.used_pct,
               a.used_pct,
               a.id,
           ),
       )
   ```
   *(Note: Verify against `test_plan_approval_gate_and_role_based_tier_routing`: `Alex` is `claude-max` (`tier=1`, `PLANNER_PROVIDER_PRIORITY['claude'] == 0`), `Sam` is `gemini-cli` (`tier=2`, `PLANNER_PROVIDER_PRIORITY['gemini'] == 2`). For `task-1-contracts` (`complexity_tier=1`, 1 target file `backend/service.py`), `Alex` is selected first, passing the test!)*
3. **Separate Bucket Accounting in `_finish_agent`**:
   - High-tier models (`argon:medium`, `argon:low`, `astra:medium`, `opus:high`) increment `agent.high_tier_turns_used` and `agent.high_tier_used_pct`.
   - High-usage-rate workhorse models (`flash:high`, `sol:high`, `opus:medium`) increment `agent.turns_used` and `agent.used_pct` without touching `high_tier_used_pct`.
4. **Signal Processing (`process_signals`), `heartbeat`, `submit_task_work`, and `ask_project_question`**:
   - Implement `@FG:` signal handling (`CLAIM`, `RELEASE`, `FACT`, `TRIPWIRE`, `HOLD`, `GO`, `STEER`, `TRIGGER`, `VETO`) and inbox delivery as specified.

---

## STEP 6: MCP IDE SERVER, API ENDPOINTS & DASHBOARD (`mcp_bridge/server.py`, `app.py`, `cli.py`, `dashboard.html`, `dashboard.js`)

1. **`funguys/mcp_bridge/server.py` & `funguys/cli.py`**:
   - Keep `public_destination` and `fetch_public_page` intact.
   - Add `MCPWorkerServer` (`fg_listen`, `fg_signal`, `fg_check_inbox`, `fg_submit`) and `@app.command('mcp-worker')`.
2. **`funguys/host/app.py` & `dashboard.js`**:
   - Default invite/local profile dropdowns to `'claude-pro'` (or `'claude-opus'`) first, then `'codex-pro'`, then `'gemini-cli'`.
   - Expose `/api/controller/signal`, `/api/agents/{id}/steer`, `/api/agents/{id}/controller`, and `/api/blackboard`.
   - Wire `LaptopKeepAwake` into `lifespan(app)`.

---

## STEP 7: FINAL VERIFICATION

Run the full test suite and verify all 55+ tests pass:
```powershell
uv run pytest -q
```
