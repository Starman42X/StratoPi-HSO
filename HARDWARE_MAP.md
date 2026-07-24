# StratoPi HSO — Hardware Connection Map

Raspberry Pi 4 Model B. BCM GPIO numbering. This is the **complete** wiring map
after adding the environmental sensor suite (AHT21, ENS160, Plantower, 2× DS18B20)
and the new GPS. Pin choices were made to avoid every already-occupied pin.

> **After wiring you must reboot once** — `config.txt` now enables I2C and UART2
> (`sudo reboot`). The DS18B20, LoRa, GPS and heater work without it; the I2C
> sensors and the Plantower come online only after the reboot.

---

## 40-pin header — full allocation

```
            3V3  (1) (2)  5V
   I2C SDA→GPIO2  (3) (4)  5V          ← Plantower VCC (5 V)
   I2C SCL→GPIO3  (5) (6)  GND         ← Plantower / DS18B20 / I2C GND
  1-Wire →GPIO4  (7) (8)  GPIO14 → LoRa E22 RXD (UART0 TX)
            GND  (9) (10) GPIO15 ← LoRa E22 TXD (UART0 RX)
          GPIO17 (11) (12) GPIO18 → Heater MOSFET gate (PWM)
  LoRa M1→GPIO27 (13) (14) GND
 LoRa M0→GPIO22 (15) (16) GPIO23
            3V3 (17) (18) GPIO24
          GPIO10 (19) (20) GND
          GPIO9  (21) (22) GPIO25
          GPIO11 (23) (24) GPIO8
            GND (25) (26) GPIO7
 PM TX→ GPIO0/1 (27)(28) ← see UART2 below   (ID_SD / ID_SC)
          GPIO5  (29) (30) GND
          GPIO6  (31) (32) GPIO12 → GPS RX (UART5 TX)
   GPS←  GPIO13 (33) (34) GND
          GPIO19 (35) (36) GPIO16
          GPIO26 (37) (38) GPIO20
            GND (39) (40) GPIO21
```

*(ASCII header is approximate for orientation; use the table below as the source of truth.)*

---

## Per-pin assignment table

| Function | Signal | BCM | Phys pin | Direction |
|---|---|---|---|---|
| **LoRa E22** | UART0 TX → E22 RXD | GPIO14 | 8 | Pi→E22 |
| | UART0 RX ← E22 TXD | GPIO15 | 10 | E22→Pi |
| | M0 mode select | GPIO22 | 15 | Pi→E22 |
| | M1 mode select | GPIO27 | 13 | Pi→E22 |
| **GPS (new)** | UART5 RX ← GPS TX | GPIO13 | 33 | GPS→Pi |
| | UART5 TX → GPS RX | GPIO12 | 32 | Pi→GPS (optional) |
| **Plantower PMS** | UART2 RX ← PM TX | GPIO1 | 28 | PM→Pi |
| | UART2 TX (unused) | GPIO0 | 27 | — |
| **AHT21 + ENS160** | I2C1 SDA | GPIO2 | 3 | bidir |
| | I2C1 SCL | GPIO3 | 5 | bidir |
| **DS18B20 ×2** | 1-Wire data | GPIO4 | 7 | bidir |
| **Heater** | MOSFET gate (100 Hz PWM) | GPIO18 | 12 | Pi→gate |

**No GPIO is shared between two functions.** Conflicts deliberately avoided:
1-Wire (GPIO4) is clear of UART2 (GPIO0/1); I2C (GPIO2/3) is clear of UART2;
heater/LoRa-mode pins (18/22/27) untouched.

---

## Per-device wiring detail

### AHT21 (temperature + humidity) — I2C, address 0x38
| AHT21 pin | → Pi |
|---|---|
| VCC | 3V3 (pin 1 or 17) |
| GND | GND |
| SDA | GPIO2 / pin 3 |
| SCL | GPIO3 / pin 5 |

### ENS160 (air quality: AQI/TVOC/eCO2) — I2C, address 0x53
Shares the **same I2C bus** as the AHT21 (parallel SDA/SCL).
| ENS160 pin | → Pi |
|---|---|
| VCC | 3V3 |
| GND | GND |
| SDA | GPIO2 / pin 3 |
| SCL | GPIO3 / pin 5 |
| ADDR | tie for 0x53 per the board (most default to 0x53) |

> Both I2C devices need SDA/SCL pulled up to 3V3 (4.7–10 kΩ). Most breakout
> boards include the pull-ups; only add external ones if neither board has them.

### Plantower PMS (PM1.0 / PM2.5 / PM10) — UART2, 9600 baud
| PMS pin | → Pi |
|---|---|
| VCC | **5V** (pin 2 or 4) — the fan needs 5 V |
| GND | GND |
| TX | GPIO1 / pin 28 (UART2 RX) |
| RX | not required (we only read) |
| SET / RESET | leave floating or tie high for continuous mode |

> The PMS TX idle level is 3.3 V — safe for the Pi. Do **not** feed the 5 V VCC
> into a GPIO.

### DS18B20 ×3 — TWO separate 1-Wire buses
The cell probe and the env probes are on **different GPIOs / different buses** so
they can't interfere with each other. Each bus needs its **own 4.7 kΩ pull-up**
to 3V3 on its data line.

**Bus 1 — CELL probe (GPIO4 / pin 7):**
| DS18B20 (cell) | → Pi |
|---|---|
| VDD | 3V3 |
| GND | GND |
| DATA | **GPIO4 / pin 7** |
| pull-up | 4.7 kΩ DATA → 3V3 |

**Bus 2 — the TWO ENV probes (GPIO17 / pin 11):**
| DS18B20 (env1, env2) | → Pi |
|---|---|
| VDD | 3V3 |
| GND | GND |
| DATA | **GPIO17 / pin 11** (both env probes share this line) |
| pull-up | 4.7 kΩ DATA → 3V3 |

Enabled by two overlays in `config.txt`:
`dtoverlay=w1-gpio,gpiopin=4` and `dtoverlay=w1-gpio,gpiopin=17` (reboot required).

Roles (logged as `DS_cell`, `DS_env1`, `DS_env2`) — identified **by port**, no ID
tracking needed:
- **Cell** = the probe **alone on the GPIO4 bus**; drives the heater PID.
- **Env1 / Env2** = the **two probes on the GPIO17 bus**.

`sensors.classify_ds18b20()` picks the cell as the lone probe on its own bus and
env as the pair on the other bus, so you just wire "cell on its own port, the two
env on the shared port" — the code figures out the rest. (Before the env bus is
wired, it falls back to the pinned id `28-000000bf78cc`, then to first-probe.)

### GPS — Matek SAM-M10Q (u-blox), UART5, **9600 baud**
| GPS | → Pi |
|---|---|
| VCC | 5V (Matek board has its own regulator) |
| GND | GND |
| TX | GPIO13 / pin 33 (UART5 RX) |
| RX | GPIO12 / pin 32 (optional) |
| antenna | built-in patch — keep facing the sky |

Baud is **auto-detected** (9600 → 115200 fallback). The SAM-M10Q is a genuine
u-blox multi-GNSS receiver; it locked 12 sats / HDOP 0.7 on the bench.

### LoRa E22 (telemetry) — UART0, 9600 baud  *(unchanged)*
GPIO14/15 + M0=GPIO22, M1=GPIO27. 433 MHz antenna. Air-rate 3 → SF12/BW125 @ ~434.106 MHz.

### Heater — IRFZ44N  *(unchanged)*
GPIO18 (pin 12) → 100 Ω → MOSFET gate, 100 Hz PWM. Load powered separately.

---

## /boot/firmware/config.txt — required overlays
```
enable_uart=1
dtoverlay=disable-bt      # frees UART0 (ttyAMA0) on GPIO14/15 for LoRa
dtoverlay=uart5           # GPS  on GPIO12/13  -> /dev/ttyAMA5
dtoverlay=uart2           # PMS  on GPIO0/1    -> /dev/ttyAMA2   (added)
dtparam=i2c_arm=on        # AHT21+ENS160 on GPIO2/3 -> /dev/i2c-1 (added)
dtoverlay=w1-gpio         # DS18B20 on GPIO4
```

## Device files (after reboot)
| Device | Path | Baud / addr |
|---|---|---|
| LoRa E22 | `/dev/serial0` (ttyAMA0) | 9600 |
| GPS | `/dev/ttyAMA5` | 115200 |
| Plantower | `/dev/ttyAMA2` | 9600 |
| AHT21 | `/dev/i2c-1` | 0x38 |
| ENS160 | `/dev/i2c-1` | 0x53 |
| DS18B20 | `/sys/bus/w1/devices/28-*` | — |

---

## Network — static IP
The Pi has a **per-connection static IP on the home WiFi** (SSID *SpaceX Starlink*):
`192.168.178.210/24`, gateway `192.168.178.1`. It's stored in the netplan YAML
(persists across reboot) and bound to that one connection only — joining any
other WiFi still uses DHCP, so the payload stays reachable when taken outside.

## Data logging
`sensors.py` samples **each sensor at its own configurable interval** and writes a
unified structured CSV (`/home/louis/stratopi/logs/sensors.csv`). Defaults:
AHT21 10 s · ENS160 10 s · Plantower 15 s · DS18B20 5 s · CSV row 10 s. Adjust
live on the control page (**Poll / Log Frequency**) or `POST /api/sensors/config`;
intervals persist in `sensor_config.json`.

```
t;DatumZeit;ts;T;RH;AQI;TVOC;eCO2;PM1;PM25;PM10;DS_cell;DS_env1;DS_env2;
GPS_lat;GPS_lon;GPS_alt;GPS_sats;GPS_fix;GPS_speed;Heat_duty;Heat_set;CellT;Flight
```

Error sentinels (sensor absent/failed): `T/RH = -999`, `AQI = 255`,
`TVOC/eCO2 = -1`, `PM = 9999`, `DS = -127`. If the column set changes, the old
CSV is rotated to `sensors.csv.old` so rows never misalign.

Download from the control page → **Environmental Sensors → ⤓ Download Data Log (CSV)**
(or `GET /download/sensors.csv`). **Reset Log** clears it (`POST /api/sensors/reset`).

## Recording reliability
A **watchdog** (`_recording_watchdog` in server.py, 4 s tick) detects if
`rpicam-vid`/`ffmpeg` dies mid-recording and respawns it into a new segment file
(`…_pN.ext`), so a transient crash (thermal throttle, V4L2 glitch, USB
re-enumeration) no longer silently ends the flight recording.
