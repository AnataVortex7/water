# install.ps1 - one-click installer (served by your Render server; __BASE__ / __SELF__ are filled in automatically)
# Win+R -> paste command -> Ctrl+Shift+Enter (runs as Administrator)
# NOTE: messages are ASCII on purpose (avoids encoding problems with `irm | iex`).

$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$ProgressPreference = 'SilentlyContinue'

$Base  = '__BASE__'
$Self  = '__SELF__'
$Dir   = 'C:\WatermarkWorker'
$PyUrl = 'https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe'

# --- 1. Administrator check (fallback if Enter was pressed instead of Ctrl+Shift+Enter)
$id = [Security.Principal.WindowsIdentity]::GetCurrent()
$isAdmin = (New-Object Security.Principal.WindowsPrincipal $id).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host 'Asking for Administrator permission...'
    Start-Process powershell -Verb RunAs -ArgumentList "-NoProfile -ExecutionPolicy Bypass -Command `"irm $Self | iex`""
    exit
}

[Console]::OutputEncoding = [Text.Encoding]::UTF8
$env:PYTHONUTF8 = '1'

function Get-Python {
    foreach ($v in '3.12', '3.11') {
        try {
            $p = & py "-$v" -c "import sys;print(sys.executable)"
            if ($LASTEXITCODE -eq 0 -and $p -and (Test-Path $p)) { return $p }
        } catch {}
    }
    try {
        $cmd = Get-Command python -ErrorAction Stop
        if ($cmd.Source -notlike '*WindowsApps*') {
            $v = & $cmd.Source -c "import sys;print('%d.%d' % sys.version_info[:2])"
            if ($v -eq '3.12' -or $v -eq '3.11') { return $cmd.Source }
        }
    } catch {}
    return $null
}

try {
    # --- 2. Python (install silently if missing; comes from python.org, not from your server)
    $py = Get-Python
    if (-not $py) {
        Write-Host 'Python 3.12 not found - downloading and installing (about 25 MB)...'
        $exe = Join-Path $env:TEMP 'python-installer.exe'
        Invoke-WebRequest $PyUrl -OutFile $exe -UseBasicParsing
        Start-Process $exe -ArgumentList '/quiet', 'InstallAllUsers=1', 'PrependPath=1', 'Include_launcher=1', 'Include_test=0' -Wait
        Remove-Item $exe -Force -ErrorAction SilentlyContinue
        $py = Get-Python
        if (-not $py) { $py = Join-Path $env:ProgramFiles 'Python312\python.exe' }
        if (-not (Test-Path $py)) { throw 'Python install failed' }
    }
    Write-Host "Python: $py"

    # --- 3. Files from your server (small: ~100 KB total). ffmpeg is NOT downloaded from your server.
    New-Item -ItemType Directory -Force $Dir | Out-Null
    Write-Host 'Downloading watermark.py ...'
    Invoke-WebRequest "$Base/watermark.py" -OutFile "$Dir\watermark.py" -UseBasicParsing
    if (-not (Test-Path "$Dir\worker.py")) {
        Write-Host 'Downloading worker.py ...'
        Invoke-WebRequest "$Base/worker.py" -OutFile "$Dir\worker.py" -UseBasicParsing
    } else {
        Write-Host 'worker.py already exists - keeping your version.'
    }
    Write-Host 'Downloading settings ...'
    Invoke-WebRequest "$Base/env" -OutFile "$Dir\.env" -UseBasicParsing

    # --- 4. Run setup (venv, packages, ffmpeg, auto-start task) - no questions asked
    Set-Location $Dir
    & $py "$Dir\watermark.py"
}
catch {
    Write-Host ''
    Write-Host "ERROR: $_" -ForegroundColor Red
}

Write-Host ''
Read-Host 'Press Enter to close this window'
