"""Append-only JSONL trace of everything the workflow does, for post-run review.

Files are written to ``<host cwd>/.funguys/logs/workflow-YYYYMMDD.jsonl``. Each line is
one JSON object: ``{"ts", "kind", "message", ...details}``. Large text fields (CLI output,
QA logs, architect prompts/responses) are truncated so a single run stays readable.
"""
from __future__ import annotations

import json
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path

MAX_FIELD = 20_000


def _clip(value):
    if isinstance(value, str) and len(value) > MAX_FIELD:
        return value[:MAX_FIELD // 2] + f'\n...[{len(value) - MAX_FIELD} chars truncated]...\n' + value[-MAX_FIELD // 2:]
    if isinstance(value, dict):
        return {k: _clip(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clip(v) for v in value]
    return value


class WorkflowLogger:
    def __init__(self, directory: Path | None):
        self.directory = Path(directory) if directory else None
        self._lock = threading.Lock()
        if self.directory:
            self.directory.mkdir(parents=True, exist_ok=True)

    @property
    def current_file(self) -> Path | None:
        if not self.directory:
            return None
        return self.directory / f"workflow-{datetime.now(timezone.utc):%Y%m%d}.jsonl"

    def log(self, kind: str, message: str = '', **details) -> None:
        path = self.current_file
        if not path:
            return
        record = {'ts': datetime.now(timezone.utc).isoformat(), 'kind': kind, 'message': message, **_clip(details)}
        line = json.dumps(record, default=str, ensure_ascii=False)
        try:
            with self._lock, path.open('a', encoding='utf-8') as stream:
                stream.write(line + '\n')
        except OSError:
            pass  # Logging must never break the workflow.

    def exception(self, kind: str, message: str, exc: BaseException, **details) -> None:
        self.log(kind, message, error=str(exc), traceback=''.join(traceback.format_exception(type(exc), exc, exc.__traceback__)), **details)


NULL_LOGGER = WorkflowLogger(None)
