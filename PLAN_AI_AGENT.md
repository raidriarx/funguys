# FunGuys — AI Agent Implementation Specification: Network-Wide `/btw` Swarm Terminal & Controller Triage

> **TARGET:** Replace the previous `PLAN_AI_AGENT.md` with the exact step-by-step instructions to implement:
> 1. **Teammate Session & Network Access** (`/api/session`, `/api/state`, `/api/chat/ask`, `/api/chat/btw/end` for `worker` role).
> 2. **Controller-Mediated `/btw` Triage with Feature #9 (WIP Checkpoint & Task Self-Reclaim), Feature #2 (`@FG:STEER` Arbitration), and Feature #4 (`@FG:HANDOFF` Context Reset)**.
> 3. **Host Website Swarm Terminal UI** (`dashboard.html` & `dashboard.js`).
> 4. **PowerShell/Bash Worker Console `/btw` Listener & CLI Command** (`runtime.py` & `cli.py`).
> 5. **MCP Bridge `fg_btw` Tool** (`funguys/mcp_bridge/server.py`).
> 6. **End-to-End Automated Pytest Coverage** (`tests/test_c2_controller_and_mcp.py`).

---

## Step 1: Open Teammate Session & `/btw` Endpoints in `funguys/host/app.py`

### 1.1 Update `authorize` middleware (`funguys/host/app.py`)
Allow `identity['role'] == 'worker'` to access:
- `GET /api/state` (so teammates signed into the website can view the dashboard)
- `POST /api/chat/ask` (so teammates on the website, CLI, or MCP can send `/btw` or project questions)
- `POST /api/chat/btw/end` (so teammates can close an active `/btw` side conversation)

Specifically, in `authorize()` (`funguys/host/app.py` around line 255), add `'/api/state'`, `'/api/chat/ask'`, and `'/api/chat/btw/end'` to the `path in (...)` tuple for `identity['role'] == 'worker'`.

### 1.2 Update `POST /api/session` (`funguys/host/app.py`)
Currently, `login()` only accepts `access.admin_token`. Update `login()` so that:
- It checks `identity = access.identify(req.token)`.
- If `identity` is valid and `identity['role'] in ('admin', 'worker')`:
  - Clears `login_failures` for the IP.
  - Returns `{'signed_in': True, 'role': identity['role'], 'agent_id': identity.get('agent_id')}` and sets the `funguys_session` cookie to `req.token`.
- Otherwise records the login failure and raises `HTTPException(401, 'Invalid administrator or worker token')`.

---

## Step 2: Controller-Mediated `/btw` Triage & Task Self-Reclaim in `funguys/host/orchestrator.py`

### 2.1 Update `ask_project_question()` in `funguys/host/orchestrator.py`
When `ask_project_question(self, question, preferred_agent_id=None)` is called:

1. **Identify Active Target Worker(s) & Main Controller:**
   - Ensure a Controller is elected (`self._ensure_controller_elected()`, `agents = self.store.list_agents()`).
   - Find `controller = next((a for a in agents if a.is_controller and a.status != AgentStatus.OFFLINE), None)`.
   - Find all currently working agents with tasks: `working_agents = [a for a in agents if a.status == AgentStatus.WORKING and a.current_task_id]`.
   - Find the primary target worker whose task receives the `/btw` note (`target_worker`):
     - If `preferred_agent_id` is explicitly set (not `'auto'`), use that agent if they have a task, or fallback to `working_agents[0]`.
     - If `is_btw`: prefer a non-controller working agent first (`next((a for a in working_agents if not a.is_controller), None) or (working_agents[0] if working_agents else None)`).

2. **Select the Responder (`selected`)**:
   - If an active `self.btw_session` is already open, continue with `self.btw_session['agent_id']`.
   - Else if `preferred_agent_id` is explicitly provided and not `'auto'`, select that agent.
   - Else if `is_btw` and `controller` is available (`controller.used_pct < controller.soft_limit_pct`):
     - Route to `selected = controller`!
   - Otherwise fall back to existing idle/working planner priority selection.

3. **Feature #9: Checkpoint & Reclaim Controller's Task If Coding a Separate Task:**
   - Check if `is_btw` is True, `selected.is_controller` is True, `selected.status == AgentStatus.WORKING`, and `selected.current_task_id` is different from `target_worker.current_task_id` (i.e., there is another active worker `target_worker` whose task is being guided, while the Controller was busy coding its own separate task):
     - Call `await self.drop_task_for_controller(selected.id, reason='Paused briefly to triage incoming /btw guidance', allow_self_reclaim=True)` before running the consultation!
     - Record `controller_dropped_task = True` and reload `selected = self.store.get_agent(selected.id)`.
   - *(Note: If the Controller is the ONLY working agent in the swarm, `target_worker.id == selected.id`, so the Controller does NOT drop its task—it stays `WORKING` and applies the `/btw` note directly to its own task, preserving single-agent regression test compatibility.)*

4. **Record `/btw` Session & Guarantee Non-Destructive Delivery to Target Worker:**
   - Determine `effective_target = target_worker or selected`.
   - Set `self.btw_session['task_id'] = effective_target.current_task_id`.
   - When `is_btw and btw_note`:
     - Append `btw_note` to `self.btw_session['notes']`.
     - Enqueue a `PromptQueueItem(target_agent_id=effective_target.id, sender_agent_id=selected.id if selected.is_controller else 'human-team', mode='steer', prompt=btw_note, priority=50)` so the target worker's `.funguys_inbox.md` receives the guidance immediately without interrupting its subprocess.
     - Append `[User /btw guidance]: {btw_note}` to `effective_target`'s task `handoff_digest`.

5. **Include Swarm Roster in Prompt for Controller `/btw` Triage (Feature #2 & #4):**
   - Build an `active_roster` summary listing all agents (`id`, `display_name`, `status`, `current_task_id`, and `target_files`).
   - Include instructions in `prompt` telling the Controller:
     - To acknowledge the `/btw` guidance, explain how it applies to the target task/contracts, and optionally emit `@FG:STEER <agent_id> <refined directive>` or `@FG:HANDOFF <agent_id> <reason>` if the worker needs an immediate context reset.
   - Parse any `@FG:...` signals from `answer` and execute `await self.process_signals(...)` ( avoiding duplicate `steer` enqueue if already queued for the same `effective_target`).

6. **Feature #9: Automatic Controller Task Resume in `finally` Block:**
   - In the `finally:` block of `ask_project_question()`, after releasing `self._consulting_agents.discard(selected.id)` and restoring `selected.status = AgentStatus.IDLE`:
     - If `controller_dropped_task` is True, call `await self.schedule_ready_tasks()` so the Controller immediately re-claims its own dropped task (`preferred_agent_id == selected.id`) from its Git WIP checkpoint and resumes coding!

---

## Step 3: Update Host Website Swarm Terminal UI (`dashboard.html` & `dashboard.js`)

1. **`funguys/host/dashboard.html`:**
   - Update the login input placeholder/label so teammates know they can sign in with either the **Admin Token or Worker Token**.
   - Update the section heading and helper text in `#chat-form` to clarify that `/btw` commands typed in the terminal are triaged by the Main Controller and streamed non-destructively to active workers.
2. **`funguys/host/dashboard.js`:**
   - Keep `fillSelect('chat-agent', [['auto', 'Auto: Main Controller C2 / Swarm Terminal'], ...allAgents])` as the default selection.

---

## Step 4: PowerShell/Bash Worker Console `/btw` Listener & CLI Command (`runtime.py` & `cli.py`)

1. **`funguys/worker/runtime.py`:**
   - Add a `console_input_loop(self)` daemon thread in `WorkerRuntime` (only active when `sys.stdin` is a TTY / interactive console) or a helper method `send_btw(self, message)` that posts `{'question': message, 'preferred_agent_id': 'auto'}` to `/api/chat/ask`.
   - If a teammate types `/btw <note>` in their running worker console, `WorkerRuntime` sends it to `/api/chat/ask` and prints the Controller's response right in the terminal!
2. **`funguys/cli.py`:**
   - Add `@app.command('btw')` (`uv run funguys btw "<message>" --host-url ... --token ...`) so any teammate can also send a `/btw` command from any terminal window.

---

## Step 5: Add `fg_btw` Tool to MCP Bridge (`funguys/mcp_bridge/server.py`)

1. Add `fg_btw` to `MCP_TOOLS` in `funguys/mcp_bridge/server.py` with `message` (required string).
2. In `MCPWorkerServer.handle_call_tool()`, handle `name == 'fg_btw'`:
   - Prepend `/btw ` if the message does not already start with `/btw`.
   - Call `self._request('POST', '/api/chat/ask', {'question': question, 'preferred_agent_id': 'auto'})` and return the response.

---

## Step 6: Verification & New Automated Tests (`tests/test_c2_controller_and_mcp.py`)

Add `test_network_btw_routes_to_controller_and_resumes_tasks` in `tests/test_c2_controller_and_mcp.py`:
1. Register 2 agents:
   - Agent 1 (`Claude-Ctrl`, `claude-opus`, Controller) working on Task 1.
   - Agent 2 (`Gemini-Worker`, `gemini-argon-flash`, Worker) working on Task 2.
2. Have Agent 2 authenticate using its `worker_token` (testing network teammate access) and send `POST /api/chat/ask` with `{"question": "/btw ensure rate limiter uses sliding window", "preferred_agent_id": "auto"}`.
3. Verify:
   - The request succeeds (`200 OK`) using the worker's credential.
   - The `/btw` is answered by `Claude-Ctrl` (the Main Controller).
   - `Gemini-Worker` was **never interrupted** (remains `working` on Task 2) and received the steering note in its prompt queue and Task 2's `handoff_digest`.
   - `Claude-Ctrl` checkpointed Task 1 during triage and **automatically reclaimed Task 1** (`status == 'working'`, `current_task_id == task1_id`) immediately after `/btw` completed!
4. Run full `uv run pytest -q` and confirm all tests pass.
