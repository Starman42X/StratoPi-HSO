#!/usr/bin/env python3
"""StratoPi HSO - HAB dual-camera controller."""

from flask import Flask, render_template, jsonify, request, send_from_directory, abort
import threading
import subprocess
import os
import glob
import time
import signal
from datetime import datetime
from pathlib import Path

app = Flask(__name__)

CAPTURES_DIR = Path("/home/louis/captures")
CAPTURES_DIR.mkdir(parents=True, exist_ok=True)

_lock = threading.Lock()

state = {
    "recording": False,
    "mode": None,
    "start_time": None,
    "settings": {
        "hq_cam": {"width": 4056, "height": 3040, "fps": 10},
        "usb_cam": {"width": 1920, "height": 1080, "fps": 30},
    },
}

hq_process = None
usb_process = None


def find_usb_cam():
    """Return first USB video device path, preferring UVC devices."""
    for dev in sorted(glob.glob("/dev/video*")):
        try:
            r = subprocess.run(
                ["v4l2-ctl", "--device", dev, "--info"],
                capture_output=True, text=True, timeout=2,
            )
            info = (r.stdout + r.stderr).lower()
            if "usb" in info or "uvc" in info:
                return dev
        except Exception:
            pass
    # Fallback: try common paths
    for dev in ("/dev/video2", "/dev/video1", "/dev/video0"):
        if os.path.exists(dev):
            return dev
    return None


def hq_cam_available():
    try:
        r = subprocess.run(
            ["libcamera-hello", "--list-cameras"],
            capture_output=True, text=True, timeout=5,
        )
        out = r.stdout + r.stderr
        return "imx477" in out.lower() or ("available" in out.lower() and "camera" in out.lower())
    except Exception:
        return False


def start_recording(mode):
    global hq_process, usb_process

    s = state["settings"]
    hq = s["hq_cam"]
    usb = s["usb_cam"]
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    prefix = f"{mode}_{ts}"

    hq_out = str(CAPTURES_DIR / f"{prefix}_hq.h264")
    usb_out = str(CAPTURES_DIR / f"{prefix}_usb.mp4")

    # Pi HQ cam via libcamera-vid (outputs raw H264; wrap later if needed)
    hq_cmd = [
        "libcamera-vid",
        "--width", str(hq["width"]),
        "--height", str(hq["height"]),
        "--framerate", str(hq["fps"]),
        "--codec", "h264",
        "--output", hq_out,
        "--nopreview",
        "-t", "0",
        "--awb", "auto",
        "--exposure", "normal",
        "--metering", "matrix",
        "--autofocus-mode", "continuous",
    ]

    usb_dev = find_usb_cam()
    usb_cmd = None
    if usb_dev:
        usb_cmd = [
            "ffmpeg", "-y",
            "-f", "v4l2",
            "-framerate", str(usb["fps"]),
            "-video_size", f"{usb['width']}x{usb['height']}",
            "-i", usb_dev,
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-crf", "23",
            usb_out,
        ]

    with _lock:
        try:
            hq_process = subprocess.Popen(
                hq_cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            hq_process = None
            print(f"[HQ cam] Failed to start: {e}")

        if usb_cmd:
            try:
                usb_process = subprocess.Popen(
                    usb_cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception as e:
                usb_process = None
                print(f"[USB cam] Failed to start: {e}")

        state["recording"] = True
        state["mode"] = mode
        state["start_time"] = time.time()


def stop_recording():
    global hq_process, usb_process

    with _lock:
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
        state["recording"] = False
        state["mode"] = None
        state["start_time"] = None


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
    return jsonify({
        "recording": state["recording"],
        "mode": state["mode"],
        "duration": duration,
        "hq_cam_available": hq_avail,
        "usb_cam_device": usb_dev,
        "settings": state["settings"],
    })


@app.route("/api/settings", methods=["POST"])
def api_settings():
    if state["recording"]:
        return jsonify({"error": "Cannot change settings while recording"}), 400
    data = request.json or {}
    if "hq_cam" in data:
        state["settings"]["hq_cam"].update(data["hq_cam"])
    if "usb_cam" in data:
        state["settings"]["usb_cam"].update(data["usb_cam"])
    return jsonify({"ok": True, "settings": state["settings"]})


@app.route("/api/start", methods=["POST"])
def api_start():
    if state["recording"]:
        return jsonify({"error": "Already recording"}), 400
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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False, threaded=True)
