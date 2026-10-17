"""End-to-end automated tests for FunGuys Multi-AI Orchestration Platform."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from funguys.host.app import create_host_app


def test_plan_approval_gate_and_role_based_tier_routing(tmp_path: Path) -> None:
    app = create_host_app(tmp_path)
    client = TestClient(app)

    # Register Friend A (Claude Max - Tier 1, 50% used) and Friend B (Gemini CLI - Tier 2, 10% used)
    r1 = client.post(
        "/api/agents/register",
        json={
            "owner_name": "Alex",
            "machine_name": "alex-pc",
            "profile_key": "claude-max",
            "initial_used_pct": 50.0,
        },
    )
    assert r1.status_code == 200
    alex_agent = r1.json()

    r2 = client.post(
        "/api/agents/register",
        json={
            "owner_name": "Sam",
            "machine_name": "sam-laptop",
            "profile_key": "gemini-cli",
            "initial_used_pct": 10.0,
        },
    )
    assert r2.status_code == 200

    # Before plan approval, no tasks should be IN_PROGRESS
    state_before = client.get("/api/state").json()
    assert state_before["project"]["plan_approved"] is False
    assert all(t["status"] != "in_progress" for t in state_before["tasks"])

    # Approve Plan -> `task-1-contracts` (Tier 1) should be routed to Alex's Claude Max (Tier 1)
    approve_res = client.post("/api/project/approve", json={}).json()
    assert approve_res["project"]["plan_approved"] is True
    t1 = next(t for t in approve_res["tasks"] if t["id"] == "task-1-contracts")
    assert t1["status"] == "in_progress"
    assert t1["assigned_agent_id"] == alex_agent["id"]


def test_baton_pass_handoff_and_full_hackathon_simulation(tmp_path: Path) -> None:
    app = create_host_app(tmp_path)
    client = TestClient(app)

    sim_res = client.post("/api/simulate/hackathon")
    assert sim_res.status_code == 200
    data = sim_res.json()

    # All 3 tasks should be COMPLETED after Baton Pass + QA Bug Auto-Fix
    for task in data["tasks"]:
        assert task["status"] == "completed", f"Task {task['id']} did not complete: {task}"

    # Verify `task-3-tests` went through 1 failed QA attempt and passed on retry!
    t3 = next(t for t in data["tasks"] if t["id"] == "task-3-tests")
    assert t3["retry_count"] == 1

    # Verify Graphify built the AST knowledge graph and emitted Contract Alerts
    assert data["graph"]["node_count"] >= 5
    assert len(data["contract_alerts"]) >= 1

    # Test Graphify query, explain, and path endpoints
    q_res = client.post("/api/graphify/query", json={"query": "compute_metrics"}).json()
    assert "compute_metrics" in q_res["result"]

    exp_res = client.post("/api/graphify/explain", json={"query": "create_item"}).json()
    assert "create_item" in exp_res["result"]


def test_circuit_breaker_trips_after_three_failed_qa_attempts(tmp_path: Path) -> None:
    app = create_host_app(tmp_path)
    client = TestClient(app)

    agent = client.post(
        "/api/agents/register",
        json={
            "owner_name": "Jordan",
            "machine_name": "jordan-pc",
            "profile_key": "codex-pro",
            "initial_used_pct": 5.0,
        },
    ).json()
    client.post("/api/project/approve", json={})

    # Submit invalid Python syntax 3 times in a row to trigger the Smart Circuit Breaker
    for attempt in range(1, 4):
        res = client.post(
            "/api/tasks/task-1-contracts/submit",
            json={
                "agent_id": agent["id"],
                "files": {"backend/service.py": "def broken_syntax(:\n    pass\n"},
                "commit_message": f"bad attempt {attempt}",
            },
        ).json()

    state = client.get("/api/state").json()
    t1 = next(t for t in state["tasks"] if t["id"] == "task-1-contracts")
    assert t1["status"] == "blocked_circuit_breaker"
    assert t1["circuit_breaker_tripped"] is True
    assert t1["retry_count"] == 3

    # Human resets circuit breaker -> task becomes ready/in_progress again
    reset_res = client.post("/api/tasks/task-1-contracts/reset-circuit-breaker")
    assert reset_res.status_code == 200
    assert reset_res.json()["circuit_breaker_tripped"] is False
