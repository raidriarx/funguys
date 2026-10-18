"""Parse generated launchers without running their enrollment or CLI commands."""
import os
from pathlib import Path
import shutil
import subprocess
import pytest
from funguys.worker.bootstrap import launcher


def config():
    return {'host_url':'https://example.com','token':'invite','name':'$(printf unsafe) " quotes\n\'\'\'','codename':'../../not-a-path','profile':'codex-pro','used_pct':0}


def test_generated_powershell_is_syntactically_valid(tmp_path):
    powershell=shutil.which('powershell') or shutil.which('pwsh')
    if not powershell:
        pytest.skip('PowerShell is not installed on this platform')
    script=tmp_path/'join.ps1'
    script.write_text(launcher(config(),powershell=True),encoding='utf-8')
    command="$tokens=$null; $errors=$null; $null=[System.Management.Automation.Language.Parser]::ParseInput([IO.File]::ReadAllText($env:FUNGUYS_TEST_SCRIPT),[ref]$tokens,[ref]$errors); if ($errors.Count -gt 0) { $errors | ForEach-Object { Write-Output $_.Message }; exit 1 }"
    result=subprocess.run([powershell,'-NoProfile','-NonInteractive','-Command',command],env={**os.environ,'FUNGUYS_TEST_SCRIPT':str(script)},capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stdout+result.stderr


def test_generated_bash_is_syntactically_valid(tmp_path):
    bash=shutil.which('bash')
    if not bash and Path('C:/Program Files/Git/bin/bash.exe').is_file():
        bash='C:/Program Files/Git/bin/bash.exe'
    if not bash:
        pytest.skip('Bash is not installed on this platform')
    script=tmp_path/'join.sh'
    script.write_text(launcher(config()),encoding='utf-8',newline='')
    result=subprocess.run([bash,'-n',str(script)],capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stdout+result.stderr
