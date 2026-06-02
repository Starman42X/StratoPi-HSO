#!/usr/bin/env python3
"""StratoPi HSO - HAB dual-camera controller."""

from flask import Flask, render_template, jsonify, request, send_from_directory, abort, Response
import threading
import subprocess
import os
import glob
import re
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
    for dev in ("/dev/video2", "/dev/video1", "/dev/video0"):
        if os.path.exists(dev):
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


def start_recording(mode):
    global hq_process, usb_process

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

    with _lock:
        try:
            hq_process = subprocess.Popen(hq_cmd, stdout=hq_log, stderr=hq_log)
        except Exception as e:
            hq_process = None
            hq_log.write(f"Failed to start: {e}\n")
            print(f"[HQ cam] Failed to start: {e}")

        if usb_cmd:
            try:
                usb_process = subprocess.Popen(usb_cmd, stdout=usb_log, stderr=usb_log)
            except Exception as e:
                usb_process = None
                usb_log.write(f"Failed to start: {e}\n")
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
    return jsonify({
        "recording": state["recording"],
        "streaming": state["streaming"],
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
        state["settings"]["hq_cam"].update({
            k: v for k, v in data["hq_cam"].items()
            if k in ("width", "height", "fps")
        })
    if "usb_cam" in data:
        state["settings"]["usb_cam"].update({
            k: v for k, v in data["usb_cam"].items()
            if k in ("width", "height", "fps", "prioritize_fps")
        })
    return jsonify({"ok": True, "settings": state["settings"]})


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
    """MJPEG live stream from Pi HQ cam for focus assist."""
    with _lock:
        if state["recording"]:
            return "Cannot stream while recording", 409
        if state["streaming"]:
            return "Stream already active — only one viewer at a time", 409
        state["streaming"] = True

    import queue as _queue
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
        """Background thread: parse JPEG frames from rpicam-vid stdout."""
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
                frame_q.put(None, timeout=1)   # sentinel — tells generator to stop
            except Exception:
                pass

    threading.Thread(target=_reader, daemon=True).start()

    def generate():
        try:
            while True:
                try:
                    frame = frame_q.get(timeout=5)   # 5s timeout detects dead camera
                except _queue.Empty:
                    break
                if frame is None:
                    break
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
                )
        except GeneratorExit:
            pass   # client disconnected
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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False, threaded=True)
