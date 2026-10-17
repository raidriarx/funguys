"""Pydantic models for FunGuys Host, Worker Nodes, Tasks, Contracts, Quotas, and QA."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class TaskStatus(str, Enum):
    PENDING = "pending"
    READY = "ready"
    IN_PROGRESS = "in_progress"
    HANDOFF_PENDING = "handoff_pending"
    IN_QA = "in_qa"
    COMPLETED = "completed"
    BLOCKED_CIRCUIT_BREAKER = "blocked_circuit_breaker"


class AgentStatus(str, Enum):
    IDLE = "idle"
    WORKING = "working"
    COOLDOWN = "cooldown"
    OFFLINE = "offline"


class ProjectState(BaseModel):
    id: str = "default-project"
    name: str = "Hackathon Project"
    description: str = ""
    workspace_path: str = ""
    github_url: Optional[str] = None
    plan_approved: bool = False
    lead_architect_agent_id: Optional[str] = None
    contracts_markdown: str = ""
    live_preview_url: Optional[str] = None
    created_at: str = Field(default_factory=utc_now_iso)


class TaskItem(BaseModel):
    id: str
    title: str
    description: str
    phase: int = 1
    complexity_tier: int = 2  # 1 = Architect/Complex/Debug, 2 = Standard Builder/UI/Tests
    dependencies: List[str] = Field(default_factory=list)
    target_files: List[str] = Field(default_factory=list)
    exported_symbols: List[str] = Field(default_factory=list)
    verification_commands: List[str] = Field(default_factory=list)
    architect_review: Optional[str] = None
    status: TaskStatus = TaskStatus.PENDING
    assigned_agent_id: Optional[str] = None
    assigned_human: Optional[str] = None
    branch_name: str = ""
    retry_count: int = 0
    max_retries: int = 3
    circuit_breaker_tripped: bool = False
    handoff_digest: Optional[str] = None
    last_error: Optional[str] = None
    updated_at: str = Field(default_factory=utc_now_iso)


class AgentNode(BaseModel):
    id: str
    owner_name: str
    machine_name: str
    profile_key: str
    provider: str
    display_name: str
    tier: int = 2
    status: AgentStatus = AgentStatus.IDLE
    used_pct: float = 0.0
    turns_used: int = 0
    turns_limit: int = 100
    soft_limit_pct: float = 80.0
    cooldown_until: Optional[str] = None
    current_task_id: Optional[str] = None
    last_heartbeat: str = Field(default_factory=utc_now_iso)


class ContractAlert(BaseModel):
    id: str
    task_id: str
    agent_id: str
    symbol_name: str
    file_path: str
    change_type: str  # "modified_signature", "added_export", "removed_export"
    old_signature: str = ""
    new_signature: str = ""
    dependent_task_ids: List[str] = Field(default_factory=list)
    summary: str = ""
    created_at: str = Field(default_factory=utc_now_iso)


class QARunReport(BaseModel):
    id: str
    task_id: str
    attempt: int
    static_passed: bool
    unit_passed: bool
    playwright_passed: bool
    overall_passed: bool
    logs: str = ""
    screenshot_path: Optional[str] = None
    escalated_to_tier1: bool = False
    created_at: str = Field(default_factory=utc_now_iso)


class ActivityEvent(BaseModel):
    id: str
    timestamp: str = Field(default_factory=utc_now_iso)
    source: str
    event_type: str
    message: str
    metadata: Dict[str, Any] = Field(default_factory=dict)


class CreateProjectRequest(BaseModel):
    name: str
    description: str
    workspace_path: Optional[str] = None
    github_url: Optional[str] = None


class ApprovePlanRequest(BaseModel):
    contracts_markdown: Optional[str] = None


class RegisterAgentRequest(BaseModel):
    owner_name: str
    machine_name: str
    profile_key: str = "claude-pro"
    initial_used_pct: float = 0.0


class ClaimTaskHumanRequest(BaseModel):
    human_name: str


class ReportQuotaRequest(BaseModel):
    used_pct: float
    turns_delta: int = 1
    rate_limited: bool = False
    cooldown_minutes: Optional[int] = None
    handoff_notes: Optional[str] = None
