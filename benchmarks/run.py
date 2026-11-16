import argparse
import csv
import json
from pathlib import Path

from .cases import MODES
from .harness import run


def main():
    parser = argparse.ArgumentParser(description="Run BrowserBridge against isolated synthetic tenants.")
    parser.add_argument("--suite", choices=("smoke", "local120"), default="smoke")
    parser.add_argument("--mode", choices=("all", *MODES), default="hybrid")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--layout", default="all")
    parser.add_argument("--out", type=Path, default=Path("benchmarks/reports/latest"))
    args = parser.parse_args()
    options = {key: value for key, value in vars(args).items() if key != "out"}
    output = run(
        options, lambda rows: print(f"{rows[-1]['id']} {rows[-1]['mode']} {rows[-1]['status']}", flush=True)
    )
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "results.json").write_text(json.dumps(output, indent=2) + "\n")
    with (args.out / "cases.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=(
                "id",
                "mode",
                "family",
                "split",
                "layout",
                "fault",
                "status",
                "task_success",
                "partial_completion",
                "browser_actions",
                "wall_ms",
                "failure_tags",
            ),
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(output["cases"])
    print(json.dumps(output["metrics"], indent=2))


if __name__ == "__main__":
    main()
