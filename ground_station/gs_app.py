#!/usr/bin/env python3
"""
StratoPi HSO — Ground Station
==============================
Receives LoRa telemetry from the HAB and displays live position on a map.

Input sources (auto-detected or configured):
  1. Serial LoRa HAT  — second Waveshare SX1268 HAT via USB-to-UART or GPIO UART
  2. UDP port 5005    — output from gr-lora GNU Radio decoder (RTL-SDR / NESDR Mini 2+)

Web UI: http://localhost:5001
"""

import serial
import serial.tools.list_ports
import socket
import threading
import json
import time
import re
import argparse
import logging
from datetime import datetime
from flask import Flask, render_template, jsonify

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [GS] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("gs")

app = Flask(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────

LORA_BAUD       = 9600
UDP_HOST        = "0.0.0.0"
UDP_PORT        = 5005      # gr-lora output port
CALLSIGN_FILTER = "STRATOPI"   # only accept packets from this callsign
MAX_TRACK_PTS   = 2000      # keep last N positions on map

# ── Shared state ───────────────────────────────────────────────────────────────

_lock = threading.Lock()
_state = {
    "telemetry":   [],      # full list of received frames (newest first)
    "track":       [],      # [{lat,lon,alt}, ...] for map polyline
    "latest":      None,    # most recent decoded frame
    "rx_count":    0,
    "last_rx":     None,
    "source":      None,    # "serial" or "udp"
    "raw_packets": [],      # last 20 raw strings
}


# ══════════════════════════════════════════════════════════════════════════════
#  Packet parser
# ══════════════════════════════════════════════════════════════════════════════

def _crc_xor(s: str) -> str:
    crc = 0
    for c in s:
        crc ^= ord(c)
    return f"{crc:02X}"


def parse_packet(raw: str) -> dict | None:
    """
    Parse UKHAS telemetry packet.
    Format: $$CALLSIGN,seq,time,lat,lon,alt,speed,heading,vspeed,sats,hdop,fix*CRC
    Returns dict or None if invalid.
    """
    raw = raw.strip()
    if not raw.startswith("$$"):
        return None

    # Check CRC
    m = re.match(r"^\$\$(.+)\*([0-9A-Fa-f]{2})$", raw)
    if not m:
        return None
    body, rx_crc = m.group(1), m.group(2).upper()
    if _crc_xor(body) != rx_crc:
        log.warning(f"CRC mismatch: {raw!r}")
        return None

    parts = body.split(",")
    if len(parts) < 12:
        return None

    try:
        callsign = parts[0]
        if CALLSIGN_FILTER and callsign != CALLSIGN_FILTER:
            return None

        return {
            "callsign": callsign,
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
            "rx_ts":    datetime.now().isoformat(),
        }
    except (ValueError, IndexError) as e:
        log.warning(f"Parse error: {e} — {raw!r}")
        return None


def ingest(raw: str, source: str):
    """Store a decoded telemetry frame."""
    frame = parse_packet(raw)
    if frame is None:
        return

    with _lock:
        _state["rx_count"]   += 1
        _state["last_rx"]     = datetime.now().isoformat()
        _state["source"]      = source
        _state["latest"]      = frame
        _state["telemetry"].insert(0, frame)
        _state["telemetry"]   = _state["telemetry"][:200]   # keep last 200

        # Track (only add if position changed)
        pt = {"lat": frame["lat"], "lon": frame["lon"], "alt": frame["alt"],
              "seq": frame["seq"]}
        _state["track"].append(pt)
        _state["track"] = _state["track"][-MAX_TRACK_PTS:]

        _state["raw_packets"].insert(0, raw.strip())
        _state["raw_packets"] = _state["raw_packets"][:20]

    log.info(f"[{source}] #{frame['seq']:05d} | "
             f"alt={frame['alt']:.0f}m | "
             f"lat={frame['lat']:.5f} lon={frame['lon']:.5f} | "
             f"sats={frame['sats']} hdop={frame['hdop']}")


# ══════════════════════════════════════════════════════════════════════════════
#  Receiver threads
# ══════════════════════════════════════════════════════════════════════════════

def _serial_rx(port: str):
    """Read UKHAS sentences from serial LoRa HAT."""
    log.info(f"Serial RX on {port} @ {LORA_BAUD}")
    while True:
        try:
            with serial.Serial(port, LORA_BAUD, timeout=5) as ser:
                # Configure HAT if it responds to AT commands
                # (leave in RX mode, accept any incoming data)
                buf = ""
                while True:
                    c = ser.read(1).decode("ascii", errors="replace")
                    if not c:
                        continue
                    if c == "\n":
                        line = buf.strip()
                        buf  = ""
                        if line:
                            ingest(line, "serial")
                    else:
                        buf += c
        except serial.SerialException as e:
            log.error(f"Serial error: {e}. Retrying in 5s…")
            time.sleep(5)
        except Exception as e:
            log.error(f"Serial RX exception: {e}")
            time.sleep(5)


def _udp_rx():
    """Receive decoded LoRa bytes from gr-lora (GNU Radio decoder) over UDP."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((UDP_HOST, UDP_PORT))
    log.info(f"UDP RX on {UDP_HOST}:{UDP_PORT} (gr-lora output)")
    while True:
        try:
            data, addr = sock.recvfrom(4096)
            text = data.decode("ascii", errors="replace").strip()
            if text:
                ingest(text, f"udp:{addr[0]}")
        except Exception as e:
            log.error(f"UDP RX error: {e}")


def _auto_detect_serial() -> str | None:
    """Find a likely LoRa serial port."""
    for p in serial.tools.list_ports.comports():
        desc = (p.description or "").lower()
        if any(k in desc for k in ("usb", "uart", "lora", "ch340", "cp210", "ft232")):
            log.info(f"Auto-detected serial: {p.device} ({p.description})")
            return p.device
    return None


# ══════════════════════════════════════════════════════════════════════════════
#  Flask routes
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return render_template("map.html")


@app.route("/api/state")
def api_state():
    with _lock:
        return jsonify({
            "latest":    _state["latest"],
            "track":     _state["track"][-500:],   # last 500 pts to map
            "rx_count":  _state["rx_count"],
            "last_rx":   _state["last_rx"],
            "source":    _state["source"],
        })


@app.route("/api/telemetry")
def api_telemetry():
    with _lock:
        return jsonify(_state["telemetry"][:50])


@app.route("/api/raw")
def api_raw():
    with _lock:
        return jsonify(_state["raw_packets"])


# ══════════════════════════════════════════════════════════════════════════════
#  Entry point
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="StratoPi HSO Ground Station")
    parser.add_argument("--serial", "-s", metavar="PORT",
                        help="Serial port for LoRa HAT (e.g. COM5 or /dev/ttyUSB0). "
                             "Auto-detected if omitted.")
    parser.add_argument("--no-serial", action="store_true",
                        help="Disable serial input (UDP/gr-lora only)")
    parser.add_argument("--no-udp",   action="store_true",
                        help="Disable UDP input")
    parser.add_argument("--port", "-p", type=int, default=5001,
                        help="Web UI port (default 5001)")
    args = parser.parse_args()

    threads = []

    # Serial receiver
    if not args.no_serial:
        port = args.serial or _auto_detect_serial()
        if port:
            t = threading.Thread(target=_serial_rx, args=(port,), daemon=True)
            t.start()
            threads.append(t)
        else:
            log.warning("No serial port found. Use --serial PORT or connect LoRa HAT.")

    # UDP receiver (gr-lora)
    if not args.no_udp:
        t = threading.Thread(target=_udp_rx, daemon=True)
        t.start()
        threads.append(t)

    log.info(f"Ground station web UI: http://localhost:{args.port}")
    app.run(host="0.0.0.0", port=args.port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
