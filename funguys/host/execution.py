"""Explicit execution boundary for generated code. Docker is the default."""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
import uuid
from pathlib import Path
from funguys.files import source_files


class EnvironmentUnavailable(RuntimeError):
    pass


def run_process(argv, cwd, timeout=60, env=None):
    """Bound capture memory/disk and kill the process group on timeout."""
    from funguys.worker.runtime import capture_process
    return capture_process(argv,cwd,timeout=timeout,env=env)


class SandboxRunner:
    def __init__(self, workspace: Path, trusted=None):
        self.workspace = workspace
        self.trusted = (os.environ.get('FUNGUYS_TRUSTED_EXECUTION') == '1') if trusted is None else trusted
        self.image = os.environ.get('FUNGUYS_QA_IMAGE', 'funguys-qa:local')

    def run(self, argv, timeout=60):
        if self.trusted:
            env = {key: value for key, value in os.environ.items() if key.upper() in {
                'PATH', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'TEMP', 'TMP', 'PATHEXT', 'LANG'}}
            env['PYTHONPATH'] = str(self.workspace)
            return run_process(argv, self.workspace, timeout, env)
        docker = shutil.which('docker')
        if not docker:
            raise EnvironmentUnavailable('Docker is required for generated-code QA. Build docker/qa.Dockerfile; use FUNGUYS_TRUSTED_EXECUTION=1 only for a project you trust.')
        try:
            code, _ = run_process([docker, 'image', 'inspect', self.image], self.workspace, 10)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise EnvironmentUnavailable('Docker daemon is unavailable') from exc
        if code:
            raise EnvironmentUnavailable(f'QA image {self.image} is unavailable; build docker/qa.Dockerfile')
        with tempfile.TemporaryDirectory(prefix='funguys-qa-') as scratch:
            root = Path(scratch)
            for path in source_files(self.workspace):
                dest = root / path.relative_to(self.workspace)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, dest)
                dest.chmod(0o666)
            for directory, _, _ in os.walk(root):
                Path(directory).chmod(0o777)
            (root / '.funguys' / 'screenshots').mkdir(parents=True)
            (root / '.funguys').chmod(0o777)
            (root / '.funguys' / 'screenshots').chmod(0o777)
            name = 'funguys-qa-' + uuid.uuid4().hex
            command = [docker, 'run', '--rm', '--name', name, '--network', 'none', '--cap-drop', 'ALL',
                       '--security-opt', 'no-new-privileges', '--pids-limit', '128', '--memory', '1g', '--cpus', '2',
                       '--user', '65534:65534', '--read-only', '--tmpfs', '/tmp:rw,nosuid,size=256m',
                       '--mount', f'type=bind,source={root},target=/workspace', '-w', '/workspace',
                       '-e', 'PYTHONPATH=/workspace', '-e','FUNGUYS_CONTAINER_QA=1',self.image, *argv]
            try:
                result = run_process(command, self.workspace, timeout)
                if result[0] == 125:
                    raise EnvironmentUnavailable('Container runtime failed: ' + result[1][-2000:])
                screenshots = root/'.funguys'/'screenshots'
                if not screenshots.resolve().is_relative_to(root.resolve()) or screenshots.is_symlink() or (hasattr(screenshots,'is_junction') and screenshots.is_junction()):
                    raise EnvironmentUnavailable('Container output escaped its screenshot directory')
                total,count = 0,0
                for image in screenshots.glob('*.png'):
                    if image.is_symlink() or not image.resolve().is_relative_to(root.resolve()) or image.stat().st_size > 5_000_000:
                        continue
                    total,count = total+image.stat().st_size,count+1
                    if total>20_000_000 or count>20:
                        raise EnvironmentUnavailable('Container screenshot output exceeded its limits')
                    if image.read_bytes()[:8]!=b'\x89PNG\r\n\x1a\n':
                        continue
                    dest = self.workspace / '.funguys' / 'screenshots' / image.name
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(image, dest)
                return result
            finally:
                subprocess.run([docker, 'rm', '-f', name], capture_output=True, timeout=10)
