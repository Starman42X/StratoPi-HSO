#!/usr/bin/env python3
"""Launcher — runs Ground Control from ground_station/."""
import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
GS = ROOT / "ground_station" / "gs_app.py"
if not GS.exists():
    print(f"Missing {GS}")
    sys.exit(1)
sys.path.insert(0, str(ROOT / "ground_station"))
runpy.run_path(str(GS), run_name="__main__")