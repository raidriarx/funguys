"""SQLite persistence layer for FunGuys Host server."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional

from funguys.models import (
    ActivityEvent,
    AgentNode,
    ContractAlert,
    ProjectState,
    QARunReport,
    TaskItem,
)


class StateStore:
    """Thread-safe SQLite store backing the Host PC state."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

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
        }
