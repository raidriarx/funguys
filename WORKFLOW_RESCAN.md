# FunGuys workflow fixes and rescan

Reviewed 2026-10-18 after implementing the fixes from [WORKFLOW_AUDIT.md](WORKFLOW_AUDIT.md). That document describes the original application; its line numbers and behavior are historical.

The original failure paths have been addressed in code, with regression coverage for the critical trust, assignment, repository, and recovery boundaries. The follow-up review also found and fixed additional concurrency and lifecycle problems. This is a source review plus local integration testing, not certification of a live distributed deployment.

## Original findings

| # | Original problem | Result |
| --- | --- | --- |
| 1 | Unauthenticated administration and host code execution | Administrator login, scoped worker credentials, origin checks, local binding by default, explicit tunnels, filtered snapshots, and restricted Docker QA by default. Direct host execution requires an explicit trusted-execution setting. |
| 2 | Snapshot and empty-submission crashes | Consistent workspace property and authenticated endpoint coverage. Empty/no-change submissions return an explicit error instead of accessing a nonexistent property. |
| 3 | Failed changes committed to the canonical checkout | Separate assignment worktrees, checked Git operations, integration against current HEAD, and merge only after configured QA passes. Failed candidates remain separate. |
| 4 | Host and worker execute the same assignment | Explicit local/remote executor type and versioned leases. The host execution loop does not run remote assignments. |
| 5 | Unknown, stale, or replayed submissions accepted | Validate credentials, ownership, approval, project, plan, assignment, base revision, and lease. Stable submission receipts make identical retries idempotent and reject changed retries. |
| 6 | Bash JSON interpolation breaks normal responses | Shared standard-library Python runtime; launchers pass encoded JSON as data. Bash and PowerShell launchers receive syntax checks. |
| 7 | Assignments missed at registration/reconnect | Poll reconciliation, saved worker credentials, independent heartbeats, network retries, and stable submission IDs. A real HTTP integration test covers assignment during registration. |
| 8 | Deleted or disconnected workers strand tasks | Lease expiry requeues tasks; deleting an agent revokes credentials and requeues its active assignment. |
| 9 | Cooldown and quota never recover | Persisted quota windows, explicit reset handling, expiring cooldowns, and saved idle reports. Percentages are identified as reported values or estimates. |
| 10 | CLI failure loops or submits stale source | Execution failures block with actionable errors; timeout/output limits and rate-limit WIP handoff replace silent retries. No production synthesized-code fallback. |
| 11 | Browser failure reported as QA success | Explicit passed/failed/skipped/unavailable checks. Required checks fail closed; browser exceptions do not fall back to fabricated success. Retained candidates can be rechecked after environment repair. |
| 12 | Full snapshots overwrite concurrent changes | Changes against an explicit base revision, binary/deletion support, isolated worktrees, checked integration, and conflicts that leave canonical source intact. Worker prompts include QA and handoff context. |
| 13 | Project replacement races active execution | Immutable project/plan/assignment identities, pause/invalidation, serialized mutations, and replacement guards. Workspace preparation failure retains the previous active project. |
| 14 | Implicit plan approval bypasses | Explicit approval tied to the reviewed plan version. Contract changes invalidate that version; human claims and submissions also enforce approval/dependencies. |
| 15 | Shell injection and unsafe scratch paths | Encoded configuration, bounded identifiers, random private scratch directories, confined paths, and checksum-verified worker downloads. Task text is not interpolated into shell code. |
| 16 | Submissions can overwrite repository metadata | Shared path validation rejects metadata, private files, traversal, rooted/drive-relative paths, symlinks/junctions, reserved names, and case-colliding paths before applying a bundle. |
| 17 | Planner/worker fallback invents unrelated work | Provider failures surface explicitly. Production execution cannot use generic demo stubs; demo builders run only in isolated simulation. |
| 18 | Stale tunnel links and duplicate startup | CLI owns tunnel lifecycle, configured port and opt-out are honored, startup is bounded, and dead process URLs are not reused. Factory construction never starts a tunnel. |
| 19 | Invalid dependency graphs wait forever | Validate IDs, tiers, duplicate tasks, missing dependencies, self-dependencies, and cycles before persisting a plan. |
| 20 | Same-owner machines share identity | Persistent per-worker identity and credentials; owner names are display data. A worker process lock prevents duplicate use of one saved identity. |
| 21 | Provider routing and consensus claims are misleading | Exact selected local provider or explicit unavailable result. Remote interactive planning/chat and direct API fallback remain unsupported and are documented. Automated checks no longer claim model consensus. |
| 22 | Breaking contract changes go unnoticed | Persisted signature baseline, removed-symbol alerts, fuller Python signatures, conservative JS/TS fingerprints, and affected-worker invalidation with updated handoff context. This is not a full TypeScript type checker. |
| 23 | Human tasks have no recovery path | Claim, bundle download/upload, retry/reclaim, release, and retained-source QA controls. Human takeover invalidates existing coding ownership. |
| 24 | Slow work blocks requests; SQLite handles leak | Long CLI/QA work crosses thread boundaries, connection contexts close explicitly, state reads use cached graph data, traversal is pruned/bounded, and broadcasts are bounded. Short Git/SQLite operations still occur synchronously; load testing remains advisable. |
| 25 | Dashboard loses drafts and connections | Local runtime assets, socket reconnect and polling, dirty draft preservation, checked API errors, clipboard fallback, and state sequencing. Real Chromium coverage exercises the principal flow. |
| 26 | Simulation replaces active state | Separate simulation database and workspace; the live task board and agents are not reset. |
| 27 | Document fetch reaches internal services | Administrator-only access, public HTTPS/443 destinations, public address validation and connection pinning, redirect revalidation, and bounded download size/time. |

## Additional problems found and fixed during the rescan

- **Two hosts sharing one state directory:** acquire an OS file lock before application construction can modify state; release it on shutdown or startup failure. Tests cover rejection and restart.
- **Cancelled planning requests:** retain job/mutation ownership until the bounded CLI operation finishes and the authorized draft is installed. A disconnected HTTP client cannot free the planner for a concurrent replacement.
- **Quota reset during QA:** preserve the currently verifying assignment; finishing the old task cannot erase a newer assignment or discard an explicit cooldown deadline.
- **Project activation failure:** prepare the replacement workspace before changing active managers/state, so a Git/setup failure leaves the previous project usable.
- **Out-of-order dashboard updates:** host-instance and sequence identifiers prevent delayed responses from replacing newer state, including after a host restart.
- **Oversized CLI output:** drain continuously into a bounded tail buffer rather than an unbounded log allocation; timeout/cancellation terminates the process tree.
- **Snapshot preparation failure:** report an execution block instead of polling indefinitely with an assignment the worker cannot initialize.
- **Cross-platform path collisions and long Git paths:** reject case-equivalent bundle names and shorten managed worktree directory names. Extremely long user-selected workspace roots can still exceed Windows/Git limits.
- **Slow maintenance amplification:** refresh only changed agent records, limit scheduling batches, and avoid a full scheduling sweep on each heartbeat.

## Validation

Latest full run: **55 passed, 1 warning in 40.94 seconds**, using `.venv\Scripts\python.exe -m pytest -q`.

Coverage includes authentication and worker scope, one-use enrollment, stale/idempotent submissions, two-worker integration and merge conflicts, failed QA preserving canonical source, pause during QA, quota/reset/handoff, expiry/deletion, human recovery, project/plan version gates, host restart/locking, cancelled planning, snapshots and binary/deletion transport, private-file/path rejection, contract changes, SSRF/redirect rejection, and isolated simulation.

The suite also includes:

- A real Chromium dashboard session: login, state updates, unsaved contracts, approval, socket reconnect, pause, logout, and safe rendering of untrusted text.
- A real HTTP worker round trip with a deterministic fake CLI: registration, immediate assignment, snapshot, changed-file submission, QA, and Git merge. This verifies transport and orchestration; it does not authenticate a commercial model provider.
- PowerShell AST parsing and Bash syntax checking for generated launchers.
- Mocked Docker policy checks for restricted flags and filtered source copying, plus missing-environment behavior.
- Bounded-output and concurrency regressions for the additional findings above.

JavaScript syntax checking, Python source parsing, and `git diff --check` also passed. The one test warning concerns Starlette's deprecated httpx TestClient backend and does not fail the suite.

## Deployment checks and current limits

- Docker is not installed in this verification environment. The actual daemon/image/browser combination has **not** been exercised. Install Docker, build `docker/qa.Dockerfile`, and configure project dependencies in the QA image before using default QA. Missing prerequisites block verification; they do not pass.
- Real Claude/Codex/Gemini authentication, provider-specific sandbox behavior, live Cloudflare tunnels, and launchers on actual remote macOS/Linux/Windows machines have not been validated here.
- Workers now explicitly require Python 3.11+ and a logged-in selected CLI. They do not require a FunGuys package/repository installation.
- Remote interactive planning/chat and direct fallback API execution are not implemented. Local CLI planning/chat requires explicit trusted execution. Quota displays are not live billing integrations; review labels are not model consensus.
- Browser QA does not automatically boot arbitrary application backends or external services. Configure realistic verification commands, test/browser requirements, and a project-specific runtime image. Public network integration tests need a deliberately configured environment.
- Snapshot filtering excludes common credentials and runtime metadata, but cannot identify every secret embedded in ordinary source. Workers must be authorized to read the shared project. Trusted execution runs project code with the host user's permissions.
- Restart the host and replace legacy unauthenticated workers with fresh invitations. Existing data is retained; historical unverified commits are not automatically reverted. Review interrupted/legacy assignments and retained blocked worktrees before resuming.

See [STARTUP_GUIDE.md](STARTUP_GUIDE.md) for configuration and recovery steps.
