"""Persisted administrator login and short-lived, scoped worker enrollment."""
from __future__ import annotations
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


class AccessControl:
    def __init__(self, store, state_dir: Path, admin_token=None):
        self.store = store
        path = state_dir / 'admin_token'
        if admin_token is None:
            if path.exists():
                admin_token = path.read_text(encoding='utf-8').strip()
            else:
                admin_token = secrets.token_urlsafe(32)
                path.write_text(admin_token, encoding='utf-8')
                path.chmod(0o600)
        self.admin_token = admin_token

    def identify(self, token):
        if not token:
            return None
        if secrets.compare_digest(token, self.admin_token):
            return {'role': 'admin', 'agent_id': None}
        identity = self.store.get_credential(digest(token))
        if identity and (not identity['expires_at'] or datetime.fromisoformat(identity['expires_at']) > datetime.now(timezone.utc)):
            return identity
        return None

    def invite(self):
        token = secrets.token_urlsafe(32)
        expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        self.store.save_credential(digest(token), 'invite', expires_at=expires)
        return {'token': token, 'expires_at': expires}

    def enroll(self, token, agent_id):
        worker_token = secrets.token_urlsafe(32)
        with self.store._connect() as conn:
            cursor = conn.execute("DELETE FROM credentials WHERE token_hash = ? AND role = 'invite' AND expires_at > ?", (digest(token), datetime.now(timezone.utc).isoformat()))
            if cursor.rowcount != 1:
                raise ValueError('Invite has expired or was already used')
            conn.execute('INSERT INTO credentials VALUES (?, ?, ?, ?)', (digest(worker_token), 'worker', agent_id, None))
        return worker_token
