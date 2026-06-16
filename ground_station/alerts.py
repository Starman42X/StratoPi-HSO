"""Altitude milestone alerts + Windows notifications for ground station."""
from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path

from bundle_paths import app_dir

log = logging.getLogger("gs.alerts")

CONFIG_FILE = app_dir() / "gs_alerts.json"

DEFAULT_MILESTONES = [500, 1000, 2000, 5000, 10000, 15000, 20000, 25000, 30000]

DEFAULT_CONFIG = {
    "notifications_enabled": True,
    "browser_notifications": True,
    "milestones_m": DEFAULT_MILESTONES.copy(),
    "enabled_milestones": [1000, 5000, 10000, 20000, 30000],
    "triggered_m": [],
    "pi_url": "http://stratopi.local:8080",  # auto-discovered; hotspot: 192.168.4.1
    "economy_mode": True,
}


def load_config() -> dict:
    try:
        if CONFIG_FILE.exists():
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            merged = {**DEFAULT_CONFIG, **data}
            return merged
    except Exception as e:
        log.warning("Alert config load failed: %s", e)
    return DEFAULT_CONFIG.copy()


def save_config(cfg: dict) -> None:
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def reset_triggered(cfg: dict) -> dict:
    cfg = {**cfg, "triggered_m": []}
    save_config(cfg)
    return cfg


def _notify_windows(title: str, message: str) -> bool:
    if sys.platform != "win32":
        return False
    try:
        from winotify import Notification

        toast = Notification(
            app_id="StratoPi Ground Station",
            title=title,
            msg=message,
            duration="long",
        )
        toast.show()
        return True
    except ImportError:
        pass
    try:
        ps = (
            f'$t="{title}"; $m="{message}"; '
            '[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, '
            'ContentType = WindowsRuntime] | Out-Null; '
            '$template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(1); '
            '$xml = [xml]$template.GetXml(); '
            '$xml.toast.visual.binding.text[0].AppendChild($xml.CreateTextNode($t)) | Out-Null; '
            '$xml.toast.visual.binding.text[1].AppendChild($xml.CreateTextNode($m)) | Out-Null; '
            '$toast = [Windows.UI.Notifications.ToastNotification]::new($xml); '
            '[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("StratoPi").Show($toast)'
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
        return True
    except Exception as e:
        log.warning("Windows notification failed: %s", e)
        return False


class AltitudeAlerts:
    """Track altitude crossings and fire notifications."""

    def __init__(self):
        self._cfg = load_config()
        self._last_alt: float | None = None
        self._pending_browser: list[dict] = []

    def reload(self):
        self._cfg = load_config()

    def get_config(self) -> dict:
        return self._cfg.copy()

    def update_config(self, patch: dict) -> dict:
        self._cfg.update(patch)
        if "enabled_milestones" in patch:
            self._cfg["enabled_milestones"] = sorted(
                {int(x) for x in self._cfg["enabled_milestones"]}
            )
        if "milestones_m" in patch:
            self._cfg["milestones_m"] = sorted({int(x) for x in self._cfg["milestones_m"]})
        save_config(self._cfg)
        return self._cfg.copy()

    def pop_browser_events(self) -> list[dict]:
        ev = self._pending_browser[:]
        self._pending_browser.clear()
        return ev

    def on_frame(self, frame: dict) -> list[dict]:
        """Returns list of alert events fired this frame."""
        self.reload()
        alt = float(frame.get("alt", 0))
        events: list[dict] = []

        if frame.get("packet_type") == "diagnostic":
            ev = self._event(
                frame,
                kind="diagnostic",
                milestone=frame.get("milestone_m"),
                title=f"StratoPi diagnostic @ {frame.get('milestone_m')} m",
            )
            events.append(ev)
            self._notify(ev)
            return events

        enabled = set(self._cfg.get("enabled_milestones") or [])
        triggered = set(self._cfg.get("triggered_m") or [])
        last = self._last_alt
        self._last_alt = alt

        if last is None or not enabled:
            return events

        for m in sorted(enabled):
            if m in triggered:
                continue
            if last < m <= alt:
                triggered.add(m)
                self._cfg["triggered_m"] = sorted(triggered)
                save_config(self._cfg)
                ev = self._event(
                    frame,
                    kind="milestone",
                    milestone=m,
                    title=f"StratoPi passed {m:,} m",
                )
                events.append(ev)
                self._notify(ev)

        return events

    def _event(self, frame: dict, kind: str, milestone, title: str) -> dict:
        parts = [
            f"Alt {frame.get('alt', 0):.0f} m",
            f"Lat {frame.get('lat', 0):.5f}",
            f"Lon {frame.get('lon', 0):.5f}",
        ]
        if frame.get("cell_temp") is not None:
            parts.append(f"Cell {frame['cell_temp']:.1f}°C")
        if frame.get("packet_type") == "diagnostic":
            parts.append(f"TX ok/fail {frame.get('tx_ok')}/{frame.get('tx_fail')}")
            if frame.get("uptime_s") is not None:
                parts.append(f"Uptime {frame['uptime_s']}s")
        return {
            "kind": kind,
            "milestone_m": milestone,
            "title": title,
            "message": " · ".join(parts),
            "frame": {
                k: frame.get(k)
                for k in (
                    "seq", "alt", "lat", "lon", "sats", "cell_temp",
                    "heater_duty", "packet_type", "tx_ok", "tx_fail", "uptime_s",
                )
            },
        }

    def _notify(self, ev: dict) -> None:
        if self._cfg.get("browser_notifications", True):
            self._pending_browser.append(ev)
        if self._cfg.get("notifications_enabled", True):
            _notify_windows(ev["title"], ev["message"])