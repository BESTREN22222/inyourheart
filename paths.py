"""Paths that work both from source and from a PyInstaller one-file exe."""

from __future__ import annotations

import sys
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))
BASE = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
RES = Path(getattr(sys, "_MEIPASS", BASE))
DATA = BASE / "data"
WEB = RES / "web"
