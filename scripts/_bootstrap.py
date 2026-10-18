"""Make `app` importable and resolve config path when scripts run from repo root or backend/."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)  # config.yaml paths are relative to backend/
os.environ.setdefault("CONFIG_PATH", str(ROOT / "config" / "config.yaml"))
