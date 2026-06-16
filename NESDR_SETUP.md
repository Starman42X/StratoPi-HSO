# NESDR Mini 2+ — LoRa Reception Setup
## Receiving StratoPi HSO telemetry on Windows

---

## Signal chain

```
Pi LoRa HAT  →  434.200 MHz RF  →  NESDR Mini 2+
  ↓ lora_gps_test.py --tx           ↓ lora_rx.py (GNU Radio + gr-lora_sdr)
  UKHAS packets                      UDP:5005
                                     ↓ gs_app.py
                                     http://localhost:5001  (live map)
```

**Transmitter parameters (must match decoder exactly):**

| Parameter | Value |
|-----------|-------|
| Frequency | 434.200 MHz |
| SF | 12 |
| BW | 125 kHz |
| CR | 4/5 |
| Sync word | **0x12** (EBYTE E22 private network) |
| Preamble | 12 |

> The sync word is critical. LoRaWAN uses 0x34 — gr-lora defaults to 0x34 and will
> silently drop every packet unless you set it to 0x12 for the E22.

---

## Step 1 — Install WinUSB driver (Zadig)

1. Plug in the NESDR Mini 2+
2. Download **Zadig** from https://zadig.akeo.ie/
3. Open Zadig → Options → List All Devices
4. Select **Bulk-In, Interface (Interface 0)** (RTL2832U)
5. Driver: **WinUSB** ← important, not libusbK
6. Click **Replace Driver**

Verify: Device Manager → Universal Serial Bus devices → RTL2832U (WinUSB)

---

## Step 2 — Verify signal with SDR# (optional but recommended)

Before attempting to decode, confirm the LoRa chirp is visible.

1. Download SDR# from https://airspy.com/download/ — extract and run `install-rtlsdr.bat`
2. Open SDRSharp.exe → Source: RTL-SDR USB
3. Tune to **434.200 MHz**, Bandwidth 2 MHz, hit ▶
4. On the Pi: `python lora_gps_test.py --tx --count 3 --interval 5`
5. Watch the waterfall — LoRa SF12 looks like **sweeping diagonal chirps**, ~2.5 s each

If you see chirps: hardware is working, move to Step 3.  
If nothing: check the HAT is transmitting (`journalctl -u stratopi_lora -f` on the Pi).

---

## Step 3 — Install radioconda (GNU Radio for Windows)

radioconda bundles GNU Radio 3.10 + all RTL-SDR support in one installer.

1. Download from https://github.com/ryanvolz/radioconda/releases/latest  
   → `radioconda-YYYY.MM.DD-Windows-x86_64.exe`
2. Run the installer → install to `C:\radioconda` (default)
3. Open **radioconda Prompt** from Start Menu
4. Test:
   ```
   python -c "import gnuradio; print(gnuradio.__version__)"
   ```
   Should print `3.10.x.x`.

---

## Step 4 — Install gr-lora_sdr

gr-lora_sdr (EPFL / Jeannin) is the current maintained LoRa decoder for GNU Radio 3.10.
Install it in the radioconda Prompt:

```
conda install -c conda-forge gnuradio-lora_sdr
```

Verify:
```
python -c "import lora_sdr; print('OK')"
```

---

## Step 5 — Run the receiver

In the radioconda Prompt, from the `ground_station` folder:

```
python lora_rx.py
```

You should see:
```
StratoPi HSO — LoRa Receiver
  RTL-SDR gain : 40 dB
  Frequency    : 434.200 MHz
  LoRa params  : SF12 / BW125k / CR4/4 / preamble 12
  Sync word    : 0x12  (EBYTE E22 private network)
  UDP output   : 127.0.0.1:5005  →  gs_app.py

Listening on 434.200 MHz …
```

**In a second terminal (normal Python, not radioconda):**
```
cd ground_station
pip install flask pyserial
python gs_app.py --no-serial
```

Open http://localhost:5001 — the map appears as soon as the first packet is decoded.

---

## Step 6 — Test end-to-end

On the Pi (emulated GPS, 5 s intervals for fast testing):
```bash
python lora_gps_test.py --tx --count 20 --interval 5
```

On the laptop, `lora_rx.py` should print packets as they arrive and the ground
station map should update in real time.

---

## Gain adjustment

If signals look overloaded in SDR# (flat-topped waveform / clipping):
- Reduce `RTL_GAIN` in `lora_rx.py` (try 30, 20, or 10)
- The R820T2 chip can also be set with `--gain` if using `rtl_test`

If you get nothing at close range (same room):
- RTL_GAIN too low → increase to 50
- Wrong sync word (0x34 vs 0x12) → check `SYNC_WORD` in lora_rx.py
- SF or BW mismatch → must match lora_tx.py exactly

---

## PPM frequency correction

Cheap RTL-SDR dongles have crystal drift (±20–100 PPM typical for NESDR Mini 2+).
At 434 MHz, 50 PPM = 21 kHz offset — wide enough to still decode SF12/BW125.

If decoding is unreliable at longer range:
1. Find the actual chirp centre in SDR# (zoom in on the waterfall)
2. Measure the offset from 434.200 MHz in Hz
3. Set `self.src.set_freq_corr(ppm)` in `lora_rx.py`

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| No device found | Zadig not applied | Reinstall WinUSB driver |
| SDR# can't open | Driver conflict | Rerun Zadig, reboot |
| Chirp visible in SDR# but no decode | Sync word mismatch | Set `SYNC_WORD = 0x12` |
| Decode output is garbage bytes | SF/BW mismatch | Confirm SF=12, BW=125000 |
| UDP arrives at gs_app but parse fails | Byte framing | Check gs_app raw log at `/api/raw` |
| `import lora_sdr` fails | gr-lora_sdr not installed | `conda install -c conda-forge gnuradio-lora_sdr` |

---

## Antenna

| Antenna | Gain | Range |
|---------|------|-------|
| Rubber duck (included) | 0 dBi | < 20 km LOS |
| 1/4-wave ground plane (DIY) | 2 dBi | ~40 km |
| 5-el Yagi 433 MHz | 10 dBi | 100+ km ✓ |

**DIY 1/4-wave ground plane for 434 MHz:**  
Vertical element = 16.3 cm, 4 radials = 16.3 cm each at 45° downward, SMA connector.
