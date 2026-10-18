"""Checked Git operations and isolated assignment worktrees."""
from __future__ import annotations
import subprocess
from pathlib import Path
from funguys.files import bundle, confined_path, source_files, validate_relative_path,runtime_directory,check_path_collisions

class GitWorkspaceManager:
    def __init__(self, workspace_path: Path):
        self.workspace_path = workspace_path.resolve()
        self.workspace_path.mkdir(parents=True, exist_ok=True)
        self.worktrees_dir = runtime_directory(self.workspace_path,'.funguys','w')

    def _run_git(self, args, cwd=None):
        proc = subprocess.run(['git', '-c', 'core.hooksPath=', '-c', 'core.longpaths=true', '-c', 'core.autocrlf=false', *args], cwd=str(cwd or self.workspace_path), capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=60)
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()

    def git(self, *args, cwd=None):
        code, out, err = self._run_git(list(args), cwd)
        if code:
            raise RuntimeError(f'Git {args[0]} failed: {err or out}')
        return out

    def ensure_initialized(self, github_url=None):
        if not (self.workspace_path / '.git').exists():
            self.git('init', '-b', 'main')
            self.git('config', 'user.name', 'FunGuys Orchestrator')
            self.git('config', 'user.email', 'orchestrator@funguys.local')
            ignore = self.workspace_path / '.gitignore'
            if not ignore.exists():
                ignore.write_text('.funguys/\n.graphify/\n.venv/\nnode_modules/\n__pycache__/\n.env\n.env.*\n', encoding='utf-8')
            names = [p.relative_to(self.workspace_path).as_posix() for p in source_files(self.workspace_path)]
            for i in range(0, len(names), 50):
                self.git('add', '--', *names[i:i+50])
            self.git('commit', '--allow-empty', '-m', 'chore: initialize FunGuys workspace')
        if github_url and 'origin' not in self.git('remote').splitlines():
            self.git('remote', 'add', 'origin', github_url)
        self.git('rev-parse', '--verify', 'HEAD')
        self.git('symbolic-ref', '--quiet', 'HEAD')

    def head(self):
        return self.git('rev-parse', 'HEAD')

    def require_clean(self):
        if self.git('status', '--porcelain', '--untracked-files=no'):
            raise ValueError('Commit or stash tracked workspace changes before approving or merging work')

    def commit_contracts(self, content):
        changed = self.git('diff', '--name-only').splitlines() + self.git('diff', '--cached', '--name-only').splitlines()
        if any(p != 'CONTRACTS.md' for p in changed):
            raise ValueError('Commit or stash workspace edits before approving a plan')
        confined_path(self.workspace_path, 'CONTRACTS.md').write_text(content, encoding='utf-8')
        self.git('add', '--', 'CONTRACTS.md')
        if self.git('diff', '--cached', '--name-only'):
            self.git('-c','user.name=FunGuys Orchestrator','-c','user.email=orchestrator@funguys.local','commit', '-m', 'docs: approve versioned project contracts')

    def prepare_candidate(self, assignment_id, base_revision=None):
        candidate = self.worktrees_dir / assignment_id[:16]
        if not candidate.resolve().is_relative_to(self.worktrees_dir.resolve()):
            raise ValueError('Invalid assignment directory')
        self.git('worktree', 'add', '--detach', str(candidate), base_revision or self.head())
        return candidate

    def remove_candidate(self,candidate):
        candidate = Path(candidate)
        if candidate.is_symlink() or (hasattr(candidate,'is_junction') and candidate.is_junction()):
            raise ValueError('Managed worktrees cannot be links')
        candidate = candidate.resolve()
        if candidate.parent != self.worktrees_dir.resolve():
            raise ValueError('Only managed assignment worktrees can be removed')
        self.git('worktree','remove','--force',str(candidate))

    def write_changes(self, candidate, files, deleted_files=()):
        check_path_collisions([*files,*deleted_files])
        destinations = {name: confined_path(candidate, name) for name in files}
        deleted = {name: confined_path(candidate, name) for name in deleted_files}
        if set(destinations) & set(deleted):
            raise ValueError('A file cannot be both updated and deleted')
        for dest in [*destinations.values(), *deleted.values()]:
            if dest.exists() and not dest.is_file():
                raise ValueError('Only regular file changes are accepted')
        for name, dest in destinations.items():
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(files[name])
        for dest in deleted.values():
            dest.unlink(missing_ok=True)

    def commit_candidate(self, candidate, task_id, agent_id, message):
        names = [p.relative_to(candidate).as_posix() for p in source_files(candidate)]
        for name in self.git('ls-files', '-z', cwd=candidate).split('\x00'):
            if not name:
                continue
            try:
                validate_relative_path(name)
            except ValueError:
                continue
            if not (candidate / name).exists():
                names.append(name)
        names = sorted(set(names))
        for i in range(0, len(names), 50):
            self.git('add', '-A', '--', *names[i:i+50], cwd=candidate)
        if not self.git('diff', '--cached', '--name-only', cwd=candidate):
            raise ValueError('Submission contains no changes relative to its assignment')
        self.git('-c', 'user.name=FunGuys Orchestrator', '-c', 'user.email=orchestrator@funguys.local', 'commit', '-m', f'[{task_id}] ({agent_id}) {message[:500]}', cwd=candidate)
        revision = self.git('rev-parse', 'HEAD', cwd=candidate)
        self.git('update-ref', f'refs/heads/task/{task_id}', revision)
        return revision

    def integrate_latest(self, candidate):
        current = self.head()
        code, _, _ = self._run_git(['merge-base', '--is-ancestor', current, 'HEAD'], cwd=candidate)
        if code:
            try:
                self.git('-c', 'user.name=FunGuys Orchestrator', '-c', 'user.email=orchestrator@funguys.local', 'merge', '--no-edit', current, cwd=candidate)
            except RuntimeError as exc:
                self._run_git(['merge', '--abort'], cwd=candidate)
                raise ValueError('Concurrent changes conflict; refresh this assignment') from exc
        return current

    def merge_verified(self, candidate, expected_head):
        self.require_clean()
        if self.head() != expected_head:
            raise ValueError('Workspace changed during verification; refresh this assignment')
        revision = self.git('rev-parse', 'HEAD', cwd=candidate)
        self.git('merge', '--ff-only', revision)
        return revision

    def checkpoint(self, task, files, deleted_files=()):
        if not task.candidate_path:
            return None
        candidate = Path(task.candidate_path)
        if files or deleted_files:
            self.write_changes(candidate, files, deleted_files)
        if self.git('status', '--porcelain', '--untracked-files=normal', cwd=candidate):
            revision = self.commit_candidate(candidate, task.id, task.assigned_agent_id or 'human', 'WIP handoff')
            self.git('update-ref', f'refs/heads/wip/{task.id}', revision)
            return revision
        return self.git('rev-parse', 'HEAD', cwd=candidate)

    def create_handoff_document(self, task_id, from_agent_id, handoff_notes, subgraph_digest):
        return f'# Handoff: {task_id}\nPrevious worker: {from_agent_id}\n\n{handoff_notes}\n\n{subgraph_digest}'

    def get_workspace_files_bundle(self, target_files=None):
        return bundle(self.workspace_path)['files']
