param(
  [int]$Port = 8765,
  [string]$PythonExecutable
)
$ErrorActionPreference = 'Stop'
if (-not $PythonExecutable) {
  $bundledPython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
  if (Test-Path -LiteralPath $bundledPython) { $PythonExecutable = $bundledPython }
  else { $PythonExecutable = (Get-Command python -ErrorAction Stop).Source }
}
& $PythonExecutable -c 'import sys; assert sys.version_info >= (3,10)'
if ($LASTEXITCODE -ne 0) { throw 'Python 3.10+ is required. Specify -PythonExecutable with a working interpreter.' }
$localDirectory = Join-Path $PSScriptRoot '.local'
New-Item -ItemType Directory -Path $localDirectory -Force | Out-Null
$localConfig = Join-Path $localDirectory 'server-config.json'
if (-not (Test-Path -LiteralPath $localConfig)) {
  & $PythonExecutable -c 'import json,secrets,sys; f=open(sys.argv[1],''x'',encoding=''utf-8''); json.dump({''admin_key'':secrets.token_urlsafe(32)},f); f.close()' $localConfig
  if ($LASTEXITCODE -ne 0) { throw 'Could not create local configuration.' }
}
Write-Host "Local dashboard: http://127.0.0.1:$Port/"
Write-Host "Local administrator key file: $localConfig"
& $PythonExecutable -X utf8 (Join-Path $PSScriptRoot 'server.py') --host '127.0.0.1' --port $Port --db (Join-Path $localDirectory 'usage.sqlite3') --prices (Join-Path $PSScriptRoot 'prices.json') --config $localConfig
exit $LASTEXITCODE
