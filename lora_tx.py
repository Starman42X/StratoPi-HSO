#!/usr/bin/env python3
"""
StratoPi HSO — LoRa GPS Telemetry Transmitter
==============================================
Hardware: Waveshare SX1268 433M LoRa HAT + Flyfish M10 GPS

UART wiring (both share /dev/serial0 = ttyAMA0 = GPIO14/15):
  GPS TX  -> Pi GPIO15 (pin 10, UART RX)   <- only pin needed for receive
  GPS RX  -> not connected
  LoRa E22 RXD -> Pi GPIO14 (pin 8, UART TX)  via HAT B-jumper
  LoRa E22 TXD -> Pi GPIO15 (pin 10, UART RX) via HAT B-jumper
  M1      -> GPIO27 (BCM) for mode switching (M1 jumper removed from HAT)
  M0      -> GND via HAT jumper (leave in)

NOTE on bus sharing: GPS TX and LoRa E22 TXD both connect to Pi GPIO15.
In a transmit-only HAB setup the E22 UART-TX is idle (HIGH) because no
LoRa packets are received from the air, so GPS data flows cleanly.

German legal limits (BNetzA / ETSI EN 300 220):
  Band:       433.05-434.79 MHz ISM SRD
  TX power:   <= 10 mW ERP = 10 dBm
  Duty cycle: <= 10 % -> 30 s interval at SF12/BW125 (~2.5 s air time)
"""

import serial
import time
import math
import json
import json as _json
import threading
import logging
from datetime import datetime, timezone
from pathlib import Path

try:
    import RPi.GPIO as GPIO
    GPIO_AVAILABLE = True
except ImportError:
    GPIO_AVAILABLE = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [LORA] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("lora_tx")

# ── Hardware ───────────────────────────────────────────────────────────────────

LORA_PORT  = "/dev/serial0"   # ttyAMA0 — GPIO14/15 (BT disabled)
LORA_BAUD  = 9600
GPS_PORT   = "/dev/ttyAMA5"   # UART5 — GPIO13 (pin 33), dedicated GPS UART
GPS_BAUD   = 115200           # Flyfish M10 factory default (dedicated UART, no conflict)
PIN_M0     = 22               # BCM — move M0 jumper on HAT from GND to GPIO22 position
PIN_M1     = 27               # BCM — M1 jumper must be removed from HAT (or on GPIO position)

# ── Radio config ───────────────────────────────────────────────────────────────
# E22 frequency grid: 410.125 MHz + CH × 1 MHz  →  434.200 is NOT reachable.
# CH=24 → 434.125 MHz. Ground station SDR must tune 434.125 MHz.
# E22 air-rate presets map to LoRa modulation: 0.3k ≈ SF12/BW125, 2.4k ≈ SF8/BW125.

# Air-rate register → actual modulation was measured empirically on THIS module
# (sweep_analyze.py); the datasheet table does NOT apply. Verified mapping:
#   air 0/1/2 -> clamped to 2 -> SF11/BW500   (module minimum; wide, no LDRO)
#   air 3     -> SF12/BW125  (narrow, best range; LDRO on)  <-- chosen
#   air 4     -> SF12/BW125
#   air 5     -> SF12/BW250
#   air 6/7   -> SF11/BW125
E22_CHANNEL   = 24            # measured center ~434.105 MHz
FREQ_HZ       = 434_105_000   # measured emission center (for logging)
E22_AIR_RATE  = 0b011         # =3 -> SF12/BW125, the robust narrow HAB mode
SF            = 12            # informational — implied by E22_AIR_RATE
TX_INTERVAL   = 35            # s — SF12/BW125 ~9% duty (DE limit 10%)

CALLSIGN     = "STRATOPI"
STATUS_FILE  = Path("/tmp/lora_status.json")
GPS_FIX_FILE = Path("/tmp/gps_fix.json")
DIAG_CONFIG  = Path("/home/louis/stratopi/lora_diag.json")
RADIO_CONFIG = Path("/home/louis/stratopi/lora_radio.json")
CRYPTO_CONFIG = Path("/home/louis/stratopi/lora_crypto.json")

try:
    from e22_regs import DEFAULT_TX_DBM, TX_POWER_DBM, erp_mw, power_reg
    from lora_cmd import build_ack_apply, parse_cmd
    from lora_crypto import crypto_from_config, crypto_status, load_crypto_config
except ImportError:
    DEFAULT_TX_DBM = 10
    TX_POWER_DBM = (22, 17, 13, 10)
    def power_reg(dbm):  # noqa: E302
        return {22: 0b00, 17: 0b01, 13: 0b10, 10: 0b11}[dbm]
    def erp_mw(dbm):
        return round(10 ** (dbm / 10.0), 2)
    def build_ack_apply(ch, air, dbm):  # noqa: E302
        body = f"STRATOPI_ACK,APPLY,{ch},{air},{dbm}"
        crc = 0
        for c in body:
            crc ^= ord(c)
        return f"$${body}*{crc:02X}"
    def parse_cmd(raw):  # noqa: E302
        return None
    def load_crypto_config(path):  # noqa: E302
        return {"enabled": False, "key_hex": ""}
    def crypto_from_config(cfg):  # noqa: E302
        return None
    def crypto_status(cfg):  # noqa: E302
        return {"enabled": False}

DEFAULT_DIAG = {
    "economy_mode": True,
    "tx_interval_economy": 55,
    "tx_interval_normal": 35,
    "milestones_m": [1000, 5000, 10000, 20000, 30000],
    "triggered_m": [],
    "lean_packets": True,
}


# ══════════════════════════════════════════════════════════════════════════════
#  GPS Emulator  (used when real GPS unavailable)
# ══════════════════════════════════════════════════════════════════════════════

class GPSEmulator:
    LAUNCH_LAT = 48.3705
    LAUNCH_LON = 10.8730
    ASCENT_MS  = 5.2
    WIND_E_MS  = 4.0
    WIND_N_MS  = 1.5

    def __init__(self):
        self._start = time.time()

    def read(self):
        t   = time.time() - self._start
        alt = t * self.ASCENT_MS           # no artificial ceiling
        lat = self.LAUNCH_LAT + t * self.WIND_N_MS / 111_320
        lon = self.LAUNCH_LON + t * self.WIND_E_MS / (111_320 * math.cos(math.radians(lat)))

        return {
            "time":    datetime.now(timezone.utc).strftime("%H:%M:%S"),
            "lat":     round(lat, 6),
            "lon":     round(lon, 6),
            "alt":     round(alt, 1),
            "speed":   round(math.hypot(self.WIND_E_MS, self.WIND_N_MS) * 3.6, 1),
            "heading": round(math.degrees(math.atan2(self.WIND_E_MS, self.WIND_N_MS)) % 360, 1),
            "vspeed":  round(self.ASCENT_MS, 2),
            "sats":    8,
            "hdop":    1.2,
            "fix":     3,
            "source":  "emulated",
        }


# ══════════════════════════════════════════════════════════════════════════════
#  NMEA GPS reader  (Flyfish M10)
# ══════════════════════════════════════════════════════════════════════════════

class NMEAReader:
    """
    Reads NMEA sentences from a serial port.

    Accepts either a port-name string (opens its own Serial) or a pre-opened
    serial.Serial object shared with the LoRa E22 driver.  Sharing is safe
    because GPS reads UART-RX (GPIO15) and LoRa writes UART-TX (GPIO14) —
    separate OS-level buffers, no locking needed between threads.

    Writes every GGA fix to GPS_FIX_FILE so server.py can serve coordinates
    without holding the serial port open.
    """

    def __init__(self, port_or_ser, baud=9600):
        import pynmea2
        self._nm = pynmea2
        if isinstance(port_or_ser, str):
            self._ser = serial.Serial(port_or_ser, baud, timeout=2)
            self._owns_ser = True
        else:
            self._ser = port_or_ser   # shared object
            self._owns_ser = False
        self._data = {}
        self._lock = threading.Lock()
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                line = self._ser.readline().decode("ascii", errors="replace").strip()
                if not line.startswith("$"):
                    continue
                msg = self._nm.parse(line)

                if msg.sentence_type == "GGA":
                    fix = {
                        "time":   str(msg.timestamp),
                        "lat":    round(float(msg.latitude  or 0), 6),
                        "lon":    round(float(msg.longitude or 0), 6),
                        "alt":    round(float(msg.altitude  or 0), 1),
                        "sats":   int(msg.num_sats or 0),
                        "hdop":   float(msg.horizontal_dil or 99.9),
                        "fix":    int(msg.gps_qual or 0),
                        "source": "gps",
                    }
                    with self._lock:
                        self._data.update(fix)
                    # Write shared fix file — server.py reads this for the web UI
                    try:
                        GPS_FIX_FILE.write_text(json.dumps({
                            **self._data,
                            "ts": time.time(),
                        }))
                    except Exception:
                        pass

                elif msg.sentence_type in ("RMC", "VTG"):
                    upd = {}
                    if hasattr(msg, "spd_over_grnd_kmph") and msg.spd_over_grnd_kmph:
                        upd["speed"] = round(float(msg.spd_over_grnd_kmph), 1)
                    elif hasattr(msg, "spd_over_grnd") and msg.spd_over_grnd:
                        upd["speed"] = round(float(msg.spd_over_grnd) * 1.852, 1)
                    if hasattr(msg, "true_track") and msg.true_track:
                        upd["heading"] = round(float(msg.true_track), 1)
                    if upd:
                        with self._lock:
                            self._data.update(upd)

            except Exception:
                pass

    def read(self):
        with self._lock:
            d = self._data.copy()
        if not d.get("lat"):
            return None
        d.setdefault("vspeed",  0.0)
        d.setdefault("speed",   0.0)
        d.setdefault("heading", 0.0)
        return d




# ══════════════════════════════════════════════════════════════════════════════
#  File-based GPS reader  (reads gps_fix.json written by server.py)
# ══════════════════════════════════════════════════════════════════════════════

class FileGPSReader:
    """
    Read GPS fix from /tmp/gps_fix.json.
    Used when GPS and LoRa share the same UART but at different baud rates
    (GPS=115200, LoRa E22=9600).  server.py owns the UART at 115200 and
    writes gps_fix.json on every GGA sentence.  lora_tx.py reads from that
    file so the two processes never fight over the baud-rate setting.
    """

    MAX_AGE = 30  # seconds before data is considered too stale

    @staticmethod
    def _placeholder():
        """Transmit even indoors / without GPS lock — coords stay 0 until fix."""
        return {
            "time":    datetime.now(timezone.utc).strftime("%H:%M:%S"),
            "lat":     0.0,
            "lon":     0.0,
            "alt":     0.0,
            "sats":    0,
            "hdop":    99.9,
            "fix":     0,
            "vspeed":  0.0,
            "speed":   0.0,
            "heading": 0.0,
            "source":  "no_fix",
        }

    def read(self):
        try:
            if GPS_FIX_FILE.exists():
                d = _json.loads(GPS_FIX_FILE.read_text())
                age = time.time() - d.get('ts', 0)
                if age < self.MAX_AGE:
                    d.setdefault('vspeed',  0.0)
                    d.setdefault('speed',   0.0)
                    d.setdefault('heading', 0.0)
                    d.setdefault('source',  'gps')
                    t = d.get('time')
                    if not t or str(t) in ('None', ''):
                        d['time'] = datetime.now(timezone.utc).strftime('%H:%M:%S')
                    else:
                        d['time'] = str(t).split('.')[0].replace('+00:00', '')[:8]
                    return d
        except Exception:
            pass
        return self._placeholder()

# ══════════════════════════════════════════════════════════════════════════════
#  EBYTE E22 driver  (Waveshare SX1268 HAT)
# ══════════════════════════════════════════════════════════════════════════════

class E22:
    """
    Driver for EBYTE E22 LoRa module on the Waveshare SX1268 HAT.

    Accepts a pre-opened serial.Serial object (shared with NMEAReader) or
    opens its own if ser=None.
    """

    def __init__(
        self,
        port=LORA_PORT,
        baud=LORA_BAUD,
        ser=None,
        tx_power_dbm=DEFAULT_TX_DBM,
        channel=E22_CHANNEL,
        air_rate=E22_AIR_RATE,
    ):
        self._port = port
        self._baud = baud
        self._ser  = ser          # None -> opened in begin()
        self._tx_power_dbm = int(tx_power_dbm)
        self._channel = int(channel)
        self._air_rate = int(air_rate) & 0b111
        self._configured = False

        if GPIO_AVAILABLE and PIN_M1 is not None:
            GPIO.setmode(GPIO.BCM)
            GPIO.setwarnings(False)
            GPIO.setup(PIN_M1, GPIO.OUT)
            if PIN_M0 is not None:
                GPIO.setup(PIN_M0, GPIO.OUT)
                GPIO.output(PIN_M0, GPIO.LOW)
            self._set_mode_tx()

    def _set_mode_config(self):
        """M0=LOW, M1=HIGH -> config mode (mode 2). NOTE: M0=M1=HIGH is DEEP SLEEP
        on the E22 — the old AT-command code used that and the module never heard
        a single config byte. Config mode only needs M1, so it works even if the
        M0 jumper is still strapped to GND on the HAT."""
        if GPIO_AVAILABLE and PIN_M1 is not None:
            if PIN_M0 is not None:
                GPIO.output(PIN_M0, GPIO.LOW)
            GPIO.output(PIN_M1, GPIO.HIGH)
            time.sleep(0.5)

    def _set_mode_tx(self):
        """M0=LOW, M1=LOW -> transparent TX mode (mode 0)."""
        if GPIO_AVAILABLE and PIN_M1 is not None:
            GPIO.output(PIN_M1, GPIO.LOW)
            if PIN_M0 is not None:
                GPIO.output(PIN_M0, GPIO.LOW)
            time.sleep(0.1)

    def _build_params(self, reg3: int = 0x00) -> bytes:
        reg0 = (0b011 << 5) | (0b00 << 3) | self._air_rate
        reg1 = (0b00 << 6) | power_reg(self._tx_power_dbm)
        reg2 = self._channel & 0xFF
        return bytes([0x00, 0x00, 0x00, reg0, reg1, reg2, reg3, 0x00, 0x00])

    def _write_config(self, reg3: int = 0x00) -> bool:
        params = self._build_params(reg3)
        frame = bytes([0xC0, 0x00, 0x09]) + params
        self._ser.reset_input_buffer()
        self._ser.write(frame)
        self._ser.flush()
        time.sleep(0.5)
        resp = self._ser.read(self._ser.in_waiting or 12)
        return resp[:3] == bytes([0xC1, 0x00, 0x09]) and resp[3:12] == params

    def begin(self):
        """Open port (if not shared), configure radio, switch to TX."""
        if self._ser is None:
            self._ser = serial.Serial(self._port, self._baud, timeout=1)
        time.sleep(0.3)
        return self.apply_config()

    def apply_config(self) -> bool:
        self._set_mode_config()
        log.info("Entered config mode (M0=LOW M1=HIGH)")
        ok = self._write_config(reg3=0x00)
        if ok:
            log.info(
                "E22 config CONFIRMED: CH=%d, air=%d -> %s, %d dBm (~%s mW), transparent",
                self._channel, self._air_rate,
                {3: 'SF12/BW125', 4: 'SF12/BW125', 5: 'SF12/BW250',
                 6: 'SF11/BW125', 2: 'SF11/BW500'}.get(self._air_rate, '?'),
                self._tx_power_dbm, erp_mw(self._tx_power_dbm),
            )
        else:
            log.warning("E22 config not confirmed — check UART/M1 wiring")
        self._set_mode_tx()
        log.info("Switched to transparent TX mode")
        self._configured = True
        return ok

    def set_tx_power(self, dbm: int) -> bool:
        return self.set_radio(channel=self._channel, air_rate=self._air_rate, tx_power_dbm=dbm)

    def set_radio(self, *, channel: int, air_rate: int, tx_power_dbm: int) -> bool:
        channel = int(channel)
        air_rate = int(air_rate) & 0b111
        tx_power_dbm = int(tx_power_dbm)
        unchanged = (
            self._configured
            and channel == self._channel
            and air_rate == self._air_rate
            and tx_power_dbm == self._tx_power_dbm
        )
        if unchanged:
            return True
        self._channel = channel
        self._air_rate = air_rate
        self._tx_power_dbm = tx_power_dbm
        if self._ser is None:
            return False
        return self.apply_config()

    @property
    def channel(self) -> int:
        return self._channel

    @property
    def air_rate(self) -> int:
        return self._air_rate

    def read_lines(self, timeout_s: float = 0.2) -> list[str]:
        """Drain UART RX (transparent downlink from ground)."""
        if self._ser is None:
            return []
        self._set_mode_tx()
        old_timeout = self._ser.timeout
        self._ser.timeout = max(0.05, timeout_s)
        lines: list[str] = []
        buf = ""
        deadline = time.time() + timeout_s
        try:
            while time.time() < deadline:
                chunk = self._ser.read(256)
                if not chunk:
                    continue
                buf += chunk.decode("ascii", errors="replace")
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if line:
                        lines.append(line)
        finally:
            self._ser.timeout = old_timeout
        return lines

    def send(self, payload: str) -> bool:
        if not self._configured:
            log.error("E22 not configured — call begin() first")
            return False
        self._set_mode_tx()
        raw = (payload + "\n").encode()
        self._ser.write(raw)
        self._ser.flush()
        return True

    def close(self):
        self._set_mode_tx()
        if GPIO_AVAILABLE:
            GPIO.cleanup()


# ══════════════════════════════════════════════════════════════════════════════
#  Telemetry packet (UKHAS format)
# ══════════════════════════════════════════════════════════════════════════════

def _crc_xor(s: str) -> str:
    crc = 0
    for c in s:
        crc ^= ord(c)
    return f"{crc:02X}"


RADIO_DEFAULT = {
    "tx_power_dbm": DEFAULT_TX_DBM,
    "channel": E22_CHANNEL,
    "air_rate": E22_AIR_RATE,
    "freq_hz": FREQ_HZ,
    "boost_once_pending": False,
    "boost_once_dbm": 22,
}


def channel_to_freq_hz(channel: int) -> int:
    """E22 grid: 410.125 MHz + CH × 1 MHz (approx; measured may differ)."""
    return int((410.125 + int(channel)) * 1_000_000)


def load_radio_config() -> dict:
    try:
        if RADIO_CONFIG.exists():
            data = _json.loads(RADIO_CONFIG.read_text())
            cfg = {**RADIO_DEFAULT, **data}
            cfg["tx_power_dbm"] = int(cfg["tx_power_dbm"])
            cfg["channel"] = int(cfg.get("channel", E22_CHANNEL))
            cfg["air_rate"] = int(cfg.get("air_rate", E22_AIR_RATE))
            cfg["freq_hz"] = int(cfg.get("freq_hz", channel_to_freq_hz(cfg["channel"])))
            cfg["boost_once_dbm"] = int(cfg.get("boost_once_dbm", 22))
            cfg["boost_once_pending"] = bool(cfg.get("boost_once_pending"))
            return cfg
    except Exception as e:
        log.warning("Radio config load failed (%s)", e)
    return RADIO_DEFAULT.copy()


def save_radio_config(cfg: dict) -> None:
    try:
        RADIO_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        RADIO_CONFIG.write_text(_json.dumps(cfg, indent=2))
    except Exception as e:
        log.warning("Radio config save failed: %s", e)


def load_diag_config() -> dict:
    try:
        if DIAG_CONFIG.exists():
            data = _json.loads(DIAG_CONFIG.read_text())
            return {**DEFAULT_DIAG, **data}
    except Exception as e:
        log.warning("Diag config load failed (%s) — defaults", e)
    return DEFAULT_DIAG.copy()


def save_diag_config(cfg: dict) -> None:
    try:
        DIAG_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        DIAG_CONFIG.write_text(_json.dumps(cfg, indent=2))
    except Exception as e:
        log.warning("Diag config save failed: %s", e)


class MilestoneTracker:
    """Fire once when altitude crosses configured milestones (ascending)."""

    def __init__(self):
        self._last_alt: float | None = None

    def check(self, alt: float, cfg: dict) -> int | None:
        milestones = sorted(int(m) for m in cfg.get("milestones_m", []))
        triggered = {int(m) for m in cfg.get("triggered_m", [])}
        last = self._last_alt
        self._last_alt = alt
        if last is None:
            return None
        for m in milestones:
            if m in triggered:
                continue
            if last < m <= alt:
                return m
        return None

    def mark(self, cfg: dict, milestone: int) -> dict:
        triggered = sorted({int(m) for m in cfg.get("triggered_m", [])} | {int(milestone)})
        cfg = {**cfg, "triggered_m": triggered}
        save_diag_config(cfg)
        return cfg


def _packet_core(seq: int, gps: dict) -> str:
    t = str(gps.get('time', ''))[:8] or '--:--:--'
    return (
        f"{CALLSIGN},{seq:05d},{t},"
        f"{gps['lat']:.6f},{gps['lon']:.6f},{gps['alt']:.1f},"
        f"{gps.get('speed', 0):.1f},{gps.get('heading', 0):.1f},"
        f"{gps.get('vspeed', 0):.2f},"
        f"{gps.get('sats', 0)},{gps.get('hdop', 99):.1f},"
        f"{gps.get('fix', 0)}"
    )


def build_packet(seq: int, gps: dict, *, lean: bool = False) -> str:
    body = _packet_core(seq, gps)
    ct = gps.get('cell_temp')
    hd = gps.get('heater_duty')
    ct_str = f'{ct:.1f}' if ct is not None else ''
    hd_str = f'{hd:.0f}' if hd is not None else ''
    if lean:
        if ct_str:
            body = f"{body},{ct_str}"
    else:
        body = f"{body},{ct_str},{hd_str}"
    return f"$${body}*{_crc_xor(body)}"


def build_diag_packet(seq: int, gps: dict, milestone: int, stats: dict) -> str:
    """One-shot vitality packet at altitude milestone (type marker D)."""
    ct = gps.get('cell_temp')
    hd = gps.get('heater_duty')
    ct_str = f'{ct:.1f}' if ct is not None else ''
    hd_str = f'{hd:.0f}' if hd is not None else ''
    body = (
        f"{_packet_core(seq, gps)},D,{int(milestone)},"
        f"{int(stats.get('tx_ok', 0))},{int(stats.get('tx_fail', 0))},"
        f"{int(stats.get('uptime_s', 0))},{ct_str},{hd_str}"
    )
    return f"$${body}*{_crc_xor(body)}"


_runtime_crypto = None
_runtime_crypto_cfg: dict = {"enabled": False}


def _wire_packet(packet: str, crypto) -> str:
    if crypto:
        return crypto.seal_packet(packet)
    return packet


def _open_wire_line(line: str, crypto) -> str | None:
    if crypto:
        return crypto.open_packet(line)
    return line


def apply_downlink_cmd(radio: E22, radio_cfg: dict, cmd: dict) -> dict:
    """Apply permanent radio settings from ground LoRa command."""
    ch = int(cmd["channel"])
    air = int(cmd["air_rate"])
    dbm = int(cmd["tx_power_dbm"])
    if dbm not in TX_POWER_DBM:
        log.warning("Downlink rejected: invalid power %d dBm", dbm)
        return radio_cfg
    if not (0 <= ch <= 83):
        log.warning("Downlink rejected: invalid channel %d", ch)
        return radio_cfg
    if not (0 <= air <= 7):
        log.warning("Downlink rejected: invalid air rate %d", air)
        return radio_cfg

    ok = radio.set_radio(channel=ch, air_rate=air, tx_power_dbm=dbm)
    radio_cfg = {
        **radio_cfg,
        "channel": ch,
        "air_rate": air,
        "tx_power_dbm": dbm,
        "freq_hz": channel_to_freq_hz(ch),
        "boost_once_pending": False,
    }
    save_radio_config(radio_cfg)
    log.info(
        "Downlink APPLY: CH=%d air=%d %d dBm (~%s mW) — permanent until next command",
        ch, air, dbm, erp_mw(dbm),
    )
    if ok:
        ack = build_ack_apply(ch, air, dbm)
        radio.send(_wire_packet(ack, _runtime_crypto))
        log.info("Downlink ACK sent: %s", ack)
    else:
        log.warning("Downlink applied to config file but E22 reconfig failed")
    return radio_cfg


def listen_for_commands(radio: E22, radio_cfg: dict, duration_s: float) -> dict:
    """Listen between TX cycles for ground config pings."""
    if duration_s <= 0:
        return radio_cfg
    end = time.time() + duration_s
    while time.time() < end:
        slice_s = min(0.5, end - time.time())
        if slice_s <= 0:
            break
        for line in radio.read_lines(slice_s):
            if not line.startswith("$$"):
                continue
            opened = _open_wire_line(line, _runtime_crypto)
            if opened is None:
                log.warning("Downlink frame decrypt failed")
                continue
            cmd = parse_cmd(opened)
            if cmd is None:
                continue
            log.info("Downlink command received (decrypted OK)")
            radio_cfg = apply_downlink_cmd(radio, radio_cfg, cmd)
    return radio_cfg


# ══════════════════════════════════════════════════════════════════════════════
#  Main loop
# ══════════════════════════════════════════════════════════════════════════════

def main():

    # Signal to server.py that we own the serial port
    _SERIAL_LOCK = Path('/tmp/stratopi_serial_lock')
    _SERIAL_LOCK.touch()
    import atexit as _atexit
    import signal as _signal
    _atexit.register(lambda: _SERIAL_LOCK.unlink(missing_ok=True))
    # SIGTERM (sent by systemd stop) must call sys.exit() to trigger atexit cleanup
    _signal.signal(_signal.SIGTERM, lambda sig, frm: __import__('sys').exit(0))

    log.info("StratoPi HSO LoRa TX starting")
    log.info(f"Port: {LORA_PORT} | ~{FREQ_HZ/1e6:.3f} MHz | "
             f"SF{SF}/BW125/CR4/5 (LDRO) | 10 dBm | interval {TX_INTERVAL}s")

    # ── GPS + Radio init ──────────────────────────────────────────────────────
    gps   = None
    radio = None

    radio_cfg = load_radio_config()
    tx_dbm = radio_cfg["tx_power_dbm"]
    ch = radio_cfg["channel"]
    air = radio_cfg["air_rate"]

    if GPS_PORT:
        try:
            gps   = FileGPSReader()
            radio = E22(tx_power_dbm=tx_dbm, channel=ch, air_rate=air)
            log.info(f"FileGPSReader: GPS from gps_fix.json | LoRa on {LORA_PORT}")
        except Exception as e:
            log.warning(f"GPS/serial init failed ({e}) — falling back to emulator")

    if gps is None:
        gps   = GPSEmulator()
        radio = E22(tx_power_dbm=tx_dbm, channel=ch, air_rate=air)
        log.info("Using emulated GPS flight data")

    radio.begin()
    last_radio = (ch, air, tx_dbm)

    global _runtime_crypto, _runtime_crypto_cfg
    _runtime_crypto_cfg = load_crypto_config(CRYPTO_CONFIG)
    _runtime_crypto = crypto_from_config(_runtime_crypto_cfg)

    seq, tx_ok, tx_fail = 0, 0, 0
    start_ts = time.time()
    tracker = MilestoneTracker()
    diag_cfg = load_diag_config()
    log.info(
        "Diag: economy=%s interval=%ss milestones=%s",
        diag_cfg.get("economy_mode"),
        diag_cfg.get("tx_interval_economy") if diag_cfg.get("economy_mode") else diag_cfg.get("tx_interval_normal"),
        diag_cfg.get("milestones_m"),
    )

    while True:
        t0 = time.time()
        diag_cfg = load_diag_config()
        radio_cfg = load_radio_config()
        crypto_cfg = load_crypto_config(CRYPTO_CONFIG)
        if crypto_cfg != _runtime_crypto_cfg:
            _runtime_crypto_cfg = crypto_cfg
            _runtime_crypto = crypto_from_config(crypto_cfg)
            if crypto_cfg.get("enabled") and _runtime_crypto:
                log.info("LoRa encryption: %s", crypto_status(crypto_cfg).get("algorithm"))
            elif crypto_cfg.get("enabled"):
                log.warning("Encryption enabled — waiting for key from ground station Wi‑Fi")

        tx_dbm = radio_cfg["tx_power_dbm"]
        ch = radio_cfg["channel"]
        air = radio_cfg["air_rate"]
        radio_tuple = (ch, air, tx_dbm)

        if radio_tuple != last_radio:
            radio.set_radio(channel=ch, air_rate=air, tx_power_dbm=tx_dbm)
            last_radio = radio_tuple
            log.info(
                "Radio updated: CH=%d air=%d %d dBm (~%s mW)",
                ch, air, tx_dbm, erp_mw(tx_dbm),
            )

        tx_interval = (
            int(diag_cfg.get("tx_interval_economy", 55))
            if diag_cfg.get("economy_mode", True)
            else int(diag_cfg.get("tx_interval_normal", TX_INTERVAL))
        )
        lean = bool(diag_cfg.get("economy_mode") and diag_cfg.get("lean_packets", True))

        fix = gps.read()
        if fix.get('fix', 0) == 0 or not fix.get('lat'):
            log.warning(
                "No GPS lock — TX continues with placeholder coords "
                "(move antenna outside for real position)"
            )

        seq += 1
        milestone = tracker.check(float(fix.get('alt', 0)), diag_cfg)
        stats = {
            "tx_ok": tx_ok,
            "tx_fail": tx_fail,
            "uptime_s": int(time.time() - start_ts),
        }

        if milestone is not None:
            packet = build_diag_packet(seq, fix, milestone, stats)
            diag_cfg = tracker.mark(diag_cfg, milestone)
            pkt_type = f"DIAG@{milestone}m"
        else:
            packet = build_packet(seq, fix, lean=lean)
            pkt_type = "lean" if lean else "full"

        src = fix.get("source", "?")
        ct = (f"{fix['cell_temp']:.1f}°C" if fix.get('cell_temp') is not None else '--')
        heat = (f" heat={fix['heater_duty']:.0f}%" if fix.get('heater_duty') is not None else '')
        log.info(
            "TX #%05d [%s/%s] alt=%.0fm lat=%.5f lon=%.5f ct=%s%s | %s",
            seq, src, pkt_type, fix['alt'], fix['lat'], fix['lon'], ct, heat, packet,
        )

        if _runtime_crypto_cfg.get("enabled") and not _runtime_crypto:
            log.error("TX skipped — encryption enabled but not configured")
            ok_tx = False
        else:
            wire = _wire_packet(packet, _runtime_crypto)
            ok_tx = radio.send(wire)
        tx_ok   += ok_tx
        tx_fail += not ok_tx

        STATUS_FILE.write_text(json.dumps({
            "state":      "tx_ok" if ok_tx else "tx_fail",
            "seq":        seq,
            "tx_ok":      tx_ok,
            "tx_fail":    tx_fail,
            "packet_type": pkt_type,
            "milestone_m": milestone,
            "gps":        fix,
            "packet":     packet,
            "diag_cfg":   diag_cfg,
            "tx_power_dbm": tx_dbm,
            "channel": ch,
            "air_rate": air,
            "ts":         datetime.now().isoformat(),
        }))

        sleep = max(0, tx_interval - (time.time() - t0))
        radio_cfg = listen_for_commands(radio, radio_cfg, sleep)
        last_radio = (
            radio_cfg["channel"],
            radio_cfg["air_rate"],
            radio_cfg["tx_power_dbm"],
        )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Stopped")
    except Exception as e:
        log.exception(f"Fatal: {e}")
        STATUS_FILE.write_text(json.dumps({"state": "error", "error": str(e)}))
        raise