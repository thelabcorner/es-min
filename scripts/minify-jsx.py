#!/usr/bin/env python3
"""minify-jsx.py — production ExtendScript minification pipeline (ESMIN).

Implements the verified pipeline for a single .jsx file:

    input .jsx
      -> encoding check (UTF-8, BOM tolerated)
      -> extract Adobe preprocessor directives
      -> resolve #include directives (recursive, circular detection)
      -> E4X + dynamic-reference risk scan (warnings only)
      -> UglifyJS minify via minify-worker.mjs (config file)
      -> switch-case repair pass (ExtendScript ASI bug)
      -> restore directives
      -> node --check on the directive-stripped body (static guard)
      -> output .jsx + stats

Usage:
  py scripts/minify-jsx.py --in bundle.jsx --config configs/conservative.json \
      --out bundle.min.jsx [--skip-includes] [--check-only]

Exit codes: 0 ok, 1 minification failed, 2 usage/toolchain error.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


def read_utf8(path: Path) -> str:
    data = path.read_bytes()
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    return data.decode("utf-8", errors="strict")


def write_utf8(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")


def run_worker(config_path: Path, body: str, workdir: Path) -> tuple[bool, dict]:
    """Invoke ESMIN's minify-worker.mjs on the plain-JS body."""
    worker = HERE / "minify-worker.mjs"
    src_file = workdir / "minify-in.js"
    out_file = workdir / "minify-out.json"
    src_file.write_text(body, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(worker), "--config", str(config_path),
         "--in", str(src_file), "--out", str(out_file)],
        capture_output=True, text=True, timeout=300, encoding="utf-8",
    )
    if not out_file.exists():
        return False, {"error": (proc.stderr or proc.stdout or "")[:500],
                       "code": proc.returncode}
    result = json.loads(out_file.read_text(encoding="utf-8"))
    return bool(result.get("ok")), result


def node_check(body: str) -> tuple[bool, str]:
    """Static guard: node --check on the directive-stripped body."""
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as fh:
        fh.write(body)
        path = fh.name
    try:
        proc = subprocess.run(["node", "--check", path],
                              capture_output=True, text=True, encoding="utf-8")
        return proc.returncode == 0, (proc.stderr or "")[:400]
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="in_path", required=True)
    ap.add_argument("--config", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--skip-includes", action="store_true",
                    help="do not resolve #include directives")
    ap.add_argument("--check-only", action="store_true",
                    help="preprocess + scan only; print report, write nothing")
    args = ap.parse_args(argv)

    from extendscript_preprocess import (
        extract_directives,
        fix_extendscript_switch,
        preprocess_source,
        render_directives,
    )

    in_path = Path(args.in_path).resolve()
    config_path = Path(args.config).resolve()
    out_path = Path(args.out).resolve()
    if not in_path.exists():
        print(f"minify-jsx: input not found: {in_path}", file=sys.stderr)
        return 2
    if not config_path.exists():
        print(f"minify-jsx: config not found: {config_path}", file=sys.stderr)
        return 2

    source = read_utf8(in_path)
    result = preprocess_source(source, in_path.parent,
                               [] if args.skip_includes else None)
    body = result.body

    warnings = []
    if result.has_e4x:
        warnings.append(f"E4X detected at {len(result.e4x_sites)} site(s); "
                        "run e4x_to_xml_string.py first or exclude this file")
    for risk in result.risks:
        if risk["severity"] == "high":
            warnings.append(f"line {risk['line']}: {risk['pattern']} — "
                            f"{risk['explanation'][:120]}")
    for w in result.warnings:
        warnings.append(w)

    if args.check_only:
        print(json.dumps({
            "directives": [d.name for d in result.directives],
            "hasE4X": result.has_e4x,
            "risks": len(result.risks),
            "warnings": warnings,
        }, indent=2))
        return 0

    with tempfile.TemporaryDirectory(prefix="esmin-minify-") as tmp:
        ok, minify_result = run_worker(config_path, body, Path(tmp))
    if not ok:
        print("minify-jsx: UglifyJS failed:",
              json.dumps(minify_result.get("error")), file=sys.stderr)
        return 1

    code = str(minify_result["code"])
    fixed = fix_extendscript_switch(code)
    if fixed != code:
        code = fixed

    final = render_directives(result.directives) + code
    ok_check, check_err = node_check(code)
    if not ok_check:
        print(f"minify-jsx: node --check failed on the minified body: {check_err}",
              file=sys.stderr)
        return 1

    write_utf8(out_path, final)
    in_bytes = len(source.encode("utf-8"))
    out_bytes = len(final.encode("utf-8"))
    pct = 100.0 * (in_bytes - out_bytes) / in_bytes if in_bytes else 0.0
    print(f"minify-jsx: {in_path.name} {in_bytes} -> {out_bytes} bytes "
          f"({pct:.1f}% reduction) -> {out_path}")
    if warnings:
        print("minify-jsx: warnings:")
        for w in warnings:
            print(f"  - {w}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
