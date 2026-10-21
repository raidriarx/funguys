# FunGuys — Collaborative AI Swarm Engineering Platform

FunGuys coordinates coding tasks across teammates' installed Claude, ChatGPT/Codex, and Gemini CLIs, as well as desktop IDE agents (Cursor, Windsurf, Claude Desktop, VS Code). A host owns the task DAG, Git repository, and shared blackboard; authenticated workers execute versioned assignments across their own machines. Submitted changes are tested in isolated worktrees and merged only after 3-layer verification checks pass.

---

## Key Capabilities & Architecture

- **Main Controller C2 (Command & Control)**: Automatically elects a coordinator AI node (ordered by planning usage rate priority: Claude Opus High $\to$ Gemini Argon Medium $\to$ OpenAI Astra Low).
- **In-Band Chat Signaling**: Real-time 1-line agent coordination (`@FG:CLAIM`, `@FG:RELEASE`, `@FG:FACT`, `@FG:TRIPWIRE`, `@FG:STEER`, `@FG:TRIGGER`, `@FG:HOLD`, `@FG:GO`, `@FG:VETO`).
- **Work-Importance Model Routing**: Intelligently routes tasks by architectural importance (`planning`, `important_coding`, `gruntwork`, `testing`) rather than token volume, keeping high-tier reasoning quotas from being burned on boilerplate.
- **Dual-Bucket Quota Management**: Separates heavy reasoning turns (`opus:high`, `argon`, `astra`) from high-throughput workhorse turns (`flash:high`, `sol:high`, `opus:medium`), automatically falling back to workhorse tiers when quotas reach 85%.
- **Non-Destructive Live Steering & `/btw` Inboxes**: Mid-run directives and `/btw` notes stream in real-time into `.funguys_inbox.md` inside the worker's active workspace without crashing or resetting progress.
- **Desktop IDE Integration (stdio MCP)**: Connect Cursor, Windsurf, Claude Desktop, or VS Code into the swarm via standard Model Context Protocol tools (`fg_poll`, `fg_listen`, `fg_check_inbox`, `fg_signal`, `fg_steer`, `fg_submit`).
- **Serverless GitHub State Sync**: Persists the project DAG, blackboard, and discovery beacons to an orphan `refs/heads/fg-c2` branch so remote nodes discover and sync state without a paid cloud server.
- **Laptop Keep-Awake**: Built-in sleep prevention for Windows (`SetThreadExecutionState`), macOS (`caffeinate`), and Linux (`systemd-inhibit`) to ensure long builds run uninterrupted.
- **Token Efficiency**: Automated `CONTRACTS.md` slicing for target files and ANSI/pointer log scrubbing for minimal LLM token consumption.

---

## Canonical Model Routing & Subscription Profiles

| Canonical Profile Key | Provider | Planning Model | Core/Important Coding | Gruntwork Scaffolding | Testing Suite | Planning Priority |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **`claude-opus`** | Anthropic | `opus` (high) | `opus` (high) | `opus` (medium) | `opus` (medium) | **Rank 0** (Highest usage headroom) |
| **`gemini-argon-flash`** | Google | `argon` (medium) | `argon` (low) | `flash` (high) | `flash` (high) *(never lower)* | **Rank 1** |
| **`chatgpt-astra-sol`** | OpenAI / Codex | `astra` (low) | `sol` (high) | `sol` (high) | `sol` (high) | **Rank 2** |
| **`mcp-ide`** | IDE Stdio MCP | — | Assigned via Host | Assigned via Host | Assigned via Host | **Rank 1** |
| **`api-fallback`** | Local / Offline | Mock | Scripted | Scripted | Scripted | **Rank 3** |

*(Legacy aliases `claude-pro`, `claude-max`, `gemini-cli`, `codex-pro`, and `ide-mcp` automatically resolve to these canonical profiles.)*

---

## Start the Host

Requirements: Python 3.11+, Git, and [uv](https://docs.astral.sh/uv/).

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
Workers only require Python 3.11+ and their installed CLI; they do not need this repo cloned.

### 2. Desktop IDE Workers (Cursor / Claude Desktop / Windsurf / VS Code)
Add the FunGuys MCP server to your editor configuration (e.g. `claude_desktop_config.json` or Cursor MCP settings):
```json
{
  "mcpServers": {
    "funguys": {
      "command": "python",
      "args": ["-m", "funguys.cli", "mcp-worker", "--host-url", "http://127.0.0.1:8420", "--token", "<WORKER_OR_ADMIN_TOKEN>", "--name", "Cursor-IDE"]
    }
  }
}
```
Available IDE tools:
- `fg_poll` & `fg_listen`: Poll or wait for the next assigned task, contracts slice, and blackboard state.
- `fg_check_inbox`: Inspect queued Controller steering directives and `/btw` notes.
- `fg_signal`: Send C2 signals (`@FG:CLAIM`, `FACT`, `TRIPWIRE`, `TRIGGER`, `STEER`, `HOLD`, `GO`, `VETO`).
- `fg_steer`: Send directives or interrupts to other agents.
- `fg_submit`: Submit file edits directly into the 3-layer automated QA pipeline.

---

## Planning, Steering & Execution

1. **Lead Architect Interview**: Start an interactive interview in Mission Control. The elected leader AI grills the requirements, scans any existing workspace code, and compiles a comprehensive `CONTRACTS.md` and multi-phase Task DAG.
2. **Review & Approve**: Review the interface contracts and tasks. Explicitly approve the plan version to unlock worker scheduling.
3. **In-Flight Guidance (`/btw`)**: Type `/btw <guidance>` in chat or send an `@FG:STEER` signal to guide active workers. Guidance appends to `.funguys_inbox.md` without killing the active process.
4. **Automated 3-Layer QA Verification**: Submitted code is tested inside isolated Git worktrees (`funguys-qa:local` Docker container or host sandbox with `FUNGUYS_TRUSTED_EXECUTION=1`). Merges only proceed when all checks pass.

---

## Testing & Quality Assurance

Run the test suite:

```powershell
uv run pytest -v
```

All 63 automated tests cover bootstrap syntax, browser dashboard integration, Main Controller C2 primitives, MCP stdio dispatch, Git C2 sync, concurrency resilience, and end-to-end hackathon simulation.

---

## License

MIT.
