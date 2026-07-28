#!/usr/bin/env python3
"""Run the package CLI directly from a source checkout."""

from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from codex_model_router.cli import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
