# Build StratoPi Ground Station as a standalone Windows .exe
# Output: dist\StratoPiGroundStation.exe

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "=== StratoPi Ground Station EXE build ===" -ForegroundColor Cyan

python -m pip install --quiet pyinstaller winotify pyserial flask cryptography zeroconf

if (Test-Path build) { Remove-Item -Recurse -Force build }
if (Test-Path dist)  { Remove-Item -Recurse -Force dist }

python -m PyInstaller --noconfirm StratoPiGroundStation.spec

$exe = Join-Path $PSScriptRoot "dist\StratoPiGroundStation.exe"
if (-not (Test-Path $exe)) {
    throw "Build failed - exe not found"
}

foreach ($f in @("config_e22_com.py", "serial_rx_test.py", "gs_alerts.json.example", "lora_crypto.json.example")) {
    if (Test-Path $f) { Copy-Item $f "dist\" -Force }
}

# Seed config next to exe if missing
foreach ($pair in @("gs_alerts.json", "lora_crypto.json")) {
    $src = Join-Path $PSScriptRoot $pair
    $dst = Join-Path (Join-Path $PSScriptRoot "dist") $pair
    if (Test-Path $src) {
        Copy-Item $src $dst -Force
    } elseif (Test-Path ($pair + ".example")) {
        Copy-Item ($pair + ".example") $dst -Force
    }
}

$bat = @"
@echo off
REM Edit COM port below if auto-detect fails (Device Manager -> CP210x)
StratoPiGroundStation.exe --no-udp
pause
"@
Set-Content -Path (Join-Path $PSScriptRoot "dist\Launch-GroundStation.bat") -Value $bat -Encoding ASCII

$readme = @"
StratoPi HSO - Ground Station (standalone)
==========================================

1. Connect LoRa HAT (CP210x) — M0 and M1 jumpers to GND
2. Double-click Launch-GroundStation.bat
   Or: StratoPiGroundStation.exe --serial COM6 --no-udp
3. Operator UI: http://localhost:5001
4. Phone map (PC hotspot): http://whereami.local:5001/whereami
   Fallback: http://192.168.137.1:5001/whereami

Config (saved next to this exe):
  gs_alerts.json      milestones, Pi URL
  lora_crypto.json    AES key — must match Pi

First-time on this PC:
  Copy lora_crypto.json from your other laptop if packets do not decrypt.

Optional (Python installed):
  python config_e22_com.py COM6 --countdown 15
  python serial_rx_test.py COM6

Flags:
  --serial COM6     LoRa port (auto-detected if omitted)
  --no-udp          Serial only (no RTL-SDR)
  --no-hotspot      Disable PC hotspot / whereami.local
  --port 5001       Web UI port
"@
Set-Content -Path (Join-Path $PSScriptRoot "dist\README.txt") -Value $readme -Encoding UTF8

Write-Host ""
Write-Host "[OK] Built: $exe" -ForegroundColor Green
Write-Host "     Copy the entire dist\ folder to another PC." -ForegroundColor Green
$sizeMb = (Get-Item $exe).Length / 1MB
Write-Host ("     Size: {0:N1} MB" -f $sizeMb)