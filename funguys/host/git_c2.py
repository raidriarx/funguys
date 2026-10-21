"""Serverless GitHub C2 state synchronization over an orphan `fg-c2` branch.

Allows a laptop Host and remote Workers to persist and recover the blackboard,
task DAG, and prompt queues using only free GitHub without a paid server.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional


class GitC2Sync:
    """Synchronizes `.funguys/c2_state.json` to an orphan `fg-c2` git branch."""

    def __init__(self, workspace_path: Path, branch: str = "fg-c2") -> None:
        self.workspace_path = workspace_path
        self.branch = branch
        self.git = shutil.which("git")

    def is_available(self) -> bool:
        if not self.git or not (self.workspace_path / ".git").exists():
            return False
        return True

    def write_local_checkpoint(self, snapshot: Dict[str, Any], beacon: Optional[Dict[str, Any]] = None) -> Optional[Path]:
        """Write compact JSON state checkpoint inside `.funguys/c2_state.json` (and optional `beacon.json`)."""
        try:
            state_dir = self.workspace_path / ".funguys"
            state_dir.mkdir(parents=True, exist_ok=True)
            dest = state_dir / "c2_state.json"
            serializable = json.loads(json.dumps(snapshot, default=lambda o: o.model_dump() if hasattr(o, "model_dump") else str(o)))
            if beacon:
                serializable["beacon"] = beacon
                (state_dir / "beacon.json").write_text(json.dumps(beacon, indent=2), encoding="utf-8")
            dest.write_text(json.dumps(serializable, indent=2), encoding="utf-8")
            return dest
        except Exception:
            return None

    def publish_beacon(self, host_url: str, public_url: Optional[str] = None, lan_ip: Optional[str] = None, remote: str = "origin", snapshot: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Publish a host discovery beacon locally and (if git remote is configured) to `refs/heads/fg-c2`."""
        from funguys.models import utc_now_iso
        beacon = {
            "host_url": host_url,
            "public_url": public_url or host_url,
            "lan_ip": lan_ip or "",
            "branch": self.branch,
            "updated_at": utc_now_iso(),
        }
        state_dir = self.workspace_path / ".funguys"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "beacon.json").write_text(json.dumps(beacon, indent=2), encoding="utf-8")
        if snapshot is not None:
            self.push_checkpoint(snapshot, remote=remote, beacon=beacon)
        return beacon

    def push_checkpoint(self, snapshot: Dict[str, Any], remote: str = "origin", beacon: Optional[Dict[str, Any]] = None) -> bool:
        """Commit and push the C2 state snapshot (and beacon.json) to the orphan `fg-c2` branch."""
        local_file = self.write_local_checkpoint(snapshot, beacon=beacon)
        if not local_file or not self.is_available():
            return False
        try:
            with tempfile.TemporaryDirectory(prefix="funguys-gitc2-") as tmp:
                tmp_dir = Path(tmp)
                subprocess.run([self.git, "init", "-q"], cwd=str(tmp_dir), check=True, timeout=10)
                subprocess.run([self.git, "config", "user.email", "c2@funguys.local"], cwd=str(tmp_dir), check=True, timeout=5)
                subprocess.run([self.git, "config", "user.name", "FunGuys C2 Controller"], cwd=str(tmp_dir), check=True, timeout=5)
                shutil.copyfile(local_file, tmp_dir / "c2_state.json")
                beacon_file = self.workspace_path / ".funguys" / "beacon.json"
                if beacon_file.exists():
                    shutil.copyfile(beacon_file, tmp_dir / "beacon.json")
                subprocess.run([self.git, "add", "."], cwd=str(tmp_dir), check=True, timeout=10)
                subprocess.run([self.git, "commit", "-q", "-m", "Update FunGuys C2 state"], cwd=str(tmp_dir), check=True, timeout=10)
                remote_url = subprocess.check_output(
                    [self.git, "remote", "get-url", remote],
                    cwd=str(self.workspace_path),
                    text=True,
                    timeout=5,
                ).strip()
                if not remote_url:
                    return False
                subprocess.run(
                    [self.git, "push", "-f", remote_url, f"HEAD:refs/heads/{self.branch}"],
                    cwd=str(tmp_dir),
                    check=True,
                    capture_output=True,
                    timeout=20,
                )
                return True
        except Exception:
            return False

    def fetch_checkpoint(self, remote: str = "origin") -> Optional[Dict[str, Any]]:
        """Fetch and read the latest `c2_state.json` from `refs/heads/fg-c2` (or local fallback)."""
        local_file = self.workspace_path / ".funguys" / "c2_state.json"
        if self.is_available():
            try:
                remote_url = subprocess.check_output(
                    [self.git, "remote", "get-url", remote],
                    cwd=str(self.workspace_path),
                    text=True,
                    timeout=5,
                ).strip()
                if remote_url:
                    subprocess.run(
                        [self.git, "fetch", "-q", remote_url, f"refs/heads/{self.branch}:refs/remotes/{remote}/{self.branch}"],
                        cwd=str(self.workspace_path),
                        check=True,
                        capture_output=True,
                        timeout=15,
                    )
                    raw = subprocess.check_output(
                        [self.git, "show", f"refs/remotes/{remote}/{self.branch}:c2_state.json"],
                        cwd=str(self.workspace_path),
                        text=True,
                        timeout=5,
                    )
                    return json.loads(raw)
            except Exception:
                pass
        if local_file.exists():
            try:
                return json.loads(local_file.read_text(encoding="utf-8"))
            except Exception:
                return None
        return None
