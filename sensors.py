#!/usr/bin/env python3
"""
StratoPi HSO — environmental sensor suite (ported from the Arduino logger).

Sensors:
  AHT21    — temperature + relative humidity   (I2C, addr 0x38)
  ENS160   — air quality: AQI / TVOC / eCO2     (I2C, addr 0x53)
  Plantower PMS — particulates PM1/PM2.5/PM10   (UART2, /dev/ttyAMA2, 9600)
  DS18B20  — up to 2× 1-wire temperature probes (GPIO4, /sys/bus/w1)

Direct lightweight I2C drivers (smbus2) are used instead of Adafruit Blinka —
far more robust on Python 3.13 and no board/busio dependency.

A single background poller reads every INTERVAL_S, keeps in-memory history for
the dashboard curves, and appends every reading to a structured CSV that can be
downloaded from the control page. server.py registers an "extra provider" so
GPS / heater / flight curves land in the SAME file (all curves, one download).
"""

import glob
import time
import threading
import csv
import json
import struct
from collections import OrderedDict
from pathlib import Path
from datetime import datetime

# ── configuration ─────────────────────────────────────────────────────────────
I2C_BUS         = 1
AHT21_ADDR      = 0x38
ENS160_ADDR     = 0x53
PLANTOWER_PORT  = "/dev/ttyAMA2"     # UART2 on GPIO0/1 (pins 27/28)
PLANTOWER_BAUD  = 9600
HISTORY_MAX     = 1440               # in-memory rows kept for the dashboard
CSV_PATH        = Path("/home/louis/stratopi/logs/sensors.csv")
CONFIG_PATH     = Path("/home/louis/stratopi/sensor_config.json")

# The first DS18B20 (cell) is pinned by ROM id; the other probes are env temps.
CELL_DS18B20_ID = "28-000000bf78cc"

# Individual poll/log frequencies (seconds) — each sensor sampled at its own rate.
# 'csv' is how often a unified row of the latest values is written to the log.
DEFAULT_INTERVALS = {
    "aht21":     10.0,   # temp / humidity
    "ens160":    10.0,   # air quality
    "plantower": 15.0,   # particulates (fan-driven; no need to hammer it)
    "ds18b20":    5.0,   # all DS18B20 probes
    "csv":       10.0,   # unified CSV row cadence
}
_MIN_INTERVAL = 1.0      # floor to protect the sensors / bus
INTERVAL_S    = DEFAULT_INTERVALS["csv"]   # back-compat alias used by the API

# Sentinels (kept identical to the Arduino logger so old/new CSVs line up)
ERR_FLOAT = -999.0
ERR_DS    = -127.0
ERR_PM    = 9999
ERR_AQI   = 255
ERR_INT   = -1

# ── optional dependencies (degrade gracefully if missing / hw absent) ─────────
try:
    import smbus2
except ImportError:
    smbus2 = None

try:
    import serial
except ImportError:
    serial = None


# ══════════════════════════════════════════════════════════════════════════════
#  AHT21 — temperature + humidity (I2C, no register addressing)
# ══════════════════════════════════════════════════════════════════════════════
class AHT21:
    def __init__(self, bus):
        self._bus = bus
        self.ok = False
        try:
            time.sleep(0.04)
            # calibrate if not already
            status = self._read(1)
            if not status or not (status[0] & 0x08):
                self._write([0xBE, 0x08, 0x00])
                time.sleep(0.02)
            self.ok = True
        except Exception:
            self.ok = False

    def _write(self, data):
        msg = smbus2.i2c_msg.write(AHT21_ADDR, data)
        self._bus.i2c_rdwr(msg)

    def _read(self, n):
        msg = smbus2.i2c_msg.read(AHT21_ADDR, n)
        self._bus.i2c_rdwr(msg)
        return list(msg)

    def read(self):
        """Return (temp_c, rh_pct) or (ERR_FLOAT, ERR_FLOAT) on failure."""
        try:
            self._write([0xAC, 0x33, 0x00])
            time.sleep(0.08)
            d = self._read(7)
            if len(d) < 6 or (d[0] & 0x80):      # still busy
                return ERR_FLOAT, ERR_FLOAT
            rh_raw = (d[1] << 12) | (d[2] << 4) | (d[3] >> 4)
            t_raw  = ((d[3] & 0x0F) << 16) | (d[4] << 8) | d[5]
            rh = rh_raw * 100.0 / 1048576.0
            t  = t_raw * 200.0 / 1048576.0 - 50.0
            return round(t, 2), round(rh, 2)
        except Exception:
            return ERR_FLOAT, ERR_FLOAT


# ══════════════════════════════════════════════════════════════════════════════
#  ENS160 — air quality (I2C, register-based)
# ══════════════════════════════════════════════════════════════════════════════
class ENS160:
    REG_PART_ID   = 0x00
    REG_OPMODE    = 0x10
    REG_TEMP_IN   = 0x13
    REG_RH_IN     = 0x15
    REG_DATA_AQI  = 0x21
    REG_DATA_TVOC = 0x22
    REG_DATA_ECO2 = 0x24

    def __init__(self, bus):
        self._bus = bus
        self.ok = False
        try:
            pid = self._rd(self.REG_PART_ID, 2)
            self._wr(self.REG_OPMODE, [0xF0])     # reset
            time.sleep(0.05)
            self._wr(self.REG_OPMODE, [0x02])     # standard gas sensing
            time.sleep(0.05)
            self.ok = (pid is not None)
        except Exception:
            self.ok = False

    def _wr(self, reg, data):
        self._bus.write_i2c_block_data(ENS160_ADDR, reg, data)

    def _rd(self, reg, n):
        return self._bus.read_i2c_block_data(ENS160_ADDR, reg, n)

    def set_compensation(self, temp_c, rh_pct):
        """Feed AHT21 readings to the ENS160 for compensated gas figures."""
        try:
            if temp_c is None or temp_c <= -100:
                temp_c, rh_pct = 20.0, 50.0
            tk = int(round((temp_c + 273.15) * 64.0))
            rh = int(round((rh_pct if rh_pct and rh_pct > 0 else 50.0) * 512.0))
            self._wr(self.REG_TEMP_IN, [tk & 0xFF, (tk >> 8) & 0xFF])
            self._wr(self.REG_RH_IN,   [rh & 0xFF, (rh >> 8) & 0xFF])
        except Exception:
            pass

    def read(self):
        """Return (aqi, tvoc_ppb, eco2_ppm) or error sentinels."""
        try:
            aqi  = self._rd(self.REG_DATA_AQI, 1)[0] & 0x07
            tvoc = self._rd(self.REG_DATA_TVOC, 2)
            eco2 = self._rd(self.REG_DATA_ECO2, 2)
            tvoc = tvoc[0] | (tvoc[1] << 8)
            eco2 = eco2[0] | (eco2[1] << 8)
            if aqi == 0:
                aqi = ERR_AQI
            return aqi, tvoc, eco2
        except Exception:
            return ERR_AQI, ERR_INT, ERR_INT


# ══════════════════════════════════════════════════════════════════════════════
#  Plantower PMS — particulate matter (UART)
# ══════════════════════════════════════════════════════════════════════════════
def read_plantower(ser, timeout=3.0):
    """Parse one PMSx003 frame -> (pm1, pm25, pm10) env values, or ERR_PM."""
    t0 = time.time()
    try:
        ser.reset_input_buffer()
    except Exception:
        pass
    while time.time() - t0 < timeout:
        if ser.read(1) != b"\x42":
            continue
        if ser.read(1) != b"\x4D":
            continue
        frame = ser.read(30)
        if len(frame) != 30:
            continue
        data = b"\x42\x4D" + frame
        if struct.unpack(">H", data[30:32])[0] != sum(data[0:30]):
            continue
        if struct.unpack(">H", data[2:4])[0] != 28:
            continue
        pm1  = struct.unpack(">H", data[10:12])[0]
        pm25 = struct.unpack(">H", data[12:14])[0]
        pm10 = struct.unpack(">H", data[14:16])[0]
        return pm1, pm25, pm10
    return ERR_PM, ERR_PM, ERR_PM


# ══════════════════════════════════════════════════════════════════════════════
#  DS18B20 — up to 2× 1-wire probes
# ══════════════════════════════════════════════════════════════════════════════
def _read_one_ds(path):
    """Read one DS18B20 w1_slave file -> degC or ERR_DS. Rejects t=0 / 85 °C
    glitches that still pass the kernel CRC on a noisy bus."""
    try:
        with open(path) as fh:
            raw = fh.read()
        if "YES" not in raw:
            return ERR_DS
        pos = raw.find("t=")
        if pos < 0:
            return ERR_DS
        milli = int(raw[pos + 2:].split()[0])
        if milli in (0, 85000):
            return ERR_DS
        return round(milli / 1000.0, 2)
    except Exception:
        return ERR_DS


def read_ds18b20_all():
    """Return (cell, env1, env2). The cell probe is pinned by ROM id; the other
    probes (sorted by id) are env temps. Missing probes -> ERR_DS."""
    all_ids = sorted(p.split("/")[-2] for p in glob.glob("/sys/bus/w1/devices/28-*/w1_slave"))
    cell = ERR_DS
    if CELL_DS18B20_ID in all_ids:
        cell = _read_one_ds(f"/sys/bus/w1/devices/{CELL_DS18B20_ID}/w1_slave")
    env_ids = [i for i in all_ids if i != CELL_DS18B20_ID]
    # if the pinned cell id isn't present, treat the first found as cell
    if cell == ERR_DS and CELL_DS18B20_ID not in all_ids and env_ids:
        cell = _read_one_ds(f"/sys/bus/w1/devices/{env_ids.pop(0)}/w1_slave")
    env = [_read_one_ds(f"/sys/bus/w1/devices/{i}/w1_slave") for i in env_ids[:2]]
    while len(env) < 2:
        env.append(ERR_DS)
    return cell, env[0], env[1]


# ══════════════════════════════════════════════════════════════════════════════
#  Unified poller + structured CSV + in-memory history
# ══════════════════════════════════════════════════════════════════════════════

# Sensor fields this module owns (order = CSV column order after t/datetime/ts)
_SENSOR_FIELDS = ["T", "RH", "AQI", "TVOC", "eCO2", "PM1", "PM25", "PM10",
                  "DS_cell", "DS_env1", "DS_env2"]

_lock          = threading.Lock()
_history       = []            # list[dict] capped at HISTORY_MAX
_latest        = {}            # most recent merged row
_extra_provider = None         # set by server.py: () -> OrderedDict of extra cols
_columns       = None          # finalized CSV header (set on first row)

_cfg_lock  = threading.Lock()
_intervals = dict(DEFAULT_INTERVALS)


# ── per-sensor interval config (persisted) ────────────────────────────────────
def load_config():
    global _intervals
    try:
        if CONFIG_PATH.exists():
            data = json.loads(CONFIG_PATH.read_text())
            with _cfg_lock:
                for k in DEFAULT_INTERVALS:
                    if k in data:
                        _intervals[k] = max(_MIN_INTERVAL, float(data[k]))
    except Exception:
        pass


def save_config():
    try:
        with _cfg_lock:
            snap = dict(_intervals)
        CONFIG_PATH.write_text(json.dumps(snap, indent=2))
    except Exception:
        pass


def get_intervals():
    with _cfg_lock:
        return dict(_intervals)


def set_intervals(updates):
    """Update one or more poll intervals (seconds). Returns the new dict."""
    with _cfg_lock:
        for k, v in (updates or {}).items():
            if k in DEFAULT_INTERVALS:
                try:
                    _intervals[k] = max(_MIN_INTERVAL, float(v))
                except (TypeError, ValueError):
                    pass
    save_config()
    return get_intervals()


def set_extra_provider(fn):
    """Register a callable returning an OrderedDict of extra curve columns
    (GPS / heater / flight) to merge into every logged row."""
    global _extra_provider
    _extra_provider = fn


def _open_hardware():
    bus = aht = ens = pm_ser = None
    if smbus2 is not None:
        try:
            bus = smbus2.SMBus(I2C_BUS)
            aht = AHT21(bus)
            ens = ENS160(bus)
        except Exception:
            bus = aht = ens = None
    if serial is not None:
        try:
            pm_ser = serial.Serial(PLANTOWER_PORT, PLANTOWER_BAUD, timeout=1)
        except Exception:
            pm_ser = None
    return bus, aht, ens, pm_ser


def _poller():
    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    load_config()
    bus, aht, ens, pm_ser = _open_hardware()
    start = time.time()

    # latest cached value per sensor group (forward-filled into each CSV row)
    cache = {f: (ERR_FLOAT if f in ("T", "RH") else
                 ERR_AQI if f == "AQI" else
                 ERR_PM if f.startswith("PM") else
                 ERR_DS if f.startswith("DS_") else ERR_INT)
             for f in _SENSOR_FIELDS}

    last = {k: 0.0 for k in ("aht21", "ens160", "plantower", "ds18b20", "csv", "hw_retry")}

    while True:
        now = time.time()
        iv = get_intervals()

        # retry hardware that failed to open (e.g. before reboot / wiring)
        if (aht is None or ens is None or pm_ser is None) and now - last["hw_retry"] > 30:
            b2, a2, e2, p2 = _open_hardware()
            if aht is None: bus, aht = b2, a2
            if ens is None: ens = e2
            if pm_ser is None: pm_ser = p2
            last["hw_retry"] = now

        # ── each sensor sampled at its OWN interval ──
        if now - last["aht21"] >= iv["aht21"]:
            cache["T"], cache["RH"] = aht.read() if (aht and aht.ok) else (ERR_FLOAT, ERR_FLOAT)
            last["aht21"] = now

        if now - last["ens160"] >= iv["ens160"]:
            if ens and ens.ok:
                t_c = cache["T"] if cache["T"] > -100 else None
                rh_v = cache["RH"] if cache["RH"] > -100 else None
                ens.set_compensation(t_c, rh_v)
                cache["AQI"], cache["TVOC"], cache["eCO2"] = ens.read()
            else:
                cache["AQI"], cache["TVOC"], cache["eCO2"] = ERR_AQI, ERR_INT, ERR_INT
            last["ens160"] = now

        if now - last["plantower"] >= iv["plantower"]:
            cache["PM1"], cache["PM25"], cache["PM10"] = (
                read_plantower(pm_ser) if pm_ser else (ERR_PM, ERR_PM, ERR_PM))
            last["plantower"] = now

        if now - last["ds18b20"] >= iv["ds18b20"]:
            cache["DS_cell"], cache["DS_env1"], cache["DS_env2"] = read_ds18b20_all()
            last["ds18b20"] = now

        # ── unified CSV row at its own cadence ──
        if now - last["csv"] >= iv["csv"]:
            row = OrderedDict()
            row["t"]         = int(now - start)
            row["DatumZeit"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            row["ts"]        = round(now, 1)
            for f in _SENSOR_FIELDS:
                row[f] = cache[f]
            if _extra_provider is not None:
                try:
                    extra = _extra_provider()
                    if extra:
                        row.update(extra)
                except Exception:
                    pass
            _append(row)
            _write_csv(row)
            last["csv"] = now

        time.sleep(0.5)   # base tick


def _append(row):
    global _latest
    with _lock:
        _latest = dict(row)
        _history.append(dict(row))
        if len(_history) > HISTORY_MAX:
            _history.pop(0)


def _write_csv(row):
    global _columns
    try:
        # finalize column order once per process (sensor cols + extras)
        if _columns is None:
            _columns = list(row.keys())
            # If an existing log has a DIFFERENT header (schema changed, e.g. new
            # columns), rotate it aside so old/new rows never misalign.
            if CSV_PATH.exists():
                try:
                    existing = CSV_PATH.read_text().split("\n", 1)[0].strip()
                    if existing != ";".join(_columns):
                        CSV_PATH.rename(CSV_PATH.with_suffix(".csv.old"))
                except Exception:
                    pass
        new_file = not CSV_PATH.exists()
        with open(CSV_PATH, "a", newline="") as f:
            w = csv.writer(f, delimiter=";")
            if new_file:
                w.writerow(_columns)
            w.writerow([row.get(c, "") for c in _columns])
    except Exception:
        pass


def start():
    threading.Thread(target=_poller, daemon=True, name="sensor-poller").start()


def snapshot():
    """Return (latest_dict, history_list) for the API."""
    with _lock:
        return dict(_latest), list(_history)


def csv_path():
    return CSV_PATH


def reset_log():
    """Truncate the CSV and clear in-memory history (start a fresh log)."""
    global _columns
    with _lock:
        _history.clear()
        _latest.clear()
    _columns = None
    try:
        if CSV_PATH.exists():
            CSV_PATH.unlink()
        return True
    except Exception:
        return False
