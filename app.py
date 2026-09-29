"""Dev entry point: puts ``src/`` on sys.path and runs the vsDSeek CLI.

Launchers and the proxy child use this shim, so the package never needs
to be installed into any Python environment (the vendored embeddable
Python ignores cwd and PYTHONPATH in ``._pth`` mode).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from vsdseek.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
