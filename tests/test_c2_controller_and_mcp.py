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
