"""Dev runner: discover and run the standard-library test suite.

The vendored embeddable Python ignores cwd on ``sys.path`` (``._pth``
mode), so ``python -m unittest`` cannot import this tree; this shim is
the supported entry point.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))


def main() -> int:
    """Run the whole suite; return the exit code."""
    suite = unittest.TestLoader().discover(
        str(ROOT / "tests"), top_level_dir=str(ROOT)
    )
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
