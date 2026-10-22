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

EXCLUDED = {'.git', '.funguys', '.funguys_inbox.md', '.graphify', '.venv', 'venv', 'env', 'node_modules', '__pycache__',
            '.pytest_cache', '.ruff_cache', '.cache', 'dist', 'build', '.aws', '.ssh', '.codex', '.agents'}
SECRETS = {'.env', '.netrc', '.npmrc', '.pypirc', 'credentials', 'credentials.json', 'id_rsa', 'id_ed25519'}
MAX_FILE, MAX_BUNDLE, MAX_FILES = 15_000_000, 60_000_000, 2000

PLANNER_PRIORITY = {'claude': 0, 'gemini': 1, 'codex': 2, 'mcp': 1, 'ide': 1, 'api': 3}

MODEL_POLICY = {
    'claude': {
        'planning': ('opus', 'high'),
        'important_coding': ('opus', 'high'),
        'gruntwork': ('opus', 'medium'),
        'testing': ('opus', 'medium'),
        'fallback': ('opus', 'medium'),
    },
    'gemini': {
        'planning': ('argon', 'medium'),
        'important_coding': ('argon', 'low'),
        'gruntwork': ('flash', 'high'),
        'testing': ('flash', 'high'),
        'fallback': ('flash', 'high'),
    },
    'codex': {
        'planning': ('astra', 'low'),
        'important_coding': ('sol', 'high'),
        'gruntwork': ('sol', 'high'),
        'testing': ('sol', 'high'),
        'fallback': ('sol', 'high'),
    },
}

SIGNAL_PATTERN = re.compile(
    r'@FG:(CLAIM|RELEASE|FACT|TRIPWIRE|TRIGGER|STEER|HOLD|GO|VETO|DISPUTE|HANDOFF)\s+([^\s\r\n]+)(?:[ \t]+([^\r\n]+))?',
    re.IGNORECASE,
)


def classify_task_role(task, role_override=None):
    """Classify a task into 'planning', 'important_coding', 'gruntwork', or 'testing'."""
    if role_override in ('planning', 'important_coding', 'gruntwork', 'testing'):
        return role_override
    if isinstance(task, dict):
        explicit = task.get('task_role') or task.get('work_role')
        if explicit in ('planning', 'important_coding', 'gruntwork', 'testing'):
            return explicit
        req_tests = bool(task.get('require_tests'))
        req_browser = bool(task.get('require_browser'))
        targets = task.get('target_files') or []
        tier = int(task.get('complexity_tier', 2))
        title_desc = f"{task.get('title', '')} {task.get('description', '')}".lower()
    else:
        explicit = getattr(task, 'task_role', None) or getattr(task, 'work_role', None)
        if explicit in ('planning', 'important_coding', 'gruntwork', 'testing'):
            return explicit
        req_tests = bool(getattr(task, 'require_tests', False))
        req_browser = bool(getattr(task, 'require_browser', False))
        targets = getattr(task, 'target_files', []) or []
        tier = int(getattr(task, 'complexity_tier', 2))
        title_desc = f"{getattr(task, 'title', '')} {getattr(task, 'description', '')}".lower()

    is_test_only = bool(targets) and all(
        'test' in p.lower() or p.lower().startswith('tests/') or p.lower().endswith(('.spec.ts', '.spec.js', '.test.ts', '.test.js'))
        for p in targets
    )
    if req_tests or req_browser or is_test_only or ('playwright' in title_desc and tier == 2):
        return 'testing'
    if tier == 1:
        return 'important_coding'
    return 'gruntwork'


def select_model_and_effort(provider, role='gruntwork', high_tier_used_pct=0.0, force_fallback=False, *, high_tier_exhausted=None):
    """Select (model, effort) for the given provider and role, enforcing fallback rules."""
    table = MODEL_POLICY.get(provider)
    if not table:
        return ('default', 'high')
    if force_fallback:
        return table['fallback']
    if high_tier_exhausted is not None:
        exhausted = bool(high_tier_exhausted)
    elif isinstance(high_tier_used_pct, bool):
        exhausted = high_tier_used_pct
    else:
        exhausted = float(high_tier_used_pct or 0.0) >= 85.0
    if provider == 'gemini' and exhausted:
        return table['fallback']  # ('flash', 'high') - never lower than flash high
    return table.get(role, table['fallback'])


def parse_raw_signals(text):
    """Extract raw (op, target, payload) tuples from CLI stdout or task output."""
    if not text:
        return []
    results = []
    for match in SIGNAL_PATTERN.finditer(text):
        op = match.group(1).upper()
        target = (match.group(2) or '').strip()
        payload = (match.group(3) or '').strip()[:500]
        results.append((op, target, payload))
    return results


def compress_error_log(raw_log, max_chars=900):
    """Deduplicate stack frames and strip ANSI codes to save tokens on QA retries."""
    if not raw_log:
        return ''
    cleaned = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', raw_log)
    lines = [line.rstrip() for line in cleaned.splitlines() if line.strip()]
    deduped = []
    seen = set()
    for line in lines:
        line = re.sub(r'\b0x[0-9a-fA-F]+\b', '0xADDR', line)
        if line in seen and not any(k in line for k in ('Error', 'Exception', 'FAILED', 'AssertionError', 'SyntaxError', 'TypeError')):
            continue
        seen.add(line)
        deduped.append(line)
    compact = '\n'.join(deduped[-28:])
    if len(compact) > max_chars:
        return compact[-max_chars:]
    return compact


def slice_contract_for_task(contracts_markdown, target_files, exported_symbols):
    """Return only relevant sections of contracts_markdown to avoid resending full markdown."""
    if not contracts_markdown:
        return ''
    keywords = set()
    for f in (target_files or []):
        keywords.add(f.lower())
        keywords.add(Path(f).stem.lower())
    for s in (exported_symbols or []):
        keywords.add(s.lower())
    sections = [s for s in re.split(r'(?m)^(?=##?\s+)', contracts_markdown) if s.strip()]
    if len(sections) > 1 and keywords:
        matched = []
        for sec in sections:
            lower_sec = sec.lower()
            if any(k and k in lower_sec for k in keywords):
                matched.append(sec.strip())
        if matched:
            joined = '\n\n'.join(matched)
            return joined[:2000]
    if len(contracts_markdown) <= 900:
        return contracts_markdown
    return contracts_markdown[:1200]


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


def resolve_cli_binary(provider):
    """Locate CLI binary across PATH and common macOS/Linux/Windows install paths."""
    binary = shutil.which(provider)
    if not binary and provider == 'gemini':
        binary = shutil.which('agy')
    if binary:
        return binary
    candidates = [provider, 'agy'] if provider == 'gemini' else [provider]
    home = Path.home()
    extra_dirs = [
        Path('/opt/homebrew/bin'),
        Path('/usr/local/bin'),
        home / '.local' / 'bin',
        home / '.npm-global' / 'bin',
        home / '.nvm' / 'current' / 'bin',
        home / 'Library' / 'pnpm',
    ]
    nvm_versions = home / '.nvm' / 'versions' / 'node'
    if nvm_versions.is_dir():
        try:
            extra_dirs.extend(sorted(nvm_versions.glob('*/bin'), reverse=True))
        except OSError:
            pass
    for directory in extra_dirs:
        for name in candidates:
            candidate = directory / name
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
    return None


def cli_command(provider, root, prompt, model=None, effort=None):
    binary = resolve_cli_binary(provider)
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
        cmd = [*prefix, 'exec', '-C', str(root), '--skip-git-repo-check', '--sandbox', 'workspace-write', '-c', 'approval_policy="never"']
        if model:
            cmd.extend(['-m', model])
        if effort:
            cmd.extend(['-c', f'model_reasoning_effort="{effort}"'])
        cmd.append(prompt)
        return cmd
    if provider == 'claude':
        cmd = [*prefix, '-p', prompt, '--permission-mode', 'acceptEdits']
        if model:
            cmd.extend(['--model', f"{model}-{effort}" if effort else model])
        cmd.extend(['--tools', 'Read,Edit,Write,Glob,Grep'])
        return cmd
    if provider == 'gemini':
        model_tag = f"{model}-{effort}" if (model and effort) else (model or '')
        if Path(binary).stem.lower() == 'agy':
            cmd = [*prefix, '-p', prompt]
            if model_tag:
                cmd.extend(['--model', model_tag])
            cmd.extend(['--sandbox', '--dangerously-skip-permissions'])
            return cmd
        cmd = [*prefix, '-p', prompt]
        if model_tag:
            cmd.extend(['-m', model_tag])
        cmd.append('--sandbox')
        return cmd
    raise RuntimeError('API fallback is not configured; choose an installed CLI provider')


def stop_process(proc):
    if proc.poll() is not None:
        return
    if os.name == 'nt':
        subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'], capture_output=True, timeout=10)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.kill()
            except OSError:
                return
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def execute_cli(provider, root, prompt, cancelled=None, timeout=600, model=None, effort=None, role=None, high_tier_used_pct=0.0):
    if (not model or not effort) and role:
        model, effort = select_model_and_effort(provider, role, high_tier_used_pct)
    env = os.environ.copy()
    if model:
        env['FUNGUYS_MODEL'] = str(model)
    if effort:
        env['FUNGUYS_EFFORT'] = str(effort)
        env['FUNGUYS_REASONING_EFFORT'] = str(effort)
    command = cli_command(provider, root, prompt, model=model, effort=effort)
    code, text = capture_process(command, root, timeout=timeout, env=env, cancelled=cancelled)
    if code != 0 and (model or effort) and re.search(r'unknown option|unrecognized arguments|invalid model|model not found', text, re.I):
        base_command = cli_command(provider, root, prompt)
        code, text = capture_process(base_command, root, timeout=timeout, env=env, cancelled=cancelled)
    limited = bool(re.search(r'rate[\s_-]?limit|\b429\b|quota[\s_-]?exceeded|usage[\s_-]?limit|too many requests', text, re.I))
    # Automatic Argon -> Flash High fallback within Gemini CLI when Argon bucket hits rate limit
    if limited and provider == 'gemini' and model == 'argon':
        fallback_cmd = cli_command(provider, root, prompt, model='flash', effort='high')
        env['FUNGUYS_MODEL'] = 'flash'
        env['FUNGUYS_EFFORT'] = 'high'
        env['FUNGUYS_REASONING_EFFORT'] = 'high'
        code, text = capture_process(fallback_cmd, root, timeout=timeout, env=env, cancelled=cancelled)
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


def controller_arbitration_prompt(dispute, project_name, contracts_markdown, blackboard_digest, tasks_summary, code_excerpts=""):
    """Build the explicit instruction prompt for the Main Controller Agent when arbitrating a swarm dispute."""
    return (
        f"You are the MAIN CONTROLLER & LEAD ARCHITECT AI for project '{project_name}'.\n"
        f"Your role is high-level swarm arbitration, contract enforcement, and task routing (NO direct file editing in this turn).\n\n"
        f"=== ACTIVE SWARM DISPUTE / ESCALATION ===\n"
        f"- Raised By Agent: {dispute.get('sender_agent_id', 'worker')}\n"
        f"- Target File / Module / Task: {dispute.get('target', '')}\n"
        f"- Problem Description: {dispute.get('reason', '')}\n\n"
        f"=== AGREED INTERFACE CONTRACTS (Excerpt) ===\n"
        f"{(contracts_markdown or '')[:2000]}\n\n"
        f"=== ACTIVE TASKS & ASSIGNMENTS ===\n"
        f"{tasks_summary}\n\n"
        f"{blackboard_digest}\n"
        f"{('=== RELEVANT WORKSPACE CODE EXCERPTS ===' + chr(10) + code_excerpts) if code_excerpts else ''}\n\n"
        f"=== YOUR CONTROLLER DUTIES ===\n"
        f"1. Diagnose the root cause of the conflict (e.g., signature mismatch against CONTRACTS.md, file lock contention, or missing export).\n"
        f"2. Provide a concise, binding architectural ruling.\n"
        f"3. Emit 1-line C2 coordination signals (one per line) to enforce your ruling across the worker swarm:\n"
        f"   - `@FG:STEER <agent_id_or_task_id> <directive>` (streams live into the worker's `.funguys_inbox.md`)\n"
        f"   - `@FG:STEER <task_id> ROLE=important_coding <directive>` (re-classifies task tier if needed)\n"
        f"   - `@FG:FACT <key> <value>` (records binding architectural decision on the shared Blackboard)\n"
        f"   - `@FG:HOLD <task_id> <reason>` or `@FG:GO <task_id>` (pauses or unblocks dependent tasks)\n"
        f"   - `@FG:VETO <task_id> <reason>` (cancels a broken implementation and forces rework)\n"
        f"   - `@FG:HANDOFF <agent_id> <target_agent_id> <reason>` (resets an agent's context window and hands off its WIP)\n"
        f"Do not call file-editing tools in this response."
    )


def task_prompt(assignment):
    task = assignment['task']
    contract_slice = slice_contract_for_task(
        assignment.get('contracts_markdown', ''),
        task.get('target_files', []),
        task.get('exported_symbols', []),
    )
    compact_err = compress_error_log(task.get('last_error') or '')
    blackboard_digest = assignment.get('blackboard_digest', '')
    queued_prompts = assignment.get('queued_prompts', [])
    steer_block = ''
    if queued_prompts:
        joined_prompts = '\n'.join(f"- [{p.get('mode','steer').upper()}] {p.get('prompt','')}" for p in queued_prompts)
        steer_block = f"\nController Steering Directives (HIGHEST PRIORITY):\n{joined_prompts}\n"
    return (
        f"Implement {task['id']}: {task['title']}\n{task['description']}\n"
        f"Role: Authenticated FunGuys Worker Agent\n"
        f"Target files: {', '.join(task['target_files'])}\n"
        f"{steer_block}"
        f"Contracts:\n{contract_slice}\n{assignment.get('subgraph_digest','')}\n"
        f"{blackboard_digest}\n"
        f"Handoff / interface updates:\n{task.get('handoff_digest') or ''}\n"
        f"QA failure to fix:\n{compact_err}\n"
        "If a live steering note appears in `.funguys_inbox.md`, follow it immediately.\n"
        "Problem Detection & Dispute Escalation Rules:\n"
        "- If you detect a contradiction between existing code and CONTRACTS.md, a `[LOCK]`/`[TRIPWIRE]` collision on the Blackboard, "
        "or a test/import error caused by a file outside your assigned `target_files`, DO NOT modify unassigned files or invent hacks.\n"
        "- Immediately emit `@FG:DISPUTE <file_or_task_id> <explanation>` in stdout so the Main Controller arbitrates.\n"
        "- If your context window is saturated or you need to pass WIP to another worker, emit `@FG:HANDOFF <target_agent_id_or_self> <handoff_notes>`.\n"
        "Optional 1-line C2 signals you may print in stdout: "
        "`@FG:FACT <key> <value>`, `@FG:CLAIM <file>`, `@FG:RELEASE <file>`, `@FG:TRIPWIRE <file> <reason>`, "
        "`@FG:DISPUTE <target> <reason>`, `@FG:HANDOFF <target> <notes>`, "
        "`@FG:TRIGGER <task_id> <agent_id>`, `@FG:STEER <agent_id> <msg>`.\n"
        "Read existing source before editing. Make focused changes. Do not read credentials or files outside this workspace. Do not claim success without implementing the task."
    )


class WorkerRuntime:
    def __init__(self, config):
        self.config = config
        self.host = config['host_url'].rstrip('/')
        parsed = urllib.parse.urlsplit(self.host)
        if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.username or parsed.path or parsed.query or parsed.fragment:
            raise ValueError('Host must be an HTTP(S) origin')
        profile = config.get('profile', '')
        self.machine = config.get('codename') or (f"{platform.node() or 'worker'}-{profile}" if profile else (platform.node() or 'worker'))
        base_name = hashlib.sha256((self.host + '\n' + self.machine).encode()).hexdigest()[:24]
        prof_name = hashlib.sha256((self.host + '\n' + self.machine + '\n' + profile).encode()).hexdigest()[:24] if profile else base_name
        self.state_dir = Path(config.get('state_dir') or Path.home() / '.funguys' / 'workers')
        self.state_dir.mkdir(parents=True, exist_ok=True)
        if prof_name and (self.state_dir / (prof_name + '.json')).exists():
            self.identity_path = self.state_dir / (prof_name + '.json')
        elif (self.state_dir / (base_name + '.json')).exists():
            try:
                data = json.loads((self.state_dir / (base_name + '.json')).read_text(encoding='utf-8'))
                if not data.get('profile') or data.get('profile') == profile:
                    self.identity_path = self.state_dir / (base_name + '.json')
                else:
                    self.identity_path = self.state_dir / ((prof_name or base_name) + '.json')
            except Exception:
                self.identity_path = self.state_dir / (base_name + '.json')
        else:
            self.identity_path = self.state_dir / ((prof_name or base_name) + '.json')
        if self.identity_path.exists():
            try:
                self.identity = json.loads(self.identity_path.read_text(encoding='utf-8'))
            except Exception:
                self.identity = {'worker_id': uuid.uuid4().hex}
        else:
            self.identity = {'worker_id': uuid.uuid4().hex}
        self.token = self.identity.get('token') or config.get('token', '')
        self.agent_id = self.identity.get('agent_id')
        self.stop = threading.Event()
        self.cancelled = threading.Event()
        self.assignment = None
        self.active_root = None
        self.last_heartbeat = time.monotonic()
        self.identity_lock = None

    def request(self, method, path, body=None, timeout=30):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.host + path, data=data, method=method, headers={'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)

    def save_identity(self):
        self.identity.update(token=self.token, agent_id=self.agent_id, profile=self.config.get('profile', ''))
        temporary = self.identity_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.identity), encoding='utf-8')
        temporary.chmod(0o600)
        temporary.replace(self.identity_path)

    def register(self):
        payload = {
            'worker_id': self.identity['worker_id'],
            'owner_name': self.config.get('name') or self.machine,
            'machine_name': self.machine,
            'profile_key': self.config.get('profile', 'codex-pro'),
            'initial_used_pct': float(self.config.get('used_pct', 0)),
        }
        try:
            result = self.request('POST', '/api/agents/register', payload)
        except urllib.error.HTTPError as exc:
            config_token = self.config.get('token')
            if exc.code in (401, 403) and config_token and config_token != self.token:
                self.token = config_token
                try:
                    result = self.request('POST', '/api/agents/register', payload)
                except urllib.error.HTTPError as retry_exc:
                    if retry_exc.code == 409:
                        self.identity['worker_id'] = uuid.uuid4().hex
                        payload['worker_id'] = self.identity['worker_id']
                        result = self.request('POST', '/api/agents/register', payload)
                    elif retry_exc.code in (401, 403):
                        raise RuntimeError('Worker registration failed: invite token expired, revoked, or invalid. Obtain a fresh invite script from the host.') from retry_exc
                    else:
                        raise
            elif exc.code == 409:
                self.identity['worker_id'] = uuid.uuid4().hex
                payload['worker_id'] = self.identity['worker_id']
                result = self.request('POST', '/api/agents/register', payload)
            elif exc.code in (401, 403):
                raise RuntimeError('Worker credential revoked or expired. Obtain a new invite script from the host.') from exc
            else:
                raise
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
                prompts = result.get('queued_prompts') or []
                changed_contracts = (
                    current
                    and result.get('assignment')
                    and current['task'].get('handoff_digest') != result['assignment']['task'].get('handoff_digest')
                )
                if self.active_root and Path(self.active_root).exists():
                    inbox = Path(self.active_root) / '.funguys_inbox.md'
                    if prompts:
                        try:
                            lines = [f"- [{p.get('mode','steer')}] {p.get('prompt','')}" for p in prompts]
                            with inbox.open('a', encoding='utf-8') as f:
                                f.write('\n'.join(lines) + '\n')
                        except Exception:
                            pass
                    if changed_contracts:
                        new_digest = result['assignment']['task'].get('handoff_digest') or ''
                        current['task']['handoff_digest'] = new_digest
                        try:
                            with inbox.open('a', encoding='utf-8') as f:
                                f.write(f"\n### Live Interface / Guidance Update\n{new_digest}\n")
                        except Exception:
                            pass
                if any(p.get('mode') in ('interrupt', 'handoff') for p in prompts):
                    self.cancelled.set()
                if current and not result['active']:
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
        agent_info = assignment.get('agent', {})
        self.assignment = assignment
        self.cancelled.clear()
        snapshot = self.request('GET', '/api/workspace/snapshot?' + urllib.parse.urlencode({'task_id': task['id'], 'assignment_id': task['assignment_id']}))
        with tempfile.TemporaryDirectory(prefix='funguys-worker-', ignore_cleanup_errors=True) as scratch:
            root = Path(scratch)
            self.active_root = root
            write_snapshot(root, snapshot)
            try:
                provider = agent_info.get('provider', 'codex')
                role = classify_task_role(task, assignment.get('work_role') or assignment.get('task_role'))
                high_pct = float(agent_info.get('high_tier_used_pct', 0.0))
                model = assignment.get('recommended_model')
                effort = assignment.get('recommended_effort')
                if not model or not effort:
                    model, effort = select_model_and_effort(provider, role, high_pct)
                ok, limited, output = execute_cli(
                    provider,
                    root,
                    task_prompt(assignment),
                    self.cancelled,
                    model=model,
                    effort=effort,
                    role=role,
                    high_tier_used_pct=high_pct,
                )
                changes = collect_changes(root, snapshot)
                signals = parse_raw_signals(output)
                if signals:
                    try:
                        self.request('POST', '/api/controller/signal', {
                            'sender_agent_id': self.agent_id,
                            'signals': [{'op': op, 'target': target, 'payload': pay} for op, target, pay in signals],
                        }, timeout=15)
                    except Exception:
                        pass
                if self.cancelled.is_set():
                    if any(changes.values()):
                        try:
                            self.request('POST', f'/api/agents/{self.agent_id}/handoff', {
                                'assignment_id': task['assignment_id'],
                                'handoff_notes': 'Interrupted for Controller promotion, arbitration, or live steering; WIP checkpointed.',
                                'wip_files': changes['files'],
                                'wip_binary_files': changes['binary_files'],
                                'deleted_files': changes['deleted_files'],
                            }, timeout=20)
                        except Exception:
                            pass
                    return
                if limited:
                    self.deliver(f'/api/agents/{self.agent_id}/quota', {'used_pct':100, 'rate_limited':True, 'assignment_id':task['assignment_id'], 'handoff_notes':output[-2000:], 'wip_files':changes['files'], 'wip_binary_files':changes['binary_files'], 'deleted_files':changes['deleted_files']})
                elif not ok:
                    self.deliver(f"/api/tasks/{task['id']}/execution-failed", {'assignment_id':task['assignment_id'], 'error':output[-10000:] or 'CLI failed'})
                elif not any(changes.values()):
                    self.deliver(f"/api/tasks/{task['id']}/execution-failed", {'assignment_id':task['assignment_id'], 'error':'CLI returned without changing the assignment workspace'})
                else:
                    payload = {**changes, 'agent_id':self.agent_id, 'project_id':task['project_id'], 'plan_version':task['plan_version'], 'base_revision':task['base_version'] if 'base_version' in task else task['base_revision'], 'assignment_id':task['assignment_id'], 'submission_id':uuid.uuid4().hex, 'commit_message':f"Complete {task['title']}", 'raw_output': output[-4000:]}
                    result = self.deliver(f"/api/tasks/{task['id']}/submit", payload)
                    print(task['id'] + ': ' + result['status'], flush=True)
            except Exception as exc:
                if self.cancelled.is_set():
                    try:
                        changes = collect_changes(root, snapshot)
                        if any(changes.values()):
                            self.request('POST', f'/api/agents/{self.agent_id}/handoff', {
                                'assignment_id': task['assignment_id'],
                                'handoff_notes': 'Cancelled for Controller promotion, arbitration, or live steering; WIP saved.',
                                'wip_files': changes['files'],
                                'wip_binary_files': changes['binary_files'],
                                'deleted_files': changes['deleted_files'],
                            }, timeout=20)
                    except Exception:
                        pass
                else:
                    try:
                        self.deliver(f"/api/tasks/{task['id']}/execution-failed", {'assignment_id':task['assignment_id'], 'error':str(exc)})
                    except urllib.error.HTTPError:
                        pass
                raise
            finally:
                self.active_root = None
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

    def send_btw(self, message):
        text = (message or '').strip()
        if not text:
            return None
        if not text.lower().startswith('/btw'):
            text = '/btw ' + text
        return self.request('POST', '/api/chat/ask', {'question': text, 'preferred_agent_id': 'auto'}, timeout=60)

    def console_input_loop(self):
        import sys
        try:
            if not sys.stdin or not sys.stdin.isatty():
                return
        except Exception:
            return
        while not self.stop.is_set():
            try:
                line = sys.stdin.readline()
                if not line:
                    break
                cmd = line.strip()
                if not cmd:
                    continue
                if cmd.lower().startswith('/btw') or cmd.startswith('?'):
                    q = cmd[1:].strip() if cmd.startswith('?') else cmd
                    res = self.request('POST', '/api/chat/ask', {'question': q, 'preferred_agent_id': 'auto'}, timeout=60)
                    print(f"\n[Controller C2]: {res.get('answer', '')}\n", flush=True)
            except Exception as exc:
                print(f'Console command error: {exc}', flush=True)

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
            input_thread = threading.Thread(target=self.console_input_loop, daemon=True)
            input_thread.start()
            print(f'Connected as {self.machine}. Waiting for assignments. (Type /btw <guidance> at any time to steer via Controller)', flush=True)
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
