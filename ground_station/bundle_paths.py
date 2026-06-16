"""Paths for dev vs PyInstaller-frozen executable."""
from __future__ import annotations

import sys
from pathlib import Path


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def bundle_dir() -> Path:
    """Read-only bundled assets (templates)."""
    if is_frozen():
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return Path(__file__).resolve().parent


def app_dir() -> Path:
    """Writable app directory (config, logs) — folder containing the .exe."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def templates_dir() -> Path:
    return bundle_dir() / "templates"