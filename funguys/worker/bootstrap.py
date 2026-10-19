"""Small launchers encode configuration as data and share one tested worker."""
import base64
import hashlib
import json
from pathlib import Path


def launcher(config, powershell=False):
    encoded = base64.b64encode(json.dumps(config).encode()).decode('ascii')
    # Normalize line endings to LF so Windows git checkout CRLF doesn't break HTTP transfer checksum
    runtime_bytes = Path(__file__).with_name('runtime.py').read_bytes().replace(b'\r\n', b'\n')
    checksum = hashlib.sha256(runtime_bytes).hexdigest()
    if powershell:
        return r'''$ErrorActionPreference = 'Stop'
$EncodedConfig = '__CONFIG__'
$Config = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($EncodedConfig)) | ConvertFrom-Json
$Python = Get-Command python -ErrorAction SilentlyContinue
if (-not $Python) { $Python = Get-Command py -ErrorAction SilentlyContinue }
if (-not $Python) { throw 'Install Python 3.11+ and the selected AI CLI, then run this invite again. No FunGuys package is required.' }
$Script = Join-Path ([IO.Path]::GetTempPath()) ('funguys-worker-' + [Guid]::NewGuid().ToString('N') + '.py')
try {
    Invoke-WebRequest -UseBasicParsing -Uri ($Config.host_url + '/worker.py') -OutFile $Script
    if ((Get-FileHash -LiteralPath $Script -Algorithm SHA256).Hash.ToLowerInvariant() -ne '__SHA__') { throw 'Worker checksum mismatch; fetch a fresh invite script.' }
    & $Python.Source $Script --config $EncodedConfig
    if ($LASTEXITCODE -ne 0) { throw 'Worker stopped. Read the error above before retrying.' }
} finally {
    if (Test-Path -LiteralPath $Script) { Remove-Item -LiteralPath $Script -Force }
}
'''.replace('__CONFIG__', encoded).replace('__SHA__', checksum)
    return '''#!/usr/bin/env bash
set -euo pipefail
command -v python3 >/dev/null || { echo 'Python 3.11+ and the selected AI CLI are required; no FunGuys package is needed.' >&2; exit 1; }
CONFIG='__CONFIG__'
HOST=$(python3 -c 'import base64,json,sys; print(json.loads(base64.b64decode(sys.argv[1]))["host_url"])' "$CONFIG")
DIR=$(mktemp -d "${TMPDIR:-/tmp}/funguys-launch.XXXXXXXX")
SCRIPT="$DIR/worker.py"
trap 'rm -f -- "$SCRIPT"; rmdir -- "$DIR"' EXIT
curl --fail --silent --show-error --location "$HOST/worker.py" -o "$SCRIPT"
python3 -c 'import hashlib,pathlib,sys; assert hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest() == sys.argv[2], "Worker checksum mismatch; fetch a fresh invite"' "$SCRIPT" '__SHA__'
python3 "$SCRIPT" --config "$CONFIG"
'''.replace('__CONFIG__', encoded).replace('__SHA__', checksum)
