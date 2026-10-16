# Money Integrated Launcher (PowerShell)
#
# 1. Check & auto-update today's market prices
# 2. Start Backend API Server (Port 3001)
# 3. Start Dashboard Web Server (Port 3000)
# 4. Open Default Web Browser (http://localhost:3000)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RootDir = $PSScriptRoot
$DashboardDir = Join-Path $RootDir "dashboard"

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "  Money System Launcher" -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Cyan

# 1. Detect Python
$PyBin = $null
foreach ($candidate in @("python", "python3", "C:\Python313\python.exe")) {
    try {
        $ver = & $candidate --version 2>&1
        if ($ver -match "Python 3") {
            $PyBin = $candidate
            break
        }
    } catch { }
}
if (-not $PyBin) {
    Write-Error "Python 3 is required."
    exit 1
}
Write-Host "[1/4] Python detected: $PyBin" -ForegroundColor Green

# Set PYTHONPATH to project root
$env:PYTHONPATH = $RootDir

# 2. Ensure Initial Report Data
& $PyBin -c "from scripts.stock_service import ensure_initial_report; ensure_initial_report()" 2>$null

# 3. Start Backend API Server
Write-Host "[2/3] Starting Backend REST API Server (http://127.0.0.1:3001)..." -ForegroundColor Yellow
$ApiProcess = Start-Process -FilePath $PyBin -ArgumentList @("-m", "scripts.api_server", "--port", "3001") -WorkingDirectory $RootDir -PassThru -NoNewWindow
Start-Sleep -Milliseconds 800

# 3. Open Browser in Full Size & Run Dashboard
Write-Host "[3/3] Starting Web Dashboard (http://localhost:3000)..." -ForegroundColor Green
Write-Host "Waiting for Dashboard server to be ready before opening browser..." -ForegroundColor Cyan
Write-Host "Press Ctrl+C to stop servers." -ForegroundColor Gray
Write-Host ""

# Launch Browser only after Dashboard HTTP 200 response is confirmed
Start-Job -ScriptBlock {
    $url = "http://localhost:3000"
    $maxAttempts = 40
    $attempt = 0
    while ($attempt -lt $maxAttempts) {
        Start-Sleep -Milliseconds 500
        $attempt++
        try {
            $resp = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 2 -ErrorAction Stop
            if ($resp.StatusCode -eq 200) {
                break
            }
        } catch { }
    }

    $chrome = "${env:ProgramFiles}\Google\Chrome\Application\chrome.exe"
    $chrome86 = "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe"
    $edge = "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe"
    $edge64 = "${env:ProgramFiles}\Microsoft\Edge\Application\msedge.exe"

    if (Test-Path $chrome) {
        Start-Process $chrome -ArgumentList "--start-maximized", $url
    } elseif (Test-Path $chrome86) {
        Start-Process $chrome86 -ArgumentList "--start-maximized", $url
    } elseif (Test-Path $edge) {
        Start-Process $edge -ArgumentList "--start-maximized", $url
    } elseif (Test-Path $edge64) {
        Start-Process $edge64 -ArgumentList "--start-maximized", $url
    } else {
        Start-Process $url -WindowStyle Maximized
    }
} | Out-Null

try {
    Set-Location $DashboardDir
    if (-not (Test-Path (Join-Path $DashboardDir "node_modules"))) {
        Write-Host "Installing dashboard npm dependencies..." -ForegroundColor Cyan
        npm install
    }
    npm run dev
} finally {
    Write-Host "Shutting down servers..." -ForegroundColor Yellow
    if ($ApiProcess -and -not $ApiProcess.HasExited) {
        Stop-Process -Id $ApiProcess.Id -Force -ErrorAction SilentlyContinue
    }
}
