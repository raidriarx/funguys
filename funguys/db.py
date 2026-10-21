"""SQLite persistence layer for FunGuys Host server."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, List, Optional

from funguys.models import (
    ActivityEvent,
    AgentNode,
    BlackboardEntry,
    ContractAlert,
    ProjectState,
    PromptQueueItem,
    QARunReport,
    TaskItem,
)


class StateStore:
    """Thread-safe SQLite store backing the Host PC state."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(str(self.db_path), timeout=15, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS project (
                    id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agents (
                    id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS contract_alerts (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS qa_reports (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY,
                    timestamp TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS credentials (
                    token_hash TEXT PRIMARY KEY,
                    role TEXT NOT NULL,
                    agent_id TEXT,
                    expires_at TEXT
                );
                CREATE TABLE IF NOT EXISTS chat (
                    id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS prompt_queue (
                    id TEXT PRIMARY KEY,
                    target_agent_id TEXT NOT NULL,
                    delivered INTEGER NOT NULL DEFAULT 0,
                    priority INTEGER NOT NULL DEFAULT 10,
                    created_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS blackboard (
                    key TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                """
            )

    def save_project(self, project: ProjectState) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM project")
            conn.execute(
                "INSERT INTO project (id, payload) VALUES (?, ?)",
                (project.id, project.model_dump_json()),
            )

    def get_project(self) -> Optional[ProjectState]:
        with self._connect() as conn:
            row = conn.execute("SELECT payload FROM project LIMIT 1").fetchone()
            return ProjectState.model_validate_json(row["payload"]) if row else None

    def save_task(self, task: TaskItem) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO tasks (id, payload) VALUES (?, ?)",
                (task.id, task.model_dump_json()),
            )

    def get_task(self, task_id: str) -> Optional[TaskItem]:
        with self._connect() as conn:
            row = conn.execute("SELECT payload FROM tasks WHERE id = ?", (task_id,)).fetchone()
            return TaskItem.model_validate_json(row["payload"]) if row else None

    def list_tasks(self) -> List[TaskItem]:
        with self._connect() as conn:
            rows = conn.execute("SELECT payload FROM tasks ORDER BY id").fetchall()
            return [TaskItem.model_validate_json(r["payload"]) for r in rows]

    def clear_tasks(self) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM tasks")

    def save_agent(self, agent: AgentNode) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO agents (id, payload) VALUES (?, ?)",
                (agent.id, agent.model_dump_json()),
            )

    def get_agent(self, agent_id: str) -> Optional[AgentNode]:
        with self._connect() as conn:
            row = conn.execute("SELECT payload FROM agents WHERE id = ?", (agent_id,)).fetchone()
            return AgentNode.model_validate_json(row["payload"]) if row else None

    def list_agents(self) -> List[AgentNode]:
        with self._connect() as conn:
            rows = conn.execute("SELECT payload FROM agents ORDER BY id").fetchall()
            return [AgentNode.model_validate_json(r["payload"]) for r in rows]

    def save_contract_alert(self, alert: ContractAlert) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO contract_alerts (id, created_at, payload) VALUES (?, ?, ?)",
                (alert.id, alert.created_at, alert.model_dump_json()),
            )

    def list_contract_alerts(self, limit: int = 50) -> List[ContractAlert]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM contract_alerts ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [ContractAlert.model_validate_json(r["payload"]) for r in rows]

    def save_qa_report(self, report: QARunReport) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO qa_reports (id, task_id, created_at, payload) VALUES (?, ?, ?, ?)",
                (report.id, report.task_id, report.created_at, report.model_dump_json()),
            )

    def list_qa_reports(self, task_id: Optional[str] = None, limit: int = 50) -> List[QARunReport]:
        with self._connect() as conn:
            if task_id:
                rows = conn.execute(
                    "SELECT payload FROM qa_reports WHERE task_id = ? ORDER BY created_at DESC LIMIT ?",
                    (task_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT payload FROM qa_reports ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [QARunReport.model_validate_json(r["payload"]) for r in rows]

    def add_event(self, event: ActivityEvent) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO events (id, timestamp, payload) VALUES (?, ?, ?)",
                (event.id, event.timestamp, event.model_dump_json()),
            )

    def list_events(self, limit: int = 100) -> List[ActivityEvent]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM events ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [ActivityEvent.model_validate_json(r["payload"]) for r in rows]

    def snapshot(self) -> Dict[str, object]:
        return {
            "project": self.get_project(),
            "tasks": self.list_tasks(),
            "agents": self.list_agents(),
            "contract_alerts": self.list_contract_alerts(limit=30),
            "qa_reports": self.list_qa_reports(limit=30),
            "events": self.list_events(limit=60),
            "blackboard": self.list_blackboard(limit=40),
            "prompt_queue": self.list_prompts(limit=30),
        }

    def replace_tasks(self, tasks, project=None):
        with self._connect() as conn:
            conn.execute('DELETE FROM tasks')
            for task in tasks:
                conn.execute('INSERT INTO tasks VALUES (?, ?)', (task.id, task.model_dump_json()))
            if project:
                conn.execute('DELETE FROM project')
                conn.execute('INSERT INTO project VALUES (?, ?)', (project.id, project.model_dump_json()))

    def delete_agent(self, agent_id):
        with self._connect() as conn:
            conn.execute('DELETE FROM agents WHERE id = ?', (agent_id,))
            conn.execute('DELETE FROM credentials WHERE agent_id = ?', (agent_id,))

    def save_credential(self, token_hash, role, agent_id=None, expires_at=None):
        with self._connect() as conn:
            conn.execute('INSERT OR REPLACE INTO credentials VALUES (?, ?, ?, ?)', (token_hash, role, agent_id, expires_at))

    def get_credential(self, token_hash):
        with self._connect() as conn:
            row = conn.execute('SELECT * FROM credentials WHERE token_hash = ?', (token_hash,)).fetchone()
            return dict(row) if row else None

    def save_chat(self, entry):
        with self._connect() as conn:
            conn.execute('INSERT OR REPLACE INTO chat VALUES (?, ?)', (entry['id'], json.dumps(entry)))

    def list_chat(self):
        with self._connect() as conn:
            return [json.loads(row['payload']) for row in conn.execute('SELECT payload FROM chat ORDER BY rowid DESC LIMIT 100').fetchall()][::-1]

    def clear_chat(self):
        with self._connect() as conn:
            conn.execute('DELETE FROM chat')

    def enqueue_prompt(self, item: PromptQueueItem) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO prompt_queue (id, target_agent_id, delivered, priority, created_at, payload) VALUES (?, ?, ?, ?, ?, ?)",
                (item.id, item.target_agent_id, int(item.delivered), item.priority, item.created_at, item.model_dump_json()),
            )

    def pop_pending_prompts(self, target_agent_id: str) -> List[PromptQueueItem]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, payload FROM prompt_queue WHERE target_agent_id = ? AND delivered = 0 ORDER BY priority DESC, created_at ASC",
                (target_agent_id,),
            ).fetchall()
            items: List[PromptQueueItem] = []
            for row in rows:
                item = PromptQueueItem.model_validate_json(row["payload"])
                item.delivered = True
                items.append(item)
                conn.execute(
                    "UPDATE prompt_queue SET delivered = 1, payload = ? WHERE id = ?",
                    (item.model_dump_json(), row["id"]),
                )
            return items

    def list_prompts(self, limit: int = 40) -> List[PromptQueueItem]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM prompt_queue ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [PromptQueueItem.model_validate_json(r["payload"]) for r in rows]

    def put_blackboard(self, entry: BlackboardEntry) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO blackboard (key, kind, updated_at, payload) VALUES (?, ?, ?, ?)",
                (entry.key, entry.kind, entry.updated_at, entry.model_dump_json()),
            )

    def remove_blackboard_locks(self, agent_id: str, key: Optional[str] = None) -> None:
        with self._connect() as conn:
            rows = conn.execute("SELECT key, payload FROM blackboard WHERE kind = 'lock'").fetchall()
            for row in rows:
                entry = BlackboardEntry.model_validate_json(row["payload"])
                if entry.agent_id == agent_id and (key is None or entry.key == key):
                    conn.execute("DELETE FROM blackboard WHERE key = ?", (row["key"],))

    def list_blackboard(self, kind: Optional[str] = None, limit: int = 50) -> List[BlackboardEntry]:
        with self._connect() as conn:
            if kind:
                rows = conn.execute(
                    "SELECT payload FROM blackboard WHERE kind = ? ORDER BY updated_at DESC LIMIT ?",
                    (kind, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT payload FROM blackboard ORDER BY updated_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [BlackboardEntry.model_validate_json(r["payload"]) for r in rows]
