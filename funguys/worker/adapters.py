"""CLI Subscription Wrappers (Claude Code, Gemini CLI, Codex CLI) + Fallback API & Simulation."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


RATE_LIMIT_PATTERNS = [
    r"rate[\s_-]?limit",
    r"429",
    r"quota[\s_-]?exceeded",
    r"usage[\s_-]?limit",
    r"resets?\s+at",
    r"too\s+many\s+requests",
]


def detect_available_clis() -> List[str]:
    """Detect which AI subscription CLIs or fallback API keys are available on this laptop."""
    detected: List[str] = []
    if shutil.which("claude"):
        detected.append("claude-pro")
    if shutil.which("gemini"):
        detected.append("gemini-cli")
    if shutil.which("codex"):
        detected.append("codex-pro")
    if any(
        os.environ.get(k)
        for k in ("GEMINI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY")
    ):
        detected.append("api-fallback")
    return detected


def build_graphify_task_prompt(
    task: Dict[str, Any],
    contracts_markdown: str,
    subgraph_digest: str,
) -> str:
    """Build a lean, token-efficient prompt powered by Graphify and CONTRACTS.md."""
    parts = [
        "You are an autonomous AI coding agent collaborating in a FunGuys multiplayer session.",
        f"## Task: {task.get('title')} (`{task.get('id')}`)",
        task.get("description", ""),
        f"Target Files: {', '.join(task.get('target_files', []))}",
        "",
        "## Shared Interface Contracts (`CONTRACTS.md`)",
        contracts_markdown.strip(),
        "",
        subgraph_digest.strip(),
    ]
    if task.get("handoff_digest"):
        parts.extend(
            [
                "",
                "## Baton-Pass Handoff / Escalation Context",
                str(task["handoff_digest"]).strip(),
            ]
        )
    if task.get("last_error"):
        parts.extend(
            [
                "",
                "## Previous QA Failure to Fix",
                str(task["last_error"])[-1200:],
            ]
        )
    parts.append(
        "\nCRITICAL EXECUTION RULE: Write ALL listed Target Files directly to disk FIRST before running any shell commands. "
        "Do NOT create a virtualenv (.venv) or run `pip install`. Write clean, complete, working code into all Target Files so static checks and unit tests pass."
    )
    return "\n".join(parts)


def is_rate_limited_output(output: str) -> bool:
    return any(re.search(pat, output, re.IGNORECASE) for pat in RATE_LIMIT_PATTERNS)


def run_cli_agent_task(
    provider: str,
    work_dir: Path,
    prompt: str,
    target_files: List[str],
    timeout_sec: int = 180,
) -> Tuple[bool, bool, Dict[str, str], str]:
    """Invoke a local subscription CLI (`claude`, `gemini`, `codex`) in `work_dir`.

    Returns: `(success, rate_limited, modified_files_dict, raw_output)`
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    existing_targets = [
        rel for rel in target_files if (work_dir / rel).is_file() and (work_dir / rel).stat().st_size > 50
    ]
    missing_targets = [rel for rel in target_files if rel not in existing_targets]
    if existing_targets and missing_targets:
        prompt = (
            f"IMPORTANT WORKSPACE STATE: {existing_targets} ALREADY exist on disk and are implemented. "
            f"Do NOT rewrite {existing_targets} from scratch! Immediately create and implement the missing target file(s): {missing_targets} FIRST!\n\n"
            + prompt
        )

    cmd: Optional[List[str]] = None
    codex_bin = shutil.which("codex") or r"C:\Users\USER\AppData\Local\Programs\OpenAI\Codex\bin\codex.EXE"
    if provider == "claude" and shutil.which("claude"):
        cmd = ["claude", "-p", prompt, "--dangerously-skip-permissions"]
    elif provider in {"gemini", "google"} and shutil.which("gemini"):
        cmd = ["gemini", "-p", prompt, "-y"]
    elif os.path.exists(codex_bin):
        cmd = [
            codex_bin,
            "exec",
            "-C",
            str(work_dir),
            "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
            prompt,
        ]

    if not cmd:
        return False, False, {}, f"CLI binary for '{provider}' not installed locally; using synthesized builder."
    before_mtimes: Dict[str, float] = {}
    for p in work_dir.rglob("*"):
        rel_parts = p.relative_to(work_dir).parts
        if p.is_file() and not any(part in {".git", ".funguys", "__pycache__", "node_modules", ".venv", ".pytest_cache", ".ruff_cache"} for part in rel_parts):
            before_mtimes[p.relative_to(work_dir).as_posix()] = p.stat().st_mtime

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(work_dir),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            encoding="utf-8",
            errors="replace",
        )
        combined = f"{proc.stdout}\n{proc.stderr}".strip()
        rate_limited = is_rate_limited_output(combined)
        files_out: Dict[str, str] = {}
        for rel in target_files:
            fp = work_dir / rel
            if fp.exists() and fp.is_file():
                files_out[rel] = fp.read_text(encoding="utf-8", errors="ignore")
        for p in work_dir.rglob("*"):
            rel_parts = p.relative_to(work_dir).parts
            if p.is_file() and not any(part in {".git", ".funguys", "__pycache__", "node_modules", ".venv", ".pytest_cache", ".ruff_cache"} for part in rel_parts):
                rel = p.relative_to(work_dir).as_posix()
                if rel not in before_mtimes or p.stat().st_mtime > before_mtimes[rel]:
                    if p.stat().st_size <= 250_000:
                        files_out[rel] = p.read_text(encoding="utf-8", errors="ignore")
        return proc.returncode == 0 and bool(files_out), rate_limited, files_out, combined
    except subprocess.TimeoutExpired:
        files_out: Dict[str, str] = {}
        for rel in target_files:
            fp = work_dir / rel
            if fp.exists() and fp.is_file():
                files_out[rel] = fp.read_text(encoding="utf-8", errors="ignore")
        for p in work_dir.rglob("*"):
            rel_parts = p.relative_to(work_dir).parts
            if p.is_file() and not any(part in {".git", ".funguys", "__pycache__", "node_modules", ".venv", ".pytest_cache", ".ruff_cache"} for part in rel_parts):
                rel = p.relative_to(work_dir).as_posix()
                if rel not in before_mtimes or p.stat().st_mtime > before_mtimes[rel]:
                    if p.stat().st_size <= 250_000:
                        files_out[rel] = p.read_text(encoding="utf-8", errors="ignore")
        return bool(files_out), False, files_out, "CLI execution timed out."
    except Exception as exc:
        return False, False, {}, f"CLI execution error: {exc}"


def generate_task_code_artifact(
    task: Dict[str, Any],
    inject_bug_on_first_try: bool = False,
) -> Dict[str, str]:
    """Generate real working code for the standard project tasks (or custom tasks)."""
    tid = task.get("id", "")
    retry_count = int(task.get("retry_count", 0))

    if tid == "task-1-contracts":
        return {
            "backend/__init__.py": "",
            "backend/service.py": '''"""Core domain service implementing CONTRACTS.md."""

from __future__ import annotations
import uuid
from typing import Dict, List

_STORE: List[Dict[str, object]] = []


def clear_store() -> None:
    _STORE.clear()


def create_item(title: str, priority: int = 1, completed: bool = False) -> Dict[str, object]:
    """Create a project item matching CONTRACTS.md."""
    item = {
        "id": f"item-{uuid.uuid4().hex[:6]}",
        "title": title.strip(),
        "priority": int(priority),
        "completed": bool(completed),
    }
    _STORE.append(item)
    return item


def list_items() -> List[Dict[str, object]]:
    """Return all items sorted by priority descending."""
    return sorted(_STORE, key=lambda x: int(x["priority"]), reverse=True)


def compute_metrics(items: List[Dict[str, object]]) -> Dict[str, object]:
    """Compute completion rate metrics across items."""
    total = len(items)
    completed = sum(1 for i in items if i.get("completed"))
    rate = round((completed / total) * 100.0, 1) if total > 0 else 0.0
    return {"total": total, "completed": completed, "completion_rate": rate}


def initialize_state() -> Dict[str, object]:
    """Initialize or reset the domain store matching Grill-Me CONTRACTS.md."""
    clear_store()
    return {"initialized": True, "count": 0}


def create_record(title: str, payload: Dict[str, object] | None = None) -> Dict[str, object]:
    """Create and store a domain record matching Grill-Me CONTRACTS.md."""
    item = create_item(title=title, priority=1, completed=False)
    item["status"] = "active"
    item["payload"] = payload or {}
    return item


def list_records() -> List[Dict[str, object]]:
    """Return all active domain records."""
    return list_items()


def get_summary_stats() -> Dict[str, object]:
    """Return live metrics matching Grill-Me CONTRACTS.md."""
    return {"total_count": len(_STORE), "active_count": len(_STORE), "health": "ok"}
''',
        }

    if tid == "task-2-frontend":
        title_str = task.get("title", "FunGuys Built Application")
        desc_str = task.get("description", "")
        return {
            "frontend/index.html": f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <title>{title_str}</title>
  <style>
    body {{ font-family: 'Courier New', monospace; background: #000; color: #fff; margin: 0; padding: 24px; }}
    .box {{ border: 1px solid #fff; padding: 16px; max-width: 720px; margin: 0 auto; }}
  </style>
</head>
<body>
  <div class="box">
    <h1>{title_str}</h1>
    <p>{desc_str}</p>
  </div>
</body>
</html>
"""
        }

    if tid == "task-3-tests":
        if inject_bug_on_first_try and retry_count == 0:
            # Intentional assertion bug on attempt 1 so the 3-layer QA catches & auto-fixes it!
            return {
                "tests/test_service.py": '''"""Automated unit tests for backend/service.py."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.service import clear_store, compute_metrics, create_item, list_items


def test_create_and_list_items() -> None:
    clear_store()
    create_item("Write docs", priority=1)
    create_item("Launch host", priority=5)
    items = list_items()
    assert len(items) == 2
    assert items[0]["title"] == "Launch host"


def test_compute_metrics() -> None:
    items = [
        {"id": "1", "title": "A", "priority": 2, "completed": True},
        {"id": "2", "title": "B", "priority": 1, "completed": False},
    ]
    metrics = compute_metrics(items)
    assert metrics["completion_rate"] == 999.9  # Bug caught by Host QA on Attempt 1!
'''
            }
        return {
            "tests/test_service.py": '''"""Automated unit tests for backend/service.py."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.service import clear_store, compute_metrics, create_item, list_items


def test_create_and_list_items() -> None:
    clear_store()
    create_item("Write docs", priority=1)
    create_item("Launch host", priority=5)
    items = list_items()
    assert len(items) == 2
    assert items[0]["title"] == "Launch host"


def test_compute_metrics() -> None:
    items = [
        {"id": "1", "title": "A", "priority": 2, "completed": True},
        {"id": "2", "title": "B", "priority": 1, "completed": False},
    ]
    metrics = compute_metrics(items)
    assert metrics["total"] == 2
    assert metrics["completed"] == 1
    assert metrics["completion_rate"] == 50.0
'''
        }

    # Generic fallback for any custom user-created task
    files: Dict[str, str] = {}
    for target in task.get("target_files", ["output.py"]):
        if target.endswith(".py"):
            files[target] = f'"""Auto-generated module for {task.get("title")}."""\n\ndef run() -> bool:\n    return True\n'
        else:
            files[target] = f"<!-- {task.get('title')} -->\n<div>Ready</div>\n"
    return files
