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
from flask import Flask, redirect, render_template, jsonify, request

from alerts import AltitudeAlerts, DEFAULT_MILESTONES, reset_triggered
from bundle_paths import app_dir, is_frozen, templates_dir
from e22_regs import TX_POWER_DBM, rssi_byte_to_dbm, rssi_label, rssi_to_quality
from lora_cmd import parse_ack
from lora_crypto import (
    ENCRYPT_MARKER,
    crypto_from_config,
    crypto_status,
    ensure_crypto_key,
    key_fingerprint,
    load_crypto_config,
    save_crypto_config,
)
from lora_downlink import send_config_ping
from pi_link import (
    CANONICAL_PI_URL,
    discover_pi_url,
    pi_link_status,
    pi_request as pi_link_request,
    pi_url_candidates,
)
from whereami_host import get_network_status, start_whereami_host, stop_whereami_host

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [GS] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("gs")

CRYPTO_PATH = app_dir() / "lora_crypto.json"
_CRYPTO_CFG = load_crypto_config(CRYPTO_PATH)
_CRYPTO_CFG, _key_generated = ensure_crypto_key(_CRYPTO_CFG)
if _key_generated:
    save_crypto_config(CRYPTO_PATH, _CRYPTO_CFG)
    log.info("Generated LoRa AES key — will sync to Pi over Wi‑Fi")
_CRYPTO = crypto_from_config(_CRYPTO_CFG)
if _CRYPTO_CFG.get("enabled") and _CRYPTO:
    log.info("LoRa decryption active: %s", crypto_status(_CRYPTO_CFG).get("algorithm"))
elif _CRYPTO_CFG.get("enabled"):
    log.error("lora_crypto.json enabled but key/library missing on ground station")

app = Flask(__name__, template_folder=str(templates_dir()))

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
    "alert_log":     [],    # recent milestone / diagnostic events
    "signal": {
        "rssi_dbm":    None,
        "quality_pct": None,
        "label":       None,
        "history":     [],   # [{ts, dbm, pct}, ...]
        "packets_with_rssi": 0,
    },
    "downlink": {
        "serial_port": None,
        "last_ping": None,
        "last_ack": None,
    },
    "crypto_link": {
        "decrypt_ok": 0,
        "decrypt_fail": 0,
        "encrypted_rx": 0,
        "pi_reachable": None,
        "pi_sync": None,
        "key_match": None,
        "last_sync": None,
        "last_sync_error": None,
        "pi_url_used": None,
        "last_check": None,
    },
}

# Shared serial port (RX thread + downlink TX)
_serial_port_name: str | None = None
_serial_lock = threading.Lock()
_serial_handle: serial.Serial | None = None
_serial_use_dtr = False
_serial_use_rts = False

_alerts = AltitudeAlerts()


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
    Normal:  $$CALLSIGN,seq,time,lat,lon,alt,speed,heading,vspeed,sats,hdop,fix[,cell_temp][,heater_duty]*CRC
    Diag:    $$CALLSIGN,...,fix,D,milestone_m,tx_ok,tx_fail,uptime_s,cell_temp,heater_duty*CRC
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

        frame = {
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
            "packet_type": "normal",
            "encrypted": bool(_CRYPTO_CFG.get("enabled") and _CRYPTO),
            "raw":      raw,
            "rx_ts":    datetime.now().isoformat(),
        }
        if len(parts) > 12 and parts[12] == "D":
            frame["packet_type"] = "diagnostic"
            frame["milestone_m"] = int(parts[13])
            frame["tx_ok"] = int(parts[14])
            frame["tx_fail"] = int(parts[15])
            frame["uptime_s"] = int(parts[16])
            if len(parts) > 17 and parts[17]:
                frame["cell_temp"] = float(parts[17])
            if len(parts) > 18 and parts[18]:
                frame["heater_duty"] = float(parts[18])
        else:
            if len(parts) > 12 and parts[12]:
                frame["cell_temp"] = float(parts[12])
            if len(parts) > 13 and parts[13]:
                frame["heater_duty"] = float(parts[13])
        return frame
    except (ValueError, IndexError) as e:
        log.warning(f"Parse error: {e} — {raw!r}")
        return None


def _update_signal(rssi_byte: int | None):
    if rssi_byte is None:
        return
    dbm = round(rssi_byte_to_dbm(rssi_byte), 1)
    pct = rssi_to_quality(dbm)
    label = rssi_label(dbm)
    pt = {"ts": datetime.now().isoformat(), "dbm": dbm, "pct": pct}
    sig = _state["signal"]
    sig["rssi_dbm"] = dbm
    sig["quality_pct"] = pct
    sig["label"] = label
    sig["packets_with_rssi"] = sig.get("packets_with_rssi", 0) + 1
    hist = sig.get("history", [])
    hist.append(pt)
    sig["history"] = hist[-120:]


def _reload_crypto() -> None:
    global _CRYPTO_CFG, _CRYPTO
    _CRYPTO_CFG = load_crypto_config(CRYPTO_PATH)
    _CRYPTO = crypto_from_config(_CRYPTO_CFG)


def _open_wire(raw: str) -> str | None:
    """Decrypt LoRa frame if AES envelope present."""
    raw = raw.strip()
    if not raw.startswith("$$"):
        return raw
    body = raw[2:].split("*", 1)[0] if "*" in raw else ""
    if body.split(",")[1:2] == [ENCRYPT_MARKER]:
        with _lock:
            _state["crypto_link"]["encrypted_rx"] += 1
        if not _CRYPTO:
            log.warning("Encrypted packet received but ground crypto not configured")
            with _lock:
                _state["crypto_link"]["decrypt_fail"] += 1
            return None
        opened = _CRYPTO.open_packet(raw)
        with _lock:
            if opened is None:
                _state["crypto_link"]["decrypt_fail"] += 1
            else:
                _state["crypto_link"]["decrypt_ok"] += 1
        if opened is None:
            log.warning("Decrypt failed (wrong key or corrupted frame)")
        return opened
    return raw


def _ingest_ack(raw: str, source: str):
    raw = _open_wire(raw) or ""
    if not raw:
        return False
    ack = parse_ack(raw)
    if ack is None:
        return False
    with _lock:
        _state["downlink"]["last_ack"] = {
            **ack,
            "rx_ts": datetime.now().isoformat(),
            "source": source,
        }
        _state["raw_packets"].insert(0, raw.strip())
        _state["raw_packets"] = _state["raw_packets"][:20]
    log.info(
        "[downlink] HAB ACK: CH%d air=%d %d dBm",
        ack["channel"], ack["air_rate"], ack["tx_power_dbm"],
    )
    return True


def ingest(raw: str, source: str, rssi_byte: int | None = None):
    """Store a decoded telemetry frame."""
    opened = _open_wire(raw)
    if opened is None:
        return
    raw = opened
    if raw.strip().startswith("$$STRATOPI_ACK"):
        _ingest_ack(raw, source)
        return

    frame = parse_packet(raw)
    if frame is None:
        return

    with _lock:
        if rssi_byte is not None:
            frame["rssi_dbm"] = round(rssi_byte_to_dbm(rssi_byte), 1)
            frame["rssi_quality"] = rssi_to_quality(frame["rssi_dbm"])
            _update_signal(rssi_byte)
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

    events = _alerts.on_frame(frame)
    if events:
        with _lock:
            for ev in events:
                _state["alert_log"].insert(0, {**ev, "ts": datetime.now().isoformat()})
            _state["alert_log"] = _state["alert_log"][:30]

    extra = ""
    if frame.get("packet_type") == "diagnostic":
        extra += f" | DIAG@{frame.get('milestone_m')}m"
    if frame.get("cell_temp") is not None:
        extra += f" | cell={frame['cell_temp']:.1f}°C"
    if frame.get("heater_duty") is not None:
        extra += f" | heat={frame['heater_duty']:.0f}%"
    if frame.get("rssi_dbm") is not None:
        extra += f" | RSSI {frame['rssi_dbm']:.0f} dBm"
    log.info(f"[{source}] #{frame['seq']:05d} | "
             f"alt={frame['alt']:.0f}m | "
             f"lat={frame['lat']:.5f} lon={frame['lon']:.5f} | "
             f"sats={frame['sats']} hdop={frame['hdop']}{extra}")


def _release_serial() -> None:
    """Close COM port (e.g. after serial error) so it can be reopened."""
    global _serial_handle
    with _serial_lock:
        if _serial_handle is not None:
            try:
                _serial_handle.close()
            except Exception:
                pass
            _serial_handle = None


# ══════════════════════════════════════════════════════════════════════════════
#  Receiver threads
# ══════════════════════════════════════════════════════════════════════════════

def _open_serial(port: str) -> serial.Serial:
    global _serial_handle
    if _serial_handle is not None and _serial_handle.is_open:
        if getattr(_serial_handle, "port", None) == port:
            return _serial_handle
        try:
            _serial_handle.close()
        except Exception:
            pass
    _serial_handle = serial.Serial(port, LORA_BAUD, timeout=1)
    if _serial_use_dtr:
        _serial_handle.setDTR(False)
    if _serial_use_rts:
        _serial_handle.setRTS(False)
    return _serial_handle


def _serial_rx(port: str):
    """Read UKHAS sentences from serial LoRa HAT (optional E22 RSSI prefix byte)."""
    global _serial_port_name
    _serial_port_name = port
    with _lock:
        _state["downlink"]["serial_port"] = port
    log.info(f"Serial RX on {port} @ {LORA_BAUD}")
    buf = ""
    rssi_byte = None
    pending_rssi: int | None = None
    while True:
        try:
            with _serial_lock:
                ser = _open_serial(port)
                chunk = ser.read(1)
            if not chunk:
                continue
            b = chunk[0]

            # E22 REG3 RSSI: one byte may arrive in a separate UART burst before '$$…'
            if pending_rssi is not None and not buf:
                if b == ord("$"):
                    rssi_byte = pending_rssi
                    pending_rssi = None
                    buf = "$"
                else:
                    pending_rssi = None
                    if b == ord("$"):
                        rssi_byte = None
                        buf = "$"
                    else:
                        pending_rssi = b
                continue

            if not buf:
                if b == ord("$"):
                    rssi_byte = None
                    pending_rssi = None
                    buf = "$"
                else:
                    pending_rssi = b
                continue

            ch = chr(b)
            if ch == "\n":
                line = buf.strip()
                buf = ""
                rb = rssi_byte
                rssi_byte = None
                pending_rssi = None
                if line.startswith("$$"):
                    ingest(line, "serial", rb)
                elif line:
                    log.info(f"Serial raw: {line!r}")
            else:
                buf += ch
        except serial.SerialException as e:
            log.error(f"Serial error: {e}. Retrying in 5s…")
            _release_serial()
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

@app.before_request
def _whereami_viewer_redirect():
    """Phones on PC hotspot (whereami.local) get map-only, not the operator UI."""
    if request.path != "/" or request.method != "GET":
        return None
    host = (request.host or "").split(":")[0].lower()
    if host in ("whereami", "whereami.local") or host.startswith("192.168.137."):
        return redirect("/whereami")
    return None


@app.route("/")
def index():
    return render_template("map.html")


@app.route("/whereami")
def whereami_viewer():
    """Map-only page for phones on the PC hotspot (whereami.local)."""
    return render_template("map_viewer.html")


@app.route("/api/state")
def api_state():
    with _lock:
        return jsonify({
            "latest":    _state["latest"],
            "track":     _state["track"][-500:],   # last 500 pts to map
            "rx_count":  _state["rx_count"],
            "last_rx":   _state["last_rx"],
            "source":    _state["source"],
            "signal":    _state["signal"],
            "crypto":    crypto_status(_CRYPTO_CFG),
            "crypto_link": _state.get("crypto_link"),
        })


@app.route("/api/telemetry")
def api_telemetry():
    with _lock:
        return jsonify(_state["telemetry"][:50])


@app.route("/api/raw")
def api_raw():
    with _lock:
        return jsonify(_state["raw_packets"])


@app.route("/api/alerts/config", methods=["GET"])
def api_alerts_config_get():
    return jsonify(_alerts.get_config())


@app.route("/api/alerts/config", methods=["POST"])
def api_alerts_config_post():
    patch = request.get_json(force=True, silent=True) or {}
    cfg = _alerts.update_config(patch)
    return jsonify(cfg)


@app.route("/api/alerts/reset", methods=["POST"])
def api_alerts_reset():
    cfg = reset_triggered(_alerts.get_config())
    _alerts.reload()
    return jsonify(cfg)


@app.route("/api/signal")
def api_signal():
    with _lock:
        return jsonify(_state["signal"])


@app.route("/api/alerts/events")
def api_alerts_events():
    browser = _alerts.pop_browser_events()
    with _lock:
        return jsonify({
            "browser": browser,
            "log": _state["alert_log"][:20],
        })


def _pi_url() -> str:
    return (_alerts.get_config().get("pi_url") or "http://stratopi.local:8080").rstrip("/")


def _pi_url_list() -> list[str]:
    return pi_url_candidates(_pi_url())


def _remember_pi_url(base: str) -> None:
    cfg = _alerts.get_config()
    if cfg.get("pi_url") != base:
        _alerts.update_config({"pi_url": base})
        log.info("Pi reachable at %s — saved for next requests", base)


def _pi_request(path: str, method: str = "GET", body: dict | None = None, timeout: int = 8):
    data, err, used = pi_link_request(
        _pi_url_list(),
        path,
        method=method,
        body=body,
        timeout=timeout,
        on_url_resolved=_remember_pi_url,
        primary=_pi_url(),
    )
    if used:
        with _lock:
            _state["crypto_link"]["pi_url_used"] = used
    return data, err


def _sync_crypto_to_pi() -> dict:
    """Push encryption key + enabled flag to Pi over Wi‑Fi."""
    payload = {
        "enabled": bool(_CRYPTO_CFG.get("enabled")),
        "key_hex": str(_CRYPTO_CFG.get("key_hex", "")),
    }
    data, err, used = pi_link_request(
        _pi_url_list(),
        "/api/lora/crypto",
        method="POST",
        body=payload,
        timeout=8,
        on_url_resolved=_remember_pi_url,
        primary=_pi_url(),
    )
    now = datetime.now().isoformat()
    with _lock:
        link = _state["crypto_link"]
        link["last_sync"] = now
        link["pi_url_used"] = used
        if err:
            link["pi_sync"] = False
            link["last_sync_error"] = err.get("error", str(err))
        else:
            link["pi_sync"] = True
            link["last_sync_error"] = None
    if err:
        log.warning("Pi crypto sync failed: %s", err)
        return {"ok": False, "pi_url": used, **err}
    log.info("LoRa crypto synced to Pi @ %s", used or _pi_url())
    return {"ok": True, "pi_url": used, **(data or {})}


def _crypto_link_check() -> dict:
    """Verify Pi Wi‑Fi reachability and that both ends share the same key."""
    local_fp = key_fingerprint(str(_CRYPTO_CFG.get("key_hex", "")))
    primary = _pi_url()
    health, health_err, used = pi_link_request(
        _pi_url_list(), "/api/health", timeout=5,
        on_url_resolved=_remember_pi_url, primary=primary,
    )
    pi_data, pi_err, _ = pi_link_request(
        _pi_url_list(), "/api/lora/crypto", timeout=5,
        on_url_resolved=_remember_pi_url, primary=primary,
    )
    pi_reachable = health_err is None and bool(health and health.get("ok"))
    pi_fp = (pi_data or {}).get("key_fingerprint") if pi_err is None else None
    pi_enabled = (pi_data or {}).get("enabled") if pi_err is None else None
    key_match = (
        local_fp is not None
        and pi_fp is not None
        and local_fp == pi_fp
        and bool(_CRYPTO_CFG.get("enabled")) == bool(pi_enabled)
    )
    now = datetime.now().isoformat()
    with _lock:
        link = _state["crypto_link"]
        link["last_check"] = now
        link["pi_reachable"] = pi_reachable
        link["key_match"] = key_match if pi_reachable else None
        if used:
            link["pi_url_used"] = used
        decrypt_ok = link.get("decrypt_ok", 0)
        decrypt_fail = link.get("decrypt_fail", 0)
        encrypted_rx = link.get("encrypted_rx", 0)
        link_stats = {
            "decrypt_ok": decrypt_ok,
            "decrypt_fail": decrypt_fail,
            "encrypted_rx": encrypted_rx,
            "decrypt_working": (
                decrypt_ok > 0 and decrypt_fail == 0
            ) if encrypted_rx > 0 else None,
        }
    return {
        "ok": True,
        "local": crypto_status(_CRYPTO_CFG),
        "pi_reachable": pi_reachable,
        "pi_sync": pi_err is None,
        "key_match": key_match,
        "pi_enabled": pi_enabled,
        "pi_fingerprint": pi_fp,
        "pi_url": used or _state["crypto_link"].get("pi_url_used"),
        "health_error": health_err.get("error") if health_err else None,
        "crypto_error": pi_err.get("error") if pi_err else None,
        "link": link_stats,
    }


def _pi_discovery_loop():
    """Keep Pi URL fresh across home LAN / Pi hotspot changes."""
    time.sleep(2.0)
    while True:
        try:
            found = discover_pi_url(_pi_url(), force=False)
            if found:
                _remember_pi_url(found)
                with _lock:
                    _state["crypto_link"]["pi_url_used"] = found
                    _state["crypto_link"]["pi_reachable"] = True
            else:
                with _lock:
                    _state["crypto_link"]["pi_reachable"] = False
        except Exception as e:
            log.debug("Pi discovery: %s", e)
        time.sleep(20)


def _crypto_bootstrap():
    """Generate key if needed, sync to Pi, then periodic health checks."""
    time.sleep(1.0)
    discover_pi_url(_pi_url(), force=True)
    result = _sync_crypto_to_pi()
    if result.get("ok"):
        log.info("Startup crypto sync OK (%s)", result.get("pi_url"))
    else:
        log.warning(
            "Startup crypto sync failed — connect to Pi Wi‑Fi or home LAN, then use Sync in UI"
        )
    while True:
        time.sleep(60)
        try:
            _crypto_link_check()
        except Exception as e:
            log.debug("Crypto link check: %s", e)


@app.route("/api/lora/crypto", methods=["GET"])
def api_gs_lora_crypto_get():
    with _lock:
        link = dict(_state.get("crypto_link") or {})
    return jsonify({
        "ok": True,
        **crypto_status(_CRYPTO_CFG),
        "link": link,
    })


@app.route("/api/lora/crypto", methods=["POST"])
def api_gs_lora_crypto_post():
    """Toggle encryption and push key to Pi."""
    patch = request.get_json(force=True, silent=True) or {}
    cfg = dict(_CRYPTO_CFG)
    if "enabled" in patch:
        cfg["enabled"] = bool(patch["enabled"])
    cfg, generated = ensure_crypto_key(cfg)
    if generated:
        save_crypto_config(CRYPTO_PATH, cfg)
    elif patch:
        save_crypto_config(CRYPTO_PATH, cfg)
    _reload_crypto()
    sync = _sync_crypto_to_pi()
    with _lock:
        link = dict(_state.get("crypto_link") or {})
    status = crypto_status(_CRYPTO_CFG)
    return jsonify({
        "ok": sync.get("ok", False),
        **status,
        "link": link,
        "sync": sync,
    })


@app.route("/api/lora/crypto/sync", methods=["POST"])
def api_gs_lora_crypto_sync():
    sync = _sync_crypto_to_pi()
    check = _crypto_link_check()
    if not sync.get("ok"):
        return jsonify({"ok": False, "sync": sync, "check": check}), 502
    return jsonify({"ok": True, "sync": sync, "check": check})


@app.route("/api/lora/crypto/check", methods=["GET"])
def api_gs_lora_crypto_check():
    return jsonify(_crypto_link_check())


@app.route("/api/lora/radio", methods=["GET"])
def api_gs_lora_radio_get():
    data, err = _pi_request("/api/lora/radio")
    if err:
        return jsonify({"ok": False, **err}), 502
    return jsonify({"ok": True, "pi_url": _pi_url(), **data})


@app.route("/api/lora/radio", methods=["POST"])
def api_gs_lora_radio_post():
    patch = request.get_json(force=True, silent=True) or {}
    data, err = _pi_request("/api/lora/radio", "POST", patch)
    if err:
        return jsonify({"ok": False, **err}), 502
    return jsonify({"ok": True, **data})


@app.route("/api/lora/downlink", methods=["GET"])
def api_lora_downlink_get():
    with _lock:
        return jsonify({
            "ok": True,
            "serial_port": _serial_port_name,
            "use_dtr": _serial_use_dtr,
            "max_tx_dbm": 22,
            "options_dbm": list(TX_POWER_DBM),
            "default_link": {"channel": 24, "air_rate": 3},
            **(_state.get("downlink") or {}),
        })


@app.route("/api/lora/downlink/ping", methods=["POST"])
def api_lora_downlink_ping():
    """
    Send LoRa config command from ground COM module at max power.
    Transmitted on link_channel/link_air_rate (HAB's current settings).
    HAB permanently applies target_channel/air_rate/tx_power_dbm.
    """
    if not _serial_port_name:
        return jsonify({"ok": False, "error": "No serial port — start with --serial PORT"}), 400

    patch = request.get_json(force=True, silent=True) or {}
    link_ch = int(patch.get("link_channel", 24))
    link_air = int(patch.get("link_air_rate", 3))
    target_ch = int(patch.get("target_channel", link_ch))
    target_air = int(patch.get("target_air_rate", link_air))
    target_dbm = int(patch.get("target_tx_power_dbm", patch.get("tx_power_dbm", 10)))
    tx_dbm = int(patch.get("tx_dbm", 22))
    repeats = int(patch.get("repeats", 3))

    if tx_dbm not in TX_POWER_DBM:
        return jsonify({"ok": False, "error": f"tx_dbm must be one of {list(TX_POWER_DBM)}"}), 400
    if target_dbm not in TX_POWER_DBM:
        return jsonify({"ok": False, "error": f"target_tx_power_dbm must be one of {list(TX_POWER_DBM)}"}), 400

    try:
        with _serial_lock:
            ser = _open_serial(_serial_port_name)
            result = send_config_ping(
                ser,
                link_channel=link_ch,
                link_air_rate=link_air,
                target_channel=target_ch,
                target_air_rate=target_air,
                target_tx_dbm=target_dbm,
                tx_dbm=tx_dbm,
                repeats=repeats,
                crypto=_CRYPTO,
            )
    except Exception as e:
        log.exception("Downlink ping failed")
        return jsonify({"ok": False, "error": str(e)}), 500

    with _lock:
        _state["downlink"]["last_ping"] = {
            **result,
            "ts": datetime.now().isoformat(),
        }
        _state["raw_packets"].insert(0, result["packet"])
        _state["raw_packets"] = _state["raw_packets"][:20]

    return jsonify({"ok": True, **result})


@app.route("/api/pi/link")
def api_pi_link():
    status = pi_link_status()
    with _lock:
        status["reachable"] = _state.get("crypto_link", {}).get("pi_reachable")
        status["saved_url"] = _alerts.get_config().get("pi_url")
    return jsonify({"ok": True, "canonical": CANONICAL_PI_URL, **status})


@app.route("/api/network")
def api_network():
    port = request.host.split(":")[-1] if ":" in request.host else "5001"
    try:
        port_n = int(port)
    except ValueError:
        port_n = 5001
    return jsonify(get_network_status(port_n))


@app.route("/api/alerts/sync-pi", methods=["POST"])
def api_alerts_sync_pi():
    """Push milestone + economy settings to Pi server API."""
    cfg = _alerts.get_config()
    data, err = _pi_request("/api/lora/diag", "POST", {
        "economy_mode": cfg.get("economy_mode", True),
        "tx_interval_economy": 55,
        "tx_interval_normal": 35,
        "milestones_m": cfg.get("enabled_milestones") or cfg.get("milestones_m"),
        "lean_packets": True,
    })
    if err:
        log.error("Pi sync failed: %s", err)
        return jsonify({"ok": False, **err}), 502
    return jsonify({"ok": True, "pi": data})


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
    parser.add_argument("--use-dtr", action="store_true",
                        help="Drive E22 M1 via USB adapter DTR for TX config at 22 dBm")
    parser.add_argument("--use-rts", action="store_true",
                        help="Drive E22 M1 via USB adapter RTS (if DTR not wired)")
    parser.add_argument("--no-hotspot", action="store_true",
                        help="Do not start Windows Wi‑Fi hotspot / whereami.local mDNS")
    args = parser.parse_args()

    global _serial_use_dtr, _serial_use_rts
    _serial_use_dtr = args.use_dtr
    _serial_use_rts = args.use_rts

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

    threading.Thread(target=_pi_discovery_loop, daemon=True).start()
    threading.Thread(target=_crypto_bootstrap, daemon=True).start()

    if not args.no_hotspot:
        try:
            net = start_whereami_host(args.port, hotspot=True)
            for url in net.get("viewer_urls", net.get("urls", [])):
                log.info("Share map (phones): %s", url)
        except Exception as e:
            log.warning("whereami.local host setup failed: %s", e)

    if is_frozen():
        log.info("StratoPi Ground Station %s", app_dir())
    log.info("Ground Control: http://localhost:%d", args.port)
    try:
        app.run(host="0.0.0.0", port=args.port, debug=False, use_reloader=False)
    finally:
        stop_whereami_host()


if __name__ == "__main__":
    main()
