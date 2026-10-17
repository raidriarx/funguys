"""Configuration, AI subscription profiles, and safety boundaries for FunGuys."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List


@dataclass(frozen=True)
class SubscriptionProfile:
    provider: str
    cli_command: str
    display_name: str
    tier: int  # 1 = High-Reasoning/Architect/Debugger, 2 = High-Throughput Builder/Tester
    window_hours: float
    estimated_turns_per_window: int
    soft_handoff_threshold_pct: float = 80.0
    strengths: List[str] = ()


SUBSCRIPTION_PROFILES: Dict[str, SubscriptionProfile] = {
    "claude-max": SubscriptionProfile(
        provider="claude",
        cli_command="claude",
        display_name="Claude Code (Max 5x/20x)",
        tier=1,
        window_hours=5.0,
        estimated_turns_per_window=225,
        soft_handoff_threshold_pct=80.0,
        strengths=["architecture", "complex-backend", "root-cause-debugging", "contracts"],
    ),
    "claude-pro": SubscriptionProfile(
        provider="claude",
        cli_command="claude",
        display_name="Claude Code (Pro)",
        tier=1,
        window_hours=5.0,
        estimated_turns_per_window=45,
        soft_handoff_threshold_pct=80.0,
        strengths=["architecture", "complex-backend", "root-cause-debugging"],
    ),
    "codex-pro": SubscriptionProfile(
        provider="codex",
        cli_command="codex",
        display_name="OpenAI Codex CLI (Plus/Pro)",
        tier=1,
        window_hours=5.0,
        estimated_turns_per_window=80,
        soft_handoff_threshold_pct=80.0,
        strengths=["algorithms", "refactoring", "backend", "api-integration"],
    ),
    "gemini-cli": SubscriptionProfile(
        provider="gemini",
        cli_command="gemini",
        display_name="Google Gemini CLI (Pro/Flash)",
        tier=2,
        window_hours=24.0,
        estimated_turns_per_window=1000,
        soft_handoff_threshold_pct=85.0,
        strengths=["frontend-ui", "unit-tests", "playwright-e2e", "boilerplate", "docs"],
    ),
    "api-fallback": SubscriptionProfile(
        provider="api",
        cli_command="api",
        display_name="Fallback API / Local Model",
        tier=2,
        window_hours=24.0,
        estimated_turns_per_window=500,
        soft_handoff_threshold_pct=90.0,
        strengths=["tests", "boilerplate", "formatting", "docs"],
    ),
}

# Dangerous terminal patterns blocked during autonomous execution
BLOCKED_COMMAND_PATTERNS: List[str] = [
    "rm -rf /",
    "rm -rf ~",
    "Format-Volume",
    "del /f /s /q C:\\",
    "DROP DATABASE",
    "shutdown",
    "reboot",
    "mkfs",
]


def is_command_safe(command: str) -> bool:
    """Verify that an autonomous AI command does not match destructive system-wide patterns."""
    lowered = command.lower()
    return not any(pattern.lower() in lowered for pattern in BLOCKED_COMMAND_PATTERNS)
