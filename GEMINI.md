# FunGuys — Persistent Session Rules, Project Context & Historical Plans

> **Auto-Loaded by Antigravity (`GEMINI.md`):** This file is automatically read at the start of every new session in `C:\Users\User\Documents\funguys`.

---

## 1. Mandatory Agent Workflow Rule (CRITICAL)

- **Main Model (Gemini 4 Argon Medium) = Planner Only:** You must **NEVER** write code, edit source files, or implement scripts yourself. You are strictly the Architect, Planner, and Coordinator.
- **Submodel (`Gemini 3.8 Flash High`) = All Coding & Execution:** Always define and invoke a dedicated coding subagent using `define_subagent` and `invoke_subagent(..., Model='flash')` for all coding, file modifications, test writing, and technical execution tasks.
- **In FunGuys Swarm Routing:** When running or testing FunGuys with Gemini, keep the Main Controller (`argon:medium`, `is_controller=True`) reserved exclusively for `planning`, Grill-Me interviews, `/btw` triage, and `@FG:DISPUTE` arbitration, while delegating coding tasks (`important_coding`, `gruntwork`, `testing`) to the **Gemini Flash High** (`flash:high`) worker submodel.

---

## 2. Project Architecture Overview (`FunGuys`)

FunGuys is a **Multiplayer, Usage-Aware Collaborative AI Swarm Engineering Platform** (`https://github.com/raidriarx/funguys`):
- **Central Host Server (`funguys/host/app.py`, `funguys/host/orchestrator.py`):** FastAPI server on port `8420` owning the Task DAG, SQLite `StateStore` (`.funguys/state.db`), single-instance `HostLock` (`.funguys/host.lock`), two-tier auth (`admin_token` vs. `worker_token`), and WebSocket `/ws` live state stream.
- **Isolated Git Worktrees (`funguys/host/git_manager.py`):** Every task assignment runs in an isolated Git worktree (`.funguys/workspaces/<project>/.funguys/w/<assignment_id>`). Changes merge to `main` only after passing 3-layer QA.
- **3-Layer Autonomous QA Pipeline (`funguys/host/qa_pipeline.py`, `funguys/host/execution.py`):**
  1. *Layer 1 (Static):* Python `ast.parse`, JSON validation, and required `target_files` verification.
  2. *Layer 2 (Unit/Build):* `pytest`, `node --check`, `npm run build/test`, and custom `verification_commands` inside Docker (`funguys-qa:local`) or local host `SandboxRunner` (`FUNGUYS_TRUSTED_EXECUTION=1`).
  3. *Layer 3 (Browser):* Headless Playwright Chromium check with full-page screenshot capture.
- **Graphify AST & Contract Delta Engine (`funguys/host/graph_engine.py`):** Zero-token AST parser and NetworkX community detector that raises `ContractAlert` notifications when exported symbols/signatures change.
- **Grill-Me Lead Architect Engine (`funguys/host/architect.py`):** Scans existing workspaces, conducts multi-turn technical interviews, and compiles `CONTRACTS.md` + multi-phase Task DAGs.
- **Standalone Worker Runtime (`funguys/worker/runtime.py`):** Zero-dependency standard-library Python worker served at `GET /worker.py` (SHA-256 verified by `/join.ps1` and `/join.sh`), with background heartbeats and live `.funguys_inbox.md` steering.
- **IDE Stdio MCP Bridge (`funguys/mcp_bridge/server.py`):** JSON-RPC 2.0 stdio server (`funguys mcp-worker`) exposing 9 tools (`fg_poll`, `fg_listen`, `fg_check_inbox`, `fg_signal`, `fg_dispute`, `fg_handoff`, `fg_steer`, `fg_btw`, `fg_submit`).

---

## 3. Past Plans & Historical Projects in This Repository

### 3.1 Plans Currently in the Repo Root
1. **`PLAN_HUMAN.md` & `PLAN_AI_AGENT.md` (Commit `092435d` — Latest):**
   - *Topic:* **Network-Wide `/btw` Swarm Terminal & Controller Triage**.
   - *Details:* Teammate website sign-in via `worker_token`, `funguys btw "<msg>"` CLI command, interactive worker stdin console listener, `fg_btw` MCP tool, and Controller-mediated `/btw` triage with Feature #9 (Git WIP checkpointing & automatic task self-reclaim).
2. **`WORKFLOW_AUDIT.md` & `WORKFLOW_RESCAN.md` (Commit `4e3e8d0`):**
   - *Topic:* **27-Point Security & Distributed Workflow Audit + Remediation**.
   - *Details:* Fixed unauthenticated host exposure, isolated Git worktree verification before merge, versioned execution leases, PowerShell/Bash injection safety, honest Playwright QA checks, and SSRF protection.

### 3.2 Earlier Plans Preserved in Git History (`git log`)
*(Originally titled under the codename **"Gaper Bingzoid (FunGuys)"**; inspectable via `git show <commit>:PLAN_HUMAN.md` and `git show <commit>:PLAN_AI_AGENT.md`)*
- **Commit `694ce98` (*Model A "Player-Coach" 5-Agent Swarm Plan*):**
  - **Model A (Player-Coach):** All 5 connected agents actively write code while the Main Controller supervises.
  - **Feature #9:** Mid-work non-destructive task drop & Git WIP checkpoint (`refs/heads/wip/<task_id>`) when promoted to Controller or summoned to arbitrate, with automatic task self-reclaim (`preferred_agent_id`) if all other workers are busy.
  - **Feature #2:** Worker problem detection & `@FG:DISPUTE` escalation protocol + `controller_arbitration_prompt()`.
  - **Feature #4:** On-demand context window reset & targeted handoff (`@FG:HANDOFF` / `POST /api/agents/{id}/handoff`) without a 60-minute quota cooldown.
- **Commits `ccb3411` $\to$ `f66c206` $\to$ `bb42452` $\to$ `635e23a` $\to$ `555bdd0` (*Multi-Agent C2 & Work-Importance Routing Plans*):**
  - Telegraphic 1-line C2 coordination signals (`@FG:CLAIM`, `RELEASE`, `FACT`, `TRIPWIRE`, `TRIGGER`, `STEER`, `HOLD`, `GO`, `VETO`) and the **Shared Blackboard**.
  - Serverless GitHub state sync over the orphan `refs/heads/fg-c2` branch (`funguys/host/git_c2.py`).
  - Evolution from token-heaviness heuristics to **Controller-driven Work Importance** (`planning`, `important_coding`, `gruntwork`, `testing`) and dual-bucket quota tracking (`Claude Opus High` $\to$ `Gemini Argon Medium` $\to$ `ChatGPT Astra Low`).

### 3.3 Past / Seeded Projects in the Repo
1. **Default Seeded Demo Project ("FunGuys Demo Project"):**
   - Defined in `funguys/host/orchestrator.py` and `funguys/worker/adapters.py`: 3-task priority item tracker (`task-1-contracts` in `backend/service.py`, `task-2-frontend` in `frontend/index.html`, `task-3-tests` in `tests/test_service.py`).
2. **Historical Desktop/Phone Gameplay Tracking & Calibration Bot (Initial Commit `6be4f32`):**
   - Viewable via `git show 6be4f32:funguys/host/architect.py`: a 3-phase project template (`task-p1-core-engine` in `src/core/engine.py` & `src/core/calibration.py`, `task-p2-desktop-ui` in `src/ui/app_window.py` & `src/core/diagnostics.py`, `task-p3-sim-and-packaging` in `src/simulator/gameplay_sim.py`, `build_portable.py`, & `tests/test_bot_suite.py`). Removed in `4e3e8d0` so the Grill-Me Architect dynamically compiles plans for any domain.

---

## 4. Verified PC Environment & Quick Commands

- **Local Virtual Environment:** `C:\Users\User\Documents\funguys\.venv` (Python `3.12.10`, all `pyproject.toml` packages installed in editable mode, plus Playwright Chromium `145.0.7632.0` installed).
- **Installed AI CLIs:** `claude` (`2.1.159`), `gemini` (`0.60.0`), `codex` (`0.167.0`), and `agy` (`C:\Users\User\AppData\Local\agy\bin\agy.exe`). `funguys/worker/runtime.py` is configured to resolve all Windows `.cmd` wrappers and native `.exe` binaries on this PC.
- **Docker:** Not installed; always set `$env:FUNGUYS_TRUSTED_EXECUTION = '1'` on this PC to use the local host `SandboxRunner`.
- **Test Status:** **67 / 67** automated pytest tests passing (`37.58s`) + **5 / 5** live PC swarm & submodel coding verification suites passing.

```powershell
# Run full 67-test regression suite
$env:FUNGUYS_TRUSTED_EXECUTION = '1'
C:\Users\User\Documents\funguys\.venv\Scripts\pytest.exe -v

# Start FunGuys Host & Mission Control Web UI (http://localhost:8420)
$env:FUNGUYS_TRUSTED_EXECUTION = '1'
C:\Users\User\Documents\funguys\.venv\Scripts\funguys.exe host
```
