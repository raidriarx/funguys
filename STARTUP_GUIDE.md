# FunGuys startup and recovery guide

## Host prerequisites

- Python 3.11+, Git and uv.
- Docker for the default generated-code QA boundary.
- `cloudflared` only if using `--tunnel`.
- A supported, logged-in CLI on each machine that executes assignments.

Run `uv sync`, then build the QA image:

```powershell
docker build -t funguys-qa:local -f docker/qa.Dockerfile .
uv run funguys host
```

The dashboard is http://localhost:8420. Read `.funguys/admin_token` locally and use it to sign in. Do not distribute the administrator token to workers; create worker invitations instead.

Only one host may own a state directory. A second process is rejected before it can alter shared state; stop the earlier host before restarting. Do not run separate hosts against the same canonical project repository.

The host defaults to `127.0.0.1`. For a public HTTPS tunnel, run `uv run funguys host --tunnel`. For an explicitly shared LAN listener, run `uv run funguys host --host 0.0.0.0`. Use `--port` to change the listener port; the owned tunnel targets the same configured port. App construction, tests and simulation never start tunnels. A failed tunnel starts neither a persistent stale URL nor an unbounded startup wait.

## Worker onboarding

1. Install Python 3.11+ and log in to the CLI selected in the invitation.
2. In Mission Control, enter the owner, machine code name and subscription profile, then create an invitation.
3. Run the generated PowerShell or Bash command on the worker machine. It downloads a checksum-verified standard-library Python worker and passes configuration as encoded JSON.
4. Keep the worker process running. It resumes assignments by polling, and sends heartbeats independently of the coding subprocess.

No FunGuys repository or package installation is needed on the worker. The previous claim of requiring no Python runtime has been removed. Missing Python or CLI dependencies are reported rather than replaced by synthesized task files.

Worker identity and credentials are stored per host and code name under `~/.funguys/workers`. Reuse the same code name to reconnect. Separate machines may have the same owner name without sharing identity. Only one process may use a saved worker identity at a time. Removing a worker in the dashboard revokes its token; to enroll again, obtain a new invitation and remove that worker's old identity JSON file (or use a new code name).

If the FunGuys package is already installed on a worker, the equivalent CLI is:

```powershell
uv run funguys join https://YOUR-HOST --name Alex --codename alex-laptop --profile codex-pro --token INVITATION_TOKEN
```

`FUNGUYS_WORKER_TOKEN` is supported instead of `--token`. Subsequent reconnects can use the saved credential. Profiles are selected explicitly; an unavailable Claude/Gemini CLI is not replaced by Codex. Windows npm batch shims are resolved to their Node entry points so task text is never interpolated into a batch shell.

## Planning

You can define tasks manually without a planning CLI. Choose a project/workspace, add tasks and their dependencies, configure verification commands and test/browser requirements, review contracts, and approve the current plan version.

For AI interviews/chat, register a local executor for the desired provider and explicitly enable trusted local execution before starting the host:

```powershell
$env:FUNGUYS_TRUSTED_EXECUTION = '1'
uv run funguys host
```

Use this only for projects you trust. It permits project commands/QA and host CLI activity under your user account. Without it, remote coding workers remain available and QA uses Docker. Local interview/chat currently cannot be delegated to a remote machine; selection returns an explicit unavailable message instead of falsely attributing host Codex execution to a remote provider.

A new project without a directory gets a unique workspace. Continuation mode reuses the current or explicitly selected existing workspace. Interview answers do not execute tasks. Compilation produces a reviewable plan and approval applies to that exact version. Pause before project replacement; ongoing verification must finish or be invalidated before state can be replaced.

## QA and project dependencies

The default image is `funguys-qa:local`; override it with `FUNGUYS_QA_IMAGE`. It includes pytest and Chromium but not every project's Python/Node dependencies. Install those dependencies in a derived QA image ahead of time. Runtime containers have no network, so automatic dependency downloads and external integration calls will fail rather than escape the execution boundary.

Static checks parse Python and JSON and require target files to exist. JavaScript receives a Node syntax check. Existing pytest suites and package build/test scripts are run; other stacks need explicit verification commands. Mark tasks as requiring tests/browser verification when omission must prevent completion. Reports show skipped checks honestly and never replace a missing browser with a fabricated success badge.

For example, a derived Python QA image can contain:

```dockerfile
FROM funguys-qa:local
USER root
RUN pip install --no-cache-dir fastapi httpx pydantic
USER 65534:65534
```

Build it, then set `FUNGUYS_QA_IMAGE` to that tag. For Node projects, prepare dependencies in your image at `/node_modules` and add `/node_modules/.bin` to PATH so the mounted `/workspace` can resolve them. Build/test commands and project-specific integration services should match the real deployment environment.

## Recovery controls

- **Environment blocked:** fix missing Docker/image/CLI/test dependencies. Use **Recheck retained code in QA** when an implementation was submitted already, or **Reset after review** when execution itself failed.
- **QA failure:** the failed implementation remains on a task commit outside main. Subsequent attempts receive the failing QA log and retained source. Three failed checks trip the default circuit breaker; reset explicitly after review.
- **Merge conflict:** main keeps the earlier verified result. Reset the conflicted task to build from the latest source, or claim it as a human and resolve against the updated contracts.
- **Worker disconnect:** the lease expires after 90 seconds without heartbeats. Work requeues with a new assignment ID; old submissions are rejected. Reconnect using the saved worker identity.
- **Rate limit/quota threshold:** the worker submits available WIP, hands off the assignment, and enters cooldown. Report a fresh quota value/reset when your window resets. Expired cooldowns/windows restore eligibility; dashboard percentages remain estimates unless reported by a user/provider.
- **Human work:** claim a task, download its JSON bundle, edit its `files` map, and submit the edited JSON. `binary_files` contains base64 data; `deleted_files` contains individual relative paths. Reclaim after a failed human attempt to prepare a fresh retry, or release to workers. Canonical workspace edits made outside this flow must be committed/stashed before a merge.
- **Host restart during QA:** the task is blocked for review, never assumed complete. Recheck the retained candidate or reset it.

Successful worktrees and superseded retry worktrees are removed through Git after they are no longer needed. Blocked worktrees and isolated simulations are retained for inspection. Review disk usage before retaining many large simulations. Windows Git may reject extremely long repository paths; use a short workspace location if Git reports a path-length error.

## Data boundaries

Only authenticated administrators can change projects, plans, agents or human tasks. Worker credentials can access their own assignment, filtered snapshot, quota, heartbeat, failure and submission routes. Invitations expire in one hour and enroll one worker. Administrator sessions are HTTP-only, same-site cookies; cross-origin requests and unauthorized sockets are rejected.

Snapshots and submissions reject traversal, rooted/drive-relative paths, symlinks/junctions, reserved Windows names, Git/runtime metadata, common credential files, `.env` files and key material. Limits are 2 MB per file, 20 MB per bundle and 2,000 files. These filters do not detect every possible secret embedded in ordinary source; share projects only with teammates authorized to read that source.

Document fetching accepts public HTTPS targets, pins the resolved public address for the TLS connection, revalidates redirects, and limits the response size/download duration. Private/local network services cannot be fetched through the documentation endpoint.

## Updating an existing installation

Restart the running host after updating source. Existing project data is retained. Legacy in-progress assignments without version/lease metadata and interrupted QA are blocked for review. Old unauthenticated worker scripts must be replaced with fresh invitations. New administrator authentication is required even on the local dashboard.

The dashboard now uses local HTML/CSS/JavaScript assets and works without React/Babel/CDN availability. It reconnects sockets, polls for recovery, preserves dirty contract drafts and displays server errors. LAN clipboard copying has a manual-selection fallback.

Run `uv run pytest -q` for regression coverage. The suite includes a real Chromium dashboard test and real HTTP worker transport with a deterministic fake CLI; live provider authentication, real remote operating systems and the Docker daemon/image still need checks in your deployment environment.
