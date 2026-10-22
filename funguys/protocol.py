"""Zero-waste C2 signal parser, contract slicer, and model/effort routing helpers."""

from __future__ import annotations

from typing import List
from funguys.models import AgentSignal, SignalOp
from funguys.worker.runtime import (
    MODEL_POLICY,
    PLANNER_PRIORITY,
    SIGNAL_PATTERN,
    classify_task_role,
    compress_error_log,
    controller_arbitration_prompt,
    parse_raw_signals,
    resolve_cli_binary,
    select_model_and_effort,
    slice_contract_for_task,
)


def parse_agent_signals(sender_agent_id: str, text: str) -> List[AgentSignal]:
    """Parse `@FG:<OP> <target> <payload>` lines into validated AgentSignal models."""
    signals: List[AgentSignal] = []
    for op_str, target, payload in parse_raw_signals(text):
        try:
            op = SignalOp(op_str)
            signals.append(
                AgentSignal(
                    sender_agent_id=sender_agent_id,
                    op=op,
                    target=target,
                    payload=payload,
                )
            )
        except ValueError:
            continue
    return signals


__all__ = [
    "MODEL_POLICY",
    "PLANNER_PRIORITY",
    "SIGNAL_PATTERN",
    "classify_task_role",
    "compress_error_log",
    "controller_arbitration_prompt",
    "parse_agent_signals",
    "parse_raw_signals",
    "resolve_cli_binary",
    "select_model_and_effort",
    "slice_contract_for_task",
]
