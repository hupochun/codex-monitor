"""Write a portable demo spec; real notifications require --notify explicitly."""
import argparse
import json
import os
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--notify", action="store_true")
    parser.add_argument("--thread", default=os.environ.get("CODEX_THREAD_ID"))
    parser.add_argument("--steps", type=int, default=8)
    parser.add_argument("--interval", type=float, default=0.5)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    if args.notify and not args.thread:
        parser.error("--notify requires --thread or CODEX_THREAD_ID")
    spec = {
        "command": [sys.executable, "-u", str(root / "examples" / "train_demo.py"),
                    "--steps", str(args.steps), "--interval", str(args.interval)],
        "cwd": str(root), "backend": "local", "thread_id": args.thread,
        "health": {"pattern": r"step=(?P<step>\d+) loss=(?P<loss>\S+)",
                   "min_advances": 2, "startup_timeout": 30, "stall_timeout": 15},
        "bridge": {"enabled": args.notify}, "max_runtime": 120,
        "follow_up": "This is a wake-up smoke test, not a real training run. Acknowledge this event using the supplied command, report whether you are in the expected original desktop conversation, and do not launch more jobs.",
    }
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(spec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
