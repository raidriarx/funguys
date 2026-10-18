"""Pydantic models for FunGuys Host, Worker Nodes, Tasks, Contracts, Quotas, and QA."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
import uuid
from typing import Any, Dict, List, Optional, Literal
from pydantic import BaseModel, Field, field_validator


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
    BLOCKED_ENVIRONMENT = "blocked_environment"


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
    plan_version: str = Field(default_factory=lambda: uuid.uuid4().hex)


class TaskItem(BaseModel):
    id: str
    title: str
    description: str
    phase: int = Field(default=1, ge=1)
    complexity_tier: Literal[1, 2] = 2
    dependencies: List[str] = Field(default_factory=list,max_length=100)
    target_files: List[str] = Field(default_factory=list,max_length=100)
    exported_symbols: List[str] = Field(default_factory=list)
    verification_commands: List[str] = Field(default_factory=list,max_length=20)
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
    assignment_id: Optional[str] = None
    project_id: Optional[str] = None
    plan_version: Optional[str] = None
    base_revision: Optional[str] = None
    candidate_revision: Optional[str] = None
    candidate_path: Optional[str] = None
    blocked_reason: Optional[str] = None
    submission_id: Optional[str] = None
    submission_digest: Optional[str] = None
    submitted_assignment_id: Optional[str] = None
    submitted_agent_id: Optional[str] = None
    submission_status: Optional[str] = None
    lease_expires_at: Optional[str] = None
    execution_failures: int = 0
    require_tests: bool = False
    require_browser: bool = False

    @field_validator('id')
    @classmethod
    def valid_id(cls, value: str) -> str:
        import re
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,95}', value):
            raise ValueError('Task IDs must contain only letters, numbers, underscores and hyphens')
        return value

    @field_validator('target_files')
    @classmethod
    def valid_files(cls, values: List[str]) -> List[str]:
        from funguys.files import validate_relative_path
        return [validate_relative_path(value) for value in values]


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
    worker_id: str = ""
    executor: Literal['remote', 'local'] = 'remote'
    credential_hash: str = ""
    quota_window_started_at: str = Field(default_factory=utc_now_iso)


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
    project_id: Optional[str] = None
    plan_version: Optional[str] = None


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
    checks: Dict[str, str] = Field(default_factory=dict)
    environment_blocked: bool = False
    project_id: Optional[str] = None
    plan_version: Optional[str] = None


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
    plan_version: str


class RegisterAgentRequest(BaseModel):
    owner_name: str = Field(min_length=1,max_length=128)
    machine_name: str = Field(min_length=1,max_length=128)
    profile_key: str = "claude-pro"
    initial_used_pct: float = Field(default=0.0, ge=0, le=100)
    worker_id: str = Field(min_length=8, max_length=128)
    executor: Literal['remote', 'local'] = 'remote'


class ClaimTaskHumanRequest(BaseModel):
    human_name: str


class ReportQuotaRequest(BaseModel):
    used_pct: float = Field(ge=0, le=100)
    turns_delta: int = Field(default=1, ge=0)
    rate_limited: bool = False
    cooldown_minutes: Optional[int] = None
    handoff_notes: Optional[str] = None
    reset_window: bool = False
    assignment_id: Optional[str] = None
