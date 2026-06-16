# StratoPI Ground Control - first-time setup on a new Windows laptop
# Run once:  powershell -ExecutionPolicy Bypass -File setup_laptop.ps1

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$Gs = Join-Path $Root "ground_station"

Write-Host "=== StratoPI Ground Control - laptop setup ===" -ForegroundColor Cyan
Set-Location $Root

# Python 3.10+
$py = $null
foreach ($cmd in @("py -3.12", "py -3.11", "py -3.10", "py -3", "python3", "python")) {
    try {
        $ver = Invoke-Expression "$cmd -c `"import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')`"" 2>$null
        if ($ver -match '^3\.(1[0-9]|[2-9][0-9])') { $py = $cmd; break }
    } catch {}
}
if (-not $py) {
    Write-Host "Python 3.10+ not found. Install from https://www.python.org/downloads/" -ForegroundColor Red
    Write-Host "Enable 'Add Python to PATH' during install, then re-run this script."
    exit 1
}
Write-Host "Using Python: $py ($ver)"

# Virtual environment
$venv = Join-Path $Root ".venv"
if (-not (Test-Path $venv)) {
    Write-Host "Creating virtual environment..."
    Invoke-Expression "$py -m venv `"$venv`""
}
$venvPy = Join-Path $venv "Scripts\python.exe"
& $venvPy -m pip install --upgrade pip --quiet
& $venvPy -m pip install -r (Join-Path $Gs "requirements.txt") --quiet
Write-Host "Dependencies installed." -ForegroundColor Green

# Local config (do not overwrite existing)
function Ensure-Config($example, $target) {
    if (Test-Path $target) {
        Write-Host "  keep  $([System.IO.Path]::GetFileName($target))"
        return
    }
    if (Test-Path $example) {
        Copy-Item $example $target
        Write-Host "  create $([System.IO.Path]::GetFileName($target)) from example" -ForegroundColor Yellow
    }
}
Write-Host "Config files:"
Ensure-Config (Join-Path $Gs "gs_alerts.json.example") (Join-Path $Gs "gs_alerts.json")
Ensure-Config (Join-Path $Gs "lora_crypto.json.example") (Join-Path $Gs "lora_crypto.json")

# LoRa AES key - must match the Pi
$crypto = Join-Path $Gs "lora_crypto.json"
if (Test-Path $crypto) {
    $j = Get-Content $crypto -Raw | ConvertFrom-Json
    if ($j.key_hex -eq "REPLACE_WITH_same_key_as_on_Pi") {
        Write-Host ""
        Write-Host "IMPORTANT: Edit ground_station\lora_crypto.json" -ForegroundColor Yellow
        Write-Host "  Copy key_hex from your other laptop or sync from Pi in Ground Control UI."
    }
}

# Serial ports
Write-Host ""
Write-Host "Serial ports (LoRa HAT - note the COM number):" -ForegroundColor Cyan
& $venvPy -c "import serial.tools.list_ports as lp; ports=list(lp.comports()); print('  (none)' if not ports else '\n'.join(f'  {p.device}  {p.description}' for p in ports))"

# Firewall (best-effort; may need admin)
Write-Host ""
Write-Host "Firewall rules (UDP 5353 mDNS + TCP 5001)..." -ForegroundColor Cyan
& $venvPy -c "import whereami_host; whereami_host._ensure_firewall_rules(5001)" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "  Run setup_laptop.ps1 as Administrator if phones cannot reach whereami.local" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "=== Setup complete ===" -ForegroundColor Green
Write-Host "Start Ground Control:"
Write-Host "  .\run_ground_control.ps1"
Write-Host "  .\run_ground_control.ps1 -Serial COM3"
Write-Host ""
Write-Host "Operator UI:  http://localhost:5001"
Write-Host "Phone map:    http://whereami.local:5001/whereami  (PC hotspot)"
Write-Host "              http://192.168.137.1:5001/whereami     (fallback)"
Write-Host "Pi web:       http://stratopi.local:8080"