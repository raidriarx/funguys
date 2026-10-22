# FunGuys — Collaborative AI Swarm Engineering Platform

FunGuys coordinates coding tasks across teammates' installed Claude, ChatGPT/Codex, and Gemini CLIs, as well as desktop IDE agents (Cursor, Windsurf, Claude Desktop, VS Code). A central host owns the task DAG, Git repository, and shared blackboard; authenticated workers execute versioned assignments across their own machines. Submitted changes are tested in isolated worktrees and merged only after 3-layer verification checks pass.

> 📖 **Looking for the complete manual?** See [READMETOO.md](READMETOO.md) for the full feature reference, API specifications, and troubleshooting steps.

---

## Key Capabilities & Architecture

- **Player-Coach 5-Agent Swarm**: Operates a 5-seat agent team where the Main Controller can actively code when supervisory demands are low, yet immediately checkpoints and drops its task mid-work to arbitrate worker disputes or handle planning.
- **Main Controller C2 (Command & Control)**: Automatically prioritizes a coordinator AI node (ordered by planning usage headroom: Claude Opus High $\to$ Gemini Argon Medium $\to$ OpenAI Astra Low $\to$ Desktop MCP IDE).
- **In-Band Chat Signaling**: Real-time 1-line agent coordination (`@FG:CLAIM`, `@FG:RELEASE`, `@FG:FACT`, `@FG:TRIPWIRE`, `@FG:STEER`, `@FG:TRIGGER`, `@FG:HOLD`, `@FG:GO`, `@FG:VETO`, `@FG:DISPUTE`, `@FG:HANDOFF`).
- **Dispute Escalation & Arbitration**: Workers hit by blockers or contradictory contracts trigger `@FG:DISPUTE` (or `fg_dispute` in MCP). The Controller receives an arbitration diagnostic snapshot, provides steering directives, and automatically reclaims its own dropped task if all other workers are busy.
- **On-Demand Context Handoff**: Agents can reset context windows or transfer tasks on demand (`@FG:HANDOFF` / `POST /api/agents/{id}/handoff`) without being locked in a 60-minute quota cooldown.
- **Work-Importance Model Routing**: Routes tasks by architectural importance (`planning`, `important_coding`, `gruntwork`, `testing`) rather than token volume, preserving top-tier reasoning quotas.
- **Dual-Bucket Quota Management**: Separates heavy reasoning turns (`opus:high`, `argon`, `astra`) from high-throughput workhorse turns (`flash:high`, `sol:high`, `opus:medium`), automatically falling back when quotas exceed 85%.
- **Non-Destructive Live Steering & `/btw` Inboxes**: Mid-run directives and `/btw` notes stream in real-time into `.funguys_inbox.md` inside the worker's active workspace without crashing or resetting progress.
- **Desktop IDE Integration (stdio MCP)**: Connect Cursor, Windsurf, Claude Desktop, or VS Code into the swarm via standard Model Context Protocol tools (`fg_get_assignment`, `fg_heartbeat`, `fg_submit_task`, `fg_dispute`, `fg_handoff`, `fg_blackboard`, `fg_graphify`).
- **Serverless GitHub State Sync**: Persists the project DAG, blackboard, and discovery beacons to an orphan `refs/heads/fg-c2` branch so remote nodes sync state without paid cloud infrastructure.
- **Laptop Keep-Awake**: Built-in sleep prevention for Windows (`SetThreadExecutionState`), macOS (`caffeinate`), and Linux (`systemd-inhibit`) to ensure long builds run uninterrupted.
- **Token Efficiency**: Automated `CONTRACTS.md` slicing for target files and ANSI/pointer log scrubbing for minimal LLM token consumption.

---

## Canonical Model Routing & Subscription Profiles

| Canonical Profile Key | Provider | Planning Model | Core/Important Coding | Gruntwork Scaffolding | Testing Suite | Planning Priority |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **`mcp-ide`** | IDE Stdio MCP | — | Assigned via Host | Assigned via Host | Assigned via Host | **Rank 1** (Top) |
| **`claude-opus`** | Anthropic | `opus` (high) | `opus` (high) | `opus` (medium) | `opus` (medium) | **Rank 2** (Highest usage headroom) |
| **`gemini-argon-flash`** | Google | `argon` (medium) | `argon` (low) | `flash` (high) | `flash` (high) *(never lower)* | **Rank 3** |
| **`chatgpt-astra-sol`** | OpenAI / Codex | `astra` (low) | `sol` (high) | `sol` (high) | `sol` (high) | **Rank 4** |
| **`api-fallback`** | Local / Offline | Mock | Scripted | Scripted | Scripted | **Rank 5** |

*(Legacy aliases `claude-pro`, `claude-max`, `gemini-cli`, `codex-pro`, and `ide-mcp` automatically resolve to these canonical profiles.)*

---

## Quickstart: Start the Host

**Requirements:** Python 3.11+, Git, and [uv](https://docs.astral.sh/uv/).

```powershell
uv sync
uv run funguys host
```

Open **http://localhost:8420** in your browser. Sign in using the administrator token saved in `.funguys/admin_token`.

To allow teammates or remote workers on your local network or the internet to join:

```powershell
# For LAN teammates on the same Wi-Fi:
uv run funguys host --host 0.0.0.0

# For internet access via Cloudflare Tunnel:
uv run funguys host --tunnel
```

---

## Connecting Workers

### 1. Remote CLI Workers (Windows / macOS / Linux)
In Mission Control, click **Workers & Invites** $\to$ **Invite a worker**, choose the subscription profile, and copy the one-line join command:
- **Windows (PowerShell)**:
  ```powershell
  irm "http://<host-ip>:8420/join.ps1?token=<token>&profile=gemini-argon-flash" | iex
  ```
- **macOS / Linux (Bash)**:
  ```bash
  curl --fail -sSL "http://<host-ip>:8420/join.sh?token=<token>&profile=claude-opus" | bash
  ```
Workers only require Python 3.11+ and their installed CLI; they do not need this repository cloned.

### 2. Desktop IDE Workers (Cursor / Claude Desktop / Windsurf / VS Code)
Add the FunGuys MCP server to your editor configuration (e.g. `claude_desktop_config.json` or Cursor MCP settings):
```json
{
  "mcpServers": {
    "funguys": {
      "command": "uv",
      "args": [
        "--directory", "C:/path/to/Gaper Bingzoid",
        "run", "funguys", "mcp-worker",
        "--host-url", "http://127.0.0.1:8420",
        "--token", "<WORKER_OR_ADMIN_TOKEN>",
        "--name", "Cursor-IDE"
      ]
    }
  }
}
```
Available IDE tools:
- `fg_get_assignment`: Claim the next assignment, contract slice, and blackboard state.
- `fg_heartbeat`: Send progress, status, and streaming terminal logs.
- `fg_submit_task`: Submit completed code directly into the 3-layer automated QA pipeline.
- `fg_dispute`: Escalate blockers or contract contradictions to the Controller.
- `fg_handoff`: Request on-demand context reset or task delegation.
- `fg_blackboard`: Read or post to the shared architectural blackboard.
- `fg_graphify`: Query repository AST and symbol cross-references.

---

## Planning, Steering & Execution

1. **Lead Architect Interview ("Grill-Me")**: Start an interactive interview in Mission Control. The elected leader AI grills the requirements, scans any existing workspace code, and compiles a comprehensive `CONTRACTS.md` and multi-phase Task DAG.
2. **Review & Approve**: Review interface contracts and tasks. Explicitly approve the plan version to unlock worker scheduling.
3. **In-Flight Guidance (`/btw`)**: Type `/btw <guidance>` in chat or send an `@FG:STEER` signal to guide active workers. Guidance appends to `.funguys_inbox.md` without killing the active process.
4. **Automated 3-Layer QA Verification**: Submitted code is tested inside isolated Git worktrees (`funguys-qa:local` Docker container or host sandbox with `FUNGUYS_TRUSTED_EXECUTION=1`). Merges only proceed when all checks pass.

---

## Testing & Quality Assurance

Run the complete test suite:

```powershell
uv run pytest -v
```

All 66 automated tests cover bootstrap syntax, browser dashboard integration, Main Controller C2 primitives, MCP stdio dispatch, Git C2 sync, concurrency resilience, Model A Player-Coach mid-work task handoffs, dispute arbitration, and end-to-end hackathon simulation.

---

## Documentation Links

- [READMETOO.md](READMETOO.md) — Comprehensive user guide, architecture breakdown, API table, and feature manual.
- [STARTUP_GUIDE.md](STARTUP_GUIDE.md) — Detailed host and worker startup, recovery, and lock troubleshooting.
- [PLAN_HUMAN.md](PLAN_HUMAN.md) — Human-readable multi-agent swarm architecture plan.
- [PLAN_AI_AGENT.md](PLAN_AI_AGENT.md) — Step-by-step technical implementation specifications.

---

## License

MIT.
