"""Shared, bounded workspace serialization and path confinement."""
from __future__ import annotations

import base64
import os
import unicodedata
from pathlib import Path, PureWindowsPath

EXCLUDED = {'.git', '.funguys', '.graphify', '.venv', 'venv', 'env', 'node_modules',
            '__pycache__', '.pytest_cache', '.ruff_cache', 'dist', 'build', '.aws', '.ssh', '.codex', '.agents'}
SECRET_NAMES = {'.env', '.netrc', '.npmrc', '.pypirc', 'credentials', 'credentials.json', 'id_rsa', 'id_ed25519'}
MAX_FILE = 2_000_000
MAX_BUNDLE = 20_000_000
MAX_FILES = 2000


def check_path_collisions(names):
    keys = [unicodedata.normalize('NFC',name).casefold() for name in names]
    if len(set(keys)) != len(keys):
        raise ValueError('File paths collide on a case-insensitive filesystem')


def is_private(parts) -> bool:
    return any(p.lower() in EXCLUDED or p.lower() in SECRET_NAMES or
               p.lower().startswith('.env.') or p.lower().endswith(('.pem', '.key', '.p12', '.pfx')) for p in parts)


def validate_relative_path(value: str) -> str:
    if not value or len(value)>240 or any(ord(ch)<32 or ch in '\\:*?"<>|' for ch in value):
        raise ValueError(f'Invalid relative file path: {value!r}')
    p = Path(value)
    win = PureWindowsPath(value)
    if p.is_absolute() or win.drive or win.root or any(x in ('', '.', '..') for x in value.split('/')):
        raise ValueError(f'File path must stay inside the workspace: {value!r}')
    if is_private(p.parts) or any(x.endswith((' ', '.')) for x in p.parts):
        raise ValueError(f'Private or reserved file path: {value!r}')
    reserved = {'con', 'prn', 'aux', 'nul', *(f'com{i}' for i in range(1,10)), *(f'lpt{i}' for i in range(1,10))}
    if any(x.split('.')[0].lower() in reserved for x in p.parts):
        raise ValueError('Reserved Windows file name')
    return p.as_posix()


def confined_path(root: Path, value: str) -> Path:
    value = validate_relative_path(value)
    dest = root / value
    resolved_root = root.resolve()
    if not dest.resolve().is_relative_to(resolved_root):
        raise ValueError('Destination escapes workspace')
    for parent in [dest, *dest.parents]:
        if parent == resolved_root:
            break
        if parent.is_symlink() or (hasattr(parent, 'is_junction') and parent.is_junction()):
            raise ValueError('Symlinks and junctions are not accepted in worker paths')
    return dest


def runtime_directory(root: Path, *parts: str) -> Path:
    """Private runtime paths must never follow repository-supplied links."""
    root = root.resolve()
    root.mkdir(parents=True,exist_ok=True)
    current = root
    for part in parts:
        if '/' in part or '\\' in part or part in ('','..','.'):
            raise ValueError('Invalid runtime directory component')
        current = current / part
        if current.is_symlink() or (hasattr(current,'is_junction') and current.is_junction()) or not current.resolve().is_relative_to(root):
            raise ValueError('Runtime directories cannot contain symlinks or junctions')
        if current.exists() and not current.is_dir():
            raise ValueError('Runtime directory is occupied by a file')
        current.mkdir(exist_ok=True)
    return current


def source_files(root: Path):
    if not root.exists():
        return
    for directory, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not is_private([d]) and not (Path(directory) / d).is_symlink()
                         and not (hasattr(Path(directory) / d, 'is_junction') and (Path(directory) / d).is_junction()))
        for name in sorted(names):
            path = Path(directory) / name
            if not is_private([name]) and not path.is_symlink() and path.is_file():
                yield path


def decode_files(files: dict[str, str], binary_files: dict[str, str] | None = None) -> dict[str, bytes]:
    binary_files = binary_files or {}
    check_path_collisions([*files,*binary_files])
    if set(files) & set(binary_files):
        raise ValueError('Duplicate text/binary file')
    if len(files) + len(binary_files) > MAX_FILES:
        raise ValueError('Too many submitted files')
    result = {}
    for name, value in files.items():
        result[validate_relative_path(name)] = value.encode('utf-8')
    for name, value in binary_files.items():
        try:
            result[validate_relative_path(name)] = base64.b64decode(value, validate=True)
        except Exception as exc:
            raise ValueError('Invalid binary file encoding') from exc
    if any(len(v) > MAX_FILE for v in result.values()) or sum(map(len, result.values())) > MAX_BUNDLE:
        raise ValueError('Submission exceeds file or bundle size limit')
    return result


def bundle(root: Path) -> dict:
    text, binary, total = {}, {}, 0
    for path in source_files(root):
        if path.stat().st_size>MAX_FILE:
            raise ValueError('Workspace file exceeds the 2 MB worker transfer limit: '+path.relative_to(root).as_posix())
        with path.open('rb') as stream:
            content = stream.read(MAX_FILE+1)
        total += len(content)
        if len(content) > MAX_FILE or total > MAX_BUNDLE or len(text) + len(binary) >= MAX_FILES:
            raise ValueError('Workspace exceeds worker transfer limits; exclude build artifacts or split the project')
        name = path.relative_to(root).as_posix()
        validate_relative_path(name)
        try:
            text[name] = content.decode('utf-8')
        except UnicodeDecodeError:
            binary[name] = base64.b64encode(content).decode('ascii')
    check_path_collisions([*text,*binary])
    return {'files': text, 'binary_files': binary}


def validate_dag(tasks) -> None:
    if len(tasks)>500:
        raise ValueError('Plans are limited to 500 tasks')
    by_id = {t.id: t for t in tasks}
    if len(by_id) != len(tasks):
        raise ValueError('Duplicate task IDs in plan')
    visited, active = set(), set()
    def visit(task_id):
        if task_id not in by_id:
            raise ValueError(f'Unknown task dependency: {task_id}')
        if task_id in active:
            raise ValueError(f'Cyclic task dependency at {task_id}')
        if task_id in visited:
            return
        active.add(task_id)
        for dep in by_id[task_id].dependencies:
            visit(dep)
        active.remove(task_id)
        visited.add(task_id)
    for task in tasks:
        visit(task.id)
