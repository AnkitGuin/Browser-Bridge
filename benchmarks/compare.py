import argparse
import json
from pathlib import Path


def render(result):
    lines = [
        "# BrowserBridge evaluation",
        "",
        "| Mode | Evaluated | Passed | Unavailable | Wilson 95% | p50 ms | p95 ms |",
        "|---|---:|---:|---:|---|---:|---:|",
    ]
    for mode, row in result["metrics"].items():
        lines.append(
            f"| {mode} | {row['n']} | {row['successes']} | {row['unavailable']} | "
            f"{row['wilson_95']} | {row['wall_ms_p50']} | {row['wall_ms_p95']} |"
        )
    lines.extend(["", "## Paired mode comparisons", ""])
    for row in result.get("paired", []):
        lines.append(
            f"- {row['left']} vs {row['right']}, N={row['n']}, "
            f"success difference {row['success_delta']:+.1%}."
        )
    lines.extend(["", "## Unsuccessful or unavailable cases", ""])
    for row in result["cases"]:
        if not row["task_success"]:
            lines.append(f"- {row['id']} / {row['mode']} · {', '.join(row['failure_tags'])}")
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    input_path = args.input / "results.json" if args.input.is_dir() else args.input
    args.output.write_text(render(json.loads(input_path.read_text())))


if __name__ == "__main__":
    main()
