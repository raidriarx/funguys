"""Comprehensive tests for Main Controller C2 primitives, MCP stdio server, dual-bucket quotas, and work-importance routing."""

import asyncio
import json
import pytest
from pathlib import Path
from starlette.testclient import TestClient

from funguys.config import MODEL_ROUTING_POLICY, PLANNER_PROVIDER_PRIORITY, SUBSCRIPTION_PROFILES
from funguys.host.app import create_host_app
from funguys.host.execution import LaptopKeepAwake
from funguys.host.git_c2 import GitC2Sync
from funguys.mcp_bridge.server import MCP_TOOLS, MCPWorkerServer
from funguys.models import AgentNode, AgentStatus, BlackboardEntry, PromptQueueItem, SignalOp, TaskItem, TaskStatus
from funguys.protocol import (
    classify_task_role,
    compress_error_log,
    parse_agent_signals,
    parse_raw_signals,
    select_model_and_effort,
    slice_contract_for_task,
)


def test_zero_waste_signal_parsing():
    raw_text = """
    Starting work on task...
    @FG:CLAIM src/auth.py
    @FG:FACT auth_scheme JWT_RS256
    @FG:TRIPWIRE src/db.py migration pending
    @FG:TRIGGER task-2-frontend agent-worker-1
    @FG:STEER agent-worker-2 prioritize caching layer
    @FG:HOLD task-3-tests
    @FG:GO task-3-tests
    @FG:VETO task-1-contracts retry with stricter validation
    @FG:RELEASE src/auth.py
    Done!
    """
    signals = parse_agent_signals("agent-ctrl", raw_text)
    ops = [s.op for s in signals]
    assert ops == [
        SignalOp.CLAIM,
        SignalOp.FACT,
        SignalOp.TRIPWIRE,
        SignalOp.TRIGGER,
        SignalOp.STEER,
        SignalOp.HOLD,
        SignalOp.GO,
        SignalOp.VETO,
        SignalOp.RELEASE,
    ]
    claim_sig = signals[0]
    assert claim_sig.sender_agent_id == "agent-ctrl"
    assert claim_sig.target == "src/auth.py"

    fact_sig = signals[1]
    assert fact_sig.target == "auth_scheme"
    assert fact_sig.payload == "JWT_RS256"


def test_contract_slicing_and_error_compression():
    full_contracts = """
# CONTRACTS.md

## Database Architecture
Database connection details and pool settings.

## Auth Module
def login(username, password) -> Token
def verify_token(token) -> User

## Payment Gateway
def charge(amount) -> Receipt
"""
    # Contract slicing should return only relevant sections when targeting Auth
    sliced = slice_contract_for_task(full_contracts, ["src/auth.py", "auth/service.py"], ["login", "verify_token"])
    assert "Auth Module" in sliced
    assert "login" in sliced
    assert "Payment Gateway" not in sliced

    # Error log compression strips ANSI codes and deduplicates identical stack traces
    raw_error = (
        "\x1b[31mError\x1b[0m: Connection refused at 0x7fff56a1b2c0\n"
        "Traceback (most recent call last):\n"
        "  File 'server.py', line 42, in connect at 0x7fff56a1b2c0\n"
        "  File 'server.py', line 42, in connect at 0x7fff56a1b2c0\n"
        "AssertionError: Expected 200, got 500\n"
    )
    compressed = compress_error_log(raw_error)
    assert "\x1b[31m" not in compressed
    assert "0x7fff56a1b2c0" not in compressed
    assert "AssertionError: Expected 200, got 500" in compressed


def test_work_importance_classification_and_model_routing():
    # 1. Tier 1 task -> important_coding
    t1 = TaskItem(
        id="task-arch-core",
        title="Core Engine Architecture",
        description="Implement core consensus engine",
        complexity_tier=1,
        target_files=["src/core.py"],
    )
    assert classify_task_role(t1) == "important_coding"

    # 2. Tier 2 non-test -> gruntwork
    t2 = TaskItem(
        id="task-ui-scaffold",
        title="UI Boilerplate",
        description="Scaffold HTML and CSS styles",
        complexity_tier=2,
        target_files=["frontend/index.html"],
    )
    assert classify_task_role(t2) == "gruntwork"

    # 3. Test tasks -> testing
    t3 = TaskItem(
        id="task-unit-tests",
        title="Unit Test Suite",
        description="Run test suite",
        complexity_tier=2,
        target_files=["tests/test_core.py"],
        require_tests=True,
    )
    assert classify_task_role(t3) == "testing"

    # Model and reasoning effort policy verification
    # Gemini
    assert select_model_and_effort("gemini", "planning") == ("argon", "medium")
    assert select_model_and_effort("gemini", "important_coding", high_tier_exhausted=False) == ("argon", "low")
    assert select_model_and_effort("gemini", "important_coding", high_tier_exhausted=True) == ("flash", "high")
    assert select_model_and_effort("gemini", "gruntwork") == ("flash", "high")
    assert select_model_and_effort("gemini", "testing") == ("flash", "high")

    # Claude
    assert select_model_and_effort("claude", "planning") == ("opus", "high")
    assert select_model_and_effort("claude", "important_coding") == ("opus", "high")
    assert select_model_and_effort("claude", "gruntwork") == ("opus", "medium")
    assert select_model_and_effort("claude", "testing") == ("opus", "medium")

    # Codex / ChatGPT
    assert select_model_and_effort("codex", "planning") == ("astra", "low")
    assert select_model_and_effort("codex", "important_coding") == ("sol", "high")
    assert select_model_and_effort("codex", "gruntwork") == ("sol", "high")
    assert select_model_and_effort("codex", "testing") == ("sol", "high")


def test_main_controller_c2_api_and_blackboard(tmp_path):
    app = create_host_app(tmp_path, admin_token="test-admin-c2")
    client = TestClient(app, headers={"Authorization": "Bearer test-admin-c2"})

    # Register 2 agents: Claude (Tier 1) and Gemini (Tier 2)
    reg_claude = client.post(
        "/api/agents/register",
        json={
            "owner_name": "Alice",
            "machine_name": "macbook-claude",
            "profile_key": "claude-pro",
            "worker_id": "worker-claude-12345",
            "executor": "remote",
        },
    ).json()
    reg_gemini = client.post(
        "/api/agents/register",
        json={
            "owner_name": "Bob",
            "machine_name": "laptop-gemini",
            "profile_key": "gemini-cli",
            "worker_id": "worker-gemini-12345",
            "executor": "remote",
        },
    ).json()

    # Claude should be auto-elected as Main Controller based on PLANNER_PROVIDER_PRIORITY
    state = client.get("/api/state").json()
    claude_node = next(a for a in state["agents"] if a["id"] == reg_claude["id"])
    assert claude_node["is_controller"] is True

    # Emit Controller Signals via API (including dynamic ROLE= reclassification)
    sig_resp = client.post(
        "/api/controller/signal",
        json={
            "sender_agent_id": reg_claude["id"],
            "signals": [
                {"op": "CLAIM", "target": "backend/service.py", "payload": "locked for refactor"},
                {"op": "FACT", "target": "api_version", "payload": "v2"},
                {"op": "TRIPWIRE", "target": "tests/test_service.py", "payload": "needs mock store"},
                {"op": "STEER", "target": "task-2-frontend", "payload": "ROLE=important_coding Make UI state machine core priority"},
            ],
        },
    )
    assert sig_resp.status_code == 200
    bb = client.get("/api/blackboard").json()["blackboard"]
    bb_keys = {b["key"]: b for b in bb}
    assert "backend/service.py" in bb_keys
    assert bb_keys["backend/service.py"]["kind"] == "lock"
    assert "api_version" in bb_keys
    assert bb_keys["api_version"]["value"] == "v2"

    # Verify dynamic ROLE=important_coding reclassification updated task-2-frontend
    updated_state = client.get("/api/state").json()
    t2_state = next(t for t in updated_state["tasks"] if t["id"] == "task-2-frontend")
    assert t2_state["work_role"] == "important_coding"
    assert t2_state["task_role"] == "important_coding"
    assert t2_state["complexity_tier"] == 1

    # Verify subscription profile aliases exist
    for alias in ("claude-opus", "gemini-argon-flash", "chatgpt-astra-sol", "mcp-ide"):
        assert alias in SUBSCRIPTION_PROFILES

    # Controller live-steers Gemini
    steer_resp = client.post(
        f"/api/agents/{reg_gemini['id']}/steer",
        json={
            "prompt": "Use FastAPI async endpoints for high throughput",
            "mode": "steer",
            "sender_agent_id": reg_claude["id"],
        },
    )
    assert steer_resp.status_code == 200

    # Heartbeat from Gemini worker retrieves the queued steering prompt
    worker_token = reg_gemini["worker_token"]
    hb_resp = client.post(
        f"/api/agents/{reg_gemini['id']}/heartbeat",
        headers={"Authorization": f"Bearer {worker_token}"},
        json={},
    )
    assert hb_resp.status_code == 200
    queued = hb_resp.json().get("queued_prompts", [])
    assert len(queued) >= 1
    assert "Use FastAPI async endpoints" in queued[0]["prompt"]
    app.state.host_lock.release()


def test_mcp_worker_server_initialization_and_tools():
    server = MCPWorkerServer(host_url="http://127.0.0.1:8420", token="dummy-token", name="TestCursor")

    # 1. Initialize
    init_res = server.handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert init_res["result"]["serverInfo"]["name"] == "funguys-mcp-worker"

    # 2. List tools
    tools_res = server.handle_rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tool_names = [t["name"] for t in tools_res["result"]["tools"]]
    assert "fg_poll" in tool_names
    assert "fg_listen" in tool_names
    assert "fg_check_inbox" in tool_names
    assert "fg_signal" in tool_names
    assert "fg_dispute" in tool_names
    assert "fg_handoff" in tool_names
    assert "fg_steer" in tool_names
    assert "fg_submit" in tool_names


def test_keep_awake_and_git_c2_sync(tmp_path):
    # LaptopKeepAwake starts and stops cleanly without raising errors on any OS
    keep_awake = LaptopKeepAwake()
    keep_awake.start()
    keep_awake.stop()

    # GitC2Sync local checkpoint & beacon writing
    c2_sync = GitC2Sync(tmp_path)
    beacon = c2_sync.publish_beacon("http://127.0.0.1:8420", public_url="https://demo.trycloudflare.com", lan_ip="192.168.1.50")
    assert beacon["public_url"] == "https://demo.trycloudflare.com"
    assert (tmp_path / ".funguys" / "beacon.json").exists()

    snapshot = {"project": {"id": "test-p1", "name": "C2 Project"}, "tasks": []}
    checkpoint_file = c2_sync.write_local_checkpoint(snapshot, beacon=beacon)
    assert checkpoint_file is not None
    assert checkpoint_file.exists()
    loaded = c2_sync.fetch_checkpoint()
    assert loaded is not None
    assert loaded["project"]["name"] == "C2 Project"
    assert loaded["beacon"]["lan_ip"] == "192.168.1.50"


def test_feature_9_controller_drops_task_on_promotion_and_arbitration(tmp_path):
    app = create_host_app(tmp_path, admin_token="test-admin-f9")
    client = TestClient(app, headers={"Authorization": "Bearer test-admin-f9"})
    try:
        # Register Agent A (Claude Tier 1) and Agent B (Claude Tier 1)
        agent_a = client.post(
            "/api/agents/register",
            json={
                "owner_name": "Alex",
                "machine_name": "mac-claude-1",
                "profile_key": "claude-opus",
                "worker_id": "worker-f9-claude-1",
                "executor": "remote",
            },
        ).json()
        agent_b = client.post(
            "/api/agents/register",
            json={
                "owner_name": "Blair",
                "machine_name": "mac-claude-2",
                "profile_key": "claude-opus",
                "worker_id": "worker-f9-claude-2",
                "executor": "remote",
            },
        ).json()

        # Approve plan so task-1-contracts is assigned to one of the Tier-1 agents
        state0 = client.get("/api/state").json()
        approved = client.post("/api/project/approve", json={"plan_version": state0["project"]["plan_version"]}).json()
        t1 = next(t for t in approved["tasks"] if t["id"] == "task-1-contracts")
        assert t1["status"] == "in_progress"
        initial_owner = t1["assigned_agent_id"]
        other_agent = agent_b["id"] if initial_owner == agent_a["id"] else agent_a["id"]

        # Promote the currently working agent (`initial_owner`) to Main Controller
        promo = client.post(f"/api/agents/{initial_owner}/controller")
        assert promo.status_code == 200
        promo_agent = promo.json()
        # Feature #9: Promoting a working agent drops its coding task, leaves it IDLE as Controller,
        # and hands the task off to the other idle Tier-1 worker (`other_agent`)!
        assert promo_agent["is_controller"] is True
        assert promo_agent["status"] == "idle"
        assert promo_agent["current_task_id"] is None

        after_state = client.get("/api/state").json()
        t1_after = next(t for t in after_state["tasks"] if t["id"] == "task-1-contracts")
        assert t1_after["status"] == "in_progress"
        assert t1_after["assigned_agent_id"] == other_agent
        assert "Promoted to Main Controller Agent" in (t1_after.get("handoff_digest") or "")
    finally:
        app.state.host_lock.release()


def test_feature_9_controller_resumes_own_task_when_all_workers_busy(tmp_path, monkeypatch):
    from funguys.worker import adapters
    monkeypatch.setattr(
        adapters,
        "run_text_agent",
        lambda provider, prompt, *a, **kw: (
            "Binding Ruling: Backend must return JSON.\n"
            "@FG:FACT api_format json\n"
            "@FG:STEER task-1-contracts Ensure return payload is JSON dict\n"
        ),
    )
    app = create_host_app(tmp_path, admin_token="test-admin-f9-resume")
    client = TestClient(app, headers={"Authorization": "Bearer test-admin-f9-resume"})
    try:
        # Register only 1 Tier-1 Controller (Claude) and 1 Tier-2 Worker (Gemini, marked high quota / busy)
        ctrl = client.post(
            "/api/agents/register",
            json={
                "owner_name": "LeadClaude",
                "machine_name": "mac-controller",
                "profile_key": "claude-opus",
                "worker_id": "worker-ctrl-only-1",
                "executor": "remote",
            },
        ).json()
        worker2 = client.post(
            "/api/agents/register",
            json={
                "owner_name": "BusyGemini",
                "machine_name": "laptop-gemini",
                "profile_key": "gemini-argon-flash",
                "initial_used_pct": 95.0,  # In cooldown / busy so no other worker is idle
                "worker_id": "worker-busy-gemini-2",
                "executor": "remote",
            },
        ).json()

        state0 = client.get("/api/state").json()
        approved = client.post("/api/project/approve", json={"plan_version": state0["project"]["plan_version"]}).json()
        t1 = next(t for t in approved["tasks"] if t["id"] == "task-1-contracts")
        assert t1["assigned_agent_id"] == ctrl["id"]
        assert t1["status"] == "in_progress"

        # Worker 2 raises a @FG:DISPUTE while the Controller is coding task-1-contracts and all other workers are busy
        arb_resp = client.post(
            "/api/controller/arbitrate",
            json={
                "sender_agent_id": worker2["id"],
                "target": "backend/service.py",
                "reason": "Contract requires JSON dict return type, found tuple",
                "task_id": "task-1-contracts",
            },
        )
        assert arb_resp.status_code == 200, arb_resp.text
        arb_data = arb_resp.json()
        assert arb_data["controller_agent_id"] == ctrl["id"]
        assert arb_data["dropped_task_id"] == "task-1-contracts"
        assert "Binding Ruling" in arb_data["ruling"]

        # Verify Blackboard recorded both the dispute tripwire and the Controller's @FG:FACT ruling
        bb = {b["key"]: b for b in client.get("/api/blackboard").json()["blackboard"]}
        assert "dispute:backend/service.py" in bb
        assert bb.get("api_format", {}).get("value") == "json"

        # Verify Controller automatically resumed coding its own task-1-contracts afterwards since all other workers were busy!
        final_state = client.get("/api/state").json()
        t1_final = next(t for t in final_state["tasks"] if t["id"] == "task-1-contracts")
        assert t1_final["status"] == "in_progress"
        assert t1_final["assigned_agent_id"] == ctrl["id"]
        assert "Paused coding on task-1-contracts to arbitrate swarm dispute" in (t1_final.get("handoff_digest") or "")
    finally:
        app.state.host_lock.release()


def test_feature_4_on_demand_context_window_reset_and_targeted_handoff(tmp_path):
    app = create_host_app(tmp_path, admin_token="test-admin-f4")
    client = TestClient(app, headers={"Authorization": "Bearer test-admin-f4"})
    try:
        agent_a = client.post(
            "/api/agents/register",
            json={
                "owner_name": "CoderA",
                "machine_name": "node-a",
                "profile_key": "claude-opus",
                "worker_id": "worker-f4-node-a",
                "executor": "remote",
            },
        ).json()
        agent_b = client.post(
            "/api/agents/register",
            json={
                "owner_name": "CoderB",
                "machine_name": "node-b",
                "profile_key": "claude-opus",
                "worker_id": "worker-f4-node-b",
                "executor": "remote",
            },
        ).json()

        state0 = client.get("/api/state").json()
        approved = client.post("/api/project/approve", json={"plan_version": state0["project"]["plan_version"]}).json()
        t1 = next(t for t in approved["tasks"] if t["id"] == "task-1-contracts")
        first_id = t1["assigned_agent_id"]
        second_id = agent_b["id"] if first_id == agent_a["id"] else agent_a["id"]

        # Trigger on-demand context window reset & targeted handoff from first_id -> second_id with WIP files
        ho_resp = client.post(
            f"/api/agents/{first_id}/handoff",
            json={
                "target_agent_id": second_id,
                "handoff_notes": "Context window reset at 60k tokens; partial service stub committed.",
                "assignment_id": t1["assignment_id"],
                "wip_files": {"backend/service.py": "# WIP checkpoint from first agent\ndef create_item(title, priority=1):\n    return {'title': title}\n"},
                "reset_window": True,
            },
        )
        assert ho_resp.status_code == 200, ho_resp.text
        ho_data = ho_resp.json()
        assert ho_data["handoff"] == "task-1-contracts"
        # First agent must be IDLE (NOT locked in COOLDOWN) with fresh window
        assert ho_data["agent"]["status"] == "idle"
        assert ho_data["agent"]["used_pct"] == 0.0

        # Second agent must now own task-1-contracts with the Baton Pass handoff digest and WIP snapshot
        state1 = client.get("/api/state").json()
        t1_new = next(t for t in state1["tasks"] if t["id"] == "task-1-contracts")
        assert t1_new["status"] == "in_progress"
        assert t1_new["assigned_agent_id"] == second_id
        assert "partial service stub committed" in (t1_new.get("handoff_digest") or "")

        snap = client.get(
            "/api/workspace/snapshot",
            params={"task_id": "task-1-contracts", "assignment_id": t1_new["assignment_id"]},
        ).json()
        assert "WIP checkpoint from first agent" in snap["files"].get("backend/service.py", "")
    finally:
        app.state.host_lock.release()


def test_network_btw_routes_to_controller_and_resumes_tasks(tmp_path: Path, monkeypatch) -> None:
    """Verify any teammate on the network can sign in and send /btw, routing through the Main Controller while preserving both Worker and Controller tasks."""
    from funguys.worker import adapters

    app = create_host_app(tmp_path, admin_token="secret-admin-token")
    client = TestClient(app)
    try:
        assert client.post("/api/session", json={"token": "secret-admin-token"}).status_code == 200

        # Add a second parallel ready task so both Controller and Worker are actively coding different tasks
        add_resp = client.post(
            "/api/tasks",
            json={
                "id": "task-parallel-2",
                "title": "Parallel Rate Limiter",
                "description": "Implement sliding window rate limiter.",
                "phase": 1,
                "complexity_tier": 1,
                "target_files": ["backend/limiter.py"],
                "dependencies": [],
                "verification_commands": [],
            },
        )
        assert add_resp.status_code == 200, add_resp.text

        ctrl = client.post(
            "/api/agents/register",
            json={
                "owner_name": "LeadCtrl",
                "machine_name": "ctrl-node",
                "profile_key": "claude-opus",
                "worker_id": "worker-btw-ctrl",
                "executor": "remote",
                "is_controller": True,
            },
        ).json()
        worker = client.post(
            "/api/agents/register",
            json={
                "owner_name": "TeammateWorker",
                "machine_name": "worker-node",
                "profile_key": "gemini-argon-flash",
                "worker_id": "worker-btw-teammate",
                "executor": "remote",
            },
        ).json()
        worker_token = worker["worker_token"]

        state0 = client.get("/api/state").json()
        approved = client.post("/api/project/approve", json={"plan_version": state0["project"]["plan_version"]}).json()

        ctrl_after = next(a for a in approved["agents"] if a["id"] == ctrl["id"])
        worker_after = next(a for a in approved["agents"] if a["id"] == worker["id"])
        assert ctrl_after["status"] == "working"
        assert worker_after["status"] == "working"
        ctrl_task_id = ctrl_after["current_task_id"]
        worker_task_id = worker_after["current_task_id"]
        assert ctrl_task_id != worker_task_id

        # Mock run_text_agent so when Controller triages /btw, it also emits an explicit @FG:STEER to the worker
        def fake_consult(provider, prompt, **kwargs):
            return f"Acknowledged /btw guidance. Routing to active worker.\n@FG:STEER {worker['id']} Use sliding window counter in limiter"

        monkeypatch.setattr(adapters, "run_text_agent", fake_consult)

        # Teammate signs into website using their worker_token and sends /btw via the Swarm Terminal
        teammate_login = client.post("/api/session", json={"token": worker_token})
        assert teammate_login.status_code == 200
        assert teammate_login.json()["role"] == "worker"

        # Teammate can send /btw using their worker_token
        btw_resp = client.post(
            "/api/chat/ask",
            headers={"Authorization": f"Bearer {worker_token}"},
            json={"question": "/btw ensure rate limiter uses sliding window", "preferred_agent_id": "auto"},
        )
        assert btw_resp.status_code == 200, btw_resp.text
        btw_entry = btw_resp.json()

        # 1. Verify the Main Controller answered the /btw
        assert btw_entry["is_btw"] is True
        assert btw_entry["agent_id"] == ctrl["id"]
        assert btw_entry["target_agent_id"] == worker["id"]

        # 2. Verify neither the Worker nor the Controller lost their active task!
        state1 = client.get("/api/state", headers={"Authorization": "Bearer secret-admin-token"}).json()
        ctrl_final = next(a for a in state1["agents"] if a["id"] == ctrl["id"])
        worker_final = next(a for a in state1["agents"] if a["id"] == worker["id"])

        # Worker was never interrupted
        assert worker_final["status"] == "working"
        assert worker_final["current_task_id"] == worker_task_id

        # Controller dropped its task during triage and automatically reclaimed its own task afterward!
        assert ctrl_final["status"] == "working"
        assert ctrl_final["current_task_id"] == ctrl_task_id

        # 3. Verify Worker's task handoff_digest and prompt queue received the /btw guidance
        worker_task = next(t for t in state1["tasks"] if t["id"] == worker_task_id)
        assert "sliding window" in (worker_task.get("handoff_digest") or "")

        hb = client.post(
            f"/api/agents/{worker['id']}/heartbeat",
            headers={"Authorization": f"Bearer {worker_token}"},
            json={"assignment_id": worker_task["assignment_id"]},
        ).json()
        prompts = [p["prompt"] for p in hb.get("queued_prompts", [])]
        assert any("sliding window" in p for p in prompts)
    finally:
        app.state.host_lock.release()
