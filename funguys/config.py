"""Configuration, AI subscription profiles, and safety boundaries for FunGuys."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple


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
    high_tier_turns_per_window: int = 30
    high_tier_window_hours: float = 5.0
    standard_turns_per_window: int = 120
    standard_window_hours: float = 5.0
    usage_rate_rank: int = 1


SUBSCRIPTION_PROFILES: Dict[str, SubscriptionProfile] = {
    "claude-opus": SubscriptionProfile(
        provider="claude",
        cli_command="claude",
        display_name="Claude Code (Opus High Planning & Hard Coding / Opus Med Gruntwork)",
        tier=1,
        window_hours=5.0,
        estimated_turns_per_window=90,
        soft_handoff_threshold_pct=80.0,
        strengths=["architecture", "complex-backend", "root-cause-debugging", "contracts"],
        high_tier_turns_per_window=35,
        high_tier_window_hours=5.0,
        standard_turns_per_window=90,
        standard_window_hours=5.0,
        usage_rate_rank=0,
    ),
    "gemini-argon-flash": SubscriptionProfile(
        provider="gemini",
        cli_command="gemini",
        display_name="Gemini CLI (Argon Med Plan / Argon Low Coding / Flash High Gruntwork & Testing)",
        tier=2,
        window_hours=5.0,
        estimated_turns_per_window=150,
        soft_handoff_threshold_pct=85.0,
        strengths=["architecture", "frontend-ui", "unit-tests", "playwright-e2e", "backend"],
        high_tier_turns_per_window=30,
        high_tier_window_hours=5.0,
        standard_turns_per_window=150,
        standard_window_hours=24.0,
        usage_rate_rank=1,
    ),
    "chatgpt-astra-sol": SubscriptionProfile(
        provider="codex",
        cli_command="codex",
        display_name="ChatGPT / Codex (Astra Low Planning / Sol High Coding & Testing)",
        tier=1,
        window_hours=5.0,
        estimated_turns_per_window=80,
        soft_handoff_threshold_pct=80.0,
        strengths=["algorithms", "refactoring", "backend", "api-integration", "testing"],
        high_tier_turns_per_window=25,
        high_tier_window_hours=5.0,
        standard_turns_per_window=80,
        standard_window_hours=5.0,
        usage_rate_rank=2,
    ),
    "mcp-ide": SubscriptionProfile(
        provider="mcp",
        cli_command="mcp",
        display_name="IDE Agent (Cursor / Claude Desktop / Windsurf MCP Bridge)",
        tier=1,
        window_hours=5.0,
        estimated_turns_per_window=45,
        soft_handoff_threshold_pct=80.0,
        strengths=["architecture", "complex-backend", "frontend-ui", "root-cause-debugging"],
        high_tier_turns_per_window=20,
        high_tier_window_hours=5.0,
        standard_turns_per_window=45,
        standard_window_hours=5.0,
        usage_rate_rank=1,
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
        high_tier_turns_per_window=500,
        high_tier_window_hours=24.0,
        standard_turns_per_window=500,
        standard_window_hours=24.0,
        usage_rate_rank=3,
    ),
}

# Compatibility aliases mapping legacy keys to canonical profiles
PROFILE_ALIASES: Dict[str, str] = {
    "claude-pro": "claude-opus",
    "claude-max": "claude-opus",
    "gemini-cli": "gemini-argon-flash",
    "codex-pro": "chatgpt-astra-sol",
    "ide-mcp": "mcp-ide",
}


def resolve_profile_key(key: str) -> str:
    """Resolve aliases or return canonical profile key."""
    return PROFILE_ALIASES.get(key, key)


def get_profile(key: str) -> SubscriptionProfile:
    """Return canonical profile object, supporting aliases."""
    canonical = resolve_profile_key(key)
    if canonical in SUBSCRIPTION_PROFILES:
        return SUBSCRIPTION_PROFILES[canonical]
    raise KeyError(f"Unknown subscription profile: {key}")

# Planning priority ordered by usage rate capacity:
# 1st: Claude (Opus High) -> 2nd: Gemini (Argon Medium) -> 3rd: OpenAI/Codex (Astra Low)
PLANNER_PROVIDER_PRIORITY: Dict[str, int] = {
    "claude": 0,
    "gemini": 1,
    "codex": 2,
    "mcp": 1,
    "ide": 1,
    "api": 3,
}

# Model & reasoning effort routing table per provider and task role:
# Roles: "planning", "important_coding", "gruntwork", "testing"
# Note: The Main Controller routes by work importance ("important_coding" vs "gruntwork"),
# NOT by token size, so large but critical architectural tasks still go to Argon Low / Opus High.
MODEL_ROUTING_POLICY: Dict[str, Dict[str, Tuple[str, str]]] = {
    "claude": {
        "planning": ("opus", "high"),
        "important_coding": ("opus", "high"),
        "gruntwork": ("opus", "medium"),
        "testing": ("opus", "medium"),
        "fallback": ("opus", "medium"),
    },
    "gemini": {
        "planning": ("argon", "medium"),
        "important_coding": ("argon", "low"),
        "gruntwork": ("flash", "high"),
        "testing": ("flash", "high"),
        "fallback": ("flash", "high"),  # Never lower than flash high
    },
    "codex": {
        "planning": ("astra", "low"),
        "important_coding": ("sol", "high"),
        "gruntwork": ("sol", "high"),
        "testing": ("sol", "high"),
        "fallback": ("sol", "high"),
    },
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
