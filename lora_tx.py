#!/usr/bin/env python3
"""
StratoPi HSO — LoRa GPS Telemetry Transmitter
==============================================
Runs as a daemon on the HAB Raspberry Pi.
Transmits UKHAS-format telemetry packets via the Waveshare SX1268 433M LoRa HAT.

German legal limits (BNetzA / ETSI EN 300 220):
  Band      : 433.05 – 434.79 MHz (ISM SRD)
  TX power  : ≤ 10 mW ERP  → 10 dBm
  Duty cycle: ≤ 10 %
  → At SF12/BW125 a 60-byte packet takes ~2.5 s air time.
    10 % duty cycle requires ≥ 25 s between packets. We use 30 s.

LoRa settings for 100 km+ range:
  SF 12 · BW 125 kHz · CR 4/5 · Preamble 12
  Receiver sensitivity ≈ −137 dBm
  Link budget @ 10 dBm TX, 0 dBi antennas, 100 km: ~ −118 dBm → 19 dB margin

Hardware: Waveshare SX1268 433M LoRa HAT
  Interface : UART (serial0 / ttyS0)
  Baud rate : 9600
  Mode pins : M0, M1 — set via GPIO or physical jumpers (see NOTE below)

NOTE — M0/M1 pin mapping:
  Check your specific HAT revision against the Waveshare wiki.
  Common mapping: M0 → GPIO22, M1 → GPIO27
  Transmission mode : M0=LOW  M1=LOW   (both jumpers shorted)
  Config/AT mode    : M0=LOW  M1=HIGH  (M0 shorted, M1 open)
  If your HAT has ONLY physical jumpers (no GPIO connection to Pi):
    Set both to "short" (LOW) for transmission mode and
    comment out the GPIO sections below.

GPS:
  Currently uses emulated flight data.
  Tomorrow's real GPS (Flyfish M10 Mini) will output NMEA on serial.
  Set GPS_PORT to the GPS serial device (e.g. /dev/ttyUSB0) to enable real GPS.
  Set GPS_PORT = None to keep emulation.
"""

import serial
import time
import math
import json
import threading
import logging
from datetime import datetime, timezone
from pathlib import Path

# Try GPIO — gracefully skip if not available (for testing on non-Pi)
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

# ── Hardware config ────────────────────────────────────────────────────────────

LORA_PORT   = "/dev/serial0"   # UART connected to SX1268 HAT
LORA_BAUD   = 9600
PIN_M0      = 22               # BCM GPIO for M0 (LOW = TX mode)
PIN_M1      = 27               # BCM GPIO for M1 (LOW = TX mode)
USE_GPIO    = True             # Set False if M0/M1 are hard-wired via jumpers

# ── LoRa radio config ──────────────────────────────────────────────────────────

FREQ_HZ        = 434_200_000   # 434.200 MHz — common European HAB frequency
TX_POWER_DBM   = 10            # 10 dBm = 10 mW (German/EU ISM legal max)
SF             = 12            # Spreading Factor (12 = max range)
BW_INDEX       = 7             # 7 = 125 kHz bandwidth
CR             = 1             # 1 = CR 4/5
PREAMBLE       = 12
NETWORK_ID     = 18            # 18 = public LoRa network
HAB_ADDRESS    = 1             # This HAB's address
TX_ADDRESS     = 0             # 0 = broadcast to all listeners

# ── Timing ─────────────────────────────────────────────────────────────────────

TX_INTERVAL_S  = 30            # ≥ 25 s for 10 % duty cycle compliance

# ── Mission config ─────────────────────────────────────────────────────────────

CALLSIGN       = "STRATOPI"

# ── GPS config ─────────────────────────────────────────────────────────────────
# Set to serial device path for real GPS (e.g. "/dev/ttyUSB0")
# Set to None to use emulated flight data
GPS_PORT       = None
GPS_BAUD       = 9600

# ── Status file (read by web UI) ───────────────────────────────────────────────
STATUS_FILE    = Path("/tmp/lora_status.json")


# ══════════════════════════════════════════════════════════════════════════════
#  GPS emulator — realistic HAB flight from south Germany
# ══════════════════════════════════════════════════════════════════════════════

class GPSEmulator:
    """Simulates a HAB flight: ascent → burst → descent."""

    LAUNCH_LAT  =  48.3705   # Near Landsberg am Lech, Bavaria
    LAUNCH_LON  =  10.8730
    ASCENT_MS   =   5.2      # m/s upward
    BURST_ALT   = 32_000     # m
    DESCENT_MS  =  -5.8      # m/s after burst (parachute)
    WIND_E_MS   =   4.0      # m/s eastward drift
    WIND_N_MS   =   1.5      # m/s northward drift

    def __init__(self):
        self._start = time.time()
        self._burst = False

    def read(self):
        elapsed = time.time() - self._start

        # Altitude
        if not self._burst:
            alt = elapsed * self.ASCENT_MS
            if alt >= self.BURST_ALT:
                self._burst = True
                self._burst_time = elapsed
            alt = min(alt, self.BURST_ALT)
        else:
            dt_descent = elapsed - self._burst_time
            alt = max(0.0, self.BURST_ALT + dt_descent * self.DESCENT_MS)

        # Horizontal drift (converting m to degrees)
        drift_e = elapsed * self.WIND_E_MS
        drift_n = elapsed * self.WIND_N_MS
        lat = self.LAUNCH_LAT + drift_n / 111_320
        lon = self.LAUNCH_LON + drift_e / (111_320 * math.cos(math.radians(lat)))

        # Speed & heading from drift
        speed_ms  = math.hypot(self.WIND_E_MS, self.WIND_N_MS)
        speed_kmh = speed_ms * 3.6
        heading   = math.degrees(math.atan2(self.WIND_E_MS, self.WIND_N_MS)) % 360

        # Vertical speed
        if not self._burst:
            vspeed = self.ASCENT_MS
        else:
            vspeed = self.DESCENT_MS if alt > 0 else 0.0

        now = datetime.now(timezone.utc)
        return {
            "time":    now.strftime("%H:%M:%S"),
            "lat":     round(lat, 6),
            "lon":     round(lon, 6),
            "alt":     round(alt, 1),
            "speed":   round(speed_kmh, 1),
            "heading": round(heading, 1),
            "vspeed":  round(vspeed, 2),
            "sats":    8,
            "hdop":    1.2,
            "fix":     3,         # 3D fix
            "source":  "emulated",
        }


# ══════════════════════════════════════════════════════════════════════════════
#  NMEA GPS reader — for real Flyfish M10 Mini tomorrow
# ══════════════════════════════════════════════════════════════════════════════

class NMEAReader:
    """Reads NMEA sentences from a serial GPS module (Flyfish M10 Mini, etc.)"""

    def __init__(self, port, baud=9600):
        try:
            import pynmea2
            self._pynmea2 = pynmea2
        except ImportError:
            raise ImportError("pip install pynmea2")
        self._ser = serial.Serial(port, baud, timeout=2)
        self._data = {}
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        while True:
            try:
                line = self._ser.readline().decode("ascii", errors="replace").strip()
                msg = self._pynmea2.parse(line)
                if msg.sentence_type == "GGA":
                    self._data.update({
                        "time":   str(msg.timestamp),
                        "lat":    round(float(msg.latitude or 0), 6),
                        "lon":    round(float(msg.longitude or 0), 6),
                        "alt":    round(float(msg.altitude or 0), 1),
                        "sats":   int(msg.num_sats or 0),
                        "hdop":   float(msg.horizontal_dil or 99),
                        "fix":    int(msg.gps_qual or 0),
                        "source": "gps",
                    })
                elif msg.sentence_type == "VTG":
                    self._data.update({
                        "speed":   round(float(msg.spd_over_grnd_kmph or 0), 1),
                        "heading": round(float(msg.true_track or 0), 1),
                    })
            except Exception:
                pass

    def read(self):
        d = self._data.copy()
        if not d.get("lat"):
            return None     # no fix yet
        d.setdefault("vspeed", 0.0)
        d.setdefault("speed",  0.0)
        d.setdefault("heading", 0.0)
        return d


# ══════════════════════════════════════════════════════════════════════════════
#  SX1268 AT-command driver (Waveshare UART HAT)
# ══════════════════════════════════════════════════════════════════════════════

class SX1268:
    """
    Minimal AT-command driver for the Waveshare SX1268 433M LoRa HAT.
    Uses RYLR-compatible AT command set (most Waveshare UART LoRa modules).
    """

    def __init__(self, port, baud=9600, pin_m0=None, pin_m1=None):
        self._port   = port
        self._baud   = baud
        self._pin_m0 = pin_m0
        self._pin_m1 = pin_m1
        self._ser    = None
        self._seq    = 0

    # ── GPIO helpers ──────────────────────────────────────────────────────────

    def _gpio_init(self):
        if not GPIO_AVAILABLE or self._pin_m0 is None:
            return
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        GPIO.setup(self._pin_m0, GPIO.OUT)
        GPIO.setup(self._pin_m1, GPIO.OUT)

    def _set_config_mode(self):
        """M0=LOW, M1=HIGH → AT command mode."""
        if GPIO_AVAILABLE and self._pin_m0 is not None:
            GPIO.output(self._pin_m0, GPIO.LOW)
            GPIO.output(self._pin_m1, GPIO.HIGH)
            time.sleep(0.2)

    def _set_tx_mode(self):
        """M0=LOW, M1=LOW → transparent / send mode."""
        if GPIO_AVAILABLE and self._pin_m0 is not None:
            GPIO.output(self._pin_m0, GPIO.LOW)
            GPIO.output(self._pin_m1, GPIO.LOW)
            time.sleep(0.2)

    # ── Serial helpers ────────────────────────────────────────────────────────

    def _cmd(self, cmd, timeout=1.0):
        """Send AT command, return response string."""
        self._ser.reset_input_buffer()
        self._ser.write((cmd + "\r\n").encode())
        time.sleep(timeout)
        resp = self._ser.read(self._ser.in_waiting).decode(errors="replace").strip()
        log.debug(f"AT >> {cmd!r}  << {resp!r}")
        return resp

    def _expect_ok(self, cmd, retries=3):
        for _ in range(retries):
            r = self._cmd(cmd)
            if "+OK" in r or "+BAND" in r or "+PARAM" in r or "+ADDR" in r:
                return True
            time.sleep(0.3)
        log.warning(f"No +OK for: {cmd!r}  (got {r!r})")
        return False

    # ── Public API ────────────────────────────────────────────────────────────

    def begin(self):
        """Open serial port, set GPIO, configure radio."""
        self._gpio_init()
        self._ser = serial.Serial(self._port, self._baud, timeout=1)
        time.sleep(0.5)

        self._set_config_mode()

        # Basic ping
        r = self._cmd("AT")
        if "+OK" not in r:
            log.warning(f"Module did not respond to AT (got {r!r}). "
                        "Check UART wiring and M0/M1 jumpers.")

        # Configure
        self._expect_ok(f"AT+ADDRESS={HAB_ADDRESS}")
        self._expect_ok(f"AT+NETWORKID={NETWORK_ID}")
        self._expect_ok(f"AT+BAND={FREQ_HZ}")
        self._expect_ok(f"AT+PARAMETER={SF},{BW_INDEX},{CR},{PREAMBLE}")
        self._expect_ok(f"AT+CRFOP={TX_POWER_DBM}")

        # Verify
        band = self._cmd("AT+BAND?")
        log.info(f"Radio configured | freq={FREQ_HZ/1e6:.3f} MHz | "
                 f"SF{SF} BW125 CR4/5 | {TX_POWER_DBM} dBm | {band}")

        self._set_tx_mode()
        return True

    def send(self, payload: str) -> bool:
        """Send a string payload to the broadcast address."""
        self._set_tx_mode()
        data    = payload.encode()
        length  = len(data)
        cmd     = f"AT+SEND={TX_ADDRESS},{length},{payload}"
        resp    = self._cmd(cmd, timeout=3.0)
        success = "+OK" in resp
        if not success:
            log.warning(f"Send failed: {resp!r}")
        return success

    def close(self):
        self._set_tx_mode()
        if self._ser:
            self._ser.close()
        if GPIO_AVAILABLE and self._pin_m0 is not None:
            GPIO.cleanup()


# ══════════════════════════════════════════════════════════════════════════════
#  Telemetry packet builder (UKHAS format)
# ══════════════════════════════════════════════════════════════════════════════

def _crc_xor(s: str) -> str:
    crc = 0
    for c in s:
        crc ^= ord(c)
    return f"{crc:02X}"


def build_packet(seq: int, gps: dict) -> str:
    """
    Build UKHAS-style telemetry sentence:
    $$CALLSIGN,seq,time,lat,lon,alt,speed,heading,vspeed,sats,hdop,fix*CRC
    """
    body = (
        f"{CALLSIGN},{seq:05d},"
        f"{gps['time']},"
        f"{gps['lat']:.6f},"
        f"{gps['lon']:.6f},"
        f"{gps['alt']:.1f},"
        f"{gps.get('speed', 0):.1f},"
        f"{gps.get('heading', 0):.1f},"
        f"{gps.get('vspeed', 0):.2f},"
        f"{gps.get('sats', 0)},"
        f"{gps.get('hdop', 99):.1f},"
        f"{gps.get('fix', 0)}"
    )
    crc    = _crc_xor(body)
    packet = f"$${body}*{crc}"
    return packet


# ══════════════════════════════════════════════════════════════════════════════
#  Status reporting to web UI
# ══════════════════════════════════════════════════════════════════════════════

def write_status(data: dict):
    try:
        STATUS_FILE.write_text(json.dumps(data))
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════════
#  Main loop
# ══════════════════════════════════════════════════════════════════════════════

def main():
    log.info("StratoPi HSO LoRa TX starting")
    log.info(f"Frequency: {FREQ_HZ/1e6:.3f} MHz | "
             f"SF{SF}/BW125/CR4/5 | {TX_POWER_DBM} dBm | "
             f"TX every {TX_INTERVAL_S}s")

    # GPS source
    gps_source = None
    if GPS_PORT:
        try:
            gps_source = NMEAReader(GPS_PORT, GPS_BAUD)
            log.info(f"Real GPS on {GPS_PORT}")
        except Exception as e:
            log.warning(f"GPS init failed ({e}), falling back to emulation")

    if gps_source is None:
        gps_source = GPSEmulator()
        log.info("Using emulated GPS (set GPS_PORT for real GPS)")

    # Radio
    radio = SX1268(
        port   = LORA_PORT,
        baud   = LORA_BAUD,
        pin_m0 = PIN_M0 if USE_GPIO else None,
        pin_m1 = PIN_M1 if USE_GPIO else None,
    )
    radio.begin()

    seq       = 0
    tx_ok     = 0
    tx_fail   = 0
    last_gps  = {}

    write_status({"state": "running", "seq": 0, "tx_ok": 0, "tx_fail": 0, "gps": {}})

    while True:
        loop_start = time.time()

        # Read GPS
        gps = gps_source.read()
        if gps is None:
            log.warning("No GPS fix, skipping TX")
            write_status({"state": "no_fix", "seq": seq, "tx_ok": tx_ok,
                          "tx_fail": tx_fail, "gps": last_gps})
            time.sleep(5)
            continue

        last_gps = gps
        seq += 1

        # Build and send packet
        packet = build_packet(seq, gps)
        log.info(f"TX #{seq:05d} | alt={gps['alt']:.0f}m | "
                 f"lat={gps['lat']:.4f} lon={gps['lon']:.4f} | {packet}")

        ok = radio.send(packet)
        if ok:
            tx_ok += 1
        else:
            tx_fail += 1

        write_status({
            "state":   "tx_ok" if ok else "tx_fail",
            "seq":     seq,
            "tx_ok":   tx_ok,
            "tx_fail": tx_fail,
            "gps":     gps,
            "packet":  packet,
            "ts":      datetime.now().isoformat(),
        })

        # Duty cycle compliance: sleep for remainder of TX_INTERVAL
        elapsed = time.time() - loop_start
        sleep   = max(0, TX_INTERVAL_S - elapsed)
        time.sleep(sleep)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Stopped by user")
    except Exception as e:
        log.exception(f"Fatal: {e}")
        write_status({"state": "error", "error": str(e)})
        raise
