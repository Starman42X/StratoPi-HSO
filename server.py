#!/usr/bin/env python3
"""StratoPi HSO - HAB dual-camera controller."""

from flask import Flask, render_template, jsonify, request, send_from_directory, abort, Response
import threading
import subprocess
import os
import glob
import re
import shutil
import time
import signal
from datetime import datetime, timezone
from pathlib import Path

app = Flask(__name__)
app.config['TEMPLATES_AUTO_RELOAD'] = True  # pick up deployed index.html without service restart

CAPTURES_DIR = Path("/home/louis/captures")
CAPTURES_DIR.mkdir(parents=True, exist_ok=True)

_lock = threading.Lock()

state = {
    "recording": False,
    "streaming": False,
    "mode": None,
    "start_time": None,
    "settings": {
        "hq_cam": {"width": 1920, "height": 1080, "fps": 30},
        "usb_cam": {"width": 1920, "height": 1080, "fps": 24, "prioritize_fps": True},
    },
}

hq_process = None
usb_process = None
_last_stop_time = 0.0

# ── recording watchdog state ──────────────────────────────────────────────────
# If rpicam-vid / ffmpeg dies mid-recording (thermal throttle, V4L2 glitch, USB
# re-enumeration, brief power dip) the old code left state['recording']=True with
# a dead process — silent loss of the rest of the flight. The watchdog detects a
# dead child and respawns it into a new segment file so recording self-heals.
_rec_hq_cmd      = None      # last HQ rpicam-vid argv (for respawn)
_rec_usb_cmd     = None      # last USB ffmpeg argv
_rec_hq_out      = None      # base output path (segment suffix added on respawn)
_rec_usb_out     = None
_rec_log_dir     = None
_rec_prefix      = None
_rec_restart     = {'hq': 0, 'usb': 0}
_MAX_REC_RESTART = 30        # backstop against a restart storm (bad device)
_rec_hq_files    = []        # all HQ raw outputs this session (base + watchdog segments)
_rec_hq_fps      = 30        # fps used for the remux step


def _segment_path(orig, seg):
    """Insert _pN before the extension so a respawn never overwrites the
    already-captured partial file. seg starts at 1."""
    p = Path(orig)
    return str(p.with_name(f"{p.stem}_p{seg}{p.suffix}"))


def _probe_fps(raw, default=30.0):
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=r_frame_rate", "-of", "csv=p=0", str(raw)],
            capture_output=True, text=True, timeout=10)
        v = r.stdout.strip()
        if "/" in v:
            n, d = v.split("/")
            if float(d):
                return float(n) / float(d)
    except Exception:
        pass
    return default


def _remux_raw_to_mp4(raw, fps):
    """rpicam-vid writes a RAW .h264/.mjpeg elementary stream — no container and
    no timing, so most players show only the first frame. Wrap it into a proper
    .mp4 with the correct framerate (stream copy, no re-encode). Returns the .mp4
    path on success and deletes the raw; keeps the raw on failure."""
    raw = Path(raw)
    if not raw.exists() or raw.stat().st_size == 0:
        return None
    try:
        fps = float(fps) if fps else 30.0
    except Exception:
        fps = 30.0
    mp4 = raw.with_suffix(".mp4")
    cmd = ["ffmpeg", "-y"]
    if raw.suffix == ".mjpeg":
        cmd += ["-f", "mjpeg"]
    cmd += ["-r", str(fps), "-i", str(raw), "-c:v", "copy", str(mp4)]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=900)
        if r.returncode == 0 and mp4.exists() and mp4.stat().st_size > 0:
            try:
                raw.unlink()
            except Exception:
                pass
            print(f"[remux] {raw.name} -> {mp4.name}")
            return str(mp4)
        print(f"[remux] FAILED {raw.name}: {r.stderr.decode(errors='replace')[-200:]}")
    except Exception as e:
        print(f"[remux] error {raw.name}: {e}")
    return None


def _remux_hq_outputs(files, fps):
    for f in list(files):
        if f and (str(f).endswith(".h264") or str(f).endswith(".mjpeg")):
            _remux_raw_to_mp4(f, fps)


def _remux_orphans():
    """On boot, remux any RAW camera files left by a crashed/killed session
    (power-loss flight) so they still yield playable videos."""
    for raw in glob.glob(str(CAPTURES_DIR / "*hq*.h264")) + \
               glob.glob(str(CAPTURES_DIR / "*hq*.mjpeg")):
        if not Path(raw).with_suffix(".mp4").exists():
            _remux_raw_to_mp4(raw, _probe_fps(raw))


# ── DS18B20 temperature ───────────────────────────────────────────

_TEMP_MAX = 120          # 10 min of history at 5 s per sample
_temp_history = []       # [{"ts": float, "c": float}, ...]

# The CELL temperature probe is identified BY PORT: it sits alone on its own
# 1-Wire bus (GPIO4) while the 2 env probes share the other bus (GPIO17). See
# sensors.classify_ds18b20(). This ROM id is only a single-bus fallback (used
# before the env bus is wired) so the heater never controls off an env probe.
CELL_DS18B20_ID = "28-000000bf78cc"


import re as _re2

# Plausible cell-temperature window. Anything outside is a bus glitch, not a
# real reading. DS18B20 hardware range is -55..125 °C; we clamp tighter because
# Li-ion cells in a HAB payload realistically stay within this.
_TEMP_MIN_C = -55.0
_TEMP_MAX_C = 110.0
# Max believable change between 5 s samples — a battery pack's thermal mass
# can't swing faster than this. Larger jumps are treated as suspect.
_TEMP_MAX_STEP_C = 15.0


def read_ds18b20(retries=3):
    """Read the CELL DS18B20 (pinned by ROM id) — degC or None on failure.

    Targets CELL_DS18B20_ID specifically so the heater never reads an env probe.
    Falls back to the first sensor only if the pinned id is absent.

    Rejects the two classic glitch artifacts that still pass the kernel CRC
    check on a noisy / long-wire 1-wire bus:
      * t=0     — all-zero scratchpad (CRC of all zeros is also zero, so the
                  kernel reports 'YES' for pure garbage)
      * t=85000 — the 85 °C power-on-reset default (sensor read before its
                  first conversion completed)
    Retries a few times because most 1-wire glitches are transient.
    """
    # Identify the cell probe BY PORT (it sits alone on its own 1-Wire bus, GPIO4)
    # via sensors.classify_ds18b20(); lazy import avoids module load-order issues.
    cell = None
    try:
        import sensors as _sensors_mod
        cell = _sensors_mod.cell_sensor_path()
    except Exception:
        cell = None
    if cell and os.path.exists(cell):
        sensors = [cell]
    else:
        # fallback: pinned id, else first probe on the bus
        pinned = f"/sys/bus/w1/devices/{CELL_DS18B20_ID}/w1_slave"
        sensors = [pinned] if os.path.exists(pinned) else \
                  glob.glob("/sys/bus/w1/devices/28-*/w1_slave")
    if not sensors:
        return None
    for attempt in range(retries):
        try:
            with open(sensors[0]) as fh:
                raw = fh.read()
            if "YES" not in raw:          # kernel CRC failed
                time.sleep(0.2)
                continue
            m = _re2.search(r"t=(-?\d+)", raw)
            if not m:
                time.sleep(0.2)
                continue
            milli = int(m.group(1))
            if milli == 0 or milli == 85000:   # glitch sentinels
                time.sleep(0.2)
                continue
            c = round(milli / 1000.0, 1)
            if not (_TEMP_MIN_C <= c <= _TEMP_MAX_C):
                time.sleep(0.2)
                continue
            return c
        except Exception:
            time.sleep(0.2)
    return None


def _temp_poller():
    """Background thread: poll DS18B20 every 5 s, with spike rejection."""
    last_good = None
    suspect = None          # candidate that broke the step limit, pending confirm
    while True:
        c = read_ds18b20()
        if c is not None:
            # Spike rejection: a single sample that jumps more than the thermal
            # mass allows is dropped, UNLESS the next reading confirms it (a real
            # fast change shows up on two consecutive samples; a glitch doesn't).
            if last_good is not None and abs(c - last_good) > _TEMP_MAX_STEP_C:
                if suspect is not None and abs(c - suspect) <= _TEMP_MAX_STEP_C:
                    pass            # two agreeing outliers → real, accept
                else:
                    suspect = c     # hold this one back, wait for confirmation
                    time.sleep(5)
                    continue
            suspect = None
            last_good = c
            with _lock:
                _temp_history.append({"ts": time.time(), "c": c})
                if len(_temp_history) > _TEMP_MAX:
                    _temp_history.pop(0)
        time.sleep(5)


threading.Thread(target=_temp_poller, daemon=True).start()

# ── GPS reader (direct serial, cooperative with lora_tx) ─────────────────────
# Matek SAM-M10Q (u-blox SAM-M10Q) on UART5/ttyAMA5, GPIO13 (pin 33), 9600 baud.
# Baud is auto-detected (9600 → 115200 fallback) by sniffing for valid NMEA.
# When lora_tx is running it reads /tmp/gps_fix.json which this thread writes.

import json as _json
_gps_state      = {}
_gps_lock       = threading.Lock()
_LORA_LOCK_FILE = Path('/tmp/stratopi_serial_lock')
_GPS_FIX_FILE   = Path('/tmp/gps_fix.json')


def _patch_gps_fix_file(**extras):
    """Merge telemetry extras into gps_fix.json for lora_tx."""
    try:
        d = _json.loads(_GPS_FIX_FILE.read_text()) if _GPS_FIX_FILE.exists() else {}
        for k, v in extras.items():
            if v is not None:
                d[k] = v
        d['ts'] = time.time()
        _GPS_FIX_FILE.write_text(_json.dumps(d))
    except Exception:
        pass


_GPS_PORT  = '/dev/ttyAMA5'           # UART5, GPIO13 (pin 33) RX
_GPS_BAUDS = (9600, 115200)           # Matek SAM-M10Q default 9600; old clone was 115200
_NMEA_KEYS = ('GGA', 'RMC', 'GLL', 'GSV', 'GSA', 'VTG', 'GNS', 'TXT')


def _auto_open_gps():
    """
    Open /dev/ttyAMA5 (UART5, GPIO13 pin 33) for GPS, auto-detecting the baud
    rate by sniffing for valid NMEA. The Matek SAM-M10Q (genuine u-blox) defaults
    to 9600; the old module used 115200. Returns an open serial.Serial at the
    working baud, or None.
    """
    try:
        import serial as _ser
    except ImportError:
        return None
    for baud in _GPS_BAUDS:
        s = None
        try:
            s = _ser.Serial(_GPS_PORT, baud, timeout=1)
            time.sleep(0.2)
            s.reset_input_buffer()
            t0 = time.time()
            while time.time() - t0 < 2.5:        # sniff a couple seconds for real NMEA
                line = s.readline().decode('ascii', errors='replace').strip()
                if line.startswith('$') and ',' in line and line[3:6] in _NMEA_KEYS:
                    s.timeout = 2
                    return s
            s.close()
        except Exception:
            try:
                if s: s.close()
            except Exception:
                pass
    return None


def _gps_reader():
    """Background thread: read GPS from /dev/ttyAMA5 (UART5 on GPIO13, pin 33).
    GPS is on a DEDICATED UART — no conflict with LoRa on serial0/ttyAMA0.
    Runs continuously regardless of lora_tx state; keeps gps_fix.json always fresh.
    """
    try:
        import pynmea2 as _nm
    except ImportError:
        return
    ser = None
    while True:
        if ser is None:
            ser = _auto_open_gps()
            if ser is None:
                time.sleep(5)
                continue

        try:
            line = ser.readline().decode('ascii', errors='replace').strip()
            if not line.startswith('$'):
                continue
            msg = _nm.parse(line)
            if msg.sentence_type == 'GGA':
                ts = msg.timestamp
                fix = {
                    'time':   ts.strftime('%H:%M:%S') if ts else datetime.now(timezone.utc).strftime('%H:%M:%S'),
                    'lat':    round(float(msg.latitude  or 0), 6),
                    'lon':    round(float(msg.longitude or 0), 6),
                    'alt':    round(float(msg.altitude  or 0), 1),
                    'sats':   int(msg.num_sats or 0),
                    'hdop':   float(msg.horizontal_dil or 99.9),
                    'fix':    int(msg.gps_qual or 0),
                    'source': 'gps',
                    'ts':     time.time(),
                }
                with _gps_lock:
                    _gps_state.update(fix)
                try:
                    with _lock:
                        _hist = list(_temp_history)
                    if _hist:
                        fix['cell_temp'] = _hist[-1]['c']
                    # Keep heater/cell extras written by _patch_gps_fix_file
                    if _GPS_FIX_FILE.exists():
                        prev = _json.loads(_GPS_FIX_FILE.read_text())
                        if 'cell_temp' not in fix and prev.get('cell_temp') is not None:
                            fix['cell_temp'] = prev['cell_temp']
                        if prev.get('heater_duty') is not None:
                            fix['heater_duty'] = prev['heater_duty']
                    fix['ts'] = time.time()
                    _GPS_FIX_FILE.write_text(_json.dumps(fix))
                except Exception:
                    pass
            elif msg.sentence_type in ('RMC', 'VTG'):
                upd = {}
                spd = getattr(msg, 'spd_over_grnd_kmph', None)
                if spd:
                    upd['speed'] = round(float(spd), 1)
                else:
                    spd2 = getattr(msg, 'spd_over_grnd', None)
                    if spd2: upd['speed'] = round(float(spd2) * 1.852, 1)
                trk = getattr(msg, 'true_track', None)
                if trk: upd['heading'] = round(float(trk), 1)
                if upd:
                    with _gps_lock:
                        _gps_state.update(upd)
        except Exception:
            try: ser.close()
            except Exception: pass
            ser = None
            time.sleep(1)


# Clean up stale lock file left by crashed lora_tx (SIGKILL etc.)
_lora_svc = subprocess.run(
    ['systemctl','is-active','stratopi_lora.service'],
    capture_output=True, text=True
).stdout.strip()
if _lora_svc != 'active' and _LORA_LOCK_FILE.exists():
    try: _LORA_LOCK_FILE.unlink()
    except Exception: pass

threading.Thread(target=_gps_reader, daemon=True).start()


# ── Mission GPS logger ────────────────────────────────────────────────────────

_MISSION_LOG_DIR = Path("/home/louis/stratopi/logs")
_MISSION_LOG_DIR.mkdir(exist_ok=True)

_mission = {
    'active':  False,
    'file':    None,
    'path':    None,
    'count':   0,
    'start_t': 0.0,
}
_mission_lock = threading.Lock()


def _mission_log_row(h_duty=0.0, cell_temp=None):
    """Write one telemetry row — called from heater loop every second."""
    with _mission_lock:
        if not _mission['active'] or _mission['file'] is None:
            return
    with _gps_lock:
        g = dict(_gps_state)
    alt = float(g.get('alt', 0) or 0)
    fix = int(g.get('fix', 0) or 0)
    now = time.time()
    vspeed = _calc_mission_vspeed(alt, now)
    with _gps_lock:
        _gps_state['vspeed'] = round(vspeed, 2)
    with _flight_lock:
        fstate = _flight_state.get('state', 'unknown')
    row = [
        datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
        g.get('lat', ''), g.get('lon', ''), alt,
        g.get('speed', ''), g.get('heading', ''), round(vspeed, 2),
        g.get('sats', ''), g.get('hdop', ''), fix,
        '' if cell_temp is None else round(cell_temp, 2),
        round(h_duty, 1),
        fstate,
    ]
    with _mission_lock:
        if _mission['file']:
            _mission['file'].write(','.join(str(x) for x in row) + '\n')
            _mission['file'].flush()
            _mission['count'] += 1
    with _lock:
        recording = state['recording']
        mode = state['mode']
    if recording and mode == 'mission':
        _check_landing_auto_stop(alt, vspeed, fix)
        _check_checkpoint_photos(alt)


def _mission_start(tag='mission'):
    ts = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
    p = _MISSION_LOG_DIR / f"{tag}_{ts}.csv"
    f = open(p, 'w', buffering=1)
    hdr = 'utc,lat,lon,alt_m,speed_kmh,heading_deg,vspeed_ms,sats,hdop,fix,cell_temp_c,heater_duty_pct,flight_state\n'
    f.write(hdr)
    _reset_mission_gps_state()
    with _mission_lock:
        _mission.update({'active': True, 'file': f, 'path': p, 'count': 0, 'start_t': time.time()})


def _mission_stop():
    with _mission_lock:
        _mission['active'] = False
        if _mission['file']:
            _mission['file'].close()
            _mission['file'] = None


# ── Mission GPS helpers (landing auto-stop + checkpoint stills) ───────────────

_LAND_CONFIRM_SAMPLES = 30   # ~30 mission-log rows at 1 Hz before auto-stop
_CHECKPOINT_TAIL_BYTES = 512 * 1024

_mission_gps = {
    'last_alt': None,
    'last_t':   None,
    'max_alt':  0.0,
    'land_samples': 0,
}
_land_auto_stop_armed = False
_checkpoint_tracker = None
_checkpoint_tracker_lock = threading.Lock()


class MilestoneTracker:
    """Fire once when altitude crosses configured milestones (ascending)."""

    def __init__(self):
        self._last_alt = None

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

    def reset(self):
        self._last_alt = None


def _reset_mission_gps_state():
    global _land_auto_stop_armed
    _mission_gps.update({
        'last_alt': None,
        'last_t': None,
        'max_alt': 0.0,
        'land_samples': 0,
    })
    _land_auto_stop_armed = False
    with _checkpoint_tracker_lock:
        global _checkpoint_tracker
        if _checkpoint_tracker is None:
            _checkpoint_tracker = MilestoneTracker()
        else:
            _checkpoint_tracker.reset()


def _calc_mission_vspeed(alt: float, now: float) -> float:
    g = _mission_gps
    vs = 0.0
    if g['last_alt'] is not None and g['last_t'] is not None:
        dt = now - g['last_t']
        if dt > 0.05:
            vs = (alt - g['last_alt']) / dt
    g['last_alt'] = alt
    g['last_t'] = now
    g['max_alt'] = max(g['max_alt'], alt)
    return vs


def _check_landing_auto_stop(alt: float, vspeed: float, fix: int):
    """After enough low, stable-altitude GPS rows, stop mission recording."""
    global _land_auto_stop_armed
    if _land_auto_stop_armed:
        return
    with _lock:
        if not state['recording'] or state['mode'] != 'mission':
            return
    if fix < 1:
        _mission_gps['land_samples'] = 0
        return
    with _flight_lock:
        launch_alt = float(_flight_state.get('launch_alt') or alt)
    if _mission_gps['max_alt'] <= launch_alt + 150:
        _mission_gps['land_samples'] = 0
        return
    if alt <= launch_alt + _LAND_ALT_M and abs(vspeed) <= _LAND_VS_MAX:
        _mission_gps['land_samples'] += 1
    else:
        _mission_gps['land_samples'] = 0
    if _mission_gps['land_samples'] < _LAND_CONFIRM_SAMPLES:
        return
    _land_auto_stop_armed = True
    print(f"[landing] Auto-stopping cameras after {_LAND_CONFIRM_SAMPLES} "
          f"stable low-alt GPS samples (alt={alt:.0f}m, v={vspeed:+.2f}m/s)")
    threading.Thread(target=stop_recording, daemon=True).start()


def _check_checkpoint_photos(alt: float):
    """Capture stills when synced milestone altitudes are crossed during recording."""
    with _lock:
        if not state['recording']:
            return
    with _checkpoint_tracker_lock:
        tracker = _checkpoint_tracker
        if tracker is None:
            return
    cfg = _load_lora_diag()
    photo_cfg = {
        "milestones_m": cfg.get("milestones_m", []),
        "triggered_m": cfg.get("photo_triggered_m", []),
    }
    milestone = tracker.check(alt, photo_cfg)
    if milestone is None:
        return
    triggered = sorted({int(m) for m in cfg.get("photo_triggered_m", [])} | {int(milestone)})
    cfg["photo_triggered_m"] = triggered
    _save_lora_diag(cfg)
    threading.Thread(
        target=_capture_checkpoint_photos,
        args=(milestone,),
        daemon=True,
    ).start()


def _ffmpeg_last_frame(src, out, *, fmt=None):
    """Best-effort last frame from a growing capture file."""
    p = Path(src)
    if not p.exists() or p.stat().st_size < 4096:
        return False
    tail_path = p.with_name(f".{p.stem}_tail{p.suffix}")
    try:
        size = p.stat().st_size
        with open(p, "rb") as f:
            f.seek(max(0, size - _CHECKPOINT_TAIL_BYTES))
            tail_path.write_bytes(f.read())
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
        if fmt:
            cmd += ["-f", fmt]
        elif p.suffix == ".h264":
            cmd += ["-f", "h264"]
        elif p.suffix == ".mjpeg":
            cmd += ["-f", "mjpeg"]
        cmd += ["-i", str(tail_path), "-frames:v", "1", "-q:v", "2", str(out)]
        r = subprocess.run(cmd, capture_output=True, timeout=30)
        return (
            r.returncode == 0
            and Path(out).exists()
            and Path(out).stat().st_size > 0
        )
    except Exception as e:
        print(f"[checkpoint] frame grab failed {p.name}: {e}")
        return False
    finally:
        try:
            tail_path.unlink(missing_ok=True)
        except Exception:
            pass


def _capture_usb_still(out_jpg, usb_dev):
    if not usb_dev:
        return False
    s = state["settings"]["usb_cam"]
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "v4l2", "-input_format", "mjpeg",
        "-video_size", f"{s['width']}x{s['height']}",
        "-i", usb_dev,
        "-frames:v", "1", "-q:v", "2", str(out_jpg),
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=15)
        return r.returncode == 0 and Path(out_jpg).exists()
    except Exception:
        return False


def _capture_checkpoint_photos(milestone_m: int):
    with _lock:
        prefix = _rec_prefix
        hq_out = _rec_hq_out
        usb_out = _rec_usb_out
        if not state["recording"] or not prefix:
            return
    base = f"{prefix}_checkpoint_{int(milestone_m)}m"
    ok_hq = ok_usb = False
    if hq_out:
        ok_hq = _ffmpeg_last_frame(hq_out, CAPTURES_DIR / f"{base}_hq.jpg")
    usb_jpg = CAPTURES_DIR / f"{base}_usb.jpg"
    usb_dev = find_usb_cam()
    if usb_dev:
        ok_usb = _capture_usb_still(usb_jpg, usb_dev)
    if not ok_usb and usb_out:
        ok_usb = _ffmpeg_last_frame(usb_out, usb_jpg)
    print(f"[checkpoint] {milestone_m}m — HQ={'ok' if ok_hq else 'skip'} "
          f"USB={'ok' if ok_usb else 'skip'}")


# ── Flight state machine ──────────────────────────────────────────────────────

_flight_lock  = threading.Lock()
_flight_state = {
    'state':       'ground',
    'launch_alt':  None,
    'launch_set':  False,
    '_asc_since':  None,
    '_land_since': None,
    '_wifi_off':   False,
}

_ASCENT_MS   =  1.0   # m/s  — positive vspeed to consider ascending
_DESCENT_MS  = -1.0   # m/s  — negative vspeed to consider descending
_LAND_VS_MAX =  0.5   # m/s  abs — max vspeed for landing
_LAND_ALT_M  =  200   # m    above launch for landing detection
_ASC_CONFIRM =  15    # s    of continuous ascent before state change


def _wifi_off():
    try:
        subprocess.run(['ip', 'link', 'set', 'wlan0', 'down'], timeout=5, capture_output=True)
        subprocess.run(['systemctl', 'stop', 'hostapd'], timeout=5, capture_output=True)
        subprocess.run(['tvservice', '-o'], timeout=3, capture_output=True)
    except Exception:
        pass
    with _flight_lock:
        _flight_state['_wifi_off'] = True


def _wifi_on():
    try:
        subprocess.run(['ip', 'link', 'set', 'wlan0', 'up'], timeout=5, capture_output=True)
        time.sleep(2)
        subprocess.run(['systemctl', 'start', 'hostapd'], timeout=5, capture_output=True)
        subprocess.run(['tvservice', '-p'], timeout=3, capture_output=True)
    except Exception:
        pass
    with _flight_lock:
        _flight_state['_wifi_off'] = False


def _flight_monitor():
    while True:
        time.sleep(5)
        try:
            with _gps_lock:
                g = dict(_gps_state)
            if not g or g.get('fix', 0) < 1:
                continue
            alt    = float(g.get('alt', 0) or 0)
            vspeed = float(g.get('vspeed', 0) or 0)
            now    = time.time()

            with _flight_lock:
                fs = _flight_state
                st = fs['state']

                if not fs['launch_set'] and st == 'ground':
                    fs['launch_alt'] = alt
                    fs['launch_set'] = True

                la = fs['launch_alt'] or 0

                if st == 'ground':
                    if vspeed >= _ASCENT_MS:
                        if fs['_asc_since'] is None:
                            fs['_asc_since'] = now
                        elif now - fs['_asc_since'] >= _ASC_CONFIRM and alt > la + 50:
                            fs['state'] = 'ascending'
                            fs['_asc_since'] = None
                            threading.Thread(target=_wifi_off, daemon=True).start()
                    else:
                        fs['_asc_since'] = None

                elif st == 'ascending':
                    if vspeed <= _DESCENT_MS:
                        fs['state'] = 'descending'

                elif st == 'descending':
                    if alt <= la + _LAND_ALT_M and abs(vspeed) <= _LAND_VS_MAX:
                        if fs['_land_since'] is None:
                            fs['_land_since'] = now
                        elif now - fs['_land_since'] >= 30:
                            fs['state'] = 'landed'
                            if fs['_wifi_off']:
                                threading.Thread(target=_wifi_on, daemon=True).start()
                    else:
                        fs['_land_since'] = None
        except Exception:
            pass


threading.Thread(target=_flight_monitor, daemon=True).start()


# ── Heater PID (IRFZ44N on GPIO18) ───────────────────────────────────────────

_HEATER_PIN      = 18    # BCM — GPIO18 pin 12 → 100Ω → IRFZ44N gate
_HEATER_PWM_FREQ = 100   # Hz  — fine for thermal mass
_HEATER_MAX_A    = 4.0   # A at 100 % PWM; avg current scales linearly with duty
_HEATER_MAH_CAP  = 5000.0 # 5 Ah flight budget — heater auto-stops when reached

_heater = {
    'enabled':   False,
    'duty':      0.0,
    'setpoint':  10.0,    # °C
    'temp':      None,
    'kp':        10.0,
    'ki':        0.05,
    'kd':        2.0,
    '_integral': 0.0,
    '_last_err': 0.0,
    '_last_t':   0.0,
    'autotuning':  False,
    '_tuner':      None,
    'last_tune':   None,
    'used_mah':       0.0,   # cumulative charge drawn by the heater (mAh)
    '_last_charge_t': 0.0,   # timestamp of last charge integration step
    'mah_cap_hit':    False,  # set when used_mah reaches _HEATER_MAH_CAP
}
_heater_lock = threading.Lock()
_heater_pwm  = None

# Persist heater settings across service restarts (Restart=always would
# otherwise silently reset the setpoint to the 10.0 default on any crash).
_HEATER_SETTINGS_FILE = Path("/home/louis/stratopi/heater_settings.json")

def _heater_save_settings():
    import json as _json
    try:
        with _heater_lock:
            data = {k: _heater[k] for k in
                    ('setpoint', 'kp', 'ki', 'kd', 'enabled', 'used_mah')}
        _HEATER_SETTINGS_FILE.write_text(_json.dumps(data))
    except Exception:
        pass

def _heater_load_settings():
    import json as _json
    try:
        data = _json.loads(_HEATER_SETTINGS_FILE.read_text())
        with _heater_lock:
            for k in ('setpoint', 'kp', 'ki', 'kd', 'enabled', 'used_mah'):
                if k in data:
                    _heater[k] = data[k]
    except Exception:
        pass

_heater_load_settings()


def _heater_enforce_mah_cap(*, log: bool = False) -> bool:
    """Disable heater when the 5 Ah budget is exhausted. Returns True if capped."""
    with _heater_lock:
        if _heater['used_mah'] < _HEATER_MAH_CAP:
            return False
        newly = not _heater.get('mah_cap_hit', False)
        _heater['mah_cap_hit'] = True
        _heater['enabled'] = False
        _heater['autotuning'] = False
        _heater['_tuner'] = None
        _heater['duty'] = 0.0
        _heater['_integral'] = 0.0
        used = _heater['used_mah']
    if log and newly:
        print(f"[heater] 5 Ah budget exhausted ({used:.0f} mAh) — heater disabled")
    return True


_heater_enforce_mah_cap()


class _RelayAutoTuner:
    """
    Relay-feedback auto-tuner (Åström-Hägglund).
    Applies bang-bang control ±HYSTERESIS around setpoint,
    measures oscillation period Pu and amplitude a, then
    computes Ziegler-Nichols PID parameters.
    Needs ~4 sign crossings (2 full cycles); at 1 s/sample
    this takes as long as the thermal time constant allows.
    """
    HYSTERESIS  = 0.3   # °C
    MIN_CROSSES = 4

    def __init__(self, setpoint):
        self.setpoint  = setpoint
        self._heating  = True
        self._crosses  = []       # timestamps of crossings
        self._samples  = []       # (time, temp)
        self.result    = None

    @property
    def crosses(self):
        return len(self._crosses)

    def update(self, temp):
        now = time.time()
        self._samples.append((now, temp))
        self._samples = [(t, v) for t, v in self._samples if t >= now - 3600]

        if self._heating:
            duty = 100.0
            if temp > self.setpoint + self.HYSTERESIS:
                self._heating = False
                self._crosses.append(now)
        else:
            duty = 0.0
            if temp < self.setpoint - self.HYSTERESIS:
                self._heating = True
                self._crosses.append(now)

        if len(self._crosses) >= self.MIN_CROSSES and self.result is None:
            self._compute()
        return duty

    def _compute(self):
        ct = self._crosses[-self.MIN_CROSSES:]
        half_periods = [ct[i+1] - ct[i] for i in range(len(ct)-1)]
        Pu = 2.0 * sum(half_periods) / len(half_periods)

        t0 = ct[0]
        temps = [v for t, v in self._samples if t >= t0]
        if len(temps) < 4:
            return
        a = (max(temps) - min(temps)) / 2.0
        if a < 0.05 or Pu < 5:
            return

        Ku = 4.0 * 100.0 / (3.14159265 * a)   # [%/°C]
        self.result = {
            'kp':       round(0.600 * Ku,       3),
            'ki':       round(1.200 * Ku / Pu,  5),
            'kd':       round(0.075 * Ku * Pu,  3),
            'Ku':       round(Ku, 3),
            'Pu_s':     round(Pu, 1),
            'amplitude': round(a, 3),
        }


def _heater_init():
    global _heater_pwm
    try:
        import RPi.GPIO as _GPIO
        _GPIO.setmode(_GPIO.BCM)
        _GPIO.setwarnings(False)
        _GPIO.setup(_HEATER_PIN, _GPIO.OUT)
        _heater_pwm = _GPIO.PWM(_HEATER_PIN, _HEATER_PWM_FREQ)
        _heater_pwm.start(0)
    except Exception as e:
        pass


def _heater_loop():
    global _heater_pwm
    _heater_init()
    while True:
        time.sleep(1.0)
        try:
            with _lock:
                hist = list(_temp_history)
            temp = hist[-1]['c'] if hist else None

            tune_done = False
            cap_logged = False
            with _heater_lock:
                _heater['temp'] = temp
                if _heater.get('mah_cap_hit') or _heater['used_mah'] >= _HEATER_MAH_CAP:
                    duty = 0.0
                    if _heater['used_mah'] >= _HEATER_MAH_CAP:
                        newly = not _heater.get('mah_cap_hit', False)
                        _heater['mah_cap_hit'] = True
                        _heater['enabled'] = False
                        _heater['autotuning'] = False
                        _heater['_tuner'] = None
                        _heater['_integral'] = 0.0
                        cap_logged = newly
                elif not _heater['enabled'] or temp is None:
                    duty = 0.0
                elif _heater['autotuning']:
                    if _heater['_tuner'] is None:
                        _heater['_tuner'] = _RelayAutoTuner(_heater['setpoint'])
                    tuner = _heater['_tuner']
                    duty  = tuner.update(temp)
                    if tuner.result is not None:
                        r = tuner.result
                        _heater.update({'kp': r['kp'], 'ki': r['ki'], 'kd': r['kd'],
                                        'last_tune': r, 'autotuning': False,
                                        '_tuner': None, '_integral': 0.0, '_last_err': 0.0})
                        tune_done = True
                else:
                    now = time.time()
                    dt  = max(0.1, min(now - _heater['_last_t'], 10.0)) if _heater['_last_t'] else 1.0
                    _heater['_last_t'] = now
                    err = _heater['setpoint'] - temp
                    max_i = 100.0 / max(_heater['ki'], 1e-6)
                    _heater['_integral'] = max(-max_i, min(max_i, _heater['_integral'] + err * dt))
                    d_err = (err - _heater['_last_err']) / dt
                    _heater['_last_err'] = err
                    duty = max(0.0, min(100.0,
                        _heater['kp'] * err +
                        _heater['ki'] * _heater['_integral'] +
                        _heater['kd'] * d_err))
                _heater['duty'] = duty

                # Integrate consumed charge: avg current = (duty/100)*MAX_A, and
                # mAh += I_mA * (dt_seconds / 3600). dt is capped so a paused loop
                # (debugger, sleep drift) can't inject a huge bogus jump.
                t_now = time.time()
                if _heater['_last_charge_t']:
                    dt_c = min(t_now - _heater['_last_charge_t'], 10.0)
                    i_ma = (duty / 100.0) * _HEATER_MAX_A * 1000.0
                    _heater['used_mah'] += i_ma * (dt_c / 3600.0)
                _heater['_last_charge_t'] = t_now

                if _heater['used_mah'] >= _HEATER_MAH_CAP:
                    newly = not _heater.get('mah_cap_hit', False)
                    _heater['mah_cap_hit'] = True
                    _heater['enabled'] = False
                    _heater['autotuning'] = False
                    _heater['_tuner'] = None
                    _heater['duty'] = 0.0
                    _heater['_integral'] = 0.0
                    duty = 0.0
                    cap_logged = cap_logged or newly

            if cap_logged:
                with _heater_lock:
                    used = _heater['used_mah']
                print(f"[heater] 5 Ah budget exhausted ({used:.0f} mAh) — heater disabled")
            if _heater_pwm is not None:
                _heater_pwm.ChangeDutyCycle(duty)
            if tune_done or cap_logged:
                _heater_save_settings()
            # persist the running mAh total every ~30 s so a restart doesn't lose it
            if int(t_now) % 30 == 0:
                _heater_save_settings()
            _patch_gps_fix_file(cell_temp=temp, heater_duty=duty)
            _mission_log_row(duty, temp)
        except Exception:
            pass


threading.Thread(target=_heater_loop, daemon=True).start()


def _is_usb_capture(dev):
    """True only for a genuine USB/UVC *video-capture* node — never the Pi's
    CSI (unicam), codec, ISP or mem2mem nodes."""
    try:
        r = subprocess.run(["v4l2-ctl", "--device", dev, "--info"],
                            capture_output=True, text=True, timeout=2)
        info = (r.stdout + r.stderr).lower()
    except Exception:
        return False
    # Must be on the USB bus / UVC driver and NOT an internal platform device.
    if not ("usb" in info or "uvc" in info):
        return False
    if any(x in info for x in ("unicam", "codec", "isp", "pisp", "platform:")):
        return False
    # Must actually expose a video-capture format (the 2nd UVC node is metadata).
    try:
        r2 = subprocess.run(["v4l2-ctl", "--device", dev, "--list-formats"],
                            capture_output=True, text=True, timeout=2)
        fmts = (r2.stdout + r2.stderr).lower()
        return "video capture" in fmts or "mjpg" in fmts or "yuyv" in fmts
    except Exception:
        return False


def find_usb_cam():
    """Return the first genuine USB (UVC) capture device, or None.

    On this Pi /dev/video0..23 are the internal CSI/codec/ISP nodes, so we never
    fall back to those. A real USB webcam shows up under /dev/v4l/by-path/*usb*
    (and as a higher /dev/videoN); we verify it's a USB capture node before use.
    """
    # Preferred: the by-path symlink whose name contains 'usb'.
    for p in sorted(glob.glob("/dev/v4l/by-path/*usb*")):
        dev = os.path.realpath(p)
        if _is_usb_capture(dev):
            return dev
    # Fallback: probe every video node, accept only verified USB capture devices.
    for dev in sorted(glob.glob("/dev/video*")):
        if _is_usb_capture(dev):
            return dev
    return None


def hq_cam_available():
    for tool in ("rpicam-hello", "libcamera-hello"):
        try:
            r = subprocess.run(
                [tool, "--list-cameras"],
                capture_output=True, text=True, timeout=5,
            )
            out = r.stdout + r.stderr
            if "no cameras" in out.lower():
                continue
            if "imx477" in out.lower() or re.search(r"\[\d+\]", out):
                return True
        except Exception:
            pass
    return False


def start_recording(mode, *, skip_mission_start: bool = False):
    global hq_process, usb_process, _last_stop_time
    global _rec_hq_cmd, _rec_usb_cmd, _rec_hq_out, _rec_usb_out
    global _rec_log_dir, _rec_prefix, _rec_restart, _rec_hq_files, _rec_hq_fps

    # Kill any stale rpicam-vid (e.g. crashed live-stream session) and let the
    # camera hardware reset — without this the V4L2 ISP node stays busy and the
    # next rpicam-vid call fails with "Failed to queue buffer: Invalid argument".
    subprocess.run(["pkill", "-SIGINT", "rpicam-vid"], capture_output=True)
    cooldown = 2.0 - (time.time() - _last_stop_time)
    time.sleep(max(2.0, cooldown))

    s = state["settings"]
    hq = s["hq_cam"]
    usb = s["usb_cam"]
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    prefix = f"{mode}_{ts}"

    usb_out = str(CAPTURES_DIR / f"{prefix}_usb.mp4")

    # Pi 4 hardware H264 encoder tops out at 1920x1080.
    # Use MJPEG for higher resolutions (no encoder size limit).
    use_h264 = hq["width"] <= 1920 and hq["height"] <= 1080
    if use_h264:
        hq_codec, hq_out = "h264", str(CAPTURES_DIR / f"{prefix}_hq.h264")
    else:
        hq_codec, hq_out = "mjpeg", str(CAPTURES_DIR / f"{prefix}_hq.mjpeg")

    hq_cmd = [
        "rpicam-vid",
        "--width", str(hq["width"]),
        "--height", str(hq["height"]),
        "--framerate", str(hq["fps"]),
        "--codec", hq_codec,
        "--output", hq_out,
        "--nopreview",
        "-t", "0",
        "--awb", "auto",
        "--exposure", "normal",
        "--metering", "matrix",
    ]

    usb_dev = find_usb_cam()
    usb_cmd = None
    if not usb_dev:
        print("[USB cam] No USB webcam detected — recording HQ camera only.")
    if usb_dev:
        dfr_val = "0" if usb.get("prioritize_fps", True) else "1"
        subprocess.run(
            ["v4l2-ctl", "--device", usb_dev,
             "--set-ctrl", f"exposure_dynamic_framerate={dfr_val}"],
            capture_output=True, timeout=3,
        )
        usb_cmd = [
            "ffmpeg", "-y",
            "-f", "v4l2",
            "-input_format", "mjpeg",
            "-framerate", str(usb["fps"]),
            "-video_size", f"{usb['width']}x{usb['height']}",
            "-i", usb_dev,
            "-c:v", "copy",
            usb_out,
        ]

    log_dir = CAPTURES_DIR / "logs"
    log_dir.mkdir(exist_ok=True)
    hq_log = open(log_dir / f"{prefix}_hq.log", "w")
    usb_log = open(log_dir / f"{prefix}_usb.log", "w")

    if mode == "mission" and not skip_mission_start:
        _mission_start("mission")
    elif mode == "mission":
        _reset_mission_gps_state()

    with _lock:
        # Try to start HQ cam; retry once on immediate crash (V4L2 timeout)
        for attempt in range(2):
            try:
                hq_process = subprocess.Popen(hq_cmd, stdout=hq_log, stderr=hq_log)
            except Exception as e:
                hq_process = None
                hq_log.write(f"[Attempt {attempt + 1}] Failed to start: {e}\n")
                print(f"[HQ cam] Attempt {attempt + 1} failed: {e}")
                if attempt == 0:
                    time.sleep(3)
                    continue
                break
            time.sleep(2.0)  # allow libcamera pipeline to fully initialize
            if hq_process.poll() is not None:
                hq_log.write(
                    f"[Attempt {attempt + 1}] Crashed on startup (rc={hq_process.returncode})\n"
                )
                print(f"[HQ cam] Attempt {attempt + 1} crashed immediately, "
                      + ("retrying…" if attempt == 0 else "giving up."))
                hq_process = None
                if attempt == 0:
                    time.sleep(3)
                    continue
            break  # started successfully (or exhausted retries)

        if usb_cmd:
            try:
                usb_process = subprocess.Popen(usb_cmd, stdout=usb_log, stderr=usb_log)
            except Exception as e:
                usb_process = None
                usb_log.write(f"Failed to start: {e}\n")
                print(f"[USB cam] Failed to start: {e}")

        # Arm the watchdog: remember how to respawn each camera into a new segment.
        _rec_hq_cmd  = hq_cmd
        _rec_usb_cmd = usb_cmd
        _rec_hq_out  = hq_out
        _rec_usb_out = usb_out if usb_cmd else None
        _rec_log_dir = log_dir
        _rec_prefix  = prefix
        _rec_restart = {'hq': 0, 'usb': 0}
        _rec_hq_files = [hq_out]          # remuxed to .mp4 on stop
        _rec_hq_fps   = hq["fps"]

        state["recording"] = True
        state["mode"] = mode
        state["start_time"] = time.time()


def stop_recording(*, preserve_mission: bool = False):
    global hq_process, usb_process, _last_stop_time
    global _rec_hq_cmd, _rec_usb_cmd

    with _lock:
        # Disarm the watchdog FIRST so it doesn't respawn a camera we're stopping.
        _rec_hq_cmd = None
        _rec_usb_cmd = None
        state["recording"] = False

        for proc, name in [(hq_process, "HQ"), (usb_process, "USB")]:
            if proc is not None:
                try:
                    proc.send_signal(signal.SIGINT)
                    proc.wait(timeout=8)
                except Exception as e:
                    print(f"[{name}] Stop error: {e}")
                    try:
                        proc.kill()
                    except Exception:
                        pass

        hq_process = None
        usb_process = None
        state["mode"] = None
        state["start_time"] = None
        hq_files = list(_rec_hq_files)
        hq_fps   = _rec_hq_fps
    if not preserve_mission:
        _mission_stop()
    _last_stop_time = time.time()
    # Wrap the raw HQ stream(s) into playable .mp4 (USB is already .mp4 via ffmpeg).
    _remux_hq_outputs(hq_files, hq_fps)


def _recording_watchdog():
    """Respawn rpicam-vid / ffmpeg if it dies while we should be recording.
    Each respawn writes a NEW segment file (…_pN.ext) so already-captured footage
    is never overwritten. Stops trying after _MAX_REC_RESTART to avoid a storm on
    a genuinely broken device."""
    global hq_process, usb_process
    while True:
        time.sleep(4)
        try:
            with _lock:
                if not state["recording"]:
                    continue
                for key in ("hq", "usb"):
                    proc = hq_process if key == "hq" else usb_process
                    cmd  = _rec_hq_cmd if key == "hq" else _rec_usb_cmd
                    base = _rec_hq_out if key == "hq" else _rec_usb_out
                    if cmd is None or proc is None:
                        continue
                    if proc.poll() is None:
                        continue                      # still alive
                    # process died unexpectedly while recording
                    if _rec_restart[key] >= _MAX_REC_RESTART:
                        continue
                    _rec_restart[key] += 1
                    seg = _rec_restart[key]
                    newcmd = list(cmd)
                    # replace the output path with a fresh segment
                    try:
                        if key == "hq":
                            oi = newcmd.index("--output") + 1
                        else:
                            oi = len(newcmd) - 1       # ffmpeg output is the last arg
                        newcmd[oi] = _segment_path(base, seg)
                    except Exception:
                        pass
                    try:
                        logf = open(_rec_log_dir / f"{_rec_prefix}_{key}_p{seg}.log", "w")
                        np = subprocess.Popen(newcmd, stdout=logf, stderr=logf)
                        if key == "hq":
                            hq_process = np
                            _rec_hq_files.append(newcmd[oi])   # remux this segment too
                        else:
                            usb_process = np
                        print(f"[watchdog] {key.upper()} cam died (rc), respawned segment {seg}: "
                              f"{newcmd[oi]}")
                    except Exception as e:
                        print(f"[watchdog] {key.upper()} respawn failed: {e}")
        except Exception as e:
            print(f"[watchdog] loop error: {e}")


threading.Thread(target=_recording_watchdog, daemon=True).start()
# Recover raw camera files left by a crashed / power-lost session into playable .mp4.
threading.Thread(target=_remux_orphans, daemon=True).start()


def get_files():
    files = []
    for f in sorted(CAPTURES_DIR.iterdir(), reverse=True):
        if f.is_file():
            stat = f.stat()
            files.append({
                "name": f.name,
                "size": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            })
    return files


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    usb_dev = find_usb_cam()
    hq_avail = hq_cam_available()
    duration = None
    if state["start_time"]:
        duration = int(time.time() - state["start_time"])
    with _settings_restart_lock:
        restarting = _settings_restart_busy
    return jsonify({
        "recording": state["recording"],
        "streaming": state["streaming"],
        "mode": state["mode"],
        "duration": duration,
        "restarting_recording": restarting,
        "hq_cam_available": hq_avail,
        "usb_cam_device": usb_dev,
        "usb_cam_available": usb_dev is not None,
        "settings": state["settings"],
    })


_settings_restart_busy = False
_settings_restart_lock = threading.Lock()


def _patch_camera_settings(data: dict) -> bool:
    """Apply settings patch; return True if any value changed."""
    changed = False
    with _lock:
        if "hq_cam" in data:
            patch = {
                k: v for k, v in data["hq_cam"].items()
                if k in ("width", "height", "fps")
            }
            for key, val in patch.items():
                if state["settings"]["hq_cam"].get(key) != val:
                    changed = True
            state["settings"]["hq_cam"].update(patch)
        if "usb_cam" in data:
            patch = {
                k: v for k, v in data["usb_cam"].items()
                if k in ("width", "height", "fps", "prioritize_fps")
            }
            for key, val in patch.items():
                if state["settings"]["usb_cam"].get(key) != val:
                    changed = True
            state["settings"]["usb_cam"].update(patch)
    return changed


def _restart_recording_for_settings(mode: str, orig_start: float | None) -> None:
    """Stop capture (remux/save), then start a new segment at the new resolution."""
    global _settings_restart_busy
    try:
        print(f"[settings] Restarting {mode} capture with new camera settings…")
        stop_recording(preserve_mission=True)
        start_recording(mode, skip_mission_start=True)
        if orig_start is not None:
            with _lock:
                state["start_time"] = orig_start
        print("[settings] Capture restarted.")
    except Exception as e:
        print(f"[settings] Restart failed: {e}")
    finally:
        with _settings_restart_lock:
            _settings_restart_busy = False


@app.route("/api/settings", methods=["POST"])
def api_settings():
    global _settings_restart_busy
    if state["streaming"]:
        return jsonify({"error": "Stop live view before changing camera settings"}), 400
    with _settings_restart_lock:
        if _settings_restart_busy:
            return jsonify({"error": "Camera restart already in progress"}), 409

    data = request.json or {}
    with _lock:
        was_recording = state["recording"]
        mode = state["mode"]
        orig_start = state["start_time"]

    changed = _patch_camera_settings(data)
    if not changed:
        return jsonify({
            "ok": True,
            "settings": state["settings"],
            "restarted_recording": False,
        })

    if was_recording and mode:
        with _settings_restart_lock:
            _settings_restart_busy = True
        threading.Thread(
            target=_restart_recording_for_settings,
            args=(mode, orig_start),
            daemon=True,
        ).start()
        return jsonify({
            "ok": True,
            "settings": state["settings"],
            "restarting_recording": True,
            "mode": mode,
        })

    return jsonify({"ok": True, "settings": state["settings"], "restarted_recording": False})


@app.route("/api/start", methods=["POST"])
def api_start():
    if state["recording"]:
        return jsonify({"error": "Already recording"}), 400
    if state["streaming"]:
        return jsonify({"error": "Stop live view before recording"}), 400
    mode = (request.json or {}).get("mode", "test")
    threading.Thread(target=start_recording, args=(mode,), daemon=True).start()
    time.sleep(0.3)
    return jsonify({"ok": True, "mode": mode})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    if not state["recording"]:
        return jsonify({"error": "Not recording"}), 400
    threading.Thread(target=stop_recording, daemon=True).start()
    time.sleep(0.3)
    return jsonify({"ok": True})


@app.route("/stream")
def stream_view():
    """MJPEG live stream for focus assist. ?cam=hq (default) or ?cam=usb."""
    cam = request.args.get("cam", "hq")

    with _lock:
        if state["recording"]:
            return "Cannot stream while recording", 409
        if state["streaming"]:
            return "Stream already active — only one viewer at a time", 409
        state["streaming"] = True

    import queue as _queue

    if cam == "usb":
        usb_dev = find_usb_cam()
        if not usb_dev:
            with _lock:
                state["streaming"] = False
            return "USB webcam not found", 404
        cmd = [
            "ffmpeg",
            "-f", "v4l2",
            "-input_format", "mjpeg",
            "-framerate", "15",
            "-video_size", "1280x720",
            "-i", usb_dev,
            "-c:v", "copy",
            "-f", "mjpeg",
            "pipe:1",
        ]
    else:
        cmd = [
            "rpicam-vid",
            "--codec", "mjpeg",
            "--output", "-",
            "--nopreview",
            "-t", "0",
            "--width", "1280",
            "--height", "720",
            "--framerate", "30",
            "--awb", "auto",
            "--exposure", "normal",
            "--metering", "centre",
            "--sharpness", "1.5",
        ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    frame_q = _queue.Queue(maxsize=4)

    def _reader():
        """Background thread: parse JPEG frames from stdout."""
        buf = b""
        try:
            while proc.poll() is None:
                chunk = proc.stdout.read(32768)
                if not chunk:
                    break
                buf += chunk
                while True:
                    start = buf.find(b'\xff\xd8')
                    if start == -1:
                        buf = b""
                        break
                    end = buf.find(b'\xff\xd9', start + 2)
                    if end == -1:
                        buf = buf[start:] if start else buf
                        break
                    frame_q.put(buf[start:end + 2], timeout=1)
                    buf = buf[end + 2:]
        except Exception:
            pass
        finally:
            try:
                frame_q.put(None, timeout=1)
            except Exception:
                pass

    threading.Thread(target=_reader, daemon=True).start()

    def generate():
        try:
            while True:
                try:
                    frame = frame_q.get(timeout=5)
                except _queue.Empty:
                    break
                if frame is None:
                    break
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
                )
        except GeneratorExit:
            pass
        finally:
            try:
                proc.send_signal(signal.SIGINT)
                proc.wait(timeout=3)
            except Exception:
                proc.kill()
            with _lock:
                state["streaming"] = False

    return Response(
        generate(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
    )


@app.route("/api/files")
def api_files():
    return jsonify(get_files())


@app.route("/files/<path:filename>")
def download_file(filename):
    return send_from_directory(CAPTURES_DIR, filename, as_attachment=True)


@app.route("/api/delete/<path:filename>", methods=["DELETE"])
def delete_file(filename):
    filepath = CAPTURES_DIR / filename
    if not filepath.exists() or not filepath.is_file():
        abort(404)
    filepath.unlink()
    return jsonify({"ok": True})


@app.route("/api/disk")
def api_disk():
    usage = shutil.disk_usage(str(CAPTURES_DIR))
    return jsonify({
        "total": usage.total,
        "used": usage.used,
        "free": usage.free,
    })


@app.route("/api/gps")
def api_gps():
    with _gps_lock:
        d = dict(_gps_state)
    if d and d.get('ts'):
        age = time.time() - d.get('ts', 0)
        d['age_s'] = round(age, 1)
        d['stale'] = age > 15
        return jsonify(d)
    # Fallback: read fix file directly
    try:
        if _GPS_FIX_FILE.exists():
            fd = _json.loads(_GPS_FIX_FILE.read_text())
            age = time.time() - fd.get('ts', 0)
            fd['age_s'] = round(age, 1)
            fd['stale'] = age > 15
            return jsonify(fd)
    except Exception:
        pass
    return jsonify({'fix': 0, 'stale': True, 'source': 'none'})


@app.route("/api/temperature")
def api_temperature():
    with _lock:
        hist = list(_temp_history)
    current = hist[-1]["c"] if hist else None
    sensor_present = bool(glob.glob("/sys/bus/w1/devices/28-*"))
    return jsonify({
        "current": current,
        "sensor_found": sensor_present,
        "history": hist,
    })


@app.route("/api/mission")
def api_mission():
    with _mission_lock:
        m = dict(_mission)
    return jsonify({
        'active':   m['active'],
        'path':     str(m['path']) if m['path'] else None,
        'count':    m['count'],
        'elapsed':  round(time.time() - m['start_t'], 0) if m['active'] else 0,
    })


@app.route("/api/mission/start", methods=["POST"])
def api_mission_start_route():
    with _mission_lock:
        if _mission['active']:
            return jsonify({'error': 'Already logging'}), 400
    tag = (request.json or {}).get('tag', 'manual')
    _mission_start(tag)
    with _mission_lock:
        p = str(_mission['path'])
    return jsonify({'ok': True, 'path': p})


@app.route("/api/mission/stop", methods=["POST"])
def api_mission_stop_route():
    with _mission_lock:
        if not _mission['active']:
            return jsonify({'error': 'Not logging'}), 400
    _mission_stop()
    with _mission_lock:
        cnt = _mission['count']
    return jsonify({'ok': True, 'count': cnt})


@app.route("/api/flight")
def api_flight():
    with _flight_lock:
        fs = {k: v for k, v in _flight_state.items() if not k.startswith('_')}
    return jsonify(fs)


@app.route("/api/heater", methods=["GET"])
def api_heater_get():
    with _heater_lock:
        h = dict(_heater)
    tuner = h.get('_tuner')
    used = float(h.get('used_mah', 0.0) or 0.0)
    cap_hit = bool(h.get('mah_cap_hit')) or used >= _HEATER_MAH_CAP
    return jsonify({
        'enabled':           h['enabled'],
        'duty':              round(h['duty'], 1),
        'setpoint':          h['setpoint'],
        'temp':              h['temp'],
        'kp':                h['kp'],
        'ki':                h['ki'],
        'kd':                h['kd'],
        'autotuning':        h['autotuning'],
        'autotune_crosses':  tuner.crosses if tuner else 0,
        'last_tune':         h['last_tune'],
        'used_mah':          round(used, 1),
        'mah_cap_mah':       _HEATER_MAH_CAP,
        'mah_cap_hit':       cap_hit,
        'mah_remaining_mah': round(max(0.0, _HEATER_MAH_CAP - used), 1),
        'mah_cap_pct':       round(min(100.0, 100.0 * used / _HEATER_MAH_CAP), 1),
        'current_a':         round((h['duty'] / 100.0) * _HEATER_MAX_A, 2),
        'max_a':             _HEATER_MAX_A,
    })


@app.route("/api/heater", methods=["POST"])
def api_heater_post():
    data = request.json or {}
    with _heater_lock:
        if 'enabled' in data:
            want_on = bool(data['enabled'])
            if want_on and (
                _heater.get('mah_cap_hit')
                or _heater['used_mah'] >= _HEATER_MAH_CAP
            ):
                return jsonify({
                    'ok': False,
                    'error': 'mah_cap_exhausted',
                    'message': (
                        f"Heater budget exhausted ({_heater['used_mah']:.0f} / "
                        f"{_HEATER_MAH_CAP:.0f} mAh). Reset mAh before re-enabling."
                    ),
                    'used_mah': round(_heater['used_mah'], 1),
                    'mah_cap_mah': _HEATER_MAH_CAP,
                }), 409
            _heater['enabled'] = want_on
            if not _heater['enabled']:
                _heater['_integral'] = 0.0
        if 'setpoint' in data:
            _heater['setpoint'] = max(-20.0, min(40.0, float(data['setpoint'])))
        for k in ('kp', 'ki', 'kd'):
            if k in data:
                _heater[k] = max(0.0, float(data[k]))
        if data.get('autotune_start'):
            if _heater.get('mah_cap_hit') or _heater['used_mah'] >= _HEATER_MAH_CAP:
                return jsonify({
                    'ok': False,
                    'error': 'mah_cap_exhausted',
                    'message': 'Heater budget exhausted — reset mAh before auto-tune.',
                }), 409
            _heater['autotuning'] = True
            _heater['_tuner']     = None
            _heater['enabled']    = True
        if data.get('autotune_stop'):
            _heater['autotuning'] = False
            _heater['_tuner']     = None
        if data.get('reset_mah'):
            _heater['used_mah'] = 0.0          # zero the charge counter (pre-flight)
            _heater['mah_cap_hit'] = False
    _heater_save_settings()
    return jsonify({'ok': True})


# ── Environmental sensor suite (AHT21 / ENS160 / Plantower / DS18B20×2) ───────
# Ported from the Arduino logger. A background poller logs every curve to a
# structured CSV; we merge the GPS / heater / flight curves in here too so the
# single downloadable file holds *all* of our curves.
from collections import OrderedDict as _OD
import sensors as _sensors


def _sensor_extra_fields():
    """Extra CSV columns merged into every sensor row (all in one structured file)."""
    e = _OD()
    with _gps_lock:
        g = dict(_gps_state)
    e['GPS_lat']   = g.get('lat', '')
    e['GPS_lon']   = g.get('lon', '')
    e['GPS_alt']   = g.get('alt', '')
    e['GPS_sats']  = g.get('sats', '')
    e['GPS_fix']   = g.get('fix', '')
    e['GPS_speed'] = g.get('speed', '')
    with _heater_lock:
        h = dict(_heater)
    e['Heat_duty'] = round(h.get('duty', 0.0), 1)
    e['Heat_set']  = h.get('setpoint', '')
    e['Heat_A']    = round((h.get('duty', 0.0) / 100.0) * _HEATER_MAX_A, 2)
    e['Heat_mAh']  = round(h.get('used_mah', 0.0), 1)
    e['CellT']     = h.get('temp', '')
    with _flight_lock:
        fs = dict(_flight_state)
    e['Flight']    = fs.get('state', '')
    return e


_sensors.set_extra_provider(_sensor_extra_fields)
_sensors.start()


@app.route("/api/sensors")
def api_sensors():
    latest, hist = _sensors.snapshot()
    return jsonify({"latest": latest, "history": hist,
                    "rates_hz": _sensors.get_rates_hz()})


@app.route("/api/sensors/config", methods=["GET", "POST"])
def api_sensors_config():
    """GET / POST per-sensor poll/log rates IN HERTZ ({sensor: hz, ...})."""
    if request.method == "POST":
        updates = request.json or {}
        return jsonify({"ok": True, "rates_hz": _sensors.set_rates_hz(updates)})
    lo, hi = _sensors.rate_bounds_hz()
    return jsonify({"rates_hz": _sensors.get_rates_hz(),
                    "min_hz": lo, "max_hz": hi})


@app.route("/download/sensors.csv")
def download_sensors_csv():
    p = _sensors.csv_path()
    if not p.exists():
        abort(404)
    return send_from_directory(p.parent, p.name, as_attachment=True,
                               download_name="stratopi_sensors.csv")


@app.route("/api/sensors/reset", methods=["POST"])
def api_sensors_reset():
    return jsonify({"ok": _sensors.reset_log()})


# ── LoRa diagnostic / economy config (read by lora_tx.py) ─────────────────────

_LORA_DIAG_FILE = Path("/home/louis/stratopi/lora_diag.json")
_LORA_DIAG_DEFAULT = {
    "economy_mode": True,
    "tx_interval_economy": 55,
    "tx_interval_normal": 35,
    "milestones_m": [1000, 5000, 10000, 20000, 30000],
    "triggered_m": [],
    "photo_triggered_m": [],
    "lean_packets": True,
}


def _load_lora_diag():
    try:
        if _LORA_DIAG_FILE.exists():
            return {**_LORA_DIAG_DEFAULT, **_json.loads(_LORA_DIAG_FILE.read_text())}
    except Exception:
        pass
    return dict(_LORA_DIAG_DEFAULT)


def _save_lora_diag(cfg):
    _LORA_DIAG_FILE.parent.mkdir(parents=True, exist_ok=True)
    _LORA_DIAG_FILE.write_text(_json.dumps(cfg, indent=2))


@app.route("/api/lora/diag", methods=["GET"])
def api_lora_diag_get():
    return jsonify(_load_lora_diag())


@app.route("/api/lora/diag", methods=["POST"])
def api_lora_diag_post():
    patch = request.get_json(force=True, silent=True) or {}
    cfg = _load_lora_diag()
    preserve = cfg.get("triggered_m", [])
    preserve_photos = cfg.get("photo_triggered_m", [])
    cfg.update(patch)
    if "triggered_m" not in patch:
        cfg["triggered_m"] = preserve
    if "photo_triggered_m" not in patch:
        cfg["photo_triggered_m"] = preserve_photos
    if "milestones_m" in patch:
        cfg["milestones_m"] = sorted({int(m) for m in cfg["milestones_m"]})
    _save_lora_diag(cfg)
    return jsonify(cfg)


@app.route("/api/lora/diag/reset", methods=["POST"])
def api_lora_diag_reset():
    cfg = _load_lora_diag()
    cfg["triggered_m"] = []
    cfg["photo_triggered_m"] = []
    _save_lora_diag(cfg)
    with _checkpoint_tracker_lock:
        if _checkpoint_tracker is not None:
            _checkpoint_tracker.reset()
    return jsonify(cfg)


_LORA_RADIO_FILE = Path("/home/louis/stratopi/lora_radio.json")
_LORA_RADIO_DEFAULT = {
    "tx_power_dbm": 10,
    "channel": 24,
    "air_rate": 3,
    "freq_hz": 434105000,
    "boost_once_pending": False,
    "boost_once_dbm": 22,
}

try:
    from e22_regs import TX_POWER_DBM, DEFAULT_TX_DBM, erp_mw
except ImportError:
    TX_POWER_DBM = (22, 17, 13, 10)
    DEFAULT_TX_DBM = 10
    def erp_mw(dbm):
        return round(10 ** (dbm / 10.0), 2)


def _load_lora_radio():
    try:
        if _LORA_RADIO_FILE.exists():
            d = {**_LORA_RADIO_DEFAULT, **_json.loads(_LORA_RADIO_FILE.read_text())}
            d["tx_power_dbm"] = int(d["tx_power_dbm"])
            d["channel"] = int(d.get("channel", 24))
            d["air_rate"] = int(d.get("air_rate", 3))
            d["freq_hz"] = int(d.get("freq_hz", 434105000))
            return d
    except Exception:
        pass
    return dict(_LORA_RADIO_DEFAULT)


def _save_lora_radio(cfg):
    _LORA_RADIO_FILE.parent.mkdir(parents=True, exist_ok=True)
    _LORA_RADIO_FILE.write_text(_json.dumps(cfg, indent=2))


@app.route("/api/lora/radio", methods=["GET"])
def api_lora_radio_get():
    cfg = _load_lora_radio()
    dbm = cfg["tx_power_dbm"]
    return jsonify({
        **cfg,
        "erp_mw": erp_mw(dbm),
        "legal_de_limit_dbm": 10,
        "options_dbm": list(TX_POWER_DBM),
    })


@app.route("/api/lora/radio", methods=["POST"])
def api_lora_radio_post():
    patch = request.get_json(force=True, silent=True) or {}
    cfg = _load_lora_radio()
    if "tx_power_dbm" in patch:
        dbm = int(patch["tx_power_dbm"])
        if dbm not in TX_POWER_DBM:
            return jsonify({"error": f"tx_power_dbm must be one of {list(TX_POWER_DBM)}"}), 400
        cfg["tx_power_dbm"] = dbm
    if "channel" in patch:
        cfg["channel"] = int(patch["channel"])
    if "air_rate" in patch:
        cfg["air_rate"] = int(patch["air_rate"])
    if "freq_hz" in patch:
        cfg["freq_hz"] = int(patch["freq_hz"])
    _save_lora_radio(cfg)
    dbm = cfg["tx_power_dbm"]
    return jsonify({
        **cfg,
        "erp_mw": erp_mw(dbm),
        "legal_de_limit_dbm": 10,
        "options_dbm": list(TX_POWER_DBM),
    })


_LORA_CRYPTO_FILE = Path("/home/louis/stratopi/lora_crypto.json")

try:
    from lora_crypto import (
        crypto_status,
        key_fingerprint,
        load_crypto_config,
        parse_key_hex,
        save_crypto_config,
    )
except ImportError:
    def load_crypto_config(path):
        return {"enabled": False, "key_hex": ""}
    def save_crypto_config(path, cfg):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_json.dumps(cfg, indent=2))
    def crypto_status(cfg):
        return {"enabled": bool(cfg.get("enabled"))}
    def key_fingerprint(key_hex):
        return None
    def parse_key_hex(key_hex):
        raise ValueError("lora_crypto not installed")


@app.route("/api/health", methods=["GET"])
def api_health():
    return jsonify({"ok": True, "service": "stratopi"})


@app.route("/api/network", methods=["GET"])
def api_pi_network():
    """URLs clients can use to reach this Pi on LAN or hotspot."""
    import socket as _socket
    ips = []
    try:
        for info in _socket.getaddrinfo(_socket.gethostname(), None, _socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    try:
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip not in ips:
            ips.append(ip)
    except OSError:
        pass
    urls = ["http://stratopi.local:8080", "http://stratopi:8080"]
    for ip in ips:
        urls.append(f"http://{ip}:8080")
    if "http://192.168.4.1:8080" not in urls:
        urls.append("http://192.168.4.1:8080")
    seen = set()
    unique = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            unique.append(u)
    return jsonify({
        "ok": True,
        "hostname": "stratopi",
        "ips": ips,
        "urls": unique,
    })


@app.route("/api/lora/crypto", methods=["GET"])
def api_lora_crypto_get():
    cfg = load_crypto_config(_LORA_CRYPTO_FILE)
    st = crypto_status(cfg)
    return jsonify({
        "ok": True,
        **st,
        "key_fingerprint": key_fingerprint(cfg.get("key_hex", "")),
    })


@app.route("/api/lora/crypto", methods=["POST"])
def api_lora_crypto_post():
    """Ground station pushes encryption key + enabled flag over Wi‑Fi."""
    patch = request.get_json(force=True, silent=True) or {}
    cfg = load_crypto_config(_LORA_CRYPTO_FILE)
    if "enabled" in patch:
        cfg["enabled"] = bool(patch["enabled"])
    if "key_hex" in patch:
        kh = str(patch["key_hex"]).strip()
        if kh:
            parse_key_hex(kh)
            cfg["key_hex"] = kh
    save_crypto_config(_LORA_CRYPTO_FILE, cfg)
    st = crypto_status(cfg)
    return jsonify({
        "ok": True,
        "message": "LoRa crypto config updated",
        **st,
        "key_fingerprint": key_fingerprint(cfg.get("key_hex", "")),
    })


@app.route("/api/lora/radio/boost-once", methods=["POST"])
def api_lora_radio_boost_once():
    """Queue one TX at max (or chosen) power — triggered from ground control."""
    patch = request.get_json(force=True, silent=True) or {}
    cfg = _load_lora_radio()
    boost_dbm = int(patch.get("boost_once_dbm", 22))
    if boost_dbm not in TX_POWER_DBM:
        return jsonify({"error": f"boost_once_dbm must be one of {list(TX_POWER_DBM)}"}), 400
    cfg["boost_once_pending"] = True
    cfg["boost_once_dbm"] = boost_dbm
    _save_lora_radio(cfg)
    return jsonify({
        "ok": True,
        "message": f"Next LoRa TX will run at {boost_dbm} dBm once, then revert to {cfg['tx_power_dbm']} dBm",
        **cfg,
        "erp_mw_boost": erp_mw(boost_dbm),
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False, threaded=True)
