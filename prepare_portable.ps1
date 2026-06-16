# Package StratoPI Ground Control for another laptop (USB / zip / cloud)
# Usage:
#   .\prepare_portable.ps1              # Python source package
#   .\prepare_portable.ps1 -BuildExe    # Also build standalone .exe
#   .\prepare_portable.ps1 -Zip         # Create portable.zip

param(
    [switch]$BuildExe,
    [switch]$Zip,
    [string]$OutDir = ""
)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$Gs = Join-Path $Root "ground_station"
$Dest = if ($OutDir) { $OutDir } else { Join-Path $Root "portable\StratoPi-GroundControl" }

Write-Host "=== Preparing portable Ground Control ===" -ForegroundColor Cyan

if (Test-Path $Dest) { Remove-Item -Recurse -Force $Dest }
New-Item -ItemType Directory -Path $Dest -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $Dest "ground_station") -Force | Out-Null

# Root launchers + setup
foreach ($f in @("gs_app.py", "run_ground_control.ps1", "setup_laptop.ps1")) {
    Copy-Item (Join-Path $Root $f) $Dest -Force
}

# Ground station source (no cache, build, or local secrets)
$skip = @("__pycache__", "build", "dist", ".pytest_cache")
$copyNames = @(
    "*.py", "requirements.txt", "StratoPiGroundStation.spec",
    "gs_alerts.json.example", "lora_crypto.json.example",
    "templates\*"
)
foreach ($pattern in $copyNames) {
    Get-ChildItem -Path $Gs -Filter $pattern -File -ErrorAction SilentlyContinue | ForEach-Object {
        Copy-Item $_.FullName (Join-Path $Dest "ground_station") -Force
    }
}
$templatesDest = Join-Path $Dest "ground_station\templates"
New-Item -ItemType Directory -Path $templatesDest -Force | Out-Null
Copy-Item (Join-Path $Gs "templates\*") $templatesDest -Recurse -Force

# Ship working configs when present (AES key must match Pi)
$configReadme = @()
foreach ($pair in @(
    @("gs_alerts.json", "gs_alerts.json.example"),
    @("lora_crypto.json", "lora_crypto.json.example")
)) {
    $live = Join-Path $Gs $pair[0]
    $example = Join-Path $Gs $pair[1]
    $target = Join-Path (Join-Path $Dest "ground_station") $pair[0]
    if (Test-Path $live) {
        Copy-Item $live $target -Force
        $configReadme += "  $($pair[0])  (from this laptop - AES key must match Pi)"
    } elseif (Test-Path $example) {
        Copy-Item $example $target -Force
        $configReadme += "  $($pair[0])  (from example - set key_hex before flight)"
    }
}

# Optional standalone exe
if ($BuildExe) {
    Write-Host "Building standalone .exe (may take a few minutes)..." -ForegroundColor Cyan
    Push-Location $Gs
    & (Join-Path $Gs "build_exe.ps1")
    Pop-Location
    $exeDir = Join-Path $Dest "exe"
    New-Item -ItemType Directory -Path $exeDir -Force | Out-Null
    Copy-Item (Join-Path $Gs "dist\*") $exeDir -Recurse -Force
}

# README for the other laptop
$readme = @"
StratoPI Ground Control - Portable Package
==========================================

QUICK START (new laptop)
------------------------
1. Install Python 3.10+ from https://www.python.org/downloads/  (check "Add to PATH")
2. Open PowerShell in this folder
3. Run:  powershell -ExecutionPolicy Bypass -File setup_laptop.ps1
4. Connect LoRa HAT (CP210x USB). Note COM port in Device Manager.
5. Run:  .\run_ground_control.ps1
   Or:   .\run_ground_control.ps1 -Serial COM3

OPERATOR UI
-----------
  http://localhost:5001

PHONE MAP (PC mobile hotspot)
-----------------------------
  http://whereami.local:5001/whereami
  http://192.168.137.1:5001/whereami   (if mDNS fails)

PI WEB (same Wi-Fi or Pi hotspot Strato-HSO)
----------------------------------------------
  http://stratopi.local:8080
  http://192.168.4.1:8080              (Pi hotspot)

HARDWARE
--------
  LoRa HAT: M0 -> GND, M1 -> GND (transparent RX)
  First-time HAT config:  python ground_station\config_e22_com.py COMx --countdown 15

CONFIG FILES INCLUDED
---------------------
$($configReadme -join "`n")

FLAGS
-----
  --serial COMx    LoRa serial port (auto-detected if omitted)
  --no-udp         Serial only (no RTL-SDR)
  --no-hotspot     Skip PC Wi-Fi hotspot / whereami.local
  --port 5001      Web UI port

STANDALONE EXE (no Python needed)
---------------------------------
$(if ($BuildExe) { "See exe\ folder - double-click Launch-GroundStation.bat (edit COM port first)" } else { "Re-run prepare_portable.ps1 -BuildExe to include exe\" })

TROUBLESHOOTING
---------------
  No COM port: Device Manager -> Ports (COM & LPT) -> Silicon Labs CP210x
  No packets:  Match lora_crypto.json key with Pi; check M0/M1 jumpers
  Pi offline:  Same Wi-Fi as Pi, or join Strato-HSO hotspot
  Firewall:    Re-run setup_laptop.ps1 as Administrator
"@
Set-Content -Path (Join-Path $Dest "README-SETUP.txt") -Value $readme -Encoding UTF8

# Zip
$zipPath = Join-Path $Root "portable\StratoPi-GroundControl.zip"
if ($Zip) {
    if (Test-Path $zipPath) { Remove-Item $zipPath -Force }
    Compress-Archive -Path $Dest -DestinationPath $zipPath -Force
    Write-Host "Zip: $zipPath" -ForegroundColor Green
}

Write-Host ""
Write-Host "Portable package ready:" -ForegroundColor Green
Write-Host "  $Dest"
if ($Zip) { Write-Host "  $zipPath" }
Write-Host ""
Write-Host "Copy the folder (or zip) to the other laptop, then run setup_laptop.ps1"