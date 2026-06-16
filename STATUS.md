# StratoPi HSO — Project Status
*Last updated: 2026-06-02 (session 2)*

---

## Hardware

| Component | Detail |
|---|---|
| Pi | Raspberry Pi 4 (4 GB RAM), overclocked to **2 GHz** |
| Pi HQ cam | IMX477 (Pi HQ Camera) on CSI |
| USB webcam | Connected on USB (UVC, MJPEG capable) |
| LoRa HAT | Waveshare SX1268 433M LoRa HAT (EBYTE E22 module) |
| GPS (tomorrow) | Flyfish M10 Mini → `/dev/ttyUSB0` |
| SDR receiver | NESDR Mini 2+ (RTL-SDR) on laptop |
| Pi SSH | `louis@stratopi` / `strato` / `192.168.178.59` |

### LoRa HAT Jumper Config (CRITICAL — must stay this way)
```
M0 jumper → GND   (M0 permanently LOW — leave it in)
M1 jumper → REMOVED (GPIO27 controls M1)
B-B jumpers       (connects Pi GPIO14/15 UART to E22 — NOT A-A)
```

---

## What Works ✅

### Camera Web App
- Flask server at `http://192.168.178.59:8080`
- Pi HQ cam: H264 (≤1920×1080) or MJPEG (higher res) — rpicam-vid
- USB webcam: MJPEG passthrough via ffmpeg, 30fps, `exposure_dynamic_framerate` control
- Live MJPEG stream with thread+queue (handles disconnects cleanly)
- Test mode (30s) + Mission mode recording to `/home/louis/captures/`
- File browser with download + delete
- **Service**: `stratopi.service` — **active/enabled**, starts on boot

### WiFi Fallback
- On boot: if no WiFi within 5s → hotspot **"Strato-HSO"** (password: `strato123`)
- **Service**: `wifi_fallback.service` — **active/enabled**

### Pi Overclock
- `over_voltage=6`, `arm_freq=2000` in `/boot/firmware/config.txt` ✅

### UART / LoRa HAT
- UART freed: `dtoverlay=disable-bt`, console=serial0 removed from `cmdline.txt`
- SPI enabled: `dtparam=spi=on`
- **LoRa HAT transmitting** — transparent TX confirmed working ✅
- AT command config not needed — transparent mode with factory defaults works fine
- `lora_tx.py` deployed and tested; `stratopi_lora.service` enabled and running

---

## What Needs To Be Done 🔲

### 1. Connect Real GPS
- Plug **Flyfish M10 Mini GPS** into `/dev/ttyUSB0`
- Edit `lora_tx.py` line 74:
  ```python
  GPS_PORT = "/dev/ttyUSB0"   # was: None
  ```
- Deploy and restart the lora service

### 2. Ground Station — Test on Laptop
```bash
cd "C:\Users\lxsch\Documents\GitHub\StratoPi HSO\ground_station"
pip install flask pyserial
python gs_app.py
```
- Opens at `http://localhost:5001`
- Serial input: auto-detected LoRa HAT, or `--serial COMx`
- UDP input port 5005 (for gr-lora RTL-SDR decoder)
- Leaflet map with live HAB track + altitude bar

### 3. RTL-SDR / NESDR Mini 2+ (optional, if using as receiver)
- See `NESDR_SETUP.md` for full instructions
- Need: Zadig (WinUSB driver) → SDR# for waterfall verification → GNU Radio + gr-lora for packet decode
- **Simpler alternative**: use the second Waveshare LoRa HAT as serial receiver (plug into laptop, run gs_app.py with `--serial COMx`)

---

## Key Files

| File | Location on Pi | Purpose |
|---|---|---|
| `server.py` | `/home/louis/stratopi/server.py` | Camera web app |
| `lora_tx.py` | `/home/louis/stratopi/lora_tx.py` | LoRa telemetry TX |
| `e22_config_test.py` | `/home/louis/stratopi/e22_config_test.py` | AT config test |
| `e22_tx_test.py` | `/home/louis/stratopi/e22_tx_test.py` | Transparent TX test |
| `templates/index.html` | `/home/louis/stratopi/templates/index.html` | Web UI |
| `ground_station/gs_app.py` | laptop only | Ground station web app |
| `stratopi.service` | `/etc/systemd/system/` | Camera service (enabled) |
| `stratopi_lora.service` | `/etc/systemd/system/` | LoRa service (NOT enabled) |
| `wifi_fallback.service` | `/etc/systemd/system/` | Hotspot fallback (enabled) |

---

## Radio Config (lora_tx.py)

```python
LORA_PORT   = "/dev/serial0"
LORA_BAUD   = 9600
PIN_M0      = None        # fixed LOW by M0-GND jumper
PIN_M1      = 27          # GPIO27 → M1 (M1 jumper removed)
FREQ_HZ     = 434_200_000 # 434.200 MHz
TX_DBM      = 10          # 10 dBm (German ISM legal max)
SF          = 12
BW_INDEX    = 7           # 125 kHz
CR          = 1           # 4/5
PREAMBLE    = 12
TX_INTERVAL = 30          # seconds (10% duty cycle compliance)
GPS_PORT    = None        # → "/dev/ttyUSB0" when real GPS connected
```

### German Legal Compliance
- Band: 433.05–434.79 MHz ISM SRD ✅ (434.200 MHz)
- TX power: ≤10 mW ERP (10 dBm) ✅
- Duty cycle: ≤10% → 30s interval at SF12/BW125 (~2.5s airtime) ✅

---

## UKHAS Telemetry Format
```
$$STRATOPI,00001,14:32:10,48.370500,10.873000,1250.3,18.5,270.0,5.20,8,1.2,3*4F
```
`$$CALLSIGN,seq,time,lat,lon,alt,speed,heading,vspeed,sats,hdop,fix*XOR_CRC`

---

## Next Conversation Starter

> "Continue StratoPi HSO. LoRa TX working and service enabled. GPS (Flyfish M10 Mini) connected to /dev/ttyUSB0. Need to verify NMEA data and real position packets. Also test ground station with live serial RX."
