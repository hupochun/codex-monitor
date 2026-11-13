"""Self-contained skill launcher: runtime lives beside this file."""
from pathlib import Path
import sys

runtime = Path(__file__).resolve().parent
if not (runtime / 'codex_monitor' / 'cli.py').is_file():
    raise SystemExit('Incomplete skill: reinstall the complete codex-training-monitor archive.')
sys.path.insert(0, str(runtime))
from codex_monitor.cli import main

if __name__ == '__main__':
    raise SystemExit(main())
