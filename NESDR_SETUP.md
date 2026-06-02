# NESDR Mini 2+ — LoRa Reception Setup Guide
## Receiving StratoPi HSO telemetry with RTL-SDR + gr-lora

---

## Overview

The NESDR Mini 2+ is an RTL-SDR receiver (RTL2832U + R820T2 chip).
To decode LoRa signals you need GNU Radio with the **gr-lora** decoder plugin.

```
NESDR Mini 2+ → GNU Radio + gr-lora → UDP:5005 → Ground Station App → Map
```

**HAB transmit parameters:**
- Frequency : **434.200 MHz**
- Modulation: LoRa SF12 · BW 125 kHz · CR 4/5
- TX power  : 10 dBm (German legal limit)
- Interval  : every 30 seconds

---

## Step 1 — Install RTL-SDR drivers (Windows)

1. Plug in the NESDR Mini 2+.
2. Download **Zadig** from https://zadig.akeo.ie/
3. Open Zadig → Options → List All Devices
4. Select **Bulk-In, Interface (Interface 0)** (or "RTL2832U")
5. Select driver: **WinUSB** (NOT libusbK or libusb-win32)
6. Click **Install Driver** → wait for completion
7. Verify: Device Manager → Universal Serial Bus devices → **RTL2832U** with WinUSB

> Only needed once. Do NOT use Zadig for the camera or other USB devices.

---

## Step 2 — Verify reception with SDR# (optional but recommended)

1. Download SDR# from https://airspy.com/download/
2. Extract, run `install-rtlsdr.bat` (installs Zadig-based driver automatically)
3. Open SDRSharp.exe
4. Source: RTL-SDR (USB)
5. Set frequency: **434.200 MHz**
6. Bandwidth: 2.048 MHz
7. Hit ▶ Play
8. When the HAB transmits you should see a **chirp signal** (diagonal stripes in the waterfall)
   — LoRa looks like frequency-sweeping chirps, very distinctive

If you see the chirp, your setup is working.

---

## Step 3 — Install GNU Radio (Windows)

GNU Radio on Windows is easiest via the **radioconda** distribution:

1. Download radioconda installer from:
   https://github.com/ryanvolz/radioconda/releases/latest
   → Get `radioconda-YYYY.MM.DD-Windows-x86_64.exe`
2. Run installer → accept defaults → install to `C:\radioconda`
3. Open **radioconda Prompt** from Start Menu
4. Test:
   ```
   python -c "import gnuradio; print(gnuradio.__version__)"
   ```

---

## Step 4 — Install gr-lora

gr-lora (rpp0) is the standard open-source LoRa decoder for GNU Radio.

In the radioconda Prompt:
```bash
conda install -c conda-forge gr-lora
```

If not available via conda, build from source (Linux recommended — see below).

**Alternative: Use Linux (easier for gr-lora)**

If you have WSL2 (Windows Subsystem for Linux) or a Linux laptop:
```bash
sudo apt update
sudo apt install gnuradio gr-lora rtl-sdr
```

Or build gr-lora from source:
```bash
sudo apt install gnuradio gnuradio-dev cmake git
git clone https://github.com/rpp0/gr-lora.git
cd gr-lora
mkdir build && cd build
cmake ..
make -j4
sudo make install
sudo ldconfig
```

---

## Step 5 — Run the LoRa decoder

### Option A: GNU Radio Companion (GUI)

1. Open GNU Radio Companion (GRC)
2. Create a new flowgraph with these blocks:

```
[RTL-SDR Source] → [Low Pass Filter] → [LoRa Receiver] → [UDP Sink]
```

**RTL-SDR Source settings:**
- Sample rate: 1,000,000 (1 Msps)
- Center freq: 434,200,000 Hz
- Gain: 40 dB (adjust to avoid overload)

**Low Pass Filter:**
- Decimation: 4
- Cutoff: 75,000 Hz
- Transition: 10,000 Hz

**LoRa Receiver (gr-lora lora_receiver):**
- Center freq: 434,200,000
- Bandwidth: 125,000
- Spreading factor: 12
- Payload length: 0 (auto)
- Coding rate: 4 (= CR 4/5)
- Implicit header: unchecked
- Low datarate optimize: checked (required for SF12)
- Decimation: 1

**UDP Sink:**
- Address: 127.0.0.1
- Port: 5005
- Payload size: 1472

3. Run the flowgraph (▶)

### Option B: Command-line (Linux, simpler)

Save this as `lora_rx.py` and run it:

```python
#!/usr/bin/env python3
"""Minimal gr-lora command-line runner."""
import osmosdr
from gnuradio import gr, blocks
try:
    from lora import lora_receiver
except ImportError:
    from lora_sdr import lora_receiver

# Parameters matching HAB transmitter
FREQ   = 434_200_000   # Hz
BW     = 125_000       # Hz
SF     = 12
CR     = 4             # = CR 4/5 in gr-lora
SAMP_R = 1_000_000

class LoRaDecoder(gr.top_block):
    def __init__(self):
        super().__init__()
        src  = osmosdr.source()
        src.set_sample_rate(SAMP_R)
        src.set_center_freq(FREQ)
        src.set_gain(40)

        rx = lora_receiver(BW, SF, [FREQ], BW, CR, False, 4)
        self.connect(src, rx)

tb = LoRaDecoder()
tb.run()
```

---

## Step 6 — Start the Ground Station App

```bash
cd ground_station
pip install -r requirements.txt
python gs_app.py --no-serial    # UDP-only mode (from gr-lora)
```

Open browser: **http://localhost:5001**

The map will show the HAB position as soon as a packet is received.

---

## Antenna

For 100 km+ range you **need a directional antenna** pointed at the HAB.

| Antenna                  | Gain     | Good for            |
|--------------------------|----------|---------------------|
| Rubber duck (included)   | 0 dBi    | < 20 km, line-of-sight|
| 1/4-wave ground plane    | 2 dBi    | ~40 km              |
| 5-element Yagi (433 MHz) | 10 dBi   | **100+ km** ✓       |
| 9-element Yagi           | 14 dBi   | 200+ km             |

For the launch, mount a Yagi on a tripod and track the balloon by pointing it toward the direction of climb. At high altitude (>15 km) the balloon is visible from most directions due to radio line-of-sight.

**Simple 1/4-wave ground plane for 434 MHz:**
- Vertical element: 16.3 cm
- 4 radials: 16.3 cm each, angled 45° downward
- Connect to SMA connector, center pin = vertical element

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Zadig doesn't show device | Try different USB port; check Device Manager |
| SDR# can't open device | Reinstall WinUSB driver with Zadig |
| No chirp in waterfall | Check HAB is transmitting; antenna connected? |
| gr-lora not decoding | Verify SF=12, BW=125k, CR=4/5 match transmitter |
| UDP packets not arriving | Check firewall; gs_app running on correct port |
| Packets corrupted | Check CRC errors in gs_app log; try gain adjustment |

---

## Alternative: Use the second LoRa HAT as receiver

If gr-lora is too complex to set up before launch, use the second Waveshare
SX1268 HAT connected to your laptop via USB-to-UART:

```bash
# Ground station with serial LoRa HAT (auto-detect port)
python gs_app.py

# Or specify port explicitly:
python gs_app.py --serial COM5          # Windows
python gs_app.py --serial /dev/ttyUSB0  # Linux
```

The LoRa HAT on the laptop will receive the same packets and feed them to
the ground station app automatically. No GNU Radio needed.

Configure the receive HAT with the same parameters as the transmitter
(the gs_app sends AT commands on startup to match automatically).
