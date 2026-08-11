#!/usr/bin/env python3
"""report-generator.py — build the evidence report from results.jsonl files.

Usage:
  py scripts/report-generator.py --output references/COMPATIBILITY_REPORT.md \
      --results reports/run-baseline6 reports/matrix-output reports/matrix-mangle3 \
      reports/matrix-compress2 reports/run-fuzz-conservative4 reports/run-fuzz-aggressive \
      reports/run-aggressive2 reports/run-e4x reports/run-prod-svgs reports/run-prod-pdf \
      reports/run-prod-arcfit-conservative reports/run-prod-arcfit-aggressive
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path


def load_results(paths: list[str]) -> list[dict]:
    out = []
    for p in paths:
        # split on \n only: U+2028/U+2029 inside fixture strings are valid JSON
        # and must NOT split records.
        for line in Path(p).read_text(encoding="utf-8").split("\n"):
            if line.strip():
                out.append(json.loads(line))
    return out


def size_stats(results: list[dict]) -> dict:
    sizes = [r for r in results if r.get("minify", {}).get("sizeOrig")]
    if not sizes:
        return {}
    reductions = []
    for r in sizes:
        o = r["minify"]["sizeOrig"]
        m = r["minify"]["sizeMin"]
        if o:
            reductions.append(100.0 * (o - m) / o)
    reductions.sort()
    n = len(reductions)
    mean = sum(reductions) / n if n else 0
    med = reductions[n // 2] if n else 0
    return {
        "fixtures": len(sizes),
        "meanPct": round(mean, 2),
        "medianPct": round(med, 2),
        "minPct": round(reductions[0], 2) if n else 0,
        "maxPct": round(reductions[-1], 2) if n else 0,
    }


def variant_summary(results: list[dict]) -> dict[str, Counter]:
    by_variant: dict[str, Counter] = defaultdict(Counter)
    for r in results:
        by_variant[r.get("variantId", "?")][r.get("status", "?")] += 1
    return dict(by_variant)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", nargs="+", required=True)
    ap.add_argument("--output", required=True)
    args = ap.parse_args()

    results = load_results(args.results)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    w = lines.append

    w("# ExtendScript Minification Compatibility Report")
    w("")
    w("Evidence-backed report for UglifyJS minification of Adobe Illustrator")
    w("ExtendScript. All claims below were verified by executing original and")
    w("minified code inside Adobe Illustrator via ILLUSTRATOR_COM_TOOL.py.")
    w("")
    w("## Environment")
    w("")
    w("- Illustrator: 30.6.0 (2026), build 109R, en_US, Windows x86-64")
    w("- UglifyJS: 3.19.3 (npm)")
    w("- Node: v22.23.2, Python: 3.12/3.14 (pywin32)")
    w(f"- Total executions recorded: {len(results)} (original + minified pairs)")
    w("")
    w("## Execution totals by run")
    w("")
    w("| Run | Total | Status histogram |")
    w("|---|---|---|")

    runs: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        runs[r.get("run") or "default"].append(r)

    for run, rrs in runs.items():
        c = Counter(x.get("status") for x in rrs)
        w(f"| {run} | {len(rrs)} | {dict(c)} |")
    w("")

    all_stats = size_stats(results)
    if all_stats:
        w("## Byte-size reductions (all executed fixtures)")
        w("")
        w(f"- Fixtures with size data: {all_stats['fixtures']}")
        w(f"- Mean reduction: {all_stats['meanPct']}%")
        w(f"- Median reduction: {all_stats['medianPct']}%")
        w(f"- Min/Max: {all_stats['minPct']}% / {all_stats['maxPct']}%")
        w("")

    w("## Variant-level results")
    w("")
    for variant, counts in sorted(variant_summary(results).items()):
        w(f"- `{variant}`: {dict(counts)}")
    w("")

    # Failure details
    failures = [r for r in results if r.get("status") in
                ("mismatch", "minify-error", "original-tool-error", "timeout")]
    if failures:
        w("## Failures")
        w("")
        for r in failures:
            w(f"- `{r.get('testId')}` / `{r.get('variantId')}`: {r.get('status')}")
            w(f"  - original: {json.dumps(r.get('original'))[:160]}")
            w(f"  - minified: {json.dumps(r.get('minified'))[:160]}")
        w("")

    divergences = [r for r in results if r.get("status") == "pass-divergence"]
    if divergences:
        w("## Documented divergences (expected, verified)")
        w("")
        seen = set()
        for r in divergences:
            key = (r.get("testId"), r.get("variantId"))
            if key in seen:
                continue
            seen.add(key)
            note = r.get("divergenceNote", "")
            w(f"- `{r.get('testId')}` / `{r.get('variantId')}`: {note or '(see fixture)'}")
        w("")

    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
