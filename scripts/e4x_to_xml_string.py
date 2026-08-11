#!/usr/bin/env python3
"""e4x_to_xml_string.py — E4X XML-literal to `new XML("...")` transformer.

ExtendScript (ES3 + E4X) supports XML literals:
    var x = <root><item id="1">value</item></root>;
    var v = x.item.@id;
Ordinary JavaScript parsers (UglifyJS, Terser, Closure) reject XML literals.
This module rewrites XML literals into ES3-safe `new XML("...")` constructor
calls so the rest of the pipeline (UglifyJS) can process the file.

Scope and guarantees
--------------------
* Tokenizes the JavaScript source (strings, regex literals, comments) and
  finds `<` tokens in EXPRESSION position only.
* Converts XML literals WITHOUT embedded `{expression}` sections into
  `new XML("literal text")`.
* Converts literals WITH `{expression}` sections into string concatenation:
      new XML("a" + (expr) + "b")
  Runtime semantics of `{expr}` insertion (toString coercion) match string
  concatenation for numbers, strings, booleans, and XML values.
* PRESERVES the literal text byte-for-byte (entities, CDATA, comments, PIs,
  whitespace) inside the string.

Explicit rejections (returned in `rejected`, source left unmodified — callers
must fall back to the unminified path):
* `<` in expression position that is actually a less-than operator (the
  tokenizer's context tracking is conservative).
* Malformed/ambiguous literals that cannot be parsed by the module.
* Unsupported E4X constructs embedded in the literal (e.g. an unbalanced
  `{` inside attribute text is valid E4X but out of scope here).

The module never evaluates anything; it is pure text transformation.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------

# Characters that can follow a `<` that begins an XML literal in E4X.
_XML_START = ("a-zA-Z_!")
# Less-than is an operator after these: identifiers, numbers, strings, ) ] }
_XML_POSITION_PRECEDING = set("=([{:;,!&|?+-*%<>~^")


@dataclass
class Token:
    kind: str      # 'string' | 'regex' | 'comment' | 'other' | 'e4x'
    start: int
    end: int
    text: str = ""
    replacement: str | None = None


@dataclass
class TransformResult:
    transformed: str = ""
    replacements: list = field(default_factory=list)
    rejected: list = field(default_factory=list)
    literal_count: int = 0


_STRING_RE = re.compile(r"""'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*" """, re.X)
_LINE_COMMENT_RE = re.compile(r"//[^\r\n]*")
_BLOCK_COMMENT_RE = re.compile(r"/\*[\s\S]*?\*/")
# Conservative regex literal guess: starts with /, not /*, not //, not /=.
_REGEX_RE = re.compile(r"/(?![*/=])(?:\\.|\[(?:[^\]\\]|\\.)*\]|[^/\\\n])*/[a-z]*")


def _next_token(src: str, i: int, prev_ends_expr: bool) -> tuple[Token, int] | None:
    """Return the next non-whitespace token starting at i, or None at EOF."""
    m = _STRING_RE.match(src, i)
    if m:
        return Token("string", i, m.end()), m.end()
    if src.startswith("//", i):
        m = _LINE_COMMENT_RE.match(src, i)
        return Token("comment", i, m.end()), m.end()
    if src.startswith("/*", i):
        m = _BLOCK_COMMENT_RE.match(src, i)
        return Token("comment", i, m.end()), m.end()
    if src[i] == "/" and not prev_ends_expr:
        m = _REGEX_RE.match(src, i)
        if m:
            return Token("regex", i, m.end()), m.end()
    return Token("other", i, i + 1), i + 1


# ---------------------------------------------------------------------------
# XML literal parsing
# ---------------------------------------------------------------------------

def _skip_xml_text(src: str, i: int, out: list[str], state: dict) -> int:
    """Consume text between XML markup. Returns the new index."""
    while i < len(src):
        if src.startswith("<![CDATA[", i):
            end = src.find("]]>", i)
            if end == -1:
                state["error"] = "unterminated CDATA"
                return i
            out.append(src[i:end + 3])
            i = end + 3
            continue
        if src.startswith("<!--", i):
            end = src.find("-->", i)
            if end == -1:
                state["error"] = "unterminated XML comment"
                return i
            out.append(src[i:end + 3])
            i = end + 3
            continue
        if src.startswith("<?", i):
            end = src.find("?>", i)
            if end == -1:
                state["error"] = "unterminated processing instruction"
                return i
            out.append(src[i:end + 2])
            i = end + 2
            continue
        c = src[i]
        if c == "<":
            return i
        if c == "{":
            # Embedded expression: balanced-brace scan (strings inside are
            # handled by simple balancing; conservative).
            depth = 1
            j = i + 1
            while j < len(src) and depth:
                if src[j] == "{":
                    depth += 1
                elif src[j] == "}":
                    depth -= 1
                j += 1
            if depth:
                state["error"] = "unbalanced { in embedded expression"
                return i
            state["embeds"].append((i, j))
            out.append(src[i:j])
            i = j
            continue
        if c == "}" and not state["embeds"]:
            # Literal text outside embedded expr cannot contain stray }
            state["error"] = "stray } in XML literal"
            return i
        out.append(c)
        i += 1
    return i


def parse_xml_literal(src: str, start: int) -> tuple[dict, int] | None:
    """Parse an XML literal beginning at src[start] == '<'.

    Returns (info, end) with info = {text_parts, embeds, tags, ok} or None
    if the construct is not a well-formed literal we can convert.
    """
    state = {"error": None, "embeds": []}
    text_parts: list[str] = []
    tags: list[str] = []
    i = start
    while True:
        if i >= len(src):
            state["error"] = "unterminated XML literal"
            break
        c = src[i]
        if c != "<":
            # Stray text outside a tag is not a literal start we recognize.
            state["error"] = "unexpected text in literal"
            break
        if src.startswith("</", i):  # closing tag
            m = re.match(r"</\s*([A-Za-z_][\w:.-]*)\s*>", src[i:])
            if not m:
                state["error"] = "malformed closing tag"
                break
            name = m.group(1)
            if not tags or tags[-1] != name:
                state["error"] = f"mismatched closing tag </{name}>"
                break
            tags.pop()
            i += m.end()
            if not tags:
                return _finish_literal(src, start, i, state, text_parts), i
            continue
        if src.startswith("<![CDATA[", i) or src.startswith("<!--", i) \
                or src.startswith("<?", i):
            # markup inside a tag body handled by text skipper below
            i = _skip_xml_text(src, i, text_parts, state)
            if state["error"]:
                break
            continue
        m = re.match(r"<([A-Za-z_][\w:.-]*)\b", src[i:])
        if not m:
            state["error"] = "malformed opening tag"
            break
        name = m.group(1)
        tag_text = m.group(0)
        j = i + m.end()
        # attributes (quoted values only; embedded {expr} values unsupported)
        while True:
            while j < len(src) and src[j] in " \t\r\n":
                tag_text += src[j]
                j += 1
            if src.startswith("/>", j):
                tag_text += "/>"
                j += 2
                break
            if src[j] == ">":
                tag_text += ">"
                j += 1
                break
            am = re.match(r"[A-Za-z_][\w:.-]*\s*=\s*", src[j:])
            if not am:
                state["error"] = "unsupported attribute syntax"
                break
            tag_text += am.group(0)
            j += am.end()
            if src[j] in "\"'":
                q = src[j]
                end = src.find(q, j + 1)
                if end == -1:
                    state["error"] = "unterminated attribute value"
                    break
                tag_text += src[j:end + 1]
                j = end + 1
            else:
                state["error"] = "attribute value must be quoted"
                break
        if state["error"]:
            break
        text_parts.append(tag_text)
        if tag_text.endswith("/>"):
            # Self-closing tag: the literal ends here unless a sibling tag
            # follows immediately (E4X XMLList of adjacent literals).
            i = j
            if i >= len(src) or src[i] != "<":
                return _finish_literal(src, start, i, state, text_parts), i
            continue
        tags.append(name)
        i = j
        i = _skip_xml_text(src, i, text_parts, state)
        if state["error"]:
            break
    return None


def _finish_literal(src: str, start: int, end: int, state: dict,
                    text_parts: list[str]) -> dict:
    embeds = state["embeds"]
    return {
        "ok": True,
        "start": start,
        "end": end,
        "text_parts": list(text_parts),
        "embeds": embeds,
        "raw": src[start:end],
    }


def _js_string(text: str) -> str:
    """Encode literal text as a JS string literal (single-quoted, escaped)."""
    single = text.count("'")
    double = text.count('"')
    quote = '"' if single > double and double == 0 else "'"
    out = [quote]
    for ch in text:
        if ch == quote:
            out.append("\\" + quote)
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 32:
            out.append("\\x%02x" % ord(ch))
        else:
            out.append(ch)
    out.append(quote)
    return "".join(out)


def _build_replacement(info: dict) -> str | None:
    """Build `new XML(...)` (or concatenation) from parsed literal info."""
    if not info["embeds"]:
        # Byte-for-byte source text is the most faithful representation.
        return "new XML(" + _js_string(info["raw"]) + ")"
    # Reconstruct with embedded expressions as concatenation parts.
    parts = []
    text_parts = info["text_parts"]
    # text_parts contains tag text and plain text; embeds are (start,end) spans
    # into the RAW literal; we reconstruct by walking raw text.
    raw = info["raw"]
    pieces: list[str] = []
    expr_parts = []
    i = 0
    for (es, ee) in info["embeds"]:
        abs_s = es - info["start"]
        abs_e = ee - info["start"]
        if abs_s > i:
            pieces.append(raw[i:abs_s])
        pieces.append(None)  # marker for expression
        expr_parts.append(raw[abs_s + 1:abs_e - 1])
        i = abs_e
    if i < len(raw):
        pieces.append(raw[i:])
    segs = []
    expr_idx = 0
    for piece in pieces:
        if piece is None:
            segs.append("(" + expr_parts[expr_idx] + ")")
            expr_idx += 1
        elif piece:
            segs.append(_js_string(piece))
    if len(segs) == 1 and not expr_parts:
        return "new XML(" + segs[0] + ")"
    return "new XML(" + " + ".join(segs) + ")"


# ---------------------------------------------------------------------------
# Main transform
# ---------------------------------------------------------------------------

_IDENT_RE = re.compile(r"[A-Za-z_$][\w$:-]*")

_DEFAULT_NS_RE = re.compile(r"^\s*default\s+xml\s+namespace\s*=")


def transform_e4x(source: str) -> TransformResult:
    """Rewrite XML literals and E4X accessors to ES3-safe equivalents.

    Rewrites:
      * XML literals       -> new XML("...")  /  new XML("a" + (expr) + "b")
      * expr.@attr         -> expr.attribute("attr")
      * expr.@*            -> expr.attribute("*")
      * expr..desc         -> expr.descendants("desc")

    Rejects (records in `rejected`; source left untouched for those sites):
      * `default xml namespace = ...` statements (no ES3 equivalent).
      * Malformed/ambiguous literals and attribute-access shapes.

    Callers must not minify output that contains rejections; fall back to the
    unminified path.
    """
    result = TransformResult()
    out: list[str] = []
    i = 0
    prev_token_kind = None
    while i < len(source):
        c = source[i]
        if c.isspace():
            out.append(c)
            i += 1
            continue
        tok, i2 = _next_token(source, i, prev_token_kind == "other_end_expr")
        if tok.kind in ("string", "regex", "comment"):
            out.append(source[i:i2])
            prev_token_kind = "string" if tok.kind == "string" else "comment"
            i = i2
            continue
        # 'other' token: single char
        ch = source[i]
        prev_was_expr = prev_token_kind in ("expr_end", "string", "ident")
        if ch == "<" and not prev_was_expr and i + 1 < len(source) \
                and (source[i + 1].isalpha() or source[i + 1] in "_!"):
            info, end = parse_xml_literal(source, i)
            if info and info.get("ok"):
                repl = _build_replacement(info)
                if repl is None:
                    result.rejected.append({
                        "start": i, "end": end,
                        "reason": "unsupported literal shape",
                    })
                    out.append(source[i:end])
                else:
                    result.replacements.append(
                        {"start": i, "end": end, "replacement": repl})
                    result.literal_count += 1
                    out.append(repl)
                    prev_token_kind = "expr_end"
                    i = end
                    continue
            else:
                # Ambiguous '<' (likely less-than); keep as-is but record.
                result.rejected.append({
                    "start": i, "end": i + 1, "reason": "ambiguous <",
                })
                out.append(ch)
                prev_token_kind = "other"
                i += 1
                continue
        if ch == "." and i + 1 < len(source):
            if source[i + 1] == "@":
                # attribute access .@name  / .@*
                if prev_token_kind == "ident" or prev_token_kind == "expr_end":
                    m = _IDENT_RE.match(source, i + 2)
                    if m and (m.group(0) != "" or source[i + 2:i + 3] == "*"):
                        end = m.end() if m.group(0) else i + 3
                        name = m.group(0) if m.group(0) else "*"
                        repl = f'.attribute("{name}")'
                        result.replacements.append(
                            {"start": i, "end": end, "replacement": repl})
                        out.append(repl)
                        prev_token_kind = "expr_end"
                        i = end
                        continue
                result.rejected.append({
                    "start": i, "end": i + 2, "reason": "unsupported attribute access",
                })
                out.append(ch)
                i += 1
                prev_token_kind = "other"
                continue
            if source[i + 1] == "." and prev_token_kind in ("ident", "expr_end"):
                # descendant access ..name / ..*
                m = _IDENT_RE.match(source, i + 2)
                if m and m.group(0):
                    end = m.end()
                    repl = f'.descendants("{m.group(0)}")'
                    result.replacements.append(
                        {"start": i, "end": end, "replacement": repl})
                    out.append(repl)
                    prev_token_kind = "expr_end"
                    i = end
                    continue
                if source[i + 2:i + 3] == "*":
                    repl = '.descendants("*")'
                    result.replacements.append(
                        {"start": i, "end": i + 3, "replacement": repl})
                    out.append(repl)
                    prev_token_kind = "expr_end"
                    i = i + 3
                    continue
        if c == "d" and _DEFAULT_NS_RE.match(source[i:]):
            result.rejected.append({
                "start": i,
                "end": min(len(source), i + 40),
                "reason": "default xml namespace statement has no ES3 equivalent",
            })
            # Emit the line as-is; the caller must fall back to unminified.
            line_end = source.find("\n", i)
            line_end = len(source) if line_end == -1 else line_end
            out.append(source[i:line_end + 1])
            i = line_end + 1
            prev_token_kind = "other"
            continue
        out.append(ch)
        if ch.isdigit():
            prev_token_kind = "number"
        elif ch.isalnum() or ch in "_$":
            prev_token_kind = "ident"
        elif ch in ")]}":
            prev_token_kind = "expr_end"
        else:
            prev_token_kind = "other"
        i += 1
    result.transformed = "".join(out)
    return result


if __name__ == "__main__":
    import json
    import sys
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    src = open(sys.argv[1], encoding="utf-8").read()
    res = transform_e4x(src)
    print(json.dumps({
        "literalCount": res.literal_count,
        "rejected": res.rejected,
        "transformed": res.transformed if res.literal_count else None,
    }, indent=2, ensure_ascii=False))
