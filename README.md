# FunGuys — collaborative AI engineering workflow

FunGuys coordinates coding tasks across teammates' installed Claude, Codex and Gemini CLIs. A host owns the task plan and Git repository; authenticated workers execute versioned assignments on their own machines. Submitted changes are tested in isolated worktrees and merged only after the configured checks pass.

## Start the host

Requirements: Python 3.11+, Git, and [uv](https://docs.astral.sh/uv/).

```powershell
uv sync
uv run funguys host
```

Open http://localhost:8420. Sign in with the token in `.funguys/admin_token` on the host. The default bind address is loopback. For teammates, explicitly enable one of:

```powershell
uv run funguys host --tunnel
uv run funguys host --host 0.0.0.0
```

`--tunnel` requires an installed `cloudflared`. Startup has a deadline; failure leaves the local dashboard available. `--no-tunnel` never creates a tunnel. Use HTTPS for connections outside a trusted LAN.

## Connect a worker

Create a short-lived invitation in Mission Control, select the worker's installed CLI profile, then copy the Windows or Mac/Linux command. Both launchers run the same Python worker. Workers require Python 3.11+ and a logged-in CLI; they do not require this repository or the FunGuys package. A worker credential is saved under the worker account's `.funguys/workers` directory for reconnects. Each invitation enrolls one machine and expires after one hour.

Workers poll for existing assignments, send heartbeats during execution, submit only changed files, and support binary files and deletions. A disconnected worker's assignment expires; late results cannot overwrite its replacement. Removing a worker revokes its credential and requeues active work.

## Plans and execution

1. Create/activate a project, or start an interview with an idle **local** architect.
2. Compile the plan, or add tasks manually. Unknown dependencies, duplicate IDs and cycles are rejected.
3. Review contracts and task verification requirements. Approve that specific plan version.
4. Connected workers execute their own assignments. The host executes only agents explicitly registered as local executors.
5. QA checks syntax/JSON, supplied verification commands, available Python/Node tests/builds, and browser targets. Reports distinguish **passed**, **failed**, **skipped**, and **unavailable** checks.

Interview answers never approve a plan automatically. Pause before replacing a project or modifying the task plan. Human teammates can claim tasks, download an assignment JSON bundle, submit edited files, retry, and release tasks.

Local planning/chat currently use the selected host CLI. Remote interactive architect/chat jobs and direct fallback APIs are not implemented; the application reports this explicitly. It does not silently substitute another provider. Structural review events describe automated checks; no model consensus is claimed.

## Configure QA

Generated tests and commands use a restricted Docker container by default. Install Docker and build the supplied QA image:

```powershell
docker build -t funguys-qa:local -f docker/qa.Dockerfile .
```

The container has no network, no host credentials or Docker socket, a read-only root filesystem, and CPU/memory/process limits. It receives a filtered copy of the assignment. The image contains Python, pytest, Playwright/Chromium, Node and npm. Add your project's dependencies to a derived image and set `FUNGUYS_QA_IMAGE` when needed. Network-dependent integrations and live external previews need a deliberately configured verification environment; unavailable checks do not pass.

For a project you trust, you can explicitly permit QA and local CLI execution directly on the host:

```powershell
$env:FUNGUYS_TRUSTED_EXECUTION = '1'
uv run funguys host
```

This opt-in runs project code with your user account's permissions. Leave it unset for untrusted projects. Builder CLIs retain their provider permission/sandbox controls; FunGuys does not pass permission-bypass flags. Selected CLIs may require their own sandbox prerequisites or interactive approvals.

If QA is blocked by missing prerequisites, fix the environment and click **Recheck retained code in QA**. This verifies the already-submitted implementation without spending another builder run. Failed checks retry up to the task limit; environment failures are blocked separately. Resolve conflicts or reset after review rather than repeatedly overwriting main.

## Inspection and tests

```powershell
uv run funguys graph . --query compute_metrics
uv run funguys simulate
uv run pytest -q
```

Simulation uses a disposable project under `.funguys/simulations`; it does not replace the active task board or agent registry. Builders are simulated and QA outcomes remain real, including missing-environment failures.

Quota percentages are reported values or turn-count estimates, not live subscription billing data. Window expiry, explicit reset, soft thresholds and rate-limit output support handoff/recovery. The graph tracks Python public signatures and conservatively fingerprints JavaScript/TypeScript exports; it is not a complete TypeScript type checker.

See [STARTUP_GUIDE.md](STARTUP_GUIDE.md) for setup, recovery and migration notes, and [WORKFLOW_RESCAN.md](WORKFLOW_RESCAN.md) for the audit follow-up.

## License

MIT.