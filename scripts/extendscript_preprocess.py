#!/usr/bin/env python3
"""extendscript_preprocess.py — Adobe-dialect preprocessing for the minification harness.

Responsibilities (this module is the only place that understands Adobe
preprocessor syntax):

1. Extract Adobe preprocessor directives (`#target`, `#targetengine`, `#include`,
   `#includepath`, `#strict`, and comment forms `//@target` etc.) from the top of
   a source file.
2. Resolve `#include` directives recursively to produce a self-contained
   plain-JavaScript body.
3. Detect E4X XML literals (conservative heuristic; UglifyJS parse failure is
   the authoritative detector).
4. Scan for dynamic-reference hazards (`eval`, `with`, `new Function`,
   `Function.prototype.toString`, string-generated callbacks).
5. Restore directives at the top of a generated `.jsx` file.

Nothing here executes Illustrator. Everything is pure text/AST-level analysis.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Directive parsing
# ---------------------------------------------------------------------------

_DIRECTIVE_RE = re.compile(
    r"^(?P<comment>\s*//)?\s*"
    r"(?P<directive>#(?:target|targetengine|include|includepath|strict))\b"
    r"(?P<rest>[^\r\n]*)$"
)

_DIRECTIVE_NAMES = frozenset({"target", "targetengine", "include", "includepath", "strict"})

# `#target` and `#include` may appear in comment form; `#targetengine`,
# `#includepath`, `#strict` are documented in `#` form but accepted in comment
# form by the harness (marked with the comment flag).

_COMMENT_FORM_SUPPORTED = frozenset({"target", "include", "targetengine", "includepath", "strict"})


@dataclass
class Directive:
    name: str
    value: str = ""
    comment: bool = False
    line: int = 0


@dataclass
class PreprocessResult:
    body: str = ""
    directives: list[Directive] = field(default_factory=list)
    resolved_includes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    has_e4x: bool = False
    e4x_sites: list[dict] = field(default_factory=list)
    risks: list[dict] = field(default_factory=list)


def extract_directives(source: str) -> tuple[list[Directive], str]:
    """Return (directives, remaining_body).

    Only directives at the very start of the file (before any code) are
    recognized, matching ExtendScript semantics: Adobe directives must appear
    before any other statement.
    """
    lines = source.splitlines(keepends=True)
    directives: list[Directive] = []
    i = 0
    while i < len(lines):
        m = _DIRECTIVE_RE.match(lines[i])
        if not m:
            break
        name = m.group("directive")[1:]
        rest = m.group("rest").strip().strip('"').strip()
        directives.append(
            Directive(name=name, value=rest, comment=bool(m.group("comment")), line=i + 1)
        )
        i += 1
    body = "".join(lines[i:])
    return directives, body


def render_directives(directives: list[Directive]) -> str:
    """Render directives back to text (canonical `#` form)."""
    lines = []
    for d in directives:
        if d.name == "target":
            lines.append(f'#target {d.value}')
        elif d.name == "targetengine":
            lines.append(f'#targetengine "{d.value}"' if d.value else '#targetengine "main"')
        elif d.name == "include":
            lines.append(f'#include "{d.value}"')
        elif d.name == "includepath":
            lines.append(f'#includepath "{d.value}"')
        elif d.name == "strict":
            lines.append(f'#strict {d.value}' if d.value else "#strict on")
    return "\n".join(lines) + ("\n" if lines else "")


# ---------------------------------------------------------------------------
# Include resolution
# ---------------------------------------------------------------------------

_INCLUDE_RE = re.compile(r'^#include\s+"(?P<path>[^"]+)"\s*$')
_INCLUDE_COMMENT_RE = re.compile(r'^//@include\s+"(?P<path>[^"]+)"\s*$')
# Directives inside INCLUDED files are dropped (they belong to the main file's
# header); only #include is processed recursively.
_DROP_DIRECTIVE_RE = re.compile(
    r"^\s*(?://@)?#(?:target|targetengine|includepath|strict)\b"
)

MAX_INCLUDE_DEPTH = 32
MAX_INCLUDE_FILES = 200


def _read_utf8(path: Path) -> str:
    data = path.read_bytes()
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    return data.decode("utf-8", errors="strict")


def resolve_includes(
    source: str,
    base_dir: Path,
    includepaths: list[Path],
    *,
    stack: list[Path],
    resolved: list[str],
    warnings: list[str],
    depth: int = 0,
) -> str:
    """Recursively inline `#include` directives.

    Duplicate includes: ExtendScript evaluates the included file every time the
    directive is reached; the harness preserves that behavior (no dedup) but
    guards against circular includes via *stack*.
    """
    if depth > MAX_INCLUDE_DEPTH:
        raise ValueError("include depth exceeded (circular includes?)")
    if len(resolved) > MAX_INCLUDE_FILES:
        raise ValueError("include count exceeded")
    lines = source.splitlines(keepends=True)
    out: list[str] = []
    for line in lines:
        m = _INCLUDE_RE.match(line) or _INCLUDE_COMMENT_RE.match(line)
        if m:
            inc_rel = m.group("path")
            inc_path = None
            candidates = [base_dir / inc_rel] + [p / inc_rel for p in includepaths]
            for cand in candidates:
                if cand.exists():
                    inc_path = cand
                    break
            if inc_path is None:
                raise FileNotFoundError(f"#include not found: {inc_rel} (searched {candidates})")
            if inc_path in stack:
                raise ValueError(f"circular #include: {inc_rel}")
            included = _read_utf8(inc_path)
            stack.append(inc_path)
            out.append(resolve_includes(
                included, inc_path.parent, includepaths,
                stack=stack, resolved=resolved, warnings=warnings, depth=depth + 1,
            ))
            stack.pop()
            continue
        if _DROP_DIRECTIVE_RE.match(line):
            continue
        out.append(line)
    return "".join(out)


# ---------------------------------------------------------------------------
# E4X detection
# ---------------------------------------------------------------------------

# Heuristic: an XML literal begins with `<` followed by a letter, `_`, `?`, `!`,
# or `/` when the `<` appears in expression position. False positives possible
# (e.g. `a < b` in expressions); the authoritative check is UglifyJS parse
# failure combined with manual inspection. We scan line-oriented for the common
# patterns: `= <`, `( <`, `return <`, `, <`, `[ <`, `: <`, `; <` at line level.
def scan_e4x(source: str) -> tuple[bool, list[dict]]:
    """E4X scan that skips strings, regex literals, and comments.

    Returns (has_e4x, sites). Line-based heuristics alone produce false
    positives (e.g. `".../" + name` inside strings), so the scan tokenizes
    with the same skipping rules as the switch fixer.
    """
    sites: list[dict] = []
    n = len(source)
    i = 0
    prev_sig = ""
    line = 1
    while i < n:
        c = source[i]
        if c == "\n":
            line += 1
            i += 1
            continue
        if c.isspace():
            i += 1
            continue
        m = _STRING_LIT_RE.match(source, i)
        if m:
            prev_sig = source[i:m.end()]
            i = m.end()
            continue
        if source.startswith("//", i):
            m = _LINE_COMMENT_LIT_RE.match(source, i)
            i = m.end()
            continue
        if source.startswith("/*", i):
            m = _BLOCK_COMMENT_LIT_RE.match(source, i)
            i = m.end()
            continue
        if c == "/" and not (prev_sig and (prev_sig[-1].isalnum()
                                           or prev_sig[-1] in "_$)]}")):
            m = _REGEX_LIT_RE.match(source, i)
            if m:
                prev_sig = source[i:m.end()]
                i = m.end()
                continue
        if c == "<" and i + 1 < n \
                and (source[i + 1].isalpha() or source[i + 1] in "_!?"):
            prev = prev_sig[-1] if prev_sig else ""
            if prev in "=([{:;,!" or prev_sig in ("return", "case", "default"):
                sites.append({
                    "line": line,
                    "snippet": source[max(0, i - 20):i + 60].strip()[:120],
                })
        prev_sig = c
        i += 1
    return bool(sites), sites


# ---------------------------------------------------------------------------
# Risk scanning
# ---------------------------------------------------------------------------

_RISK_PATTERNS = [
    ("eval", re.compile(r"\beval\s*\("), "high",
     "Dynamic code evaluation; string-based symbol references cannot be tracked statically. Verify eval content and that the evaluated code does not depend on mangled names."),
    ("with", re.compile(r"\bwith\s*\("), "high",
     "with-scope property resolution is invisible to UglifyJS name analysis. Ensure identifiers inside with blocks are reserved or verify runtime equivalence."),
    ("Function", re.compile(r"\bnew\s+Function\s*\("), "high",
     "String-compiled code bypasses scope analysis entirely."),
    ("toString", re.compile(r"\.toString\s*\("), "medium",
     "Function.prototype.toString output changes after minification; code depending on source text breaks."),
    ("apply", re.compile(r"\.apply\s*\("), "low",
     "apply argument arrays are fine, but combined with arguments-object aliasing needs care."),
    ("call", re.compile(r"\.call\s*\("), "low",
     "call is safe under mangling; listed for completeness."),
]


def scan_risks(source: str) -> list[dict]:
    risks: list[dict] = []
    for name, pattern, severity, explanation in _RISK_PATTERNS:
        for m in pattern.finditer(source):
            line = source.count("\n", 0, m.start()) + 1
            risks.append({
                "pattern": name, "line": line, "severity": severity,
                "explanation": explanation,
                "snippet": source[max(0, m.start() - 40): m.start() + 60].replace("\n", " "),
            })
    return risks


# ---------------------------------------------------------------------------
# Switch-statement output fixes (Illustrator ExtendScript parser bugs)
# ---------------------------------------------------------------------------
#
# VERIFIED on Illustrator 30.6.0: the ExtendScript switch parser requires
#   1. every case clause to END with an explicit `;` or a `}` (no ASI at the
#      clause boundary): `case 1: f()}` -> "Error 25: Expected: ;."
#   2. at least ONE non-empty statement in the whole switch body:
#      `switch(x){}`, `switch(x){default:;}` -> "Error 25: Expected: ;."
# UglifyJS's printer drops the trailing semicolon before `}` and happily emits
# empty switches, so minified output needs this repair pass.

_STRING_LIT_RE = re.compile(r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"")
_LINE_COMMENT_LIT_RE = re.compile(r"//[^\r\n]*")
_BLOCK_COMMENT_LIT_RE = re.compile(r"/\*[\s\S]*?\*/")
_REGEX_LIT_RE = re.compile(r"/(?![*/=])(?:\\.|\[(?:[^\]\\]|\\.)*\]|[^/\\\n])*/[a-z]*")

_EXPR_END_CHARS = set(")]} \t\r\n")
_EXPR_END_KEYWORDS = ("true", "false", "null", "this")


def _is_regex_start(js: str, i: int, prev_sig: str) -> bool:
    if prev_sig and (prev_sig[-1].isalnum() or prev_sig[-1] in "_$)]}"):
        return False
    if prev_sig in _EXPR_END_KEYWORDS:
        return False
    return True


def fix_extendscript_switch(js: str) -> str:
    """Repair minified output for the ExtendScript switch-parser bugs."""
    out = list(js)
    insert_before: dict[int, str] = {}
    insert_after: dict[int, str] = {}
    paren_stack: list[bool] = []
    brace_stack: list[dict] = []
    expect_switch_paren = False
    prev_sig = ""  # last significant token text
    i = 0
    n = len(js)
    while i < n:
        c = js[i]
        if c.isspace():
            i += 1
            continue
        m = _STRING_LIT_RE.match(js, i)
        if m:
            prev_sig = js[i:m.end()]
            i = m.end()
            continue
        if js.startswith("//", i) or js.startswith("/*", i):
            m = (_LINE_COMMENT_LIT_RE if js.startswith("//", i)
                 else _BLOCK_COMMENT_LIT_RE).match(js, i)
            i = m.end()
            continue
        if c == "/" and _is_regex_start(js, i, prev_sig):
            m = _REGEX_LIT_RE.match(js, i)
            if m:
                prev_sig = js[i:m.end()]
                i = m.end()
                continue
        if c.isalpha() or c == "_" or c == "$":
            j = i
            while j < n and (js[j].isalnum() or js[j] in "_$"):
                j += 1
            word = js[i:j]
            if word == "switch":
                expect_switch_paren = True
            if brace_stack and brace_stack[-1]["isSwitch"]:
                if word in ("case", "default"):
                    brace_stack[-1]["labelMode"] = True
                elif not brace_stack[-1]["labelMode"]:
                    brace_stack[-1]["nonEmpty"] = True
            prev_sig = word
            i = j
            continue
        if c.isdigit():
            j = i
            while j < n and (js[j].isalnum() or js[j] in "._xX"):
                j += 1
            prev_sig = js[i:j]
            i = j
            continue
        if c == "(":
            paren_stack.append(expect_switch_paren)
            expect_switch_paren = False
            prev_sig = c
            i += 1
            continue
        if c == ")":
            prev_sig = c
            i += 1
            continue
        if c == "{":
            is_switch = bool(paren_stack) and paren_stack[-1] and prev_sig == ")"
            if paren_stack:
                paren_stack.pop()  # the paren group ends at the opening brace
            brace_stack.append({
                "isSwitch": is_switch,
                "openIdx": i,
                "nonEmpty": False,
                "labelMode": False,
            })
            prev_sig = c
            i += 1
            continue
        if paren_stack and prev_sig == ")":
            # A non-brace token ends a paren group (e.g. `if (x) stmt;`).
            paren_stack.pop()
        if c == "}":
            if brace_stack:
                entry = brace_stack.pop()
                if entry["isSwitch"]:
                    if not entry["nonEmpty"]:
                        # Inject a harmless real statement so the body parses.
                        insert_after[entry["openIdx"]] = "case 0:break;"
                    elif prev_sig and prev_sig[-1] not in ";}" \
                            and not prev_sig.endswith("}"):
                        insert_before[i] = ";"
            prev_sig = c
            i += 1
            continue
        if c == ";":
            # `;` alone marks an empty statement (not counted as non-empty).
            prev_sig = c
            i += 1
            continue
        if c == ":":
            if brace_stack and brace_stack[-1]["isSwitch"]:
                brace_stack[-1]["labelMode"] = False
            prev_sig = c
            i += 1
            continue
        if brace_stack and brace_stack[-1]["isSwitch"] \
                and not brace_stack[-1]["labelMode"]:
            # Any other significant token inside a switch body is a real
            # statement (or its operand); mark the body non-empty.
            brace_stack[-1]["nonEmpty"] = True
        prev_sig = c
        i += 1
    # Apply all insertions in ONE rebuild pass: insertion positions were
    # computed against the ORIGINAL text, so mutating the list incrementally
    # (insert_before first, then insert_after) would shift indices and corrupt
    # the output. Merge both maps and rebuild the string directly.
    merged: dict[int, list[str]] = {}
    for pos in insert_before:
        merged.setdefault(pos, []).append(insert_before[pos])
    for pos in insert_after:
        merged.setdefault(pos + 1, []).append(insert_after[pos])
    if not merged:
        return js
    parts: list[str] = []
    last = 0
    for pos in sorted(merged):
        parts.append(js[last:pos])
        parts.extend(merged[pos])
        last = pos
    parts.append(js[last:])
    return "".join(parts)


# ---------------------------------------------------------------------------
# Top-level API
# ---------------------------------------------------------------------------

def preprocess_source(
    source: str, base_dir: Path, includepaths: list[Path] | None = None
) -> PreprocessResult:
    """Preprocessing pipeline operating on source text (directive extraction,
    include resolution, E4X scan, risk scan)."""
    result = PreprocessResult()
    directives, body = extract_directives(source)
    result.directives = directives

    for d in directives:
        if d.name not in _DIRECTIVE_NAMES:
            result.warnings.append(f"unknown directive # {d.name} (line {d.line})")
        elif d.comment and d.name not in _COMMENT_FORM_SUPPORTED:
            result.warnings.append(f"comment-form # {d.name} is not supported by the harness")

    includepaths = list(includepaths or [])
    for d in directives:
        if d.name == "includepath":
            includepaths.append(base_dir / d.value)

    if any(d.name == "include" for d in directives) or "#include" in body or "//@include" in body:
        body = resolve_includes(
            body, base_dir, includepaths,
            stack=[base_dir / "<main>"], resolved=[], warnings=result.warnings,
        )

    result.body = body
    result.has_e4x, result.e4x_sites = scan_e4x(body)
    result.risks = scan_risks(body)
    return result


def preprocess_file(path: Path, includepaths: list[Path] | None = None) -> PreprocessResult:
    """Convenience: preprocess a .jsx/.jsxinc file on disk."""
    return preprocess_source(_read_utf8(path), path.resolve().parent, includepaths)


def bundle_file(path: Path, includepaths: list[Path] | None = None) -> PreprocessResult:
    """Convenience: preprocess + render final .jsx text.

    Returns result with `body` being the full final text (directives restored
    on top of the minified body is the caller's job — this returns directives
    and the plain body separately).
    """
    return preprocess_file(path, includepaths)


def to_json(result: PreprocessResult) -> str:
    return json.dumps({
        "directives": [
            {"name": d.name, "value": d.value, "comment": d.comment, "line": d.line}
            for d in result.directives
        ],
        "hasE4X": result.has_e4x,
        "e4xSites": result.e4x_sites,
        "risks": result.risks,
        "warnings": result.warnings,
    }, indent=2)


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    result = preprocess_file(Path(sys.argv[1]))
    print(to_json(result))
