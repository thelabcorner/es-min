---
name: adobe-extendscript-minification
description: >-
  Bundle, minify, validate, and distribute Adobe Illustrator ExtendScript (.jsx/.jsxinc)
  with UglifyJS, using only transformations verified inside a real Illustrator
  runtime. Covers include bundling, Adobe preprocessor directives (#target, #targetengine,
  #include), E4X handling, reserved-name management, the mandatory output options that
  work around Illustrator parser bugs (nested ternaries, switch-case semicolons,
  reserved-word keys), a conservative and an aggressive configuration with evidence,
  and a live differential test harness driven by ILLUSTRATOR_COM_TOOL.py. Use when
  building distributable Illustrator scripts, shrinking JSX, bundling .jsxinc
  dependencies, or diagnosing minification-related failures. Do not use for
  Photoshop-only scripts, browser JavaScript, or Node builds.
---

# Adobe ExtendScript Minification

Safe UglifyJS minification of Illustrator ExtendScript, **verified by executing
original and minified code inside Adobe Illustrator** (30.6.0, Windows) through
`ILLUSTRATOR_COM_TOOL.py`. Every claim below is backed by the harness results in
`references/COMPATIBILITY_REPORT.md`; do not trust this document over a live run.

**Golden rule: a minified script is only "safe" after the differential harness
passes in Illustrator. Parsing in Node/UglifyJS proves nothing about the
ExtendScript engine.**

---

## 1. The verified pipeline

```text
Source .jsx / .jsxinc
   1. Encoding check (UTF-8; BOM tolerated)
   2. Extract Adobe preprocessor directives (#target, #targetengine,
      #includepath, #strict, //@comment forms) from the file header
   3. Resolve #include directives recursively (anywhere in the file);
      detect circular includes; drop foreign directives inside includes
   4. E4X risk scan -> if XML literals/accessors present: transform or
      exclude (see §3.1)
   5. Dynamic-reference risk scan (eval/with/new Function/toString)
   6. UglifyJS minify with the verified config (§4)
   7. Switch-case repair pass (engine parser bug, §3.3)
   8. Restore directives at the top of the output
   9. Static checks (node --check on the directive-stripped body)
  10. REAL Illustrator differential test (harness) before distribution
```

The harness implements steps 2-10: `scripts/test_extendscript_minification.py`
(see §9). For a single-file production build, use `scripts/minify-jsx.py`
(implements steps 2-9 as a CLI; invoked by the ArcFit build). Do not hand-roll
this pipeline for production builds.

## 2. When to use

- Building distributable Illustrator scripts (File > Scripts).
- Bundling `.jsxinc` dependencies into one file.
- Reducing JSX size / applying local identifier mangling.
- Diagnosing a minification-related failure (use `--only <fixture-id>` to repro).

## 3. WHAT CANNOT BE MINIFIED — read this before writing any script

This section is the contract. If your source contains these constructs, the
stated consequence applies; the harness classifies each case with verified
evidence.

### 3.1 Cannot be parsed by UglifyJS at all (exclude or transform)

| Construct | Example | Consequence (verified) | Handling |
|---|---|---|---|
| E4X XML literals | `var x = <root><a>1</a></root>;` | UglifyJS: `Unexpected token: operator <` | Transform via `e4x_to_xml_string.py` (literal → `new XML("...")`) — verified equivalent in Illustrator for literals, `.@attr`, `..desc`, `{expr}`; or exclude the file from AST minification; or isolate in an unminified `.jsxinc` |
| `default xml namespace` | `default xml namespace = new Namespace(...)` | UglifyJS: `Unexpected token: keyword default`; **no ES3 equivalent exists** | **Cannot be transformed.** Keep such files unminified |
| Adobe preprocessor directives | `#target illustrator`, `#include "lib.jsxinc"` | UglifyJS parse error; also a syntax error if passed to `DoJavaScript` | Extract before minify, resolve includes, restore after (§1). Never rely on `//@include` surviving comment stripping |
| JSXBIN binary | `.jsxbin` | not text | never feed to UglifyJS |
| Invalid JS that ExtendScript happens to accept | `break}` in switch, `a\n++b` | see §3.3 — the engine's parser differs from the spec | the repair pass + explicit-semicolon writing rules (§3.5) |

The E4X transform is opt-in (`"transformE4X": true` per fixture). It rewrites
XML literals byte-for-byte into `new XML("...")` (embedded `{expr}` becomes
string concatenation), `.@attr` → `.attribute("attr")`, `.@*` → `.attribute("*")`,
`..desc` → `.descendants("desc")`. It REJECTS `default xml namespace` and
malformed literals — rejection means "keep unminified", never "best effort".

### 3.2 Mandatory output options — without them minified output can fail to parse

These are not optional "nice to haves". Each was reproduced as a hard parse
failure inside Illustrator:

| Option | Why mandatory | Verified failure without it |
|---|---|---|
| `output.extendscript: true` | ExtendScript rejects nested conditionals without parens | `a?b?c:d:e` → `Error 25: Expected: :.` in return, assignment, argument, array, object, and chained positions — every position tested |
| `output.quote_keys: true` + `keep_quoted_props: true` | Reserved words as unquoted object keys are illegal | `{if: 2}` → `Error 9: Illegal use of reserved word 'if'` |
| `output.semicolons: true` | ExtendScript does not apply ASI at switch-case boundaries | `case 1: f()}` / `break}` → `Error 25: Expected: ;.` (the harness repair pass §3.3 also emits the `;`) |
| `parse.module: false` (worker-enforced) | uglify-js minify defaults `module: true` → strict parsing | `with` statement rejected: `Strict mode may not include a with statement` |

### 3.3 Verified ExtendScript engine bugs that minification must accommodate

| Bug (verified on 30.6.0) | Behavior | Consequence |
|---|---|---|
| Nested ternary without parens | `Error 25: Expected: :.` | extendscript:true is mandatory; do not hand-write unparenthesized nested ternaries |
| ASI missing at switch-case boundary | `case 1: stmt}` and `break}` fail; even `switch(x){}` and all-empty cases fail (`Error 25: Expected: ;.`) | Write explicit `;` at the end of every case body; never write empty switch bodies; the harness runs `fix_extendscript_switch` on minified output to repair UglifyJS's `;`-stripping |
| Error in `finally` clause silently kills the script | No exception propagates; execution just stops; `try{return 'x'}finally{err}` returns nothing | **Never throw (or reference undeclared names) inside `finally`.** The minifier may move code out of `finally` (verified divergence) |
| Host-object errors inside `return` expressions bypass try/catch | `try { return (d.close(), d.name) } catch(e){}` — the error escapes; the statement form `var x = d.name` is caught | Keep host reads out of return expressions; `sequences` around host calls is an aggressive-only hazard (§7) |
| `a\n++b` fails to parse | restricted-production ASI not honored: `Error 25` | the minifier normalizes to `a;++b` (spec-correct); original fails — documented divergence |
| No `Array.prototype.forEach` (ES3) | `[1,2,3].forEach(...)` → `is not an object` | write ES3 loops |

### 3.4 Mangling hazards — identifiers that must never be renamed

Local mangling (with `toplevel:false, eval:false` and the reserved list) is
verified safe on the corpus EXCEPT for:

- **Indirect eval**: `var e = eval;` — UglifyJS's eval-scope protection does not
  extend to indirect eval; string-referenced locals get renamed (verified
  `eval-indirect-001`). Direct `eval('name')` is protected by `mangle.eval:false`.
- **eval strings referencing any local**: `eval('secret')` with `mangle.eval:true`
  breaks (verified `eval-direct-001` under `mangle-local-evaltrue`).
- **Function names used as strings**: `eval('add')`, `Function.prototype.toString`,
  BridgeTalk bodies, ScriptUI string handlers, ExternalObject/DLL-facing symbols,
  menu callback names, `$.global`-shared names, names other scripts read.
- **Property mangling**: verified breaking (`mangle-properties-all` renamed
  object keys and bracket-string accesses; host objects and result keys changed).
  Never enable for production.
- **`unused` removes eval-string-referenced locals** even without mangling
  (verified `compress-unused` + `eval-indirect-001`).

Reserved-name list: `configs/reserved-adobe.json` (probed runtime globals —
`app`, `File`, `Folder`, `BridgeTalk`, `ExternalObject`, `ScriptUI`, `Window`,
`UnitValue`, `Socket`, `System`, `XML`, `XMLList`, `Namespace`, `QName`,
`SaveOptions`, `UserInteractionLevel`, `alert`, `confirm`, `prompt`, `JSON`,
`documents`, ...). Do not indiscriminately reserve every local; reserve only
boundary-crossing names.

### 3.5 Writing rules — what agents MAY write (keeps minifiable)

- Nested ternaries: write them **with parentheses** (`a ? (b ? c : d) : e`) —
  safe under every config.
- switch: always end every case body with `break;` / `;`; never empty switches
  or empty cases (the repair pass covers minifier output, but source should be
  clean too).
- `with`: legal in ExtendScript and parses fine with `module:false`; treat the
  block conservatively (verified safe on plain objects in the corpus).
- `eval` (direct): fine under the conservative config (`mangle.eval:false`);
  marks the scope as not-mangle-safe — do not combine with aggressive.
- Reserved-word keys: always write them quoted (`{'if': 1}`).
- Unicode/emoji/control chars in strings: fine; `ascii_only:true` escapes them
  (can grow size: verified CJK 138→185 B under baseline).
- Sparse arrays: holes are preserved under conservative (verified); aggressive
  rewrites the hole check — see §7.
- File-mode scripts (run via `$.evalFile` or File > Scripts): **never rely on
  the file's last-expression value** — minifiers drop it (verified under
  aggressive). Assign to a global sentinel (`$.global.__result = ...`) and read
  it from the caller.
- Everything ES3: `var`, closures, recursion, prototypes, switch (with `;`),
  labels, try/catch/finally (never throw in finally), `arguments` (alias,
  `.length`, `callee`), `call`/`apply`, `instanceof`, `in`, regex, bitwise,
  `delete`, `typeof`, `void` — all verified safe under the conservative config.

### 3.6 What agents should NOT write when the script must be minifiable

1. E4X literals / `.@` / `..` / `default xml namespace` — use `new XML("...")`
   and `.attribute()` / `.descendants()` (or accept unminified status).
2. `eval('identifier')` / `new Function('...')` / string-generated callbacks —
   they defeat all static analysis; direct eval is the only tolerated form.
3. Code depending on `fn.toString()` source text (BridgeTalk bodies, debug
   dumps).
4. Implicit globals (bare assignments `x = 1`) — they leak across scripts and
   sessions and are invisible to the minifier; declare with `var`.
5. Errors thrown inside `finally`, host reads inside `return` expressions.
6. Switch cases without explicit trailing `;`, empty switch bodies.
7. Reserved words as unquoted keys, unparenthesized nested ternaries.
8. `.forEach`/`.map`/other ES5 array methods (ES3 runtime).
9. `#include` without going through the bundler; `//@` directives you expect to
   survive comment stripping.

## 4. Canonical safe configuration (conservative)

Exact file: `configs/conservative.json`. Invariant checks are enforced by the
worker (`module:false` is forced, meta keys stripped) and by
`tests/run-static-tests.mjs`. Do not change these options without rerunning the
harness in Illustrator:

```jsonc
{
  "parse": { "module": false, "html5_comments": false },
  "compress": {
    // safe, verified individually on the sensitive corpus:
    "booleans": true, "comparisons": true, "dead_code": true, "evaluate": true,
    "join_vars": true, "loops": true, "switches": true, "unused": true,
    // deliberately disabled (see §3.3/§7 for why):
    "conditionals": false, "if_return": false, "sequences": false,
    "collapse_vars": false, "inline": false, "reduce_funcs": false,
    "merge_vars": false, "hoist_props": false, "properties": false,
    "pure_getters": false, "directives": false, "negate_iife": false,
    "arguments": false, "typeofs": false, "keep_fargs": true,
    "unsafe": false, "unsafe_comps": false, "unsafe_Function": false,
    "unsafe_math": false, "unsafe_proto": false, "unsafe_regexp": false,
    "unsafe_undefined": false, "passes": 1
  },
  "mangle": { "toplevel": false, "eval": false, "keep_fnames": false,
              "keep_fargs": true, "reserved": [ /* see reserved-adobe.json */ ] },
  "output": {
    "extendscript": true, "semicolons": true, "braces": true, "ascii_only": true,
    "keep_quoted_props": true, "quote_keys": true, "quote_style": 3,
    "inline_script": false, "comments": false
  }
}
```

## 5. Forbidden defaults

- **Unrestricted property mangling** (`mangle.properties` without evidence) —
  verified breaking.
- **Top-level/global mangling** (`mangle.toplevel: true`) without a full
  reserved list covering every cross-script symbol.
- **`unsafe*` transforms** — keep disabled; individually they passed the corpus
  in isolation but their interactions are not exhaustively proven.
- **Getter-purity assumptions for Adobe objects** (`pure_getters`) — host
  getters are not pure; keep disabled in production.
- **E4X through ordinary UglifyJS** — it cannot parse it; transform or exclude.
- **Dropping Adobe directives without restoration** — the file then targets no
  engine and `#include` resolution is lost.
- **Skipping the Illustrator execution test** — static checks alone are not
  validation (see the engine bugs in §3.3, none of which Node can catch).

## 6. Optional aggressive configuration

`configs/aggressive.json` (collapse_vars, conditionals, if_return, inline:1,
merge_vars, negate_iife, reduce_funcs, reduce_vars, sequences:20, hoist_props,
passes:2, plus the conservative base). Verified on the full corpus: 103 pass +
6 documented divergences + 1 skip; 60/60 fuzz programs pass; ArcFit bundle
parse-verified.

**Prerequisites** (all verified failure modes if violated):
- No indirect eval (`var e = eval`) — locals referenced by eval strings get
  removed (`unused`) or renamed.
- No function-name-as-string usage (eval('add'), toString, BridgeTalk bodies).
- No sparse-array hole checks (`(i in a) ? a[i] : x`) — holes get rewritten
  (verified `es3-sparse-array-001` divergence).
- No host-object error semantics inside return expressions — `sequences` moves
  host reads into return commas and the error escapes try/catch (verified
  `errors-host-error-001` divergence).
- File-mode scripts must use the global-result-sentinel pattern (§3.5).
- Rerun the harness on your actual script before shipping.

Aggressive on the corpus: mean reduction ~2-5 points over conservative
(measured on the ArcFit bundle: 51.1% → 52.8%).

## 7. Verified classification summary (option matrix)

| Class | Options (verified in Illustrator) |
|---|---|
| **Unsafe** (failures on the corpus) | `output.extendscript:false` (7/7 nested-ternary fixtures fail); property mangling (`mangle.properties` any mode); `mangle.eval:true` (direct-eval fixtures fail); `unused` combined with eval-string references |
| **Verified safe on corpus (individual)** | all compress options tested individually: arguments, assignments, booleans, collapse_vars, comparisons, conditionals, dead_code, directives, evaluate, functions, hoist_funs, hoist_props, hoist_vars, if_return, inline, join_vars, loops, merge_vars, negate_iife, objects, passes:2, properties, pure_getters, reduce_funcs, reduce_vars, sequences, side_effects, strings, switches, typeofs, keep_fargs:false, and the unsafe_* set (isolated) |
| **Safe with restrictions** | local mangling (`mangle-local`, `toplevel`, `keep-fnames`, `keep-fargs:false` — all pass except indirect-eval fixture); `output.semicolons:false` (passes due to the switch repair pass — still not recommended); `ascii_only:false` |
| **Unsupported for Adobe syntax** | E4X literals; `default xml namespace`; Adobe directives (handled by preprocessing) |
| **Engine-bug dependent** | switch cases without `;`; empty switch bodies; errors in `finally`; host errors in return expressions; `a\n++b` (see §3.3) |

Full per-option counts: `references/COMPATIBILITY_REPORT.md`.

## 8. Failure diagnosis (decision tree)

1. **UglifyJS parse failure** → E4X? (`<` / `default` tokens) → transform or
   exclude. Directive line? → extract first. `with`? → `module:false` missing
   (worker forces it) — check you are not passing `module:true`.
2. **Illustrator parse failure of minified output** → `Error 25` → nested
   ternary without parens (extendscript:false?) or switch-case missing `;`
   (run the repair pass / write explicit `;`). `Error 9` → unquoted reserved
   key (quote_keys). `Error 6` → malformed number emitted (investigate, rare).
3. **Runtime mismatch** → check `risks`/`e4x` recorded in the harness JSONL:
   eval strings? indirect eval? finally-throw? host read in return? sparse
   arrays + aggressive? file-mode last-expression value?
4. **Missing callback / global** → name crossed a boundary (mangle) — add to
   `reserved`.
5. **Include failure** → circular includes, `#includepath` relative paths,
   mid-file includes (supported), duplicate includes (evaluated twice — native
   behavior preserved).
6. **Timeout / silent stop** → `finally` error swallowed (restart Illustrator,
   fix the script); infinite loop in script; modal dialog open.
7. **Unicode tool crash** → run the tool with `PYTHONIOENCODING=utf-8` (the
   harness does this automatically).

## 9. Commands

```bash
# Install
cd esmin && npm install

# Static checks (no Illustrator)
npm test

# Run ONE fixture live in Illustrator (original vs minified differential)
py scripts/test_extendscript_minification.py \
  --input fixtures/conditional-nested-consequent-001.json \
  --config configs/baseline.json --output reports/repro

# Full curated corpus, canonical config
py scripts/test_extendscript_minification.py \
  --input fixtures --config configs/conservative.json --output reports/full

# Option matrix (isolates individual options; use --matrix-subset)
py scripts/test_extendscript_minification.py \
  --input fixtures --config configs/matrix-compress.json \
  --output reports/matrix --matrix-subset --skip-tags dom

# Generated fuzz programs (differential only)
node scripts/fuzz-generate.mjs --count 60 --seed 42 --out reports/fuzz
py scripts/test_extendscript_minification.py --input reports/fuzz \
  --config configs/conservative.json --output reports/fuzz-run

# Production single-file minify (no Illustrator; used by arcfit build.mjs)
node bin/esmin.mjs --in path/to/Script.jsx \
  --config configs/conservative.json --out path/to/Script.min.jsx
#   env overrides in arcfit: ARCFIT_SKIP_MINIFY=1, ARCFIT_MINIFY_CONFIG=<cfg>

# Production script: parse-only validation (does NOT execute the script body)
py scripts/test_extendscript_minification.py --input path/to/Script.jsx \
  --config configs/conservative.json --output reports/prod --parse-guard

# Minify only (no Illustrator)
py scripts/test_extendscript_minification.py --input fixtures \
  --config configs/conservative.json --output reports/min --no-execute

# Evidence report
py scripts/report-generator.py --output references/COMPATIBILITY_REPORT.md \
  --results reports/full/results.jsonl reports/matrix-output/results.jsonl \
  reports/matrix-mangle3/results.jsonl reports/matrix-compress2/results.jsonl

# Reproduce a failed case by ID (then binary-reduce the config variants)
py scripts/test_extendscript_minification.py --input fixtures \
  --config configs/matrix-compress.json --output reports/repro \
  --only <fixture-id> --matrix-subset
```

## 10. Evidence links

- `references/COMPATIBILITY_REPORT.md` — full evidence report (environment,
  methodology, per-option results, failures, divergences, size metrics).
- `fixtures/` — 100+ curated fixtures (ES3, ASI, conditionals, host objects,
  directives/includes, E4X, ScriptUI, BridgeTalk, errors, channels) with
  expected values and cleanup.
- `configs/` — baseline, conservative, aggressive, and the three option
  matrices.
- `reports/` — machine-readable `results.jsonl` + `summary.json` per run.
- `scripts/e4x_to_xml_string.py` — E4X → `new XML(...)` transformer.
- `scripts/extendscript_preprocess.py` — directives, includes, switch repair.
- `scripts/test_extendscript_minification.py` — the differential harness.
