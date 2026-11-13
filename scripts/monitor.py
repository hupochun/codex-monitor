"""Run the checkout from any working directory without pip installation."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from codex_monitor.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
