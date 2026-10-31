#!/usr/bin/env python3
"""Update the generated CI benchmark table in the project README."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re


START_MARKER = "<!-- CPYTE_CI_BENCHMARKS_START -->"
END_MARKER = "<!-- CPYTE_CI_BENCHMARKS_END -->"


def format_time(ns):
    if ns < 1_000:
        return f"{ns}ns"
    if ns < 1_000_000:
        return f"{ns / 1_000:.2f}us"
    if ns < 1_000_000_000:
        return f"{ns / 1_000_000:.2f}ms"
    return f"{ns / 1_000_000_000:.3f}s"


def render_table(results, commit, run_url, updated_at):
    lines = [
        START_MARKER,
        f"Latest run: [{commit[:12]}]({run_url}) · updated {updated_at}",
        "",
        "| Benchmark | C | Cpy | Python | Cpy/C | Python/C |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for name in sorted(results):
        row = results[name]
        if "error" in row:
            continue
        c_ns = row["C"]["median_ns"]
        cpy_ns = row["Cpy"]["median_ns"]
        py_ns = row["Python"]["median_ns"]
        cpy_ratio = cpy_ns / c_ns if c_ns else None
        py_ratio = py_ns / c_ns if c_ns else None
        lines.append(
            f"| `{name}` | {format_time(c_ns)} | {format_time(cpy_ns)} "
            f"| {format_time(py_ns)} | "
            f"{f'{cpy_ratio:.2f}x' if cpy_ratio is not None else 'n/a'} | "
            f"{f'{py_ratio:.2f}x' if py_ratio is not None else 'n/a'} |"
        )
    lines.extend(
        [
            "",
            "Process wall time includes startup. These are measurements from "
            "the dedicated `cpyte-official-runner-1`, not the historical "
            "benchmark figures elsewhere in this README; results can vary "
            "with host load and software versions.",
            END_MARKER,
        ]
    )
    return "\n".join(lines)


def update_readme(readme_path, results, commit, run_url):
    readme = readme_path.read_text(encoding="utf-8")
    start_count = readme.count(START_MARKER)
    end_count = readme.count(END_MARKER)
    if start_count != end_count or start_count > 1:
        raise ValueError("README must contain at most one matching benchmark marker pair")

    updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    block = render_table(results, commit, run_url, updated_at)
    if start_count:
        pattern = re.compile(
            re.escape(START_MARKER) + r".*?" + re.escape(END_MARKER),
            flags=re.DOTALL,
        )
        updated = pattern.sub(lambda _: block, readme, count=1)
    else:
        updated = readme.rstrip() + "\n\n## CI Benchmark Results\n\n" + block + "\n"
    if updated != readme:
        readme_path.write_text(updated, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results_json", type=Path)
    parser.add_argument("--readme", type=Path, default=Path("readme.md"))
    parser.add_argument("--commit", default=os.environ.get("GITHUB_SHA", "local"))
    parser.add_argument("--run-url", default="local run")
    args = parser.parse_args()

    try:
        results = json.loads(args.results_json.read_text(encoding="utf-8"))
        if not isinstance(results, dict) or not results:
            raise ValueError("benchmark results must be a non-empty JSON object")
        for name, row in results.items():
            if "error" in row:
                continue
            for language in ("C", "Cpy", "Python"):
                if "median_ns" not in row.get(language, {}):
                    raise ValueError(
                        f"{name}: missing median for {language}; refusing to "
                        "publish incomplete results"
                    )
        update_readme(args.readme, results, args.commit, args.run_url)
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
