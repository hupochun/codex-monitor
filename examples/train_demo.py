"""Small deterministic training stand-in; no ML dependencies or GPU required."""
import argparse
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--steps", type=int, default=8)
    parser.add_argument("--interval", type=float, default=0.5)
    parser.add_argument("--fail-at", type=int)
    parser.add_argument("--stall-at", type=int)
    args = parser.parse_args()
    for step in range(1, args.steps + 1):
        if step == args.fail_at:
            raise RuntimeError("Simulated training failure")
        if step == args.stall_at:
            time.sleep(3600)
        print(f"step={step} loss={1 / step:.6f}", flush=True)
        time.sleep(args.interval)
    print("Training complete", flush=True)


if __name__ == "__main__":
    main()
