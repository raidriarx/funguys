# 🍄 FunGuys — Multiplayer, Usage-Aware Multi-AI Engineering Platform

**FunGuys** is a real-time, multi-agent engineering platform that pools your and your friends' existing AI subscriptions (**Claude Code Pro/Max**, **Google Gemini CLI**, **OpenAI Codex CLI**, plus local/fallback API models) into a unified, self-testing software development swarm.

---

## ✨ Key Architecture Highlights

### 1. Hub & Spoke Multi-Node System (`funguys host` & Zero-Install Workers)
- **Central Host PC / Mission Control:** Runs the master repository, SQLite state store, the **Graphify AST Knowledge Graph**, and the **3-Layer Autonomous QA Pipeline**.
- **Zero-Install Remote Worker Nodes:** Teammates or secondary PCs join with **one single command** in Windows PowerShell:
  ```powershell
  irm "https://<HOST_TUNNEL_URL>/join.ps1" | iex
  ```
  *(No Python, `uv`, or project repository setup required on joining nodes!)*
- **Interactive Onboarding:** The worker script auto-detects installed AI CLIs (`codex`, `gemini`, `claude`), lets the user choose their engine, and prompts for their custom machine code name.

### 2. Real-Time Dynamic Tunnels & Click-to-Copy Links
- Built-in support for **Cloudflare Quick Tunnels** and **ngrok**.
- The Web Mission Control Dashboard dynamically detects the active public tunnel URL on startup and displays a click-to-copy join command right in the header banner.
- Works across different Wi-Fi networks and the public internet.

### 3. Contract-First Planning + Zero-Token AST Knowledge Graph
- **"Approve the Plan, Autonomous on the Code":** Review and approve `CONTRACTS.md` on the Web Mission Control Dashboard before agents execute tasks.
- **Zero-Token AST Mapping:** Instead of wasting context windows chatting back and forth, FunGuys compiles `.graphify/GRAPH_REPORT.md` and broadcasts compact **~100-token AST Contract Deltas** only when shared signatures change.

### 4. Role-Based Tier Routing + 80% Soft-Quota "Baton Pass"
- Routes architecture, contracts, and complex root-cause debugging to **Tier-1 AIs** (Claude Code Max/Pro, OpenAI Codex Pro) with the most remaining quota headroom.
- Routes UI, CRUD, tests, and documentation to **Tier-2 AIs** (Gemini CLI, Flash, fallback APIs).
- When a teammate's AI hits **80% window quota** or a rate limit mid-task, FunGuys automatically commits a `wip/<task_id>` branch, compiles a `HANDOFF.md` + Graphify subgraph digest, and performs an automated Baton Pass to the next available teammate's AI.

### 5. 3-Layer Autonomous QA + Smart Circuit Breaker
- Every task submission is verified on the Host PC:
  1. **Layer 1: Static AST & Syntax Checks**
  2. **Layer 2: Automated Pytest Suite**
  3. **Layer 3: Headless Playwright Browser & DOM Inspection** with visual snapshots.
- **Smart Escalation & Circuit Breaker:** Retries up to 2 times on the original AI, escalates to a Tier-1 Debugger with Graphify call-path context on attempt 3, and trips the Circuit Breaker after 3 failures to prevent infinite loops.

---

## 🚀 Quickstart

### 1. Install & Setup Host (Main Machine)
Requirements: Python 3.11+ and [uv](https://github.com/astral-sh/uv).

```bash
git clone https://github.com/raidriarx/funguys.git
cd funguys
uv sync
```

### 2. Start the Host Server & Mission Control Dashboard
```bash
uv run funguys host --port 8420
```
Open **http://localhost:8420** in your browser to view the Mission Control Dashboard.

To launch with an automatic public tunnel for remote teammates across different Wi-Fi networks:
```bash
uv run funguys host --port 8420 --tunnel
```

### 3. Connect Teammates / Remote Nodes (Zero Setup)
On any Windows machine, open PowerShell and paste the command displayed on your dashboard:
```powershell
irm "https://<YOUR_HOST_URL>/join.ps1" | iex
```

### 4. Run the Multiplayer Simulation Demo
Experience Tiered Routing, AST Contract Deltas, an 80% Baton Pass, and QA Bug Auto-Fixing:
```bash
uv run funguys simulate
```

### 5. Run the Automated Test Suite
```bash
uv run pytest -v
```

---

## 📄 License
MIT License. Built for multi-AI collaborative development.
