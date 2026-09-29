"""Test suite helpers: put ``src/`` on sys.path; scope TMP to the app tree."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_ROOT / "src"))

_scratch = tempfile.TemporaryDirectory(prefix="vsdseek-tests-", dir=APP_ROOT)
os.environ["TMP"] = _scratch.name
os.environ["TEMP"] = _scratch.name
