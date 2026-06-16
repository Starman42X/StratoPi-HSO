#!/usr/bin/env python3
"""
StratoPi HSO — LoRa GPS test (TX with emulated GPS, or RX/decode mode)

Usage — on the Pi (transmit):
  python lora_gps_test.py --tx
  python lora_gps_test.py --tx --count 10 --interval 5

Usage — on the laptop (receive via second LoRa HAT on USB serial):
  python lora_gps_test.py --rx --port COM5
  python lora_gps_test.py --rx --port /dev/ttyUSB0

TX sends real UKHAS packets with emulated HAB flight data.
RX decodes and pretty-prints any UKHAS packet it sees on the serial port.
"""

import argparse
import math
import re
import serial
import serial.tools.list_ports
import sys
import time
from datetime import datetime, timezone

try:
    import RPi.GPIO as GPIO
    _GPIO = True
except ImportError:
    _GPIO = False

# ── Hardware ──────────────────────────────────────────────────────────────────

PORT_DEFAULT = "/dev/serial0"
BAUD         = 9600
PIN_M1       = 27        # GPIO27 = M1; M0 fixed LOW via jumper
CALLSIGN     = "STRATOPI"

# ── GPS emulator (same as lora_tx.py) ────────────────────────────────────────

class GPSEmulator:
    LAUNCH_LAT = 48.3705
    LAUNCH_LON = 10.8730
    ASCENT_MS  = 5.2
    BURST_ALT  = 32_000
    DESCENT_MS = -5.8
    WIND_E_MS  = 4.0
    WIND_N_MS  = 1.5

    def __init__(self):
        self._start = time.time()
        self._burst = False
        self._burst_t = None

    def read(self):
        t = time.time() - self._start
        if not self._burst:
            alt = min(t * self.ASCENT_MS, self.BURST_ALT)
            if alt >= self.BURST_ALT:
                self._burst = True
                self._burst_t = t
        else:
            alt = max(0.0, self.BURST_ALT + (t - self._burst_t) * self.DESCENT_MS)

        lat = self.LAUNCH_LAT + t * self.WIND_N_MS / 111_320
        lon = self.LAUNCH_LON + t * self.WIND_E_MS / (111_320 * math.cos(math.radians(lat)))
        vs  = self.ASCENT_MS if not self._burst else (self.DESCENT_MS if alt > 0 else 0.0)
        return {
            "time":    datetime.now(timezone.utc).strftime("%H:%M:%S"),
            "lat":     round(lat, 6),
            "lon":     round(lon, 6),
            "alt":     round(alt, 1),
            "speed":   round(math.hypot(self.WIND_E_MS, self.WIND_N_MS) * 3.6, 1),
            "heading": round(math.degrees(math.atan2(self.WIND_E_MS, self.WIND_N_MS)) % 360, 1),
            "vspeed":  round(vs, 2),
            "sats":    8,
            "hdop":    1.2,
            "fix":     3,
        }

# ── UKHAS packet builder / parser ─────────────────────────────────────────────

def _crc_xor(s: str) -> str:
    crc = 0
    for c in s:
        crc ^= ord(c)
    return f"{crc:02X}"

def build_packet(seq: int, gps: dict) -> str:
    body = (
        f"{CALLSIGN},{seq:05d},"
        f"{gps['time']},"
        f"{gps['lat']:.6f},{gps['lon']:.6f},"
        f"{gps['alt']:.1f},"
        f"{gps['speed']:.1f},{gps['heading']:.1f},{gps['vspeed']:.2f},"
        f"{gps['sats']},{gps['hdop']:.1f},{gps['fix']}"
    )
    return f"$${body}*{_crc_xor(body)}"

def parse_packet(raw: str) -> dict | None:
    raw = raw.strip()
    if not raw.startswith("$$"):
        return None
    m = re.match(r"^\$\$(.+)\*([0-9A-Fa-f]{2})$", raw)
    if not m:
        return None
    body, rx_crc = m.group(1), m.group(2).upper()
    if _crc_xor(body) != rx_crc:
        return {"_crc_error": True, "raw": raw}
    parts = body.split(",")
    if len(parts) < 12:
        return None
    try:
        return {
            "callsign": parts[0],
            "seq":      int(parts[1]),
            "time":     parts[2],
            "lat":      float(parts[3]),
            "lon":      float(parts[4]),
            "alt":      float(parts[5]),
            "speed":    float(parts[6]),
            "heading":  float(parts[7]),
            "vspeed":   float(parts[8]),
            "sats":     int(parts[9]),
            "hdop":     float(parts[10]),
            "fix":      int(parts[11]),
            "raw":      raw,
        }
    except (ValueError, IndexError):
        return None

# ── GPIO helpers ──────────────────────────────────────────────────────────────

def gpio_tx_mode():
    if not _GPIO:
        return
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    GPIO.setup(PIN_M1, GPIO.OUT)
    GPIO.output(PIN_M1, GPIO.LOW)   # M1=LOW + M0=LOW(jumper) → transparent TX
    time.sleep(0.15)

def gpio_cleanup():
    if _GPIO:
        try:
            GPIO.cleanup()
        except Exception:
            pass

# ── TX mode ───────────────────────────────────────────────────────────────────

def tx_mode(port: str, count: int, interval: float):
    print(f"TX mode — {port} @ {BAUD} baud")
    print(f"Sending {count} packets, {interval}s apart (UKHAS format, emulated GPS)\n")

    gpio_tx_mode()
    if _GPIO:
        print("GPIO: M1=LOW → transparent TX mode")
    else:
        print("No RPi.GPIO — assuming M0/M1 already set by jumpers")

    try:
        ser = serial.Serial(port, BAUD, timeout=1)
    except Exception as e:
        print(f"Cannot open {port}: {e}")
        gpio_cleanup()
        sys.exit(1)

    time.sleep(0.3)
    gps = GPSEmulator()

    for seq in range(1, count + 1):
        fix    = gps.read()
        packet = build_packet(seq, fix)
        raw    = (packet + "\n").encode()

        ser.write(raw)
        ser.flush()

        phase = "ASCENT" if fix["vspeed"] > 0 else ("DESCENT" if fix["vspeed"] < 0 else "GROUND")
        print(
            f"[{seq:03d}/{count}] {phase:7s} "
            f"alt={fix['alt']:7.0f}m  "
            f"lat={fix['lat']:.5f}  lon={fix['lon']:.5f}  "
            f"vspd={fix['vspeed']:+.1f}m/s"
        )
        print(f"         {packet}")

        if seq < count:
            print(f"         waiting {interval}s…")
            time.sleep(interval)

    ser.close()
    gpio_cleanup()
    print(f"\n{count} packets sent.")

# ── RX / decode mode ──────────────────────────────────────────────────────────

FIX_LABELS = {0: "No fix", 1: "GPS", 2: "DGPS", 3: "3D GPS"}

def _auto_port() -> str | None:
    for p in serial.tools.list_ports.comports():
        desc = (p.description or "").lower()
        if any(k in desc for k in ("usb", "uart", "lora", "ch340", "cp210", "ft232")):
            return p.device
    return None

def rx_mode(port: str | None):
    if port is None:
        port = _auto_port()
        if port is None:
            print("No serial port found. Use --port COMx or --port /dev/ttyUSB0")
            sys.exit(1)
        print(f"Auto-detected: {port}")

    print(f"RX mode — {port} @ {BAUD} baud")
    print("Listening for UKHAS packets… (Ctrl-C to stop)\n")

    rx_count = 0
    buf = ""

    while True:
        try:
            with serial.Serial(port, BAUD, timeout=2) as ser:
                while True:
                    c = ser.read(1).decode("ascii", errors="replace")
                    if not c:
                        continue
                    if c == "\n":
                        line = buf.strip()
                        buf = ""
                        if not line:
                            continue

                        if line.startswith("$$"):
                            frame = parse_packet(line)
                            ts = datetime.now().strftime("%H:%M:%S")
                            if frame is None:
                                print(f"[{ts}] BAD PACKET  {line!r}")
                            elif frame.get("_crc_error"):
                                print(f"[{ts}] CRC ERROR   {line!r}")
                            else:
                                rx_count += 1
                                vs = frame['vspeed']
                                phase = "ASCENT" if vs > 0 else ("DESCENT" if vs < -0.5 else "GROUND")
                                fix_str = FIX_LABELS.get(frame['fix'], str(frame['fix']))
                                print(
                                    f"[{ts}] #{frame['seq']:05d}  {phase:7s}  "
                                    f"alt={frame['alt']:7.0f}m  "
                                    f"lat={frame['lat']:.5f}  lon={frame['lon']:.5f}  "
                                    f"vspd={vs:+.1f}m/s  "
                                    f"sats={frame['sats']}  hdop={frame['hdop']:.1f}  {fix_str}  "
                                    f"[rx#{rx_count}]"
                                )
                        else:
                            # Non-UKHAS line — show raw (useful for debugging)
                            ts = datetime.now().strftime("%H:%M:%S")
                            print(f"[{ts}] RAW  {line!r}")
                    else:
                        buf += c

        except serial.SerialException as e:
            print(f"Serial error: {e} — retrying in 3s…")
            time.sleep(3)
        except KeyboardInterrupt:
            print(f"\nStopped. {rx_count} packets decoded.")
            break

# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="StratoPi HSO — LoRa GPS test (TX emulated GPS / RX decode)"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--tx", action="store_true", help="Transmit emulated GPS packets via LoRa")
    group.add_argument("--rx", action="store_true", help="Receive and decode UKHAS packets from serial")

    parser.add_argument("--port",     "-p", default=None,
                        help="Serial port (TX default: /dev/serial0, RX: auto-detected)")
    parser.add_argument("--count",    "-n", type=int,   default=20,
                        help="Number of packets to send (TX only, default 20)")
    parser.add_argument("--interval", "-i", type=float, default=5.0,
                        help="Seconds between packets (TX only, default 5)")
    args = parser.parse_args()

    if args.tx:
        tx_mode(args.port or PORT_DEFAULT, args.count, args.interval)
    else:
        rx_mode(args.port)


if __name__ == "__main__":
    main()
