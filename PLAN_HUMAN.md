# Gaper Bingzoid (FunGuys) — Multi-Agent C2 & Coordination Plan (Human Guide)

## 1. Executive Summary & Goal

The goal of this upgrade is to transform **FunGuys** from a static batch-task runner into a **token-efficient, multi-agent command-and-control (C2) coordination network** where:
1. **AI agents can communicate, trigger each other, and queue/steer prompts** to any other agent mid-flight.
2. **All coordination routes through One Main Controller Agent** (hub-and-spoke architecture) so agents never get stuck in chaotic N×N chat loops.
3. **Strict Model & Reasoning-Effort Routing + Separate Quota Buckets**:
   - Protects scarce high-tier model buckets (**Argon** on Google, **Astra** on ChatGPT, **Opus High** on Claude) by strictly parsing out work by role (**Planning Only** vs. **Hard Coding** vs. **Easy Coding** vs. **Testing**) and never dropping below the minimum quality floor (**Flash High**, **Sol High**, **Opus Medium**).
4. **Coordination uses the July 2026 ExploitGym / Hugging Face Swarm primitives**—stripped of cryptographic signatures (`Ed25519`) and folder-name encoding—leaving pure, telegraphic synchronization signals (`CLAIM`, `RELEASE`, `FACT`, `TRIPWIRE`, `TRIGGER`, `STEER`, `HOLD`, `GO`, `VETO`) and a **Shared Blackboard**.
5. **Cross-Platform & Serverless Laptop Operation**: Works natively on **macOS, Windows, and Linux**, supports **Google (`gemini`/`agy`), ChatGPT (`codex`), Claude (`claude`), and IDE/Desktop AI apps** (Cursor, Windsurf, Antigravity, Claude Desktop, ChatGPT App), and runs **100% free on laptops using GitHub (`fg-c2` branch)** and free tunnels with zero paid servers.

---

## 2. Model & Effort Routing Matrix (Quota-Aware Tiering)

Each provider enforces a strict **Role-to-Model-Effort policy** when booting up a CLI window or dispatching a task, with **separate usage-bucket tracking** so high-tier models never get wasted on routine work or testing:

| Task Role | Google (`gemini` / `agy`) | OpenAI / ChatGPT (`codex`) | Anthropic (`claude`) |
| :--- | :--- | :--- | :--- |
| **1. Planning & Controller Only** *(Zero Coding)* | **`argon` (`medium`)**<br>*(Highest allowed; strictly NO coding)* | **`astra` (`medium`)**<br>*(Highest allowed; strictly NO coding)* | **`opus` (`high`)** |
| **2. Hard Coding** *(Tier 1: Core/Backend/Algorithms)* | **`argon` (`low`)**<br>*(Auto-fallbacks to **`flash` (`high`)** if Argon bucket runs out)* | **`sol` (`high`)**<br>*(Handles all coding so Astra never runs out)* | **`opus` (`high`)** |
| **3. Easy Coding** *(Tier 2: UI/Wiring/Boilerplate)* | **`argon` (`low`)** -> **`flash` (`high`)**<br>*(Shifts to `flash-high` early at 60% Argon usage to preserve Argon)* | **`sol` (`high`)** | **`opus` (`medium`)** |
| **4. Testing & QA** *(All test authoring & fixes)* | **`flash` (`high`)**<br>*(100% isolated from Argon bucket)* | **`sol` (`high`)** | **`opus` (`medium`)** |
| **Hard Floor** *(Never go lower than)* | **`flash` (`high`)** | **`sol` (`high`)** | **`opus` (`medium`)** |

### How We Keep Argon (and Astra) From Running Out
1. **Dual-Bucket Usage Tracking (`featured_used_pct` vs. `standard_used_pct`)**:
   - **Argon** (`argon-medium` + `argon-low`) runs on a separate usage rate from **Flash** (`flash-high`). Likewise, **Astra** (`astra-medium`) runs on a separate usage rate from **Sol** (`sol-high`).
   - The Host tracks both buckets separately per worker.
2. **Hard Ban on Coding with `argon-medium` or `astra-medium`**:
   - `argon-medium` and `astra-medium` are **only** booted for Planning (`GrillArchitectEngine`) and Main Controller arbitration (`_consult_controller_agent`). They are never assigned file-editing tasks.
3. **Reserved Planning Buffer + Seamless In-Task Fallback (`argon-low` $\to$ `flash-high`)**:
   - For Google workers:
     - **0%–60% Argon Usage**: `argon-low` handles both Hard Coding and Easy Coding; `flash-high` handles all Testing.
     - **60%–85% Argon Usage**: Easy Coding automatically shifts to `flash-high`; `argon-low` is reserved only for Hard Coding (`complexity_tier == 1`).
     - **85%–100% Argon Usage (or Argon 429 Rate Limit)**: The remaining 15% of the Argon bucket is locked exclusively for `argon-medium` planning/steering. **All** coding tasks automatically boot with `flash-high`—and if `argon-low` hits a rate limit mid-run, the worker catches it and retries in-place with `flash-high` instead of failing the task!
4. **Fine-Grained Work Parsing (Micro-Task Decomposition)**:
   - Multi-file monolithic tasks burn through Argon's rate limit because the CLI loops 15+ times in a single session.
   - The Planner (`argon-medium` / `astra-medium` / `opus-high`) is instructed to **parse work into atomic 1–2 file tasks** and **always split Tests into dedicated `role="testing"` tasks** (`complexity_tier=2, require_tests=True`) so test writing and debugging are 100% offloaded to `flash-high`, `sol-high`, and `opus-medium`.

---

## 3. Self-Audit Corrections Applied to This Plan

Upon re-auditing the plan against the repository's 55-test regression suite (`tests/`), **6 critical traps** were identified and corrected so the coding agent will not break existing functionality:

1. **Standalone `/worker.py` Single-File Constraint (`funguys/worker/runtime.py`)**:
   - *Trap*: Remote workers join via `/join.ps1` or `/join.sh` by downloading **only** `funguys/worker/runtime.py` (`/worker.py`) and running it with standard-library Python—without the `funguys` package installed. If `runtime.py` imported `from funguys.protocol import ...`, standalone workers on other laptops would crash with `ModuleNotFoundError`.
   - *Correction*: All core wire-protocol parsing (`parse_raw_signals`), model-flag resolution (`resolve_model_and_effort`), error compression (`compress_error_log`), and compact prompt building (`task_prompt`) are implemented directly inside `funguys/worker/runtime.py` using 100% Python standard library, and re-exported by `funguys/protocol.py` for the Host.
2. **Tier-Routing Test Compatibility (`funguys/config.py` & `funguys/host/orchestrator.py`)**:
   - *Trap*: `test_plan_approval_gate_and_role_based_tier_routing` registers `claude-max` (Tier 1, 50% used) and `gemini-cli` (Tier 2, 10% used) and asserts that a Tier-1 task routes to `claude-max`. Changing `gemini-cli` to `tier=1` would cause `gemini-cli` to win that task and break the test.
   - *Correction*: Keep the legacy profile key `gemini-cli` as `tier=2` for backward compatibility with existing tests, add the new profiles (`gemini-argon`, `chatgpt-astra-sol`, `claude-opus`), and update `select_best_agent_for_task()` so Tier-1 tasks prefer Tier-1 agents when available, and automatically fall back to Tier-2 agents (or `flash-high` / `sol-high`) if no idle Tier-1 agent is available.
3. **Preserving Test Fixtures (`funguys/host/demo.py`, `adapters.py`, `mcp_bridge/server.py`)**:
   - *Trap*: Existing tests (`test_orchestrator_e2e.py`, `test_workflow_regressions.py`, `test_concurrency_recovery.py`) explicitly import `generate_task_code_artifact`, `/api/simulate/hackathon`, `public_destination`, and `fetch_public_page`. Deleting them would break 4 regression tests.
   - *Correction*: Keep those functions in place (they use 0 LLM tokens at runtime since they are only called by tests/simulations) and add the new `MCPWorkerServer` alongside them in `funguys/mcp_bridge/server.py`.
4. **Host HTTP Middleware Worker Permissions (`funguys/host/app.py:228`)**:
   - *Trap*: `app.py` has a strict allowlist of URL paths that `role == 'worker'` tokens can call. New endpoints (`/api/controller/signal` and `/api/blackboard`) would return `403 Forbidden` to workers unless added to the middleware allowlist.
   - *Correction*: Explicitly add `/api/controller/signal` and `/api/blackboard` to the worker path allowlist in `authorize()`.
5. **`/btw` Side Conversation Regression Compatibility (`funguys/host/orchestrator.py:728`)**:
   - *Trap*: `test_btw_side_conversation_lifecycle_and_guidance` monkeypatches `adapters.run_text_agent` and tests `/api/chat/ask` with a remote worker record.
   - *Correction*: When `/btw` or `/api/chat/ask` is called, **both** enqueue the `PromptQueueItem` into the worker's live inbox (`store.enqueue_prompt`) **and** attempt `run_text_agent` (catching `RuntimeError` if the CLI isn't installed locally on the host and returning a clean `[Queued to worker inbox]` confirmation instead of crashing).
6. **Playwright QA Check Compatibility (`funguys/host/qa_pipeline.py:176`)**:
   - *Trap*: `test_browser_failure_is_not_a_pass_and_checks_are_honest` calls `_run_playwright_check(task, 1, 'http://127.0.0.1:1/missing')` on a task where `require_browser=False`.
   - *Correction*: Always run Playwright if `live_preview_url` is explicitly provided OR `task.require_browser` is `True` OR any file in `task.target_files` is an HTML/frontend file; only skip when `not live_preview_url and not task.require_browser` and the task only targets backend files.

---

## 4. How the Coordination Protocol Works (ExploitGym Style, Zero Bloat)

Instead of agents chatting in conversational paragraphs, agents communicate with the **Main Controller Agent** via single-line directives at the end of their output or via MCP tool calls:

| Signal | Format | Purpose | Token Cost |
| :--- | :--- | :--- | :--- |
| **`CLAIM`** | `@FG:CLAIM :: src/auth.py,src/db.py` | Locks files on the Blackboard before editing to prevent Git merge conflicts. | ~10 tokens |
| **`RELEASE`** | `@FG:RELEASE :: *` | Releases file locks held by the agent. | ~5 tokens |
| **`FACT`** | `@FG:FACT :: api:auth_header=X-Token` | Posts a shared discovery to the Blackboard so other agents don't waste turns rediscovering it. | ~15 tokens |
| **`TRIPWIRE`** | `@FG:TRIPWIRE :: build:vitest needs --run flag` | Warns all agents of a dead-end or environment trap. | ~15 tokens |
| **`TRIGGER`** | `@FG:TRIGGER TARGET=role:testing TASK=task-3 :: Run unit tests on new auth module` | Asks the Main Controller to wake up another agent (`flash-high` / `sol-high` / `opus-medium`) to run tests or follow-up tasks. | ~25 tokens |
| **`STEER`** | `@FG:STEER TARGET=agent-abc TASK=task-2 :: Rename login() to authenticate()` | Queues a live steering instruction into another agent's `.funguys_inbox.md` without killing its process. | ~25 tokens |
| **`HOLD` / `GO`** | `@FG:HOLD TASK=task-2 :: Waiting on schema migration` | Pauses or unblocks a downstream task/agent. | ~15 tokens |
| **`VETO`** | `@FG:VETO TASK=task-1 :: Circular dependency in spec; need Controller redesign` | Halts a broken task approach and escalates to the Main Controller (`argon-medium` / `astra-medium` / `opus-high`). | ~20 tokens |

### Two-Layer Main Controller (Saving 90% of Controller Quota)
- **Layer 1: Deterministic Python Switchboard (0 Tokens)**:
  - Automatically processes `CLAIM`, `RELEASE`, `FACT`, `TRIPWIRE`, `HOLD`, `GO`, and direct `STEER`/`TRIGGER` routing in <5ms using zero LLM calls.
- **Layer 2: Controller LLM Arbitration (`argon-medium` / `astra-medium` / `opus-high`)**:
  - Only wakes the designated Main Controller LLM when a `VETO`, `BLOCKED_CIRCUIT_BREAKER`, or ambiguous `TRIGGER` occurs.

---

## 5. Existing Codebase Bugs We Are Fixing

1. **macOS Environment Stripping (`funguys/host/execution.py:32`)**: `SandboxRunner` stripped `HOME`, `USERPROFILE`, and `APPDATA`, breaking Playwright, Node, and CLIs on macOS and Windows.
2. **macOS CLI Path Blindness (`funguys/worker/runtime.py:108`)**: Non-interactive shells on Mac miss `/opt/homebrew/bin`, `/usr/local/bin`, `~/.local/bin` (Claude CLI), and `~/.nvm` (Codex/Gemini CLI).
3. **macOS `os.killpg` Race Condition (`funguys/worker/runtime.py:139`)**: Uncaught `ProcessLookupError`/`PermissionError` when killing exiting process groups on macOS BSD kernel.
4. **Heartbeat Cancellation Destroys Work (`funguys/worker/runtime.py:293-325`)**: Any update to `handoff_digest` (like a `/btw` note or contract alert) killed the running worker CLI and returned before saving WIP changes.
5. **Unbounded Error Logs in Worker Prompts (`funguys/worker/runtime.py:191`)**: Up to 100KB of raw terminal output in `last_error` was dumped verbatim into retry prompts.
6. **Middle-Prompt Truncation & Claude `--tools ""` Bug (`funguys/worker/adapters.py:34,44`)**: Naive `prompt[:3000] + prompt[-3000:]` slicing broke structured prompts, and passing `--tools ""` broke newer Claude Code CLIs.
7. **Host Restart Forces Trusted Execution (`funguys/host/app.py:280`)**: `/api/host/restart` forced `FUNGUYS_TRUSTED_EXECUTION=1` and dropped `--port`/`--host`/`--tunnel` flags.
8. **Missing `psutil` Dependency (`funguys/host/tunnel.py:20`)**: `get_lan_ip()` imported `psutil` which is not in `pyproject.toml`.

---

## 6. 100% Free Laptop-Only Operation (No Paid Server Required)

1. **GitHub `fg-c2` Branch Rendezvous (`funguys/host/git_c2.py`)**:
   - Whenever the Host starts or updates its free Cloudflare Quick Tunnel (`trycloudflare.com`) or LAN IP, it pushes a lightweight beacon (`c2_beacon.json`) to an orphan branch `fg-c2` on your GitHub repo using fast Git plumbing commands (without touching your working branch).
   - If the Host laptop sleeps, wakes up, or changes Wi-Fi and gets a new free tunnel URL, workers automatically read `origin/fg-c2` from GitHub to discover the new URL and stay connected.
2. **Laptop Sleep Prevention (`LaptopKeepAwake`)**:
   - Uses native `caffeinate -di` on macOS and `SetThreadExecutionState` on Windows so laptops never go to sleep while coordinating or executing tasks.
