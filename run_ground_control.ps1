# StratoPI Ground Control - start from repo root
param(
    [string]$Serial = "",
    [int]$Port = 5001,
    [switch]$NoUdp,
    [switch]$NoHotspot,
    [switch]$NoSerial
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# Prefer project venv
$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (Test-Path $venvPy) {
    $python = $venvPy
} else {
    $python = "python"
    if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
        Write-Host "Python not found. Run setup_laptop.ps1 first." -ForegroundColor Red
        exit 1
    }
}

# First-run hint
$alerts = Join-Path $PSScriptRoot "ground_station\gs_alerts.json"
if (-not (Test-Path $alerts)) {
    Write-Host "First run? Execute:  .\setup_laptop.ps1" -ForegroundColor Yellow
}

$argsList = @("gs_app.py", "--port", $Port)
if ($Serial) { $argsList += @("--serial", $Serial) }
if ($NoUdp) { $argsList += "--no-udp" }
if ($NoHotspot) { $argsList += "--no-hotspot" }
if ($NoSerial) { $argsList += "--no-serial" }
# Default: serial-only (typical field laptop without RTL-SDR)
if (-not $NoUdp -and -not $PSBoundParameters.ContainsKey("NoUdp")) {
    $argsList += "--no-udp"
}

Set-Location (Join-Path $PSScriptRoot "ground_station")
Write-Host "Starting Ground Control on http://localhost:$Port" -ForegroundColor Cyan
& $python @argsList