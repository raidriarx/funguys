# Gaper Bingzoid (FunGuys) — AI-Executable Implementation Plan (`PLAN_AI_AGENT.md`)

> **DIRECTIVE FOR CODING AGENT**:
> Follow Steps 1 through 7 in exact sequential order.
> **Critical Invariants to Preserve**:
> 1. `funguys/worker/runtime.py` is served standalone via `GET /worker.py` to remote machines that do **not** have the `funguys` package installed. Therefore, `funguys/worker/runtime.py` MUST ONLY import Python standard-library modules at the top level. Define model-tier resolution, protocol parsing, and compression helpers inside `runtime.py` and re-export them in `funguys/protocol.py`.
> 2. Preserve existing test-facing symbols (`generate_task_code_artifact` in `adapters.py`, `simulate_demo` in `demo.py`, `public_destination` and `fetch_public_page` in `mcp_bridge/server.py`, and legacy profile keys `'claude-max'`, `'claude-pro'`, `'codex-pro'`, `'gemini-cli'`) so all 55 existing tests in `tests/` continue to pass without regression.
> 3. Enforce the **Cross-Provider Planning Priority (`claude opus:high` $\to$ `gemini argon:medium` $\to$ `codex astra:low`)** and **Main Controller Classification of `"important_coding"` (`argon:low` / `opus:high` / `sol:high`) vs. `"gruntwork"` & `"testing"` (`flash:high` / `opus:medium` / `sol:high`)** defined in Steps 1, 2, 3, and 5 below.

---

## STEP 1: CONTROLLER WORK CLASSIFICATION, PRIORITY MATRIX, CONFIG & MODELS (`config.py`, `models.py`, `db.py`)

### 1.1 Exact Cross-Provider Priority & Controller Work Classification Policy

1. **Planning / Controller Priority Order (Highest Usage Rate to Lowest)**:
   - **Priority 1 (`rank=0`)**: `provider == "claude"` $\to$ **`("opus", "high")`**
   - **Priority 2 (`rank=1`)**: `provider == "gemini"` $\to$ **`("argon", "medium")`**
   - **Priority 3 (`rank=2`)**: `provider == "codex"` $\to$ **`("astra", "low")`**
2. **Main Controller Work Classification (No Blind Token-Size Downgrading)**:
   - Do **not** downgrade a task based on file size or token count—a large task can still be critical architectural work! Instead, the **Main Controller / Planner Agent** classifies every task into one of 4 roles (and can dynamically re-classify any task via `@FG:STEER` or `complexity_tier`):
     - `"planning"` — Architect interview, DAG compilation, and Main Controller arbitration (**NO file editing**).
     - `"important_coding"` — (`complexity_tier == 1`): Core domain logic, complex algorithms, critical architecture, tricky bug fixes (even if large/token-heavy).
     - `"gruntwork"` — (`complexity_tier == 2` non-test): Boilerplate, UI scaffolding, CRUD wiring, config/docs, mechanical refactors.
     - `"testing"` — (`require_tests == True` or `tests/*` target files): Unit, integration, and E2E test authoring and verification.
3. **Provider-to-Model Mapping**:

| Provider | `"planning"` (No Coding) | `"important_coding"` (Tier 1: Critical Work, Even If Large) | `"gruntwork"` (Tier 2: Boilerplate / Easy Work) | `"testing"` (Tests / QA) | Hard Floor (Never Lower) |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **`claude`** *(Priority 1)* | `("opus", "high")` | `("opus", "high")` | `("opus", "medium")` | `("opus", "medium")` | `("opus", "medium")` |
| **`gemini`** *(Priority 2)* | `("argon", "medium")` | **`("argon", "low")`** *(Fallback to `("flash", "high")` ONLY if Argon quota exhausted)* | **`("flash", "high")`** *(100% of gruntwork runs on Flash High to save Argon)* | **`("flash", "high")`** | `("flash", "high")` |
| **`codex`** *(Priority 3)* | `("astra", "low")` | `("sol", "high")` | `("sol", "high")` | `("sol", "high")` | `("sol", "high")` |

### 1.2 Edit `funguys/config.py`
Add `PLANNER_PROVIDER_PRIORITY = {"claude": 0, "gemini": 1, "codex": 2, "mcp": 1, "api": 3}` and `MODEL_ROUTING_POLICY`:
```python
@dataclass(frozen=True)
class SubscriptionProfile:
    provider: str
    cli_command: str
    display_name: str
    tier: int  # 1 = Important Work / Architect, 2 = Gruntwork / Testing
    window_hours: float
    estimated_turns_per_window: int
    soft_handoff_threshold_pct: float = 80.0
    high_tier_turns_per_window: int = 25  # Separate rate limit bucket for Opus-High / Argon / Astra-Low
    usage_rate_rank: int = 1
    strengths: List[str] = ()

PLANNER_PROVIDER_PRIORITY: Dict[str, int] = {
    "claude": 0,  # 1st choice for planning: Claude Opus High (highest usage rate)
    "gemini": 1,  # 2nd choice for planning: Gemini Argon Medium
    "codex": 2,   # 3rd choice for planning: ChatGPT Astra Low (lowest planning usage rate)
    "mcp": 1,
    "api": 3,
}

MODEL_ROUTING_POLICY: Dict[str, Dict[str, tuple[str, str]]] = {
    "claude": {
        "planning": ("opus", "high"),
        "important_coding": ("opus", "high"),
        "gruntwork": ("opus", "medium"),
        "testing": ("opus", "medium"),
        "fallback": ("opus", "medium"),
    },
    "gemini": {
        "planning": ("argon", "medium"),
        "important_coding": ("argon", "low"),
        "gruntwork": ("flash", "high"),
        "testing": ("flash", "high"),
        "fallback": ("flash", "high"),
    },
    "codex": {
        "planning": ("astra", "low"),
        "important_coding": ("sol", "high"),
        "gruntwork": ("sol", "high"),
        "testing": ("sol", "high"),
        "fallback": ("sol", "high"),
    },
}
```
Keep legacy keys `"claude-max"`, `"claude-pro"`, `"codex-pro"`, `"gemini-cli"` (keeping `"gemini-cli"` as `tier=2` for test compatibility), and add `"claude-opus"`, `"gemini-argon-flash"`, `"chatgpt-astra-sol"`, and `"mcp-ide"`.

### 1.3 Edit `funguys/models.py`
1. Add `SignalOp`, `AgentSignal`, `PromptQueueItem`, and `BlackboardEntry` models.
2. Add fields to `TaskItem`:
   - `held: bool = False`
   - `hold_reason: Optional[str] = None`
   - `task_role: Optional[Literal["planning", "important_coding", "gruntwork", "testing"]] = None`
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

## STEP 2: STANDALONE PROTOCOL & CONTROLLER ROLE SELECTOR (`runtime.py` & `protocol.py`)

### 2.1 Edit `funguys/worker/runtime.py`
Remember: `runtime.py` MUST only import Python standard library modules at the top level.

1. **Add Controller Role Classifier (`classify_task_role`) & Model-Effort Selector (`select_model_and_effort`)**:
   ```python
   PLANNER_PRIORITY = {'claude': 0, 'gemini': 1, 'codex': 2, 'mcp': 1, 'api': 3}

   MODEL_POLICY = {
       'claude': {
           'planning': ('opus', 'high'),
           'important_coding': ('opus', 'high'),
           'gruntwork': ('opus', 'medium'),
           'testing': ('opus', 'medium'),
           'fallback': ('opus', 'medium'),
       },
       'gemini': {
           'planning': ('argon', 'medium'),
           'important_coding': ('argon', 'low'),
           'gruntwork': ('flash', 'high'),
           'testing': ('flash', 'high'),
           'fallback': ('flash', 'high'),
       },
       'codex': {
           'planning': ('astra', 'low'),
           'important_coding': ('sol', 'high'),
           'gruntwork': ('sol', 'high'),
           'testing': ('sol', 'high'),
           'fallback': ('sol', 'high'),
       },
   }

   def classify_task_role(task: dict) -> str:
       """Use the Main Controller's explicit role or complexity_tier (1=important_coding, 2=gruntwork/testing)."""
       explicit = task.get('task_role')
       if explicit in ('planning', 'important_coding', 'gruntwork', 'testing'):
           return explicit
       files = [str(f).lower() for f in (task.get('target_files') or [])]
       title_lower = str(task.get('title', '')).lower()
       if (
           task.get('require_tests')
           or (files and all('test' in f or f.startswith('tests/') for f in files))
           or ('test' in title_lower and any('test' in f for f in files))
       ):
           return 'testing'
       # Controller sets complexity_tier=1 for important work (even if large) -> argon:low / opus:high / sol:high
       # Controller sets complexity_tier=2 for gruntwork -> flash:high / opus:medium / sol:high
       return 'important_coding' if int(task.get('complexity_tier', 2)) == 1 else 'gruntwork'

   def select_model_and_effort(provider: str, role: str, high_tier_used_pct: float = 0.0, force_fallback: bool = False) -> tuple[str, str]:
       policy = MODEL_POLICY.get(provider, MODEL_POLICY['claude'])
       if force_fallback:
           return policy['fallback']
       if provider == 'gemini':
           if role == 'planning':
               return ('argon', 'medium')
           if role == 'important_coding':
               # Important work uses argon:low even if large, only falling back to flash:high if Argon bucket is exhausted (>=85%)
               return ('flash', 'high') if high_tier_used_pct >= 85.0 else ('argon', 'low')
           # All gruntwork and testing run on flash:high so Argon is conserved for important work and planning
           return ('flash', 'high')
       if provider == 'codex':
           return ('astra', 'low') if role == 'planning' else ('sol', 'high')
       if provider == 'claude':
           return ('opus', 'high') if role in ('planning', 'important_coding') else ('opus', 'medium')
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
Re-export `PLANNER_PRIORITY`, `MODEL_POLICY`, `classify_task_role`, `select_model_and_effort`, `SIGNAL_PATTERN`, `parse_raw_signals`, `compress_error_log`, `slice_contract_for_task`, and `parse_agent_signals`.

---

## STEP 3: PLANNER PRIORITY (`OPUS HIGH` $\to$ `ARGON MEDIUM` $\to$ `ASTRA LOW`) & IMPORTANT vs. GRUNTWORK PARSING (`architect.py`, `adapters.py`, `app.py`)

1. **Edit `funguys/worker/adapters.py` (`detect_available_clis`)**:
   - Order CLI detection by planning usage-rate priority:
     1. `claude` (`claude-pro`)
     2. `gemini` / `agy` (`gemini-cli`)
     3. `codex` (`codex-pro`)
2. **Edit `funguys/host/app.py` (`select_architect`)**:
   - When `agent_id` is not explicitly specified (`Auto: idle local architect`), sort idle local architects by `(PLANNER_PROVIDER_PRIORITY.get(a.provider, 99), a.high_tier_used_pct, a.used_pct, a.tier)` so **Claude (`opus:high`) is ALWAYS selected first**, followed by **Google (`argon:medium`)**, and finally **ChatGPT (`astra:low`)**:
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
   - Default `self.provider = "claude"` (falling back to `"gemini"` then `"codex"` based on `select_architect`).
   - Pass `role='planning'` to `run_text_agent(self.provider, ..., role='planning')` so planning always uses `opus:high`, `argon:medium`, or `astra:low`.
   - In `compile_plan_and_tasks()`, instruct the planner (Main Controller) to classify each task explicitly into:
     - **`complexity_tier: 1`, `task_role: "important_coding"`**: Core algorithms, architecture, and critical logic (even if the task is large)—routed to `argon:low`, `opus:high`, or `sol:high`.
     - **`complexity_tier: 2`, `task_role: "gruntwork"`**: Boilerplate, CRUD, UI scaffolding, config, and simple wiring—offloaded to `flash:high`, `opus:medium`, or `sol:high`.
     - **`complexity_tier: 2`, `task_role: "testing"`, `require_tests: true`**: Test suites and QA verification—offloaded to `flash:high`, `opus:medium`, or `sol:high`.
     - If an important feature is large, split the critical logic into an `"important_coding"` task and split the surrounding boilerplate/tests into `"gruntwork"` and `"testing"` tasks.

---

## STEP 4: CROSS-PLATFORM EXECUTION, KEEP-AWAKE, QA & GITHUB C2 (`execution.py`, `qa_pipeline.py`, `tunnel.py`, `files.py`, `git_c2.py`)

1. **`funguys/files.py`**: Add `'.funguys_inbox.md'` to `EXCLUDED` and fix error string on line 110.
2. **`funguys/host/execution.py`**: Preserve `HOME`, `USERPROFILE`, `APPDATA`, `LOCALAPPDATA`, `TMPDIR`, `XDG_CACHE_HOME`, `XDG_CONFIG_HOME`, `PLAYWRIGHT_BROWSERS_PATH`, `SSL_CERT_FILE`, `NODE_PATH` in `SandboxRunner.run`, and add `LaptopKeepAwake`.
3. **`funguys/host/qa_pipeline.py`**: Skip Playwright when `not live_preview_url and not task.require_browser` and no target file is a frontend/HTML file.
4. **`funguys/host/tunnel.py`**: Add stdlib `ifconfig`/`ipconfig` fallback in `get_lan_ip()`.
5. **Create `funguys/host/git_c2.py`**: Implement `GitC2Sync` to publish host beacon and C2 state to `refs/heads/fg-c2` on GitHub.

---

## STEP 5: MAIN CONTROLLER & WORK-IMPORTANCE SCHEDULER (`funguys/host/orchestrator.py`)

1. **Default Controller Selection Priority (`claude` $\to$ `gemini` $\to$ `codex`)**:
   - When selecting or auto-assigning the Main Controller Agent (`designate_controller` / `_get_controller_agent`), rank candidates by `(PLANNER_PROVIDER_PRIORITY.get(a.provider, 99), a.high_tier_used_pct, a.used_pct)`.
2. **Route by Controller-Assigned Work Importance (`important_coding` vs. `gruntwork` / `testing`) in `select_best_agent_for_task`**:
   ```python
   def select_best_agent_for_task(self, task, available=None):
       from funguys.config import PLANNER_PROVIDER_PRIORITY
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
       if role in ('gruntwork', 'testing'):
           # Gruntwork and testing use flash:high (Gemini), opus:medium (Claude), or sol:high (ChatGPT)
           return min(
               pool,
               key=lambda a: (
                   a.tier != task.complexity_tier if role != 'testing' else False,
                   a.used_pct,
                   a.id,
               ),
           )
       # role == 'important_coding' (Tier 1 critical work, even if large):
       # Runs on argon:low (Gemini), opus:high (Claude), or sol:high (ChatGPT)
       tier_matched = [a for a in pool if task.complexity_tier != 1 or a.tier == 1]
       candidates = tier_matched if tier_matched else pool
       return min(
           candidates,
           key=lambda a: (
               a.tier != task.complexity_tier,
               a.high_tier_used_pct if a.provider == 'gemini' else a.used_pct,
               a.used_pct,
               PLANNER_PROVIDER_PRIORITY.get(a.provider, 99),
               a.id,
           ),
       )
   ```
   *(Note: Verify against `test_plan_approval_gate_and_role_based_tier_routing`: `Alex` is `claude-max` (`tier=1`), `Sam` is `gemini-cli` (`tier=2`). For `task-1-contracts` (`complexity_tier=1`), `a.tier != task.complexity_tier` is `False` for `Alex` and `True` for `Sam`, so `Alex` is selected, passing the test!)*
3. **Dynamic Re-Classification via Controller `@FG:STEER`**:
   - Allow `@FG:STEER` signals from the Main Controller to include `ROLE=important_coding` or `ROLE=gruntwork` to promote or demote a task's importance (`task.task_role` and `task.complexity_tier`) at any time.
3. **Separate Bucket Accounting in `_finish_agent`**:
   - High-tier models (`argon:medium`, `argon:low`, `astra:low`, `opus:high`) increment `agent.high_tier_turns_used` and `agent.high_tier_used_pct`.
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
