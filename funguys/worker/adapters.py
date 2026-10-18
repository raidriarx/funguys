"""Explicit provider adapters; synthesized artifacts are used only by the demo."""
from __future__ import annotations
import os
import tempfile
from pathlib import Path
from typing import Any,Dict
from funguys.worker.runtime import execute_cli, collect_changes, task_prompt, cli_command
from funguys.host.execution import run_process

def detect_available_clis():
    import shutil
    return [profile for binary,profile in [('claude','claude-pro'),('gemini','gemini-cli'),('codex','codex-pro')] if shutil.which(binary)]

def build_graphify_task_prompt(task, contracts_markdown, subgraph_digest):
    return task_prompt({'task':task, 'contracts_markdown':contracts_markdown, 'subgraph_digest':subgraph_digest})

def run_cli_agent_task(provider, work_dir, prompt, target_files, timeout_sec=240, cancelled=None):
    if os.environ.get('FUNGUYS_TRUSTED_EXECUTION') != '1':
        return False, False, {}, 'Host CLI execution is disabled. Use a remote worker or explicitly enable FUNGUYS_TRUSTED_EXECUTION=1 for trusted local projects.'
    try:
        ok, limited, output = execute_cli(provider, work_dir, prompt, cancelled=cancelled, timeout=timeout_sec)
        return ok, limited, {}, output
    except Exception as exc:
        return False, False, {}, str(exc)

def run_text_agent(provider, prompt, timeout=90):
    if os.environ.get('FUNGUYS_TRUSTED_EXECUTION') != '1':
        raise RuntimeError('Local planning/chat requires FUNGUYS_TRUSTED_EXECUTION=1 and the selected CLI; remote coding tasks remain available.')
    with tempfile.TemporaryDirectory(prefix='funguys-consult-') as directory:
        root = Path(directory)
        command = cli_command(provider, root, prompt)
        output = root / 'answer.txt'
        if provider == 'codex':
            command = [arg if arg != 'workspace-write' else 'read-only' for arg in command]
            command[-1:-1] = ['--ephemeral', '--output-last-message', str(output)]
        elif provider == 'claude':
            command[command.index('--tools') + 1] = ''
        code, text = run_process(command, root, timeout)
        if code:
            raise RuntimeError(f'{provider} consultation failed: {text[-2000:]}')
        answer = output.read_text(encoding='utf-8') if output.exists() else text
        if not answer.strip():
            raise RuntimeError('Selected provider returned an empty answer')
        return answer.strip()

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

    raise ValueError('Synthetic artifacts are available only for the three explicit demo tasks')
