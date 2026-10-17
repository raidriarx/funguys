# 🍄 FunGuys — Complete Startup & Deployment Guide

This guide walks you step-by-step through setting up, configuring, and operating the **FunGuys** Multi-AI Collaborative Engineering Platform.

Whether you're running on a single laptop, coordinating with friends on the same Wi-Fi, or collaborating across the internet with teammates in different cities, follow the steps below.

---

## 🏗️ Architecture Overview: Hub & Spoke

FunGuys is built around a **Zero-Friction Hub & Spoke** model:

```
                      ┌────────────────────────────────────────┐
                      │        CENTRAL HOST PC / LAPTOP        │
                      │  - Master Git Repository & Branches    │
                      │  - SQLite State Store & Task Board     │
                      │  - Graphify AST Knowledge Graph        │
                      │  - 3-Layer QA (AST, Pytest, Playwright)│
                      │  - Real-Time Web Mission Control       │
                      └──────────────────┬─────────────────────┘
                                         │
                 ┌───────────────────────┼───────────────────────┐
                 │ (Public Tunnel / LAN) │ (Public Tunnel / LAN) │
                 ▼                       ▼                       ▼
      ┌─────────────────────┐ ┌─────────────────────┐ ┌─────────────────────┐
      │   WORKER NODE 1     │ │    WORKER NODE 2    │ │    WORKER NODE 3    │
      │   (Alex's Laptop)   │ │    (Jordan's PC)    │ │    (Sam's MacBook)  │
      │  Runs: Claude Code  │ │  Runs: Codex CLI    │ │  Runs: Gemini CLI   │
      │  Install: ZERO      │ │  Install: ZERO      │ │  Install: ZERO      │
      └─────────────────────┘ └─────────────────────┘ └─────────────────────┘
```

- **Host Machine (Mission Control):** The only machine that needs the repository and Python environment installed.
- **Worker Nodes (Teammates):** **Zero installation required.** Workers connect simply by running a single PowerShell command (`join.ps1`) which leverages their existing AI subscriptions (`codex`, `gemini`, `claude`).

---

## 🖥️ Part 1: Host Machine Setup (Primary Node)

### 1. System Requirements
- **OS:** Windows 10/11, macOS, or Linux.
- **Python:** Version 3.11 or newer.
- **Package Manager:** [`uv`](https://github.com/astral-sh/uv) (recommended for 10x faster startup and deterministic environments).

If you don't have `uv` installed, install it in seconds:
```powershell
# Windows (PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### 2. Clone the Repository & Install Dependencies
```bash
git clone https://github.com/raidriarx/funguys.git
cd funguys
uv sync
```

### 3. (Optional) Install Cloudflared for Automatic Public Tunnels
If you plan to collaborate with remote teammates who are **not on your local Wi-Fi**, install `cloudflared` on the host:
```powershell
# Windows
winget install Cloudflare.cloudflared

# macOS
brew install cloudflare/cloudflare/cloudflared
```

### 4. Launch the Host Server
Run the host server from the repository root:
```bash
# Standard local run (localhost & LAN):
uv run funguys host --port 8420

# With automatic public internet tunnel for remote friends:
uv run funguys host --port 8420 --tunnel
```

When started, your terminal displays your connection endpoints:
```
╭──────────────────────────── FunGuys Host Server ─────────────────────────────╮
│ [FunGuys] Central Host PC Online!                                            │
│                                                                              │
│ * Mission Control Dashboard: http://localhost:8420                           │
│ * LAN Dashboard / Join URL:  http://192.168.1.47:8420                        │
│ * Public Tunnel URL:         https://<subdomain>.trycloudflare.com           │
│                                                                              │
│ Teammate Join Command:                                                       │
│   irm "https://<subdomain>.trycloudflare.com/join.ps1" | iex                 │
╰──────────────────────────────────────────────────────────────────────────────╯
```

---

## 🌐 Part 2: Accessing Mission Control

### From the Host Machine:
Open your browser and navigate to:
👉 **`http://localhost:8420`**

### From Any Remote Machine or Mobile Device:
Navigate directly to your public tunnel URL:
👉 **`https://<subdomain>.trycloudflare.com`**
*(Or on the same local network: `http://<HOST_LAN_IP>:8420`)*

The dashboard provides a real-time terminal UI with:
- **Active AI Subscriptions & Quota meters**
- **Contract Approval Bar (`[ APPROVE_PLAN_&_UNLOCK_AIS ]`)**
- **Dynamic Quick Join Banner with one-click copy**
- **Interactive Kanban Task Board**
- **Live Graphify AST Inspector & 3-Layer QA Logs**

---

## 💻 Part 3: Connecting Worker Nodes (Teammates)

Joining a FunGuys session requires **zero project installation, zero python setup, and zero account logins** on the joining PC.

### Step 1: Open Terminal on the Worker PC
On any Windows machine, open **Windows PowerShell** or **Windows Terminal**.

### Step 2: Run the 1-Line Quick Join Command
Copy the command shown in the Host's Mission Control header:
```powershell
irm "https://<HOST_TUNNEL_URL>/join.ps1" | iex
```
*(If on the same local Wi-Fi network, you can also use `irm "http://<HOST_LAN_IP>:8420/join.ps1" | iex`)*

### Step 3: Interactive Onboarding Flow
The script will run through an automated 3-step setup in the terminal:

1. **AI CLI Detection:** Scans the machine's `PATH` for installed developer tools:
   ```
   [1/3] Scanning local environment for AI CLI engines...
     [+] FOUND: OpenAI Codex CLI (C:\...\codex.cmd)
     [+] FOUND: Google Gemini CLI (C:\...\gemini.cmd)
   ```
2. **Model Selection:** Presents detected engines. Press `1`, `2`, etc., to choose which AI model you want to contribute to the swarm.
3. **Machine Identity:** Prompts for your machine Code Name (defaults to your Windows computer name, e.g. `alex-laptop`).

Once confirmed:
```
==========================================================
 [CONNECTED] Successfully registered as Alex's OpenAI Codex CLI (Plus/Pro)!
 Agent ID: agent_b82a... | Machine: alex-laptop
==========================================================
Waiting for task assignments from Host Mission Control...
```
The node is now active in the Mission Control dashboard and ready to take on development tasks!

---

## 📋 Part 4: The FunGuys Development Workflow

### Phase 1: Interactive Alignment (Grill Interview)
1. On the Mission Control Dashboard, open the **Grill / Interview Tab** or terminal CLI.
2. The AI Architect conducts a focused interview with the team to identify requirements, tech stack preferences, and architectural boundaries.
3. The Architect distills the session into **`CONTRACTS.md`** (API signatures, data models, component interfaces).

### Phase 2: "Approve the Plan, Autonomous on the Code"
1. Review the generated `CONTRACTS.md` in the **04:CONTRACTS_MD** tab.
2. Click **`[ APPROVE_PLAN_&_UNLOCK_AIS ]`** at the top of the dashboard.
3. Once approved, the orchestrator breaks down the architecture into discrete tasks and schedules them to the connected AI nodes.

### Phase 3: Autonomous Multi-AI Execution
- **Tier-1 Routing:** Core contracts, architecture, and complex logic are routed to **Claude Code Max/Pro** and **Codex Pro** nodes.
- **Tier-2 Routing:** UI components, tests, and documentation are routed to **Gemini CLI** and fallback models.
- **Real-Time Task Polling:** Worker nodes automatically pull workspace snapshots, run local AI execution silently, and submit the generated files back to the host.

### Phase 4: 80% Quota Soft-Limit "Baton Pass"
- If a teammate's AI reaches **80% of its subscription usage window** or gets rate-limited mid-task:
  1. The worker node automatically saves its work-in-progress on a `wip/<task_id>` branch.
  2. FunGuys generates a compact `HANDOFF.md` containing the AST subgraph context.
  3. The task is seamlessly reassigned to the next available teammate node with remaining quota.

### Phase 5: 3-Layer Autonomous QA Pipeline
Every task submission undergoes three layers of automated verification on the Host PC:
1. **Layer 1: Static AST & Syntax Checks:** Verifies code parseability and interface contract conformance.
2. **Layer 2: Automated Pytest Suite:** Runs targeted unit and integration tests.
3. **Layer 3: Playwright Headless Browser:** Inspects DOM components and captures visual layout snapshots stored under `/screenshots`.

If any QA step fails:
- FunGuys auto-retries the task with the error feedback.
- If it fails twice, it auto-escalates to a Tier-1 debugging model.
- If it fails three times, the **Circuit Breaker** trips to protect team resources and requests human review.

---

## 🛠️ Part 5: Running the Built-In Simulation

To test the entire workflow locally without external nodes, run the end-to-end hackathon simulation:
```bash
uv run funguys simulate
```
Or click the **`[ RUN_DEMO_SIMULATION ]`** button directly in the Mission Control web dashboard. This triggers a full multi-agent simulation demonstrating:
- Multi-AI subscription registration (Claude Max, Codex Pro, Gemini).
- Automated plan compilation & contract generation.
- Tiered task routing.
- Mid-task 80% quota Baton Pass.
- Layer-2 Pytest bug catch & automated self-healing retry.

---

## ❓ Troubleshooting & FAQs

### Q: Why does the join command say "Unable to connect to remote server"?
- **Remote / Separate Wi-Fi:** Ensure the Host was started with the `--tunnel` flag (`uv run funguys host --tunnel`) and that the joining node is using the `https://...trycloudflare.com` URL.
- **Same Wi-Fi (LAN):** Ensure Windows Firewall on the Host allows incoming connections on port 8420:
  ```powershell
  # Run as Administrator in PowerShell on the Host PC:
  New-NetFirewallRule -DisplayName "FunGuys 8420" -Direction Inbound -LocalPort 8420 -Protocol TCP -Action Allow
  ```

### Q: Does the Cloudflare tunnel link change?
- The tunnel URL remains open and stable for the entire duration the Host server is running.
- If the Host server restarts or reboots, Cloudflare will generate a new subdomain. The dashboard header updates automatically—simply copy the updated command.

### Q: Can I contribute an AI if I only have API keys, not CLI tools?
- Yes! The interactive join script offers the **API Direct Fallback** profile option, allowing nodes without CLI tools installed to contribute via direct API keys or local LLM endpoints.

---

## 📜 Commands Quick Reference

| Action | Command |
| :--- | :--- |
| **Start Host (Local Only)** | `uv run funguys host --port 8420` |
| **Start Host (Public Tunnel)** | `uv run funguys host --port 8420 --tunnel` |
| **Worker Quick Join** | `irm "https://<HOST_URL>/join.ps1" \| iex` |
| **Run CLI Simulation** | `uv run funguys simulate` |
| **Run Test Suite** | `uv run pytest -v` |
