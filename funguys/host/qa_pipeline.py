"""QA reports distinguish verified, skipped, failed and unavailable checks."""
from __future__ import annotations
import ast
import asyncio
import json
import shlex
import sys
import uuid
from pathlib import Path
from funguys.files import source_files, confined_path,runtime_directory
from funguys.host.execution import SandboxRunner, EnvironmentUnavailable
from funguys.models import QARunReport

BROWSER_SCRIPT = r'''
import json, pathlib, threading, http.server, functools
from playwright.sync_api import sync_playwright
root = pathlib.Path.cwd()
config = json.loads(__import__('sys').argv[1])
server = None
url = config['url']
if not url:
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = 'http://127.0.0.1:%s/%s' % (server.server_port, config['html'])
errors = []
try:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, chromium_sandbox=not bool(__import__('os').environ.get('FUNGUYS_CONTAINER_QA')))
        try:
            page = browser.new_page()
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.on('console', lambda m: errors.append(m.text) if m.type == 'error' else None)
            response = page.goto(url, wait_until='networkidle', timeout=15000)
            if response and response.status >= 400:
                errors.append('HTTP %s' % response.status)
            if not page.locator('body').inner_text().strip() and not page.locator('canvas,svg,img,video').count():
                errors.append('Empty application body')
            page.screenshot(path=config['screenshot'], full_page=True)
            print(json.dumps({'url': url, 'title': page.title(), 'errors': errors}))
        finally:
            browser.close()
finally:
    if server:
        server.shutdown()
raise SystemExit(1 if errors else 0)
'''

class AutonomousQAPipeline:
    def __init__(self, workspace_path: Path, trusted=None):
        self.workspace_path = workspace_path
        self.screenshots_dir = runtime_directory(workspace_path,'.funguys','screenshots')
        self.runner = SandboxRunner(workspace_path, trusted)

    async def verify_task(self, task, live_preview_url=None):
        checks, logs = {}, []
        blocked = False
        static_ok, detail = await asyncio.to_thread(self._run_static_checks, task)
        checks['static'] = 'passed' if static_ok else 'failed'
        logs.append('Static: ' + detail)
        if static_ok:
            try:
                unit_ok, detail, skipped = await asyncio.to_thread(self._run_unit_tests, task)
                checks['unit'] = 'skipped' if skipped else ('passed' if unit_ok else 'failed')
                logs.append('Tests/build: ' + detail)
            except EnvironmentUnavailable as exc:
                unit_ok, blocked = False, True
                checks['unit'] = 'unavailable'
                logs.append(str(exc))
            if unit_ok:
                try:
                    browser_ok, detail, screenshot = await self._run_playwright_check(task, task.retry_count + 1, live_preview_url)
                    checks['browser'] = 'skipped' if detail.startswith('SKIPPED:') else ('passed' if browser_ok else 'failed')
                    logs.append('Browser: ' + detail)
                except EnvironmentUnavailable as exc:
                    browser_ok, blocked, screenshot = False, True, None
                    checks['browser'] = 'unavailable'
                    logs.append(str(exc))
            else:
                browser_ok, screenshot = False, None
                checks['browser'] = 'skipped'
        else:
            unit_ok, browser_ok, screenshot = False, False, None
            checks.update(unit='skipped', browser='skipped')
        return QARunReport(id='qa-' + uuid.uuid4().hex, task_id=task.id, attempt=task.retry_count + 1,
                           static_passed=static_ok, unit_passed=checks['unit'] == 'passed',
                           playwright_passed=checks['browser'] == 'passed',
                           overall_passed=static_ok and unit_ok and browser_ok,
                           logs='\n'.join(logs), screenshot_path=screenshot, checks=checks,
                           environment_blocked=blocked, escalated_to_tier1=not (static_ok and unit_ok and browser_ok) and task.retry_count >= 1)

    def _run_static_checks(self, task):
        errors, count = [], 0
        for path in source_files(self.workspace_path):
            from funguys.files import MAX_FILE
            if path.stat().st_size>MAX_FILE:
                errors.append('Source file exceeds the static analysis size limit: '+path.relative_to(self.workspace_path).as_posix())
                continue
            if path.suffix == '.py':
                count += 1
                try:
                    ast.parse(path.read_text(encoding='utf-8-sig'), filename=str(path))
                except (SyntaxError, UnicodeError) as exc:
                    errors.append(f'{path.relative_to(self.workspace_path)}: {exc}')
            elif path.suffix == '.json':
                try:
                    json.loads(path.read_text(encoding='utf-8-sig'))
                except (ValueError, UnicodeError) as exc:
                    errors.append(f'{path.relative_to(self.workspace_path)}: {exc}')
        for name in task.target_files:
            if not confined_path(self.workspace_path, name).is_file():
                errors.append('Missing target file: ' + name)
        return not errors, '\n'.join(errors) if errors else f'Parsed {count} Python files; checked JSON and required targets'

    def _execute(self, argv, timeout=90):
        if self.runner.trusted:
            if argv[0] in ('python', 'python3'):
                argv = [sys.executable, *argv[1:]]
            else:
                import shutil
                target = Path(self.workspace_path) / argv[0]
                if target.is_file():
                    resolved = str(target.resolve())
                else:
                    resolved = shutil.which(argv[0])
                if not resolved:
                    raise EnvironmentUnavailable(f'Verification tool not found on host PATH or workspace: {argv[0]}')
                argv = [resolved, *argv[1:]]
        try:
            return self.runner.run(argv, timeout)
        except FileNotFoundError as exc:
            raise EnvironmentUnavailable(f'Verification tool not found: {argv[0]}') from exc

    def _run_unit_tests(self, task):
        commands = []
        for cmd in task.verification_commands:
            c = cmd.strip()
            if not c:
                continue
            # Strip PowerShell call operator & prefix and quotes around binary if present
            if c.startswith('& '):
                c = c[2:].strip()
            parts = shlex.split(c, posix=(os.name != 'nt'))
            if parts:
                commands.append(parts)
        for path in source_files(self.workspace_path):
            if path.suffix in ('.js','.mjs','.cjs'):
                commands.append(['node','--check',path.relative_to(self.workspace_path).as_posix()])
        tests = [p for p in source_files(self.workspace_path) if p.name.startswith('test_') and p.suffix == '.py']
        if tests:
            commands.append(['python', '-m', 'pytest', '-q', '-p', 'no:cacheprovider'])
        package = self.workspace_path / 'package.json'
        if package.exists():
            scripts = json.loads(package.read_text(encoding='utf-8')).get('scripts', {})
            for name in ('build', 'test'):
                if name in scripts:
                    commands.append(['npm', 'run', name, '--', '--run'] if name == 'test' and 'vitest' in scripts[name] else ['npm', 'run', name])
        if not commands:
            unsupported = {Path(name).suffix for name in task.target_files} & {'.ts','.tsx','.jsx','.rs','.go','.sol','.java','.cs','.cpp','.c'}
            if unsupported:
                return False,'Configure a compiler/build verification command for '+', '.join(sorted(unsupported)),False
            return (False, 'A required test suite or verification command is missing', False) if task.require_tests else (True, 'SKIPPED: no test suite or verification commands supplied', True)
        logs = []
        for argv in commands:
            try:
                code, output = self._execute(argv)
            except EnvironmentUnavailable:
                raise
            except Exception as exc:
                return False, f'Execution failed: {exc}', False
            logs.append('$ ' + shlex.join(argv) + '\n' + output)
            if code:
                return False, '\n'.join(logs), False
        return True, '\n'.join(logs), False

    async def _run_playwright_check(self, task, attempt, live_preview_url):
        html = sorted([p for p in source_files(self.workspace_path) if p.suffix == '.html'], key=lambda p: (p.name != 'index.html', str(p)))
        if not live_preview_url and not html:
            return (False, 'Required browser target is missing', None) if task.require_browser else (True, 'SKIPPED: no browser target for this task', None)
        filename = f'qa_{task.id}_{uuid.uuid4().hex[:8]}.png'
        screenshot = '.funguys/screenshots/' + filename
        config = {'url': live_preview_url, 'html': html[0].relative_to(self.workspace_path).as_posix() if html else None, 'screenshot': screenshot}
        try:
            code, out = await asyncio.to_thread(self._execute, ['python', '-c', BROWSER_SCRIPT, json.dumps(config)], 45)
        except EnvironmentUnavailable:
            raise
        except Exception as exc:
            return False, f'Browser verification failed: {exc}', None
        exists = (self.screenshots_dir / filename).exists()
        if code and any(message in out.lower() for message in ("executable doesn't exist",'playwright install','no usable sandbox','error while loading shared libraries')):
            raise EnvironmentUnavailable('Browser environment unavailable: ' + out[-2000:])
        return code == 0 and exists, out or 'Browser produced no result', f'/screenshots/{filename}' if exists else None
