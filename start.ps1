param([int]$Port = 8765)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath 'backend/.venv/Scripts/python.exe')) {
    python -m venv backend/.venv
    if ($LASTEXITCODE -ne 0) { throw 'Python venv creation failed' }
}
$trainerPython = Join-Path $PSScriptRoot 'backend/.venv/Scripts/python.exe'
& $trainerPython -m pip install -c './backend/constraints.txt' -e './backend'
if ($LASTEXITCODE -ne 0) { throw 'Backend dependency installation failed' }
Push-Location -LiteralPath 'frontend'
try {
    npm.cmd ci
    if ($LASTEXITCODE -ne 0) { throw 'Frontend dependency installation failed' }
    npm.cmd run build
    if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed' }
} finally { Pop-Location }
Write-Host "Open http://127.0.0.1:$Port"
& $trainerPython run.py --port $Port
