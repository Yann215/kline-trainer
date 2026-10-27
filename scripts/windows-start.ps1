$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $root

try {
    if (-not (Test-Path -LiteralPath 'frontend/dist/index.html')) {
        throw 'The prebuilt frontend is missing. Download the Windows resource bundle or build frontend first.'
    }
    $python = Join-Path $root 'backend/.venv/Scripts/python.exe'
    $pythonw = Join-Path $root 'backend/.venv/Scripts/pythonw.exe'
    $marker = Join-Path $root 'backend/.venv/.trainer-installed'
    if (-not (Test-Path -LiteralPath $python)) {
        Write-Host 'Creating Python environment...'
        $basePython = $null
        $baseArgs = @()
        # The launcher can exist without any registered Pythons; check candidates before using them.
        $oldPreference = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            if (Get-Command python.exe -ErrorAction SilentlyContinue) {
                & python.exe -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>$null
                if ($LASTEXITCODE -eq 0) { $basePython = 'python.exe' }
            }
            if (-not $basePython -and (Get-Command py.exe -ErrorAction SilentlyContinue)) {
                & py.exe -3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>$null
                if ($LASTEXITCODE -eq 0) { $basePython = 'py.exe'; $baseArgs = @('-3') }
            }
        } finally {
            $ErrorActionPreference = $oldPreference
        }
        if (-not $basePython) { throw 'Install Python 3.11 or newer and retry.' }
        & $basePython @baseArgs -m venv 'backend/.venv'
        if ($LASTEXITCODE -ne 0) { throw 'Python environment creation failed. Install Python 3.11 or newer and retry.' }
    }
    if (-not (Test-Path -LiteralPath $marker)) {
        Write-Host 'Installing dependencies (internet connection required)...'
        & $python -m pip install -c 'backend/constraints.txt' -e './backend'
        if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. Check Python version and network, then retry.' }
        Set-Content -LiteralPath $marker -Value 'installed' -Encoding ASCII
    }
    if (-not (Test-Path -LiteralPath $pythonw)) { throw 'pythonw.exe is missing from the Python environment.' }
    $launcher = Join-Path $root 'desktop_launcher.pyw'
    Start-Process -FilePath $pythonw -ArgumentList ('"' + $launcher + '"') -WorkingDirectory $root
    Write-Host 'Starting Kline Trainer...'
} catch {
    Write-Host "Startup failed: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
