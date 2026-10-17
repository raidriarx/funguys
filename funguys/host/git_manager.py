"""Git Repository, Worktree Isolation, and Baton-Pass Branch Manager for Host PC."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple


class GitWorkspaceManager:
    """Manages the canonical Git repository and isolated task branches on the Host PC."""

    def __init__(self, workspace_path: Path) -> None:
        self.workspace_path = workspace_path
        self.workspace_path.mkdir(parents=True, exist_ok=True)
        self.worktrees_dir = self.workspace_path / ".funguys" / "worktrees"
        self.worktrees_dir.mkdir(parents=True, exist_ok=True)

    def _run_git(self, args: List[str], cwd: Optional[Path] = None) -> Tuple[int, str, str]:
        target_cwd = cwd or self.workspace_path
        proc = subprocess.run(
            ["git", *args],
            cwd=str(target_cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()

    def ensure_initialized(self, github_url: Optional[str] = None) -> None:
        """Initialize a Git repo if not already present, or link remote GitHub repo."""
        git_dir = self.workspace_path / ".git"
        if not git_dir.exists():
            self._run_git(["init", "-b", "main"])
            self._run_git(["config", "user.name", "FunGuys Orchestrator"])
            self._run_git(["config", "user.email", "orchestrator@funguys.local"])
            gitignore = self.workspace_path / ".gitignore"
            if not gitignore.exists():
                gitignore.write_text(
                    ".funguys/\n__pycache__/\nnode_modules/\n.venv/\n*.pyc\n",
                    encoding="utf-8",
                )
            readme = self.workspace_path / "README.md"
            if not readme.exists():
                readme.write_text("# FunGuys Project Workspace\n", encoding="utf-8")
            self._run_git(["add", "."])
            self._run_git(["commit", "-m", "chore: initialize FunGuys workspace"])

        if github_url:
            code, out, _ = self._run_git(["remote"])
            if "origin" not in out.splitlines():
                self._run_git(["remote", "add", "origin", github_url])

    def prepare_task_branch(self, task_id: str, is_wip_handoff: bool = False) -> str:
        """Ensure a dedicated branch exists for the given task."""
        self.ensure_initialized()
        branch_name = f"wip/{task_id}" if is_wip_handoff else f"task/{task_id}"
        self._run_git(["branch", branch_name, "main"])
        return branch_name

    def apply_worker_changes(
        self,
        task_id: str,
        agent_id: str,
        files: Dict[str, str],
        commit_message: str,
        is_wip: bool = False,
    ) -> Tuple[bool, str]:
        """Write changed files from a worker node and commit them on the Host repo."""
        self.ensure_initialized()
        branch_name = f"wip/{task_id}" if is_wip else f"task/{task_id}"

        # Write files into workspace and commit on branch
        for rel_path, content in files.items():
            safe_rel = Path(rel_path)
            if safe_rel.is_absolute() or ".." in safe_rel.parts:
                continue
            full_path = self.workspace_path / safe_rel
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(content, encoding="utf-8")

        self._run_git(["add", "."])
        code, out, err = self._run_git(
            ["commit", "--allow-empty", "-m", f"[{task_id}] ({agent_id}) {commit_message}"]
        )
        # Also update the task/wip branch ref to current HEAD
        self._run_git(["branch", "-f", branch_name, "HEAD"])
        return code == 0, out or err

    def create_handoff_document(
        self,
        task_id: str,
        from_agent_id: str,
        handoff_notes: str,
        subgraph_digest: str,
    ) -> str:
        """Write a compact Baton-Pass HANDOFF.md for the next teammate's AI."""
        handoff_dir = self.workspace_path / ".funguys" / "handoffs"
        handoff_dir.mkdir(parents=True, exist_ok=True)
        handoff_file = handoff_dir / f"HANDOFF_{task_id}.md"

        content = (
            f"# Baton Pass Handoff — `{task_id}`\n"
            f"- **Previous AI Node:** `{from_agent_id}`\n"
            f"- **WIP Branch:** `wip/{task_id}`\n\n"
            f"## Progress & Immediate Next Step\n"
            f"{handoff_notes}\n\n"
            f"{subgraph_digest}\n"
        )
        handoff_file.write_text(content, encoding="utf-8")
        return content

    def get_workspace_files_bundle(self, target_files: Optional[List[str]] = None) -> Dict[str, str]:
        """Return a dictionary of relative file paths to file contents for worker sync."""
        bundle: Dict[str, str] = {}
        for path in self.workspace_path.rglob("*"):
            rel_parts = path.relative_to(self.workspace_path).parts
            if any(
                part in {".git", ".funguys", "node_modules", "__pycache__", ".venv"}
                for part in rel_parts
            ):
                continue
            if path.is_file():
                rel = path.relative_to(self.workspace_path).as_posix()
                if target_files and not any(tf in rel or rel in tf for tf in target_files) and not rel.startswith(".graphify/"):
                    continue
                try:
                    bundle[rel] = path.read_text(encoding="utf-8")
                except Exception:
                    continue
        return bundle
