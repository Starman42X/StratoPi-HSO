# Tesla in-car browser — needs port 80 + 443 and firewall (Administrator)
# Right-click -> Run with PowerShell as Administrator

$ErrorActionPreference = "Stop"
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "Tesla requires Administrator (ports 80/443 + firewall)." -ForegroundColor Red
    Write-Host "Right-click this file -> Run with PowerShell as Administrator"
    exit 1
}

Set-Location $PSScriptRoot

# Free port 80 if IIS/World Wide Web Publishing is installed
Get-Service W3SVC -ErrorAction SilentlyContinue | ForEach-Object {
    if ($_.Status -eq 'Running') {
        Write-Host "Stopping IIS (W3SVC) so port 80 is free for Tesla map..."
        Stop-Service W3SVC -Force
    }
}

# Firewall for hotspot clients (Tesla on 192.168.137.x)
foreach ($p in 80, 443, 5001) {
    $n = "StratoPi Tesla TCP $p"
    if (-not (Get-NetFirewallRule -DisplayName $n -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -DisplayName $n -Direction Inbound -Action Allow `
            -Protocol TCP -LocalPort $p -RemoteAddress 192.168.137.0/24 -Profile Any -Enabled True | Out-Null
    }
}

$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (Test-Path $venvPy) {
    & $venvPy -c "import sys; sys.path.insert(0,'ground_station'); import tesla_host; tesla_host.ensure_tesla_firewall()"
}

Write-Host ""
Write-Host "Tesla URLs (PC hotspot Wi-Fi):" -ForegroundColor Cyan
Write-Host "  https://192.168.137.1/   <- try this first (HW4)"
Write-Host "  http://192.168.137.1/"
Write-Host "  Test: https://192.168.137.1/api/tesla/ping"
Write-Host "  Do NOT use :8080 or whereami.local"
Write-Host ""

& (Join-Path $PSScriptRoot "run_ground_control.ps1") @args