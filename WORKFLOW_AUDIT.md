# FunGuys workflow audit

Reviewed 2026-10-18. Scope: all application modules, dashboard, worker scripts, persistence, CLI, tests, and startup documentation. This is a review; application source was not changed.

The highest risks are failed remote onboarding, duplicate execution, writes reaching the canonical repository before validation, and misleading completion signals. Resolve these before relying on unattended multiplayer execution.

## Evidence and limits

- The existing suite passed: **3 passed, 1 warning in 33.48 seconds**. These tests cover the seeded demo, tier selection, handoff simulation, and circuit breaker. They do not establish that real distributed execution works.
- Focused local probes confirmed the snapshot/empty-submit crashes, unknown-agent submission before approval, completed-task replay, expired cooldown remaining stuck, quota refusing a lower usage report, same-owner machine identity collision, deleted-agent task stranding, writable Git metadata, cyclic plan acceptance, missing contract alerts, and browser failure returning success.
- Git operations and QA were mocked in submission/state probes to avoid executing generated code or changing real repositories. Browser failure was injected. Findings based on those probes concern validation/control flow, not successful deployment.
- The new Mac/Linux join script and tunnel edits appeared during the review. They were included in the final source review and focused JSON/cache probes; the full suite result above predates those edits. No live remote CLI, tunnel, or browser session was used to validate them.
- A probe cleanup also encountered an open SQLite handle on Windows. Temporary probe directories were removed; existing project state was not changed.

## P0: Public-host exposure

### 1. Anyone reaching the server has administrative and code-execution capabilities

**Source:** `funguys/host/app.py:78`, `funguys/host/app.py:604`, `funguys/host/orchestrator.py:910`, `funguys/cli.py:38`.

There is no authentication or worker authorization on REST endpoints or WebSockets. The host binds to all interfaces and the current CLI enables tunneling by default. A reachable visitor can change projects/workspaces, approve plans, submit files, delete tasks/agents, and invoke chat. Chat invokes a host CLI with approvals and sandbox bypassed when that CLI is available. Uploaded Python tests and verification commands also execute on the host. Directory exclusions do not protect `.env` or similar secrets from workspace export once snapshot delivery is repaired.

**Fix:** Authenticate dashboard users; issue scoped worker credentials; authorize each task operation; bind locally by default; make public exposure explicit. Run generated code/commands in an isolated execution environment with deliberate access to secrets and host resources.

## P1: Correctness and workflow blockers

### 2. Remote workspace snapshot and empty submissions crash

**Source:** `funguys/host/app.py:240`, `funguys/host/app.py:563`, `funguys/host/git_manager.py:14`.

The routes access `orchestrator.git.workspace_dir`; the manager only defines `workspace_path`. Both `/api/workspace/snapshot` and an empty submission reproduced an `AttributeError`. Remote scripts catch the snapshot error and continue building without the repository or contracts.

**Fix:** Use one workspace property everywhere and add endpoint coverage for real snapshot and empty-submission behavior.

### 3. Failed work is committed to the active repository before QA

**Source:** `funguys/host/git_manager.py:62`, `funguys/host/orchestrator.py:555`.

`apply_worker_changes()` writes directly into the shared checkout, stages everything, commits the current branch, then moves a task branch reference to HEAD. It never checks out a task branch or creates an isolated worktree. QA runs afterward and failure does not restore the checkout. The advertised merge after QA is only an event message. Another task's half-written files can also fail workspace-wide QA.

**Fix:** Build and verify each assignment in an isolated worktree; merge only a verified commit; preserve failed work separately. Check every Git result and avoid staging unrelated user changes.

### 4. The host and remote worker execute the same assignment

**Source:** `funguys/host/orchestrator.py:368`, `funguys/host/app.py:442`, `funguys/worker/node.py:114`, generated join scripts in `funguys/host/app.py`.

Approval starts a host loop that executes every assigned task, including assignments owned by remote machines. Remote workers independently execute those assignments. The host's late status check happens after its CLI has already modified the canonical checkout. There is no execution lease or explicit local/remote executor type.

**Fix:** Give each assignment exactly one executor, a lease, and a version. Only perform deliberate takeover after lease expiry.

### 5. Submissions do not validate assignment, status, approval, or freshness

**Source:** `funguys/host/orchestrator.py:536`.

Only task existence is required. A probe submitted work using a nonexistent agent while the plan was unapproved and received `completed` with mocked passing QA. A completed task also accepted a replay. Old workers can overwrite newer results after handoff; simultaneous submissions can each validate against mutable shared files.

**Fix:** Require an approved project, authenticated current owner, matching project/assignment version, and valid state transition. Reject stale/replayed submissions and make completion idempotent.

### 6. Mac/Linux workers cannot reliably parse normal host state

**Source:** `funguys/host/app.py:1075`, `funguys/host/app.py:1098`, `funguys/host/app.py:1112`.

The new Bash script interpolates response JSON into Python source using `json.loads('''$STATE_JSON''')`. Python interprets JSON backslash escapes before the JSON parser runs. Ordinary multiline `contracts_markdown` produces `JSONDecodeError: Invalid control character`; the polling parser suppresses the exception and yields no task. Windows paths and triple quotes create further failures.

**Fix:** Pass JSON through stdin or files and call `json.load()`. Pass scalar values through arguments/environment, never generated Python source. Check `python3` availability and HTTP status explicitly.

### 7. Python workers miss assignments and do not recover connections

**Source:** `funguys/worker/node.py:47`, `funguys/worker/node.py:114`, `funguys/host/app.py:753`.

Registration can assign a task before the worker connects its WebSocket. The socket's first message contains a state snapshot, but the worker only handles future `task_assigned` events. Reconnecting workers also ignore existing assignments. Socket/submit errors terminate the worker; there is no reconnect/resume loop. The synchronous CLI call also blocks that worker's event loop.

**Fix:** Reconcile assignments on registration and every reconnect, use a durable claim/poll mechanism, retry network operations with backoff, and offload CLI execution from the event loop.

### 8. Disconnecting or deleting a worker strands its task

**Source:** `funguys/host/app.py:502`, `funguys/host/orchestrator.py:290`, `funguys/models.py:83`.

Heartbeat timestamps are recorded during orchestration actions, but workers send no periodic heartbeat and no code expires assignments or marks dead workers offline. Deletion removes the agent without requeuing its task. A probe left a task `in_progress` with a nonexistent assigned agent.

**Fix:** Implement heartbeat leases, reconnect grace periods, and atomic reassignment when deleting or expiring a worker.

### 9. Cooldown and quota recovery never restore usable workers

**Source:** `funguys/host/orchestrator.py:271`, `funguys/host/orchestrator.py:459`, `funguys/host/orchestrator.py:588`.

No code processes `cooldown_until`. A past cooldown remains `cooldown`, and quota reports apply `max(previous_usage, reported_usage)`, preventing reset to a fresh quota window. Completion can place an agent into cooldown without setting an expiry. Re-registration preserves these fields. A report at the threshold while no task is active also falls through without saving the changed usage.

**Fix:** Track quota windows, accept explicit reset events, expire cooldowns, and save every valid report. Distinguish measured quota from turn-count estimates.

### 10. Host CLI failures can loop indefinitely or submit stale files

**Source:** `funguys/host/orchestrator.py:421`, `funguys/worker/adapters.py:84`.

The host loop receives `ok` and `rate_limited` but does not use them. When no files exist, it retries after a short sleep without consuming the QA retry budget or triggering handoff. When old targets exist, the adapter/host can submit those files even after CLI failure or timeout. This can spend quota indefinitely or mark unchanged work complete.

**Fix:** Separate execution failures from QA failures; honor rate limits; use bounded retry/backoff; require a meaningful change or explicit no-change result; never substitute old files for successful execution.

### 11. QA reports success when browser validation fails

**Source:** `funguys/host/qa_pipeline.py:222`.

Any browser exception, including navigation timeout or unavailable Chromium, enters a fallback that can return success. Without local HTML, even a supplied unreachable preview URL returns `True, "Verified cleanly."` This was reproduced with an injected browser failure. The HTML fallback only checks two literal error strings, so it cannot establish runtime correctness. Missing tests also count as passing, and non-Python builds are not checked unless an architect supplied commands.

**Fix:** Represent checks as passed/failed/skipped/unavailable. Required validation must fail closed; do not label skipped browser/tests as verified. Add stack-appropriate build and behavioral checks.

### 12. Snapshot uploads overwrite concurrent work and cannot represent deletions

**Source:** generated worker upload loops in `funguys/host/app.py:949`, `funguys/host/app.py:1152`; `funguys/host/git_manager.py:62`.

The scripts upload all files in their downloaded workspace, including unchanged files from an older snapshot. Two workers editing different modules can therefore overwrite each other's newer changes. Text-only serialization can corrupt binary assets, exclusions are inconsistent, and there is no deletion format or base revision for conflict detection. The PowerShell prompt also omits `last_error` and `handoff_digest`, so retries do not receive the specific QA failure they must fix.

**Fix:** Submit patches or commits against an explicit base revision, with binary/deletion support and conflict handling. Include handoff and QA context in every worker implementation.

### 13. Starting or recompiling a project can replace active state underneath execution

**Source:** `funguys/host/app.py:255`, `funguys/host/app.py:364`, `funguys/host/orchestrator.py:151`, `funguys/host/orchestrator.py:421`.

The singleton interview, project, workspace manager, graph, and task board can change while an earlier CLI thread is still running. Compilation clears/replaces tasks. Old and new plans can reuse task IDs; the host's post-run check only checks ID and status. A new interview without an explicit directory also reuses the previous project's workspace when compiled, rather than creating a clean project directory.

**Fix:** Give projects and plan revisions immutable identities, isolate their state, pause/cancel active execution during replacement, and reject old-generation results.

### 14. The review/approval gate has implicit bypasses

**Source:** `funguys/host/app.py:340`, `funguys/host/orchestrator.py:736`.

Interview answers containing phrases such as `start building`, or certain recommended answers after a compile hint, automatically compile, approve, and execute without showing the generated plan for review. Substring matching also catches negations such as "don't start building." Human claiming directly starts tasks regardless of plan approval or dependency state. Submission has its own approval bypass described above.

**Fix:** Make plan approval an explicit action tied to the reviewed plan version; validate approval and dependency gates at each state transition.

### 15. Generated join scripts allow code injection and unsafe scratch paths

**Source:** `funguys/host/app.py:792`, `funguys/host/app.py:1006`, `funguys/host/app.py:911`, `funguys/host/app.py:1120`.

Invite query values are inserted into executable shell strings with only whitespace normalization. A harmless probe confirmed that a PowerShell name containing `$(Write-Output AUDIT_MARKER)` is emitted unchanged inside an expandable string. Bash has the same problem and additionally interpolates names, responses, and task values into Python code. Unvalidated task IDs are used to construct directories that are recursively deleted; snapshot destinations are not checked for confinement.

**Fix:** Serialize data separately from executable code, validate identifiers, use fixed private temporary directories, and resolve/check every downloaded or deleted path against its intended root.

### 16. Submitted paths can write repository metadata

**Source:** `funguys/host/git_manager.py:75`.

The relative-path check does not exclude `.git`, `.funguys`, or other control files, nor resolve the destination to enforce confinement through symlinks or Windows path forms. A probe wrote `.git/audit-marker` through the submission writer. Overwriting real Git metadata can break or alter subsequent Git operations. Invalid paths are silently skipped rather than rejecting the submission.

**Fix:** Resolve destinations under a dedicated task root, reject rooted/drive-relative paths and control directories, and reject the entire invalid submission before writing anything.

### 17. Planner fallback and generic worker fallback fabricate unrelated work

**Source:** `funguys/host/architect.py:472`, `funguys/worker/adapters.py:188`, `funguys/worker/node.py:99`.

If planning fails, every domain receives the same tracking/calibration/desktop/gameplay/phone packaging plan. A normal Python worker defaults to synthesized demo code; for custom tasks it emits `run() -> True` or a minimal HTML fragment. Missing CLI/error paths can therefore replace real requirements with stubs that pass syntax-only QA.

**Fix:** Make simulation an explicit mode. On provider failure, expose a blocked/retryable state and retain the user's domain requirements; never silently substitute demo work for production execution.

### 18. Tunnel restart and configuration handling can leave dead invite links

**Source:** `funguys/host/tunnel.py:46`, `funguys/host/tunnel.py:97`, `funguys/host/app.py:85`, `funguys/cli.py:40`.

The current tunnel code trusts a persisted or cached URL without checking process liveness. A probe supplied an expired URL file: startup returned that URL with no process and never launched a replacement. The app factory independently launches a tunnel to hardcoded port 8420, ignoring CLI `--no-tunnel` and potentially exposing the wrong port. This also adds tunnel side effects to tests and `simulate`. Initial stdout reading has no wall-clock deadline, so startup can hang before the server begins listening.

**Fix:** Keep tunnel startup in one lifecycle owner, honor configured port/opt-out, validate cached URLs/process health, clear stale state on shutdown, and enforce a startup timeout.

## P2: Reliability and misleading status

### 19. Invalid task graphs are accepted and wait forever

**Source:** `funguys/host/architect.py:441`, `funguys/host/app.py:509`, `funguys/host/orchestrator.py:307`.

There is no validation for duplicate task IDs, missing dependencies, self-dependencies, cycles, or invalid tiers. A two-task cycle was accepted. Tasks then remain pending with no actionable blocked reason. Duplicate IDs overwrite rows through `INSERT OR REPLACE`.

**Fix:** Validate the full DAG before saving, enforce unique IDs and valid references, and show blocked reasons.

### 20. Agent identities collapse different machines owned by one person

**Source:** `funguys/host/orchestrator.py:231`.

Registration matches either machine name or owner name. Two machines with the same owner reproduced the same agent ID; the second registration changes the first agent's provider while preserving its task/status/quota. Reconnect also does not refresh all profile limits.

**Fix:** Use a persistent machine/worker ID plus a credential. Treat owner names as display data and update profile/window settings consistently.

### 21. Provider routing and consensus labels do not reflect real execution

**Source:** `funguys/host/architect.py:123`, `funguys/host/orchestrator.py:672`, `funguys/host/orchestrator.py:910`, `funguys/worker/adapters.py:112`.

Interview and chat execute host Codex regardless of the selected node's provider or location. Builder adapters can fall back from a missing selected CLI to host Codex. The architect/consensus review function only generates event strings and task metadata; it does not ask any model to review code, yet reports that all bots reviewed and aligned it.

**Fix:** Dispatch to the selected executor/provider or show a clear unavailable state. Label automated metadata checks honestly; require actual review responses before claiming model consensus.

### 22. Contract monitoring misses breaking API changes

**Source:** `funguys/host/graph_engine.py:94`, `funguys/host/graph_engine.py:287`.

There is no removed-symbol alert branch. Python argument annotations/defaults/keyword-only parameters and method signatures are absent from the contract representation; TypeScript interfaces include names rather than member shape. Both changing a Python parameter type/default and deleting the function produced zero alerts in probes. The signature baseline is in memory and is not restored on restart. Alerts are broadcast as events rather than injected into running workers' prompts.

**Fix:** Persist a verified baseline, model full public signatures, detect removals, and give affected workers an explicit reconciliation action.

### 23. Human tasks lack a complete retry/release workflow

**Source:** `funguys/host/orchestrator.py:736`, `funguys/host/orchestrator.py:625`, `funguys/host/orchestrator.py:324`.

A failed human submission retains `assigned_human` while becoming ready, so automatic scheduling skips it. This was reproduced. There is no dedicated dashboard completion, release, or human-submission flow, and claiming a running task does not cancel its existing AI execution.

**Fix:** Define human claim/release/submit transitions, retain exclusive ownership, and expose retry/release controls.

### 24. Server requests can block the entire event loop

**Source:** `funguys/host/qa_pipeline.py:28`, `funguys/host/qa_pipeline.py:109`, `funguys/host/app.py:185`, `funguys/db.py:28`.

Async routes call synchronous subprocess tests/builds, Git, SQLite, full workspace scans, graph construction, and tunnel discovery. A single QA command can block WebSockets and polling for tens of seconds. Every state fetch scans available workspaces, and event broadcasts make each dashboard fetch state again. SQLite connections are created for every operation without explicit closure; a Windows probe hit a still-open database file during cleanup.

**Fix:** Use a background job queue/thread boundary, explicit database connection lifetime, cached/incremental state, pruned directory traversal, and bounded broadcasts. Keep state reads cheap and side-effect free.

### 25. Dashboard reconnects and unsaved plan editing are fragile

**Source:** `funguys/host/dashboard.html:179`, `funguys/host/dashboard.html:197`, `funguys/host/dashboard.html:245`, `funguys/host/dashboard.html:7`.

The socket closes without reconnect or polling fallback. State updates overwrite the contract editor, losing unsaved changes when unrelated worker events arrive. Several actions do not check `res.ok`, so HTTP failures look like successful clicks. React/Babel and styles depend on external CDNs; without the required scripts, the local dashboard cannot render. Clipboard calls also lack a fallback for non-secure LAN origins.

**Fix:** Add reconnect/backoff and stale-state indication, preserve dirty edits, use shared request/error handling, bundle runtime assets, and provide a copy fallback.

### 26. Simulation can replace the active project

**Source:** `funguys/host/app.py:641`, `funguys/cli.py:152`.

The simulation endpoint reinitializes the singleton project and deletes registered agents in the active state store. Its dashboard action and CLI command do not use a separate demo database. Running a demo during real work discards the active task board/agent registry and can race existing execution.

**Fix:** Run simulations in disposable isolated state/workspaces, separate from the active project.

### 27. Document fetching can reach internal services

**Source:** `funguys/host/app.py:636`, `funguys/mcp_bridge/server.py:57`.

The public fetch-docs route accepts arbitrary URLs and follows redirects without destination restrictions or a response-size cap before parsing. Reachable callers can make requests from the host to local/private services and retrieve their text. Error and response status handling is also minimal.

**Fix:** Authenticate callers, constrain allowed destinations/schemes, validate redirects and resolved addresses, cap response size, and return explicit upstream errors.

## Recommended implementation order

1. **Establish trust and single ownership:** authentication, isolated execution, project/assignment versions, exclusive executor leases, stale-result rejection.
2. **Repair the remote round trip:** workspace property, Bash JSON transport, reconnect/resume, heartbeat recovery, diff-based submissions with QA/handoff context.
3. **Protect the repository and truthfulness of results:** isolated Git worktrees, checked Git operations, honest QA states, explicit provider failures, real review semantics.
4. **Make the workflow recoverable:** cooldown/window reset, bounded execution retries, validated DAGs, human release/retry, safe project switching, tunnel lifecycle.
5. **Harden operation:** cheap state reads, background jobs, explicit SQLite closure, dashboard reconnect/edit preservation, bundled assets, isolated simulation.

Minimum meaningful regression coverage should include two concurrent workers, reconnect after assignment, worker deletion/expiry, quota-window reset, stale submission after handoff, failed QA leaving main untouched, missing browser, multiline Bash state, stale tunnel cache, and project replacement during execution.
