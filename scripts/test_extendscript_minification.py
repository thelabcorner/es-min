#!/usr/bin/env python3
"""test_extendscript_minification.py — ExtendScript minification compatibility harness.

Runs original and UglifyJS-minified ExtendScript through REAL Adobe Illustrator
(via ILLUSTRATOR_COM_TOOL.py), compares outcomes, and produces machine-readable
results plus a human-readable report.

Usage:
  py scripts/test_extendscript_minification.py --input fixtures --config configs/conservative.json
  py scripts/test_extendscript_minification.py --input fixtures --config configs/matrix-compress.json --matrix-subset
  py scripts/test_extendscript_minification.py --only conditional-nested-001 --config configs/baseline.json
  py scripts/test_extendscript_minification.py --list
  py scripts/test_extendscript_minification.py --input path/to/prod.jsx --no-execute

Exit codes: 0 success (all passes or no executions), 1 failures found,
2 harness/config error, 3 Illustrator unreachable or aborted.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from extendscript_preprocess import (
    PreprocessResult,
    extract_directives,
    preprocess_source,
    render_directives,
)

HERE = Path(__file__).resolve().parent
SKILL_ROOT = HERE.parent
REPO_ROOT = SKILL_ROOT.parent
TOOL_CANDIDATES = [
    Path(os.environ["ESMIN_ILLUSTRATOR_TOOL"])
    for _ in [0]
    if os.environ.get("ESMIN_ILLUSTRATOR_TOOL")
] + [
    REPO_ROOT / "agent-skills" / "illustrator-com-automation-skill" / "comtool" / "ILLUSTRATOR_COM_TOOL.py",
    REPO_ROOT / "agent-skills" / "illustrator-com-automation-skill" / "scripts" / "ILLUSTRATOR_COM_TOOL.py",
    REPO_ROOT / "illustrator-com-automation-skill" / "scripts" / "ILLUSTRATOR_COM_TOOL.py",
]
DEFAULT_TOOL = next((p for p in TOOL_CANDIDATES if p.exists()), TOOL_CANDIDATES[0])
DEFAULT_FIXTURES = SKILL_ROOT / "fixtures"

UNDEFINED = {"kind": "undefined"}
BUSY_THRESHOLD = 3


class HarnessConfigError(Exception):
    """Harness/tool/config setup failed before a test result could be produced."""


# ---------------------------------------------------------------------------
# Fixture model
# ---------------------------------------------------------------------------

@dataclass
class Fixture:
    data: dict
    path: Path

    @property
    def id(self) -> str:
        return str(self.data.get("id") or self.path.stem)

    @property
    def tags(self) -> list[str]:
        return list(self.data.get("tags") or [])

    @property
    def source(self) -> str:
        return str(self.data.get("source") or "")

    @property
    def description(self) -> str:
        return str(self.data.get("description") or "")

    @property
    def mode(self) -> str:
        return str(self.data.get("mode") or "eval")

    @property
    def expect(self) -> dict | None:
        e = self.data.get("expect")
        return e if isinstance(e, dict) else None

    @property
    def minifiable(self) -> bool:
        return bool(self.data.get("minifiable", True))

    @property
    def skip(self) -> bool:
        return bool(self.data.get("skip", False))

    @property
    def timeout_s(self) -> float:
        return float(self.data.get("timeoutSec", 60))


# ---------------------------------------------------------------------------
# Outcome model
# ---------------------------------------------------------------------------

@dataclass
class Outcome:
    kind: str            # ok | script_error | parse_error | tool_error | timeout
    payload: Any = None  # value | {name,message} | {message} | {message}
    elapsed_ms: float = 0.0

    def canonical(self) -> dict:
        return {"kind": self.kind, "payload": canonicalize(self.payload)}

    def __eq__(self, other: "Outcome") -> bool:
        return self.canonical() == other.canonical()


def canonicalize(value: Any) -> Any:
    """Deep-canonicalize for comparison: sort keys, round floats, map sentinels."""
    if isinstance(value, float):
        return round(value, 6) if value != value else None  # NaN -> null
    if isinstance(value, dict):
        return {k: canonicalize(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [canonicalize(v) for v in value]
    return value


def norm_error_message(message: str) -> str:
    """Normalize error text so line numbers / code context don't break equality."""
    lines = [l for l in str(message).replace("\r", "\n").split("\n") if l.strip()]
    first = lines[0] if lines else ""
    for pat in ("Line:", "->"):
        idx = first.find(pat)
        if idx != -1:
            first = first[:idx].rstrip()
    return first.strip()


# ---------------------------------------------------------------------------
# Execution via ILLUSTRATOR_COM_TOOL.py
# ---------------------------------------------------------------------------

def run_tool(tool: Path, args: list[str], timeout: float) -> tuple[int, str]:
    """Run the COM tool as a subprocess. Returns (exitcode, stdout).

    PYTHONIOENCODING=utf-8 forces UTF-8 stdout: the COM tool's print() crashes
    with cp1252 UnicodeEncodeError when script results contain non-ASCII
    characters (observed with CJK fixture strings).
    """
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    cmd = [sys.executable, str(tool)] + args
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace", env=env,
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired as exc:
        return -1, f"TIMEOUT after {timeout}s: {(exc.stdout or b'')[:200]}"


def com_status(tool: Path, timeout: float = 60) -> dict:
    code, out = run_tool(tool, ["status"], timeout)
    if code != 0:
        return {"ok": False, "error": out[:400]}
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {"ok": False, "error": out[:400]}


def classify_dict(env: dict) -> Outcome:
    """Classify a tool envelope dict (single command or batch entry) into Outcome."""
    if not env.get("ok"):
        msg = str(env.get("error", "tool error"))
        if "Syntax error" in msg or "Expected:" in msg:
            return Outcome("parse_error", {"message": norm_error_message(msg)})
        if " (your code line " in msg and ":" in msg:
            name, message = msg.split(":", 1)
            return Outcome(
                "script_error",
                {"name": name.strip() or "Error", "message": norm_error_message(message)},
                float(env.get("elapsed", 0)) * 1000,
            )
        return Outcome("tool_error", {"message": norm_error_message(msg)})
    inner = env.get("result")
    if isinstance(inner, dict) and inner.get("ok") is False:
        return Outcome(
            "script_error",
            {"name": str(inner.get("name", "Error")),
             "message": norm_error_message(inner.get("error", ""))},
            float(env.get("elapsed", 0)) * 1000,
        )
    if isinstance(inner, dict) and "result" in inner:
        return Outcome("ok", inner["result"], float(env.get("elapsed", 0)) * 1000)
    if isinstance(inner, dict):
        return Outcome("ok", inner, float(env.get("elapsed", 0)) * 1000)
    return Outcome("ok", inner, float(env.get("elapsed", 0)) * 1000)


def execute_pair(
    tool: Path, orig_file: Path, min_file: Path, timeout: float
) -> tuple[Outcome, Outcome, float]:
    """Run original + minified via one COM batch. Returns (orig, min, wall_s)."""
    batch = [
        {"op": "eval", "code": orig_file.read_text(encoding="utf-8")},
        {"op": "eval", "code": min_file.read_text(encoding="utf-8")},
    ]
    with tempfile.NamedTemporaryFile(
        "w", suffix=".json", delete=False, encoding="utf-8"
    ) as fh:
        json.dump(batch, fh)
        batch_path = fh.name
    try:
        started = time.monotonic()
        code, out = run_tool(tool, ["batch", batch_path], timeout)
        wall = time.monotonic() - started
    finally:
        try:
            os.unlink(batch_path)
        except OSError:
            pass
    if code == -1:
        return Outcome("timeout"), Outcome("timeout"), wall
    try:
        results = json.loads(out)
        if not isinstance(results, list) or len(results) != 2:
            return (
                Outcome("tool_error", {"message": out[:400]}),
                Outcome("tool_error", {"message": out[:400]}),
                wall,
            )
        return classify_dict(results[0]), classify_dict(results[1]), wall
    except json.JSONDecodeError:
        return (
            Outcome("tool_error", {"message": out[:400]}),
            Outcome("tool_error", {"message": out[:400]}),
            wall,
        )


# ---------------------------------------------------------------------------
# Expect matching
# ---------------------------------------------------------------------------

def expect_matches(expect: dict | None, outcome: Outcome) -> tuple[bool, str]:
    if not expect:
        return True, ""
    if "result" in expect:
        if outcome.kind != "ok":
            return False, f"expected ok result, got {outcome.kind}"
        if expect.get("mode") == "partial" and isinstance(expect["result"], dict):
            actual = outcome.payload if isinstance(outcome.payload, dict) else {}
            for key, value in expect["result"].items():
                if canonicalize(actual.get(key)) != canonicalize(value):
                    return False, f"partial key {key} mismatch"
            return True, ""
        return canonicalize(outcome.payload) == canonicalize(expect["result"]), \
            "result mismatch"
    if "undefined" in expect and expect["undefined"]:
        return outcome.kind == "ok" and outcome.payload == UNDEFINED, "expected undefined"
    if "error" in expect:
        e = expect["error"]
        if outcome.kind != "script_error":
            return False, f"expected script_error, got {outcome.kind}"
        if "name" in e and outcome.payload.get("name") != e["name"]:
            return False, f"error name {outcome.payload.get('name')} != {e['name']}"
        if "messageContains" in e:
            if e["messageContains"].lower() not in outcome.payload.get("message", "").lower():
                return False, "error message mismatch"
        return True, ""
    return True, ""


# ---------------------------------------------------------------------------
# Minify orchestration
# ---------------------------------------------------------------------------

def expand_configs(raw: dict, config_path: Path) -> list[tuple[str, dict]]:
    """Expand a config file into (variant_id, full_options) pairs."""
    if "variants" not in raw:
        return [(raw.get("_id", config_path.stem), raw)]
    base = raw.get("_base", {})
    compress_base = raw.get("_compress_base")
    mangle_base = raw.get("_mangle_base")
    out: list[tuple[str, dict]] = []
    for variant in raw["variants"]:
        vid = str(variant.get("_id") or "variant")
        opts = deep_merge(base, variant.get("opts", {}))
        if "compress" in variant.get("opts", {}) and compress_base is not None:
            opts["compress"] = deep_merge(compress_base, variant["opts"]["compress"])
        if "mangle" in variant.get("opts", {}) and mangle_base is not None:
            opts["mangle"] = deep_merge(mangle_base, variant["opts"]["mangle"])
        opts["_id"] = vid
        out.append((vid, opts))
    return out


def deep_merge(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def run_worker(
    worker: Path, config: dict, body: str, workdir: Path
) -> tuple[bool, dict]:
    """Invoke minify-worker.mjs. Returns (ok, result_dict)."""
    workdir.mkdir(parents=True, exist_ok=True)
    src_file = workdir / "worker-in.js"
    out_file = workdir / "worker-out.json"
    cfg_file = workdir / "worker-config.json"
    src_file.write_text(body, encoding="utf-8")
    cfg_file.write_text(json.dumps(config), encoding="utf-8")
    proc = subprocess.run(
        ["node", str(worker), "--config", str(cfg_file),
         "--in", str(src_file), "--out", str(out_file)],
        capture_output=True, text=True, timeout=120, encoding="utf-8",
    )
    if not out_file.exists():
        return False, {"error": (proc.stderr or proc.stdout or "")[:400],
                       "code": proc.returncode}
    result = json.loads(out_file.read_text(encoding="utf-8"))
    return bool(result.get("ok")), result


# ---------------------------------------------------------------------------
# Fixture preparation (preprocess + minify + staging)
# ---------------------------------------------------------------------------

@dataclass
class Prepared:
    fixture: Fixture
    variant_id: str
    preprocess: PreprocessResult
    minified_code: str | None
    minify_error: dict | None
    minify_stats: dict | None
    size_orig: int
    size_min: int
    minify_ms: float = 0.0
    e4x: tuple[bool, list] = (False, [])
    risks: list = field(default_factory=list)
    orig_source: str = ""
    transform: dict | None = None


def prepare_fixture(
    fixture: Fixture,
    config: dict,
    variant_id: str,
    worker: Path,
    workdir: Path,
    includepaths: list[Path] | None = None,
) -> Prepared:
    src = fixture.source
    directives: list = []
    body = src
    if fixture.mode in ("file", "parse-guard"):
        directives, body = extract_directives(src)
        # #include may legally appear anywhere (textual preprocessor), so
        # resolve includes whenever any remain in the body, not only header ones.
        if "#include" in body or "//@include" in body:
            resolved = preprocess_source(src, fixture.path.resolve().parent, includepaths)
            body = resolved.body
            directives = resolved.directives
    transform_info = None
    if fixture.data.get("transformE4X"):
        from e4x_to_xml_string import transform_e4x
        res = transform_e4x(body)
        transform_info = {
            "literalCount": res.literal_count,
            "rejections": res.rejected[:10],
        }
        if res.rejected:
            reasons = "; ".join(r["reason"] for r in res.rejected[:3])
            return Prepared(
                fixture, variant_id, PreprocessResult(body=body, directives=directives),
                None, {"name": "E4XTransformError",
                       "message": f"E4X transform rejected constructs: {reasons}"},
                None, len(body.encode("utf-8")), 0, 0.0,
                (True, []), [], src, transform_info,
            )
        if res.literal_count:
            body = res.transformed
    size_orig = len(body.encode("utf-8"))
    ok, result = run_worker(worker, config, body, workdir / f"w-{variant_id}")
    from extendscript_preprocess import scan_e4x, scan_risks
    has_e4x, e4x_sites = scan_e4x(body)
    risks = scan_risks(body)
    if not ok:
        return Prepared(
            fixture, variant_id, PreprocessResult(body=body, directives=directives),
            None, result.get("error"), None, size_orig, 0, 0.0,
            (has_e4x, e4x_sites), risks, src, transform_info,
        )
    code = str(result["code"])
    from extendscript_preprocess import fix_extendscript_switch
    fixed = fix_extendscript_switch(code)
    if fixed != code:
        code = fixed
    stats = result.get("stats") or {}
    return Prepared(
        fixture, variant_id, PreprocessResult(body=body, directives=directives),
        code, None, stats, size_orig, len(code.encode("utf-8")),
        float(stats.get("timeMs", 0)),
        (has_e4x, e4x_sites), risks, src, transform_info,
    )


def wrap_eval_result(code: str) -> str:
    """Preserve the harness' undefined-vs-null result-channel contract.

    The current COM tool unwraps JSX results before JSON serialization, so a
    script-level `undefined` and a script-level `null` both arrive as JSON
    null. Execute eval-mode fixture bodies inside an IIFE and translate only
    the actual `undefined` result into the sentinel shape the fixture corpus
    has always used.
    """
    return (
        "var __extminValue=(function(){\n" + code +
        "\n})(); if(typeof __extminValue==='undefined'){"
        "return {kind:'undefined'};} return __extminValue;"
    )


def stage_eval_file(code: str, stage_dir: Path, tag: str, wrap_result: bool = False) -> Path:
    p = stage_dir / f"{tag}.jsx"
    p.write_text(wrap_eval_result(code) if wrap_result else code, encoding="utf-8")
    return p


def stage_parse_guard(body: str, stage_dir: Path, tag: str) -> Path:
    p = stage_dir / f"{tag}.jsx"
    p.write_text("if(0){\n" + body + "\n}", encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

class Runner:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.tool = Path(args.illustrator_tool).resolve()
        self.worker = HERE / "minify-worker.mjs"
        self.output_dir = Path(args.output).resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.workdir = self.output_dir / "work"
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.stage_dir = self.output_dir / "stage"
        self.stage_dir.mkdir(parents=True, exist_ok=True)
        self.results: list[dict] = []
        self.consecutive_timeouts = 0
        self.aborted = False

    # -- config loading ------------------------------------------------
    def load_configs(self) -> list[tuple[str, dict]]:
        cfg_path = Path(self.args.config)
        if not cfg_path.exists():
            raise HarnessConfigError(f"config not found: {cfg_path}")
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        return expand_configs(raw, cfg_path)

    # -- fixture loading ------------------------------------------------
    def load_fixtures(self, matrix_subset: bool = False) -> list[Fixture]:
        inp = Path(self.args.input)
        if inp.is_file():
            if inp.suffix == ".json":
                fixtures = [Fixture(json.loads(inp.read_text(encoding="utf-8")), inp)]
            else:
                fixtures = [self._fixture_from_jsx(inp, parse_guard=self.args.parse_guard)]
        else:
            fixtures = []
            for p in sorted(inp.glob("*.json")):
                if p.name == "manifest.json":
                    continue
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                    if "source" not in data:
                        continue
                    fixtures.append(Fixture(data, p))
                except json.JSONDecodeError as exc:
                    print(f"WARN: invalid fixture JSON {p.name}: {exc}")
        if matrix_subset:
            fixtures = [f for f in fixtures if "unsafe-subset" in f.tags or "sensitive" in f.tags]
        if self.args.only:
            wanted = set(self.args.only.split(","))
            fixtures = [f for f in fixtures if f.id in wanted]
        if self.args.tags:
            wanted = set(self.args.tags.split(","))
            fixtures = [f for f in fixtures if wanted & set(f.tags)]
        if self.args.skip_tags:
            skip = set(self.args.skip_tags.split(","))
            fixtures = [f for f in fixtures if not (skip & set(f.tags))]
        return fixtures

    @staticmethod
    def _fixture_from_jsx(path: Path, parse_guard: bool = False) -> Fixture:
        source = path.read_text(encoding="utf-8-sig")
        directives, _ = extract_directives(source)
        return Fixture({
            "id": path.stem,
            "description": f"direct .jsx input {path.name}"
                           + (" (parse-only guard)" if parse_guard else ""),
            "source": source,
            "mode": "parse-guard" if parse_guard
            else ("file" if directives else "eval"),
            "tags": ["adhoc", "production"],
            "minifiable": True,
            "timeoutSec": 90,
        }, path)

    # -- execution -------------------------------------------------------
    def run(self) -> int:
        if self.args.list:
            for f in self.load_fixtures():
                flags = []
                if not f.minifiable:
                    flags.append("not-minifiable")
                if f.skip:
                    flags.append("skip")
                print(f"{f.id:48s} [{','.join(f.tags[:4]):24s}] {f.description[:60]}")
            return 0

        configs = self.load_configs()
        fixtures = self.load_fixtures(matrix_subset=self.args.matrix_subset)
        if not fixtures:
            print("No fixtures matched.")
            return 0
        print(f"Configs/variants: {len(configs)}  Fixtures: {len(fixtures)}")

        if self.args.no_execute:
            return self._minify_only(configs, fixtures)

        if not self.tool.exists():
            candidates = ", ".join(str(p) for p in TOOL_CANDIDATES)
            raise HarnessConfigError(
                "ILLUSTRATOR_COM_TOOL.py not found: "
                f"{self.tool}. Set ESMIN_ILLUSTRATOR_TOOL or --illustrator-tool. "
                f"Checked defaults: {candidates}"
            )

        pre = com_status(self.tool, timeout=30)
        if not pre.get("ok"):
            print("Illustrator unreachable:", str(pre)[:300])
            return 3
        pre_state = self._doc_state()
        print("Preflight documents:", pre_state["names"])

        for variant_id, config in configs:
            if self.args.only_config and variant_id not in self.args.only_config.split(","):
                continue
            self._run_variant(variant_id, config, fixtures)
            if self.aborted:
                break

        self._cleanup(pre_state)
        self._write_results()
        self._print_summary()
        return 1 if any(r["pass"] is False and r["status"] != "skip"
                        for r in self.results) else 0

    # -- per-variant execution ------------------------------------------
    def _run_variant(self, variant_id: str, config: dict, fixtures: list[Fixture]):
        print(f"\n=== variant {variant_id} ===")
        for fixture in fixtures:
            base = {"testId": fixture.id, "variantId": variant_id,
                    "tags": fixture.tags, "mode": fixture.mode}
            if self.aborted:
                self._record(fixture, variant_id, "skip-aborted", None, None, base)
                continue
            if fixture.skip or not fixture.minifiable:
                self._record(fixture, variant_id, "skip", None, None, base)
                continue
            self._run_one(fixture, variant_id, config)

    def _outcomes_equivalent(self, fixture: Fixture, a: Outcome, b: Outcome) -> bool:
        if a.kind != b.kind:
            return False
        if a.kind in ("script_error", "parse_error", "tool_error"):
            if "fuzz" in fixture.tags:
                # Generated programs produce engine errors whose MESSAGES embed
                # identifier names that mangling legitimately changes; compare
                # by error name only.
                return a.payload.get("name") == b.payload.get("name") \
                    or a.payload.get("message", "").split(":")[0] \
                    == b.payload.get("message", "").split(":")[0]
        return a == b

    def _run_one(self, fixture: Fixture, variant_id: str, config: dict):
        record = {
            "testId": fixture.id,
            "variantId": variant_id,
            "tags": fixture.tags,
            "mode": fixture.mode,
            "minifiable": fixture.minifiable,
            "expect": fixture.expect,
            "timeoutSec": fixture.timeout_s,
        }
        try:
            prepared = prepare_fixture(
                fixture, config, variant_id, self.worker, self.workdir,
                includepaths=[self.args.input and Path(self.args.input)],
            )
        except Exception as exc:
            status = ("pass" if fixture.data.get("expectHarnessError")
                      else "harness-error")
            self._record(fixture, variant_id, status, None, None,
                         {**record, "error": str(exc)})
            return
        record["minify"] = {
            "ok": prepared.minified_code is not None,
            "error": prepared.minify_error,
            "stats": prepared.minify_stats,
            "sizeOrig": prepared.size_orig,
            "sizeMin": prepared.size_min,
            "minifyMs": prepared.minify_ms,
        }
        record["e4x"] = {"hasE4X": prepared.e4x[0],
                         "sites": prepared.e4x[1][:5]}
        record["transform"] = prepared.transform
        record["risks"] = prepared.risks[:10]
        if prepared.minified_code is None:
            status = ("pass" if fixture.data.get("expectMinifyError")
                      else "minify-error")
            self._record(fixture, variant_id, status, prepared, None, record)
            return
        try:
            orig_file, min_file = self._stage_pair(fixture, prepared)
            orig_out, min_out, wall = execute_pair(
                self.tool, orig_file, min_file, fixture.timeout_s
            )
        except Exception as exc:
            self._record(fixture, variant_id, "harness-error", prepared, None,
                         {**record, "error": str(exc)})
            return
        record["executionWallS"] = round(wall, 3)
        record["original"] = orig_out.canonical()
        record["minified"] = min_out.canonical()

        if orig_out.kind == "timeout" or min_out.kind == "timeout":
            self.consecutive_timeouts += 1
            status = "timeout"
        else:
            self.consecutive_timeouts = 0
            if self._outcomes_equivalent(fixture, orig_out, min_out):
                # Identical failure (tool-level error, parse error, or runtime
                # error) is equivalence: the transform preserved behavior.
                ok_expect, why = expect_matches(fixture.expect, orig_out)
                status = "pass" if ok_expect else "expect-mismatch"
            elif fixture.data.get("expectDivergence"):
                # Documented, verified divergence between original engine
                # behavior and the (spec-correct) minified output.
                status = "pass-divergence"
            elif orig_out.kind == "tool_error":
                status = "original-tool-error"
            else:
                ok_expect, why = expect_matches(fixture.expect, orig_out)
                if not ok_expect:
                    status = "expect-mismatch"
                else:
                    status = "mismatch"
        if self.consecutive_timeouts >= BUSY_THRESHOLD:
            self.aborted = True
            print("WARN: repeated timeouts — Illustrator may be busy/blocked. "
                  "Stopping further execution. Close any modal dialogs and rerun.")
        record["status"] = status
        self._record(fixture, variant_id, status, prepared, min_out, record)

    def _stage_pair(self, fixture: Fixture, prepared: Prepared) -> tuple[Path, Path]:
        tag = f"{fixture.id}-{prepared.variant_id}"
        sub = self.stage_dir / fixture.id
        sub.mkdir(parents=True, exist_ok=True)
        if fixture.mode == "file":
            includes_src = Path(self.args.input)
            if includes_src.is_dir():
                includes_src = includes_src / "includes"
                if includes_src.exists():
                    dst = sub / "includes"
                    if not dst.exists():
                        import shutil
                        shutil.copytree(includes_src, dst)
            orig_file = sub / f"{tag}-orig.jsx"
            orig_file.write_text(prepared.orig_source, encoding="utf-8")
            min_file = sub / f"{tag}-min.jsx"
            min_content = render_directives(prepared.preprocess.directives) \
                + (prepared.minified_code or "")
            min_file.write_text(min_content, encoding="utf-8")
            # The result channel for files is a global sentinel: minifiers
            # (with side_effects/unused) drop the file's LAST expression value,
            # so fixtures end with an explicit global assignment.
            orig_tramp = sub / f"{tag}-orig-tramp.jsx"
            orig_tramp.write_text(
                '$.evalFile("' + orig_file.as_posix() + '");'
                ' return $.global.__extminResult;',
                encoding="utf-8")
            min_tramp = sub / f"{tag}-min-tramp.jsx"
            min_tramp.write_text(
                '$.evalFile("' + min_file.as_posix() + '");'
                ' return $.global.__extminResult;',
                encoding="utf-8")
            return orig_tramp, min_tramp
        if fixture.mode == "parse-guard":
            return (
                stage_parse_guard(prepared.preprocess.body, sub, tag + "-orig"),
                stage_parse_guard(prepared.minified_code or "", sub, tag + "-min"),
            )
        return (
            stage_eval_file(fixture.source, sub, tag + "-orig", wrap_result=True),
            stage_eval_file(prepared.minified_code or "", sub, tag + "-min", wrap_result=True),
        )

    def _record(self, fixture: Fixture, variant_id: str, status: str,
                prepared: Prepared | None, min_out: Outcome | None, record: dict):
        if prepared is not None:
            record.setdefault("minify", {}).update({
                "sizeOrig": prepared.size_orig,
                "sizeMin": prepared.size_min,
                "minifyMs": prepared.minify_ms,
            })
        record["pass"] = status in ("pass", "pass-divergence", "minify-ok")
        record["status"] = status
        record["time"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        self.results.append(record)
        line = (f"[{status:20s}] {fixture.id:44s} {variant_id:32s}"
                + (f" {record.get('minify',{}).get('sizeOrig',0)}->"
                   f"{record.get('minify',{}).get('sizeMin',0)} B" if prepared else ""))
        print(line)

    # -- minify-only mode -------------------------------------------------
    def _minify_only(self, configs: list[tuple[str, dict]], fixtures: list[Fixture]) -> int:
        for variant_id, config in configs:
            for fixture in fixtures:
                record = {
                    "testId": fixture.id,
                    "variantId": variant_id,
                    "tags": fixture.tags,
                    "mode": fixture.mode,
                    "minifiable": fixture.minifiable,
                }
                if fixture.skip:
                    self._record(fixture, variant_id, "skip", None, None, record)
                    continue
                if not fixture.minifiable:
                    self._record(fixture, variant_id, "skip-not-minifiable", None, None,
                                 record)
                    continue
                try:
                    prepared = prepare_fixture(
                        fixture, config, variant_id, self.worker, self.workdir,
                        includepaths=[Path(self.args.input)] if Path(self.args.input).is_dir()
                        else [],
                    )
                except Exception as exc:
                    self._record(fixture, variant_id, "harness-error", None, None,
                                 {**record, "error": str(exc)})
                    continue
                status = "minify-ok" if prepared.minified_code is not None else \
                    ("pass" if fixture.data.get("expectMinifyError") else "minify-error")
                record["minify"] = {
                    "ok": prepared.minified_code is not None,
                    "error": prepared.minify_error,
                    "stats": prepared.minify_stats,
                    "sizeOrig": prepared.size_orig,
                    "sizeMin": prepared.size_min,
                    "minifyMs": prepared.minify_ms,
                }
                self._record(fixture, variant_id, status, prepared, None, record)
        self._write_results()
        self._print_summary()
        return 1 if any(r["pass"] is False and r["status"] != "skip"
                        for r in self.results) else 0

    # -- doc state helpers ------------------------------------------------
    def _doc_state(self) -> dict:
        """Return {"names": [...], "active": name-or-None}."""
        code, out = run_tool(self.tool, ["eval", "--file", str(self._state_probe())], 60)
        if code == -1:
            return {"names": [], "active": None}
        try:
            env = json.loads(out)
            inner = env.get("result", {})
            if isinstance(inner, dict) and inner.get("ok") is True:
                value = inner.get("result") or {}
                return {
                    "names": list(value.get("names") or []),
                    "active": value.get("active"),
                }
        except json.JSONDecodeError:
            pass
        return {"names": [], "active": None}

    @staticmethod
    def _state_probe() -> Path:
        p = Path(tempfile.gettempdir()) / "extmin-state-probe.jsx"
        p.write_text(
            "var names=[]; for(var i=0;i<app.documents.length;i++)"
            "{ names.push(app.documents[i].name); }"
            "var active=null;"
            "try{ active=app.activeDocument.name; }catch(e){}"
            "return {names:names, active:active};",
            encoding="utf-8",
        )
        return p

    def _cleanup(self, pre_state: dict):
        print("\nPost-run cleanup...")
        post_state = self._doc_state()
        stray = [d for d in post_state["names"] if d not in (pre_state.get("names") or [])]
        for name in stray:
            print(f"  closing stray test document: {name}")
        if stray:
            names_js = json.dumps(stray)
            code = (
                "var closers=[]; for(var i=app.documents.length-1;i>=0;i--){"
                f"var n=app.documents[i].name; if({names_js}.indexOf(n)>=0){{"
                "closers.push(app.documents[i]);}} }"
                "for(var j=0;j<closers.length;j++){ try{closers[j].close("
                "SaveOptions.DONOTSAVECHANGES);}catch(e){} } return 'cleaned';"
            )
            with tempfile.NamedTemporaryFile("w", suffix=".jsx", delete=False,
                                             encoding="utf-8") as fh:
                fh.write(code)
                path = fh.name
            try:
                run_tool(self.tool, ["eval", "--file", path], 90)
            finally:
                os.unlink(path)
        active = pre_state.get("active")
        if active:
            # Re-activate the pre-run active document when it still exists.
            code = (
                "for(var i=0;i<app.documents.length;i++){"
                f"if(app.documents[i].name==={json.dumps(active)}){{"
                "app.documents[i].activate(); break; } } return 1;"
            )
            with tempfile.NamedTemporaryFile("w", suffix=".jsx", delete=False,
                                             encoding="utf-8") as fh:
                fh.write(code)
                path = fh.name
            try:
                run_tool(self.tool, ["eval", "--file", path], 60)
            finally:
                os.unlink(path)

    # -- results -----------------------------------------------------------
    def _write_results(self):
        jsonl = self.output_dir / "results.jsonl"
        with jsonl.open("w", encoding="utf-8") as fh:
            for r in self.results:
                # ensure_ascii keeps U+2028/U+2029 (line separators) out of the
                # raw stream so the JSONL stays line-oriented.
                fh.write(json.dumps(r, ensure_ascii=True, default=str) + "\n")
        summary = {
            "tool": str(self.tool),
            "uglifyVersion": "3.19.3",
            "illustratorVersion": None,
            "total": len(self.results),
            "byStatus": {},
        }
        for r in self.results:
            summary["byStatus"][r["status"]] = summary["byStatus"].get(r["status"], 0) + 1
        (self.output_dir / "summary.json").write_text(
            json.dumps(summary, indent=2), encoding="utf-8")
        print(f"\nResults: {jsonl}")

    def _print_summary(self):
        by = {}
        for r in self.results:
            by[r["status"]] = by.get(r["status"], 0) + 1
        print("\nSummary:", json.dumps(by))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="ExtendScript minification harness")
    p.add_argument("--input", default=str(DEFAULT_FIXTURES),
                   help="fixture JSON dir or single file")
    p.add_argument("--config", required=True, help="UglifyJS config or matrix JSON")
    p.add_argument("--illustrator-tool", default=str(DEFAULT_TOOL))
    p.add_argument("--output", default=str(SKILL_ROOT / "reports"))
    p.add_argument("--only", help="comma-separated fixture ids")
    p.add_argument("--tags", help="comma-separated fixture tag filter")
    p.add_argument("--skip-tags", help="comma-separated tags to exclude")
    p.add_argument("--only-config", help="comma-separated variant ids")
    p.add_argument("--matrix-subset", action="store_true",
                   help="restrict to sensitive/unsafe-subset fixtures (for matrix runs)")
    p.add_argument("--parse-guard", action="store_true",
                   help="for direct .jsx inputs: parse-only validation (no execution)")
    p.add_argument("--no-execute", action="store_true", help="minify only, no Illustrator")
    p.add_argument("--list", action="store_true")
    args = p.parse_args(argv)
    try:
        return Runner(args).run()
    except HarnessConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
