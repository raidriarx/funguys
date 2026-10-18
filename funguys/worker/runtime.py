"""Standalone standard-library worker shared by Python, PowerShell and Bash launchers."""
from __future__ import annotations
import argparse
import base64
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import unicodedata
from collections import deque
from pathlib import Path, PureWindowsPath

EXCLUDED = {'.git', '.funguys', '.graphify', '.venv', 'venv', 'env', 'node_modules', '__pycache__',
            '.pytest_cache', '.ruff_cache', 'dist', 'build', '.aws', '.ssh', '.codex', '.agents'}
SECRETS = {'.env', '.netrc', '.npmrc', '.pypirc', 'credentials', 'credentials.json', 'id_rsa', 'id_ed25519'}
MAX_FILE, MAX_BUNDLE, MAX_FILES = 2_000_000, 20_000_000, 2000


def private(parts):
    return any(p.lower() in EXCLUDED or p.lower() in SECRETS or p.lower().startswith('.env.') or
               p.lower().endswith(('.pem', '.key', '.p12', '.pfx')) for p in parts)


def safe_path(root, name):
    win = PureWindowsPath(name)
    if not name or len(name)>240 or any(ord(ch)<32 or ch in '\\:*?"<>|' for ch in name) or win.drive or win.root or any(p in ('', '.', '..') for p in name.split('/')) or private(name.split('/')):
        raise ValueError('Unsafe worker file path')
    if any(p.endswith((' ', '.')) or p.split('.')[0].lower() in {'con','prn','aux','nul', *(f'com{i}' for i in range(1,10)), *(f'lpt{i}' for i in range(1,10))} for p in name.split('/')):
        raise ValueError('Reserved file name')
    path = root / name
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('Worker path escapes its private directory')
    for parent in [path, *path.parents]:
        if parent == root.resolve():
            break
        if parent.is_symlink() or (hasattr(parent, 'is_junction') and parent.is_junction()):
            raise ValueError('Worker paths cannot contain symlinks or junctions')
    return path


def decoded_snapshot(snapshot):
    result = {name: value.encode('utf-8') for name,value in snapshot.get('files', {}).items()}
    for name,value in snapshot.get('binary_files', {}).items():
        if name in result:
            raise ValueError('Duplicate snapshot path')
        result[name] = base64.b64decode(value, validate=True)
    keys = [unicodedata.normalize('NFC',name).casefold() for name in result]
    if len(set(keys)) != len(keys):
        raise ValueError('Snapshot paths collide on a case-insensitive filesystem')
    if len(result) > MAX_FILES or any(len(v) > MAX_FILE for v in result.values()) or sum(map(len,result.values())) > MAX_BUNDLE:
        raise ValueError('Snapshot exceeds worker limits')
    return result


def write_snapshot(root, snapshot):
    data = decoded_snapshot(snapshot)
    paths = {name: safe_path(root, name) for name in data}
    for name,path in paths.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data[name])


def collect_changes(root, snapshot):
    original, current, total = decoded_snapshot(snapshot), {}, 0
    for directory, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if not private([d]) and not (Path(directory)/d).is_symlink() and not (hasattr(Path(directory)/d, 'is_junction') and (Path(directory)/d).is_junction())]
        for name in names:
            if private([name]):
                continue
            path = Path(directory) / name
            if path.is_symlink():
                raise ValueError('Generated symlink cannot be submitted')
            rel = path.relative_to(root).as_posix()
            safe_path(root, rel)
            if path.stat().st_size>MAX_FILE:
                raise ValueError('Generated file exceeds the worker transfer limit')
            with path.open('rb') as stream:
                value = stream.read(MAX_FILE+1)
            total += len(value)
            if len(value) > MAX_FILE or total > MAX_BUNDLE or len(current) >= MAX_FILES:
                raise ValueError('Worker output exceeds transfer limits')
            current[rel] = value
    files, binary = {}, {}
    for name,value in current.items():
        if original.get(name) == value:
            continue
        try:
            files[name] = value.decode('utf-8')
        except UnicodeDecodeError:
            binary[name] = base64.b64encode(value).decode('ascii')
    keys = [unicodedata.normalize('NFC',name).casefold() for name in [*files,*binary,*(set(original)-set(current))]]
    if len(set(keys)) != len(keys):
        raise ValueError('Output paths collide on a case-insensitive filesystem')
    return {'files': files, 'binary_files': binary, 'deleted_files': sorted(set(original) - set(current))}


def cli_command(provider, root, prompt):
    binary = shutil.which(provider)
    if not binary:
        raise RuntimeError(f'The selected {provider} CLI is not installed on this machine; no provider was substituted')
    prefix = [binary]
    if Path(binary).suffix.lower() in ('.cmd','.bat'):
        entries = {'codex':'@openai/codex/bin/codex.js','claude':'@anthropic-ai/claude-code/cli.js','gemini':'@google/gemini-cli/dist/index.js'}
        entry = Path(binary).parent/'node_modules'/entries.get(provider,'missing')
        node = shutil.which('node')
        if not node or not entry.is_file():
            raise RuntimeError('A native CLI or a discoverable Node entry point is required; batch wrappers are not executed with task text')
        prefix = [node,str(entry)]
    if provider == 'codex':
        return [*prefix, 'exec', '-C', str(root), '--skip-git-repo-check', '--sandbox', 'workspace-write', '-c', 'approval_policy="never"', prompt]
    if provider == 'claude':
        return [*prefix, '-p', prompt, '--permission-mode', 'acceptEdits', '--tools', 'Read,Edit,Write,Glob,Grep']
    if provider == 'gemini':
        return [*prefix, '-p', prompt, '--sandbox']
    raise RuntimeError('API fallback is not configured; choose an installed CLI provider')


def stop_process(proc):
    if proc.poll() is not None:
        return
    if os.name == 'nt':
        subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'], capture_output=True, timeout=10)
    else:
        os.killpg(proc.pid, signal.SIGKILL)
    proc.wait(timeout=10)


def execute_cli(provider, root, prompt, cancelled=None, timeout=240):
    command = cli_command(provider, root, prompt)
    code,text = capture_process(command,root,timeout=timeout,cancelled=cancelled)
    limited = bool(re.search(r'rate[\s_-]?limit|\b429\b|quota[\s_-]?exceeded|usage[\s_-]?limit|too many requests', text, re.I))
    return code == 0, limited, text


def capture_process(command,root,timeout=60,env=None,cancelled=None):
    """Drain stdout continuously, retaining only a bounded in-memory tail."""
    options = {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {'start_new_session': True}
    chunks = deque(maxlen=2)
    output_lock = threading.Lock()
    proc = subprocess.Popen(command,cwd=str(root),stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,env=env,**options)
    def drain():
        try:
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                with output_lock:
                    chunks.append(chunk)
        finally:
            proc.stdout.close()
    reader = threading.Thread(target=drain,daemon=True)
    reader.start()
    started = time.monotonic()
    try:
        while proc.poll() is None:
            if cancelled and cancelled.is_set():
                raise RuntimeError('Assignment cancelled; partial work was not marked complete')
            if time.monotonic()-started>timeout:
                raise subprocess.TimeoutExpired(command,timeout)
            time.sleep(.05)
    finally:
        try:
            if proc.poll() is None:
                stop_process(proc)
        finally:
            reader.join(timeout=5)
    with output_lock:
        text = b''.join(chunks)[-100_000:].decode('utf-8',errors='replace')
    return proc.returncode,text


def task_prompt(assignment):
    task = assignment['task']
    return (f"Implement {task['id']}: {task['title']}\n{task['description']}\nTarget files: {', '.join(task['target_files'])}\n"
            f"Contracts:\n{assignment.get('contracts_markdown','')}\n{assignment.get('subgraph_digest','')}\n"
            f"Handoff / interface updates:\n{task.get('handoff_digest') or ''}\nQA failure to fix:\n{task.get('last_error') or ''}\n"
            'Read existing source before editing. Make focused changes. Do not read credentials or files outside this workspace. Do not claim success without implementing the task.')


class WorkerRuntime:
    def __init__(self, config):
        self.config = config
        self.host = config['host_url'].rstrip('/')
        parsed = urllib.parse.urlsplit(self.host)
        if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.username or parsed.path or parsed.query or parsed.fragment:
            raise ValueError('Host must be an HTTP(S) origin')
        self.machine = config.get('codename') or platform.node() or 'worker'
        name = hashlib.sha256((self.host + '\n' + self.machine).encode()).hexdigest()[:24]
        self.state_dir = Path(config.get('state_dir') or Path.home() / '.funguys' / 'workers')
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.identity_path = self.state_dir / (name + '.json')
        self.identity = json.loads(self.identity_path.read_text(encoding='utf-8')) if self.identity_path.exists() else {'worker_id': uuid.uuid4().hex}
        self.token = self.identity.get('token') or config.get('token', '')
        self.agent_id = self.identity.get('agent_id')
        self.stop = threading.Event()
        self.cancelled = threading.Event()
        self.assignment = None
        self.last_heartbeat = time.monotonic()
        self.identity_lock = None

    def request(self, method, path, body=None, timeout=30):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.host + path, data=data, method=method, headers={'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)

    def save_identity(self):
        self.identity.update(token=self.token, agent_id=self.agent_id)
        temporary = self.identity_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.identity), encoding='utf-8')
        temporary.chmod(0o600)
        temporary.replace(self.identity_path)

    def register(self):
        result = self.request('POST', '/api/agents/register', {'worker_id': self.identity['worker_id'], 'owner_name': self.config.get('name') or self.machine, 'machine_name': self.machine, 'profile_key': self.config.get('profile','codex-pro'), 'initial_used_pct': float(self.config.get('used_pct',0))})
        self.agent_id = result['id']
        self.token = result.get('worker_token', self.token)
        self.save_identity()
        return result

    def heartbeat_loop(self):
        while not self.stop.is_set():
            try:
                current = self.assignment
                result = self.request('POST', f'/api/agents/{self.agent_id}/heartbeat', {'assignment_id': current['task']['assignment_id'] if current else None}, timeout=10)
                self.last_heartbeat = time.monotonic()
                changed_contracts = current and result.get('assignment') and current['task'].get('handoff_digest') != result['assignment']['task'].get('handoff_digest')
                if current and (not result['active'] or changed_contracts):
                    self.cancelled.set()
            except Exception:
                if time.monotonic() - self.last_heartbeat > 60:
                    self.cancelled.set()
            self.stop.wait(10)

    def deliver(self, path, payload):
        # Keep one immutable payload/submission ID across transient response loss.
        while not self.stop.is_set():
            try:
                return self.request('POST', path, payload, timeout=300)
            except urllib.error.HTTPError as exc:
                if exc.code < 500:
                    raise
            except (urllib.error.URLError, TimeoutError, OSError):
                pass
            self.stop.wait(3)

    def _perform_assignment(self, assignment):
        task = assignment['task']
        self.assignment = assignment
        self.cancelled.clear()
        snapshot = self.request('GET', '/api/workspace/snapshot?' + urllib.parse.urlencode({'task_id': task['id'], 'assignment_id': task['assignment_id']}))
        with tempfile.TemporaryDirectory(prefix='funguys-worker-') as scratch:
            root = Path(scratch)
            write_snapshot(root, snapshot)
            try:
                ok, limited, output = execute_cli(assignment['agent']['provider'], root, task_prompt(assignment), self.cancelled)
                changes = collect_changes(root, snapshot)
                if self.cancelled.is_set():
                    return
                if limited:
                    self.deliver(f'/api/agents/{self.agent_id}/quota', {'used_pct':100, 'rate_limited':True, 'assignment_id':task['assignment_id'], 'handoff_notes':output[-2000:], 'wip_files':changes['files'], 'wip_binary_files':changes['binary_files'], 'deleted_files':changes['deleted_files']})
                elif not ok:
                    self.deliver(f"/api/tasks/{task['id']}/execution-failed", {'assignment_id':task['assignment_id'], 'error':output[-10000:] or 'CLI failed'})
                elif not any(changes.values()):
                    self.deliver(f"/api/tasks/{task['id']}/execution-failed", {'assignment_id':task['assignment_id'], 'error':'CLI returned without changing the assignment workspace'})
                else:
                    payload = {**changes, 'agent_id':self.agent_id, 'project_id':task['project_id'], 'plan_version':task['plan_version'], 'base_revision':task['base_revision'], 'assignment_id':task['assignment_id'], 'submission_id':uuid.uuid4().hex, 'commit_message':f"Complete {task['title']}"}
                    result = self.deliver(f"/api/tasks/{task['id']}/submit", payload)
                    print(task['id'] + ': ' + result['status'], flush=True)
            except Exception as exc:
                if not self.cancelled.is_set():
                    try:
                        self.deliver(f"/api/tasks/{task['id']}/execution-failed", {'assignment_id':task['assignment_id'], 'error':str(exc)})
                    except urllib.error.HTTPError:
                        pass
                raise
            finally:
                self.assignment = None

    def execute(self,assignment):
        try:
            return self._perform_assignment(assignment)
        except Exception as exc:
            if not self.cancelled.is_set():
                try:
                    self.deliver(f"/api/tasks/{assignment['task']['id']}/execution-failed", {'assignment_id':assignment['task']['assignment_id'],'error':str(exc)[:10000]})
                except urllib.error.HTTPError:
                    pass
            raise
        finally:
            self.assignment = None

    def run(self):
        lock = self.identity_path.with_suffix('.lock').open('a+b')
        lock.seek(0)
        if os.name == 'nt':
            import msvcrt
            if lock.read(1) == b'':
                lock.write(b'0')
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.identity_lock = lock
        try:
            self.register()
            thread = threading.Thread(target=self.heartbeat_loop, daemon=True)
            thread.start()
            print(f'Connected as {self.machine}. Waiting for assignments.', flush=True)
            try:
                while not self.stop.is_set():
                    try:
                        result = self.request('GET', '/api/worker/assignment')
                        if result.get('assignment'):
                            self.execute(result['assignment'])
                    except urllib.error.HTTPError as exc:
                        if exc.code in (401,403):
                            raise RuntimeError('Worker credential revoked. Obtain a new invite and remove the old worker identity file.') from exc
                        print(f'Assignment request: HTTP {exc.code}', flush=True)
                    except Exception as exc:
                        print(f'Worker retry: {exc}', flush=True)
                    self.stop.wait(3)
            finally:
                self.stop.set()
                self.cancelled.set()
                thread.join(timeout=12)
        finally:
            lock.close()


def main():
    import sys
    if sys.version_info < (3,11):
        raise SystemExit('FunGuys workers require Python 3.11 or newer')
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True, help='Base64 encoded JSON; data only')
    args = parser.parse_args()
    config = json.loads(base64.b64decode(args.config, validate=True))
    try:
        WorkerRuntime(config).run()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
