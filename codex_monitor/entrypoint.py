"""Run this installation by absolute script path from any working directory."""
from pathlib import Path
import sys


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from codex_monitor.cli import main

    raise SystemExit(main())
