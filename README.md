<div align="center">

# ESMIN: ExtendScript Minification for Adobe ExtendScript (ES3)

## ExtendScript MINification = E.S.MIN

### A UglifyJS-based `.jsx` / `.jsxinc` minification pipeline for Adobe Illustrator scripts, with directive preservation, include bundling, E4X risk handling, and live-engine differential validation

[![Differential: Illustrator corpus](https://img.shields.io/badge/differential-3350%20live%20executions-purple)](#validation)
[![Fuzz: generated](https://img.shields.io/badge/fuzz-60%2F60%20conservative-purple)](#validation)
[![Engine parity: corpus](https://img.shields.io/badge/corpus-104%20pass%20%2B%205%20documented%20divergences-success)](#validation)
[![Adobe: Creative Suite](https://img.shields.io/badge/Adobe%20-Creative%20Suite-red?logo=adobe&logoColor=white)](https://extendscript.docsforadobe.dev/)
[![Engine](https://img.shields.io/badge/ExtendScript-ES3-green)](#compatibility)
[![Size](https://img.shields.io/badge/median%20reduction-6.49%25-orange)](#performance)
[![License: GPL-3.0-or-later](https://img.shields.io/badge/license-GPL%203.0--or--later-blue)](https://www.gnu.org/licenses/gpl-3.0.html)

</div>

---

## Part Of The Same Toolkit

> Production-grade ExtendScript infrastructure for Illustrator-era JavaScript engines.

<table>
<tr>
<td width="50%" valign="top">

### Runtime Primitives

**[ESON](https://github.com/thelabcorner/eson)**  
Strict RFC 8259 JSON for ExtendScript.

**[ESB64](https://github.com/thelabcorner/es-b64)**  
Base64 and UTF-8 utilities.

**[ESARR](https://github.com/thelabcorner/es-arr)**  
ES5+ Array compatibility methods.

**[ESSTR](https://github.com/thelabcorner/es-str)**  
String whitespace and trim methods.

**[ESCHARS](https://github.com/thelabcorner/es-chars)**  
Native bulk byte operations.

**[ESHTTP](https://github.com/thelabcorner/es-http)**  
HTTP transport for ExtendScript automation.

**[ESTIMER](https://github.com/thelabcorner/es-timer)**  
Microsecond timing for ExtendScript automation.

**[ESRAND](https://github.com/thelabcorner/es-rand)**  
Deterministic random streams and sampling for ExtendScript.

</td>
<td width="50%" valign="top">

### Build & Integration Tools

**[ESPACK](https://github.com/thelabcorner/espack)**  
Self-extracting ExternalObject bundles.

**[ESMIN](https://github.com/thelabcorner/es-min)**  
Minification for shipped JSX bundles.

**[ESABI](https://github.com/thelabcorner/esabi)**  
Modern ExternalObject ABI declarations for native integrations.

**[VectorIPC](https://github.com/thelabcorner/vector-ipc)**  
Bounded local IPC for scripting hosts and native plug-ins.

**[ESTC](https://github.com/thelabcorner/estc)**  
TypeScript-to-ExtendScript build, compatibility, and live-parse tooling.

**[ESDB](https://github.com/thelabcorner/esdb)**  
Native state and durable storage for Adobe tooling.

**ESOBF** <sub>coming soon</sub>  
Obfuscation for hardened JSX distribution.

</td>
</tr>
</table>

Also from the same team: **[ArcFit.dev](https://arcfit.dev)**, deterministic arc warp for Illustrator.

---

## Table of Contents

- [Why ESMIN?](#why-esmin)
- [Features](#features)
- [Installation](#installation)
- [Quick Start](#quick-start)
- [API](#api)
- [Validation](#validation)
- [Performance](#performance)
- [Security Model](#security-model)
- [Compatibility](#compatibility)
- [Engine quirks that shaped the design](#engine-quirks-that-shaped-the-design)
- [Development](#development)
- [Repository layout](#repository-layout)
- [Known limitations](#known-limitations)
- [Credits](#credits)
- [License](#license)

---

## Why ESMIN?

Illustrator 30.6.0 accepts ES3-era JavaScript plus Adobe-specific syntax that ordinary JavaScript minifiers do not model. `#target` and `#include` lines are not JavaScript tokens, E4X literals cannot be parsed by UglifyJS, and the engine has parser differences around nested ternaries and switch-case semicolon boundaries.

ESMIN keeps UglifyJS in the parts it can prove, and wraps it with the Adobe-specific steps that were verified by executing original/minified pairs inside Illustrator: UTF-8 reading, Adobe directive extraction/restoration, recursive include resolution, E4X and dynamic-reference risk scans, UglifyJS 3.19.3 with the conservative config, switch-case repair, Node parse guards, and the optional Illustrator differential harness.

---

## Features

- **Conservative config with live evidence** — `configs/conservative.json` keeps `output.extendscript`, `quote_keys`, `keep_quoted_props`, and `semicolons` enabled; the committed aggregate evidence in `references/COMPATIBILITY_REPORT.md` documents the live Illustrator harness, and the conservative corpus gate records 104 pass, 5 documented divergences, and 1 skip when regenerated.
- **Directive and include handling** — `scripts/extendscript_preprocess.py` extracts Adobe header directives, resolves `#include` recursively, detects circular includes, and restores valid directives above the minified body.
- **E4X risk handling** — the pipeline detects E4X; `scripts/e4x_to_xml_string.py` can transform verified literal/accessor cases to `new XML(...)`, while `default xml namespace` remains an explicit non-minifiable case.
- **Switch-case repair** — generated text runs through `fix_extendscript_switch` for Illustrator's verified switch-case ASI parser bug.
- **Fixture and matrix harness** — 100+ JSON fixtures cover ES3 syntax, host objects, directives/includes, E4X, ScriptUI, BridgeTalk, eval, strings, properties, and parser edges; matrix configs isolate output, mangle, and compress options.
- **Production CLI** — `esmin` wraps the single-file minifier (`scripts/minify-jsx.py`) for npm-style invocation while preserving the Python harness for live Illustrator validation.

---

## Installation

```bash
npm install es-min
```

From another local repo while this is checked out as a sibling:

```bash
node ../esmin/bin/esmin.mjs --in Script.jsx --config ../esmin/configs/conservative.json --out Script.min.jsx
```

The npm package name is `es-min`; the installed CLI command is `esmin`.

---

## Quick Start

```bash
esmin --in ArcFit_NativeWarp_FixedWidth.jsx \
  --config configs/conservative.json \
  --out ArcFit_NativeWarp_FixedWidth.min.jsx
```

Run `--check-only` before shipping a new source shape:

```bash
esmin --in Script.jsx --config configs/conservative.json --out Script.min.jsx --check-only
```

---

## API

### CLI: `esmin`

| Flag | Meaning |
|---|---|
| `--in <jsx>` | input `.jsx` / `.jsxinc` entry file |
| `--config <json>` | UglifyJS option file; use `configs/conservative.json` for distribution |
| `--out <jsx>` | output file |
| `--skip-includes` | do not resolve `#include` directives |
| `--check-only` | preprocess and risk-scan only; write no output |

### Harness scripts

| Script | Purpose |
|---|---|
| `scripts/test_extendscript_minification.py` | original/minified differential harness through Illustrator COM |
| `scripts/minify-jsx.py` | production single-file minifier used by the CLI |
| `scripts/e4x_to_xml_string.py` | opt-in E4X literal/accessor transform for verified cases |
| `scripts/report-generator.py` | compatibility report generator from JSONL harness outputs |
| `scripts/fuzz-generate.mjs` | generated fixture corpus |

### Config selection

| Config | Use it for | Live evidence |
|---|---|---|
| `configs/baseline.json` | identity/debug runs and option-matrix comparisons | baseline corpus evidence in `references/COMPATIBILITY_REPORT.md` |
| `configs/conservative.json` | production distribution default | 104 pass, 5 documented divergences, 1 skip on the curated corpus |
| `configs/aggressive.json` | extra reduction only after checking the documented prerequisites | 103 pass, 6 documented divergences, 1 skip on the curated corpus |

Aggressive mode is not a drop-in default: avoid it when scripts contain indirect eval, function-name-as-string usage, sparse-array hole checks, host-object reads in return expressions, or file-mode last-expression dependencies.

---

## Validation

| Check | Command | Result |
|---|---|---|
| Static repository checks | `npm test` | validates package metadata, configs, 110 fixtures, Python compilation, and Node parse guards |
| Conservative live corpus | `python scripts/test_extendscript_minification.py --input fixtures --config configs/conservative.json --output reports/final-conservative` | regenerate gate; recorded conservative evidence is 104 pass, 5 documented divergences, 1 skip |
| Conservative fuzz corpus | `node scripts/fuzz-generate.mjs --count 60 --seed 42 --out reports/fuzz` then `python scripts/test_extendscript_minification.py --input reports/fuzz --config configs/conservative.json --output reports/run-fuzz-conservative` | regenerate gate; recorded evidence is 60/60 pass |
| Full evidence report | `python scripts/report-generator.py --output references/COMPATIBILITY_REPORT.md --results ...` | `references/COMPATIBILITY_REPORT.md` records 3350 original/minified executions |

The live oracle is Adobe Illustrator through `ILLUSTRATOR_COM_TOOL.py`; Node parsing is a static guard, not proof of ExtendScript compatibility.

---

## Performance

The current measured performance claim is byte-size reduction, not runtime speed. `references/COMPATIBILITY_REPORT.md` records 3346 fixture variants with size data from Illustrator-executed runs: mean reduction 11.26%, median reduction 6.49%, min/max -35.51% / 100.0%.

Runtime minifier latency depends on UglifyJS, Python process startup, include depth, and input size; no shipped benchmark table is included until those timings are measured on a tagged commit.

---

## Security Model

ESMIN is a build-time transformer. It reads input source files, resolves local `#include` paths, invokes Node/UglifyJS, and writes a minified output file. It does not load an ExtendScript `ExternalObject` DLL and does not execute the target script during `esmin` CLI minification.

The live harness does execute fixture source and minified output inside Illustrator. Treat fixture code and input scripts as code with the privileges of the Illustrator process. Run the live harness only on trusted corpora or inside an isolated Illustrator profile.

---

## Compatibility

| Target | Status |
|---|---|
| Adobe Illustrator 30.6.0 / ExtendScript 4.5.6 / Windows x86-64 | live evidence in `references/COMPATIBILITY_REPORT.md` |
| Node.js | `>=18.0.0` for worker scripts; evidence report used Node v22.23.2 |
| Python | Python 3; evidence report used Python 3.12 / 3.14 with pywin32 for COM harness |
| UglifyJS | pinned to 3.19.3 |
| Non-Illustrator ExtendScript hosts | not claimed until live-tested in that host |

---

## Engine quirks that shaped the design

- **Nested ternary output requires `output.extendscript: true`** — without it, Illustrator 30.6.0 reports `Error 25: Expected: :.` across nested conditional positions.
- **Switch-case boundaries require explicit semicolons** — empty switches and `case` bodies ending without `;` can fail with `Error 25`; the repair pass exists for this bug.
- **Reserved-word object keys must be quoted** — unquoted keys such as `{if: 2}` fail with `Error 9`; `quote_keys` and `keep_quoted_props` are mandatory.
- **E4X is not ordinary JavaScript** — UglifyJS cannot parse XML literals or `default xml namespace`; verified transforms exist for a subset only.
- **Eval and string-referenced identifiers constrain mangling** — indirect eval and function names used as strings can break local mangling or `unused` removal.
- **Host-object errors differ by expression context** — host reads inside return expressions can bypass `try/catch`; aggressive sequence transforms are therefore restricted.

---

## Development

```bash
npm install
npm test

# Production minify
npm run minify -- --in Script.jsx --config configs/conservative.json --out Script.min.jsx

# Live Illustrator corpus gate
npm run fixture:conservative
npm run fuzz:generate
npm run fuzz:conservative
npm run live-verify
```

---

## Repository layout

```
bin/          npm CLI wrapper
configs/      baseline, conservative, aggressive, and option-matrix configs
fixtures/     curated JSON fixtures and include trees
references/   compatibility report and evidence documents
scripts/      Python/Node minification, preprocessing, E4X, fuzz, report tools
tests/        static repository checks
docs/         release-note draft and release planning notes
SKILL.md      agent-facing operating guide retained for compatibility
```

---

## Known limitations

- No DLL or ESPACK accel bundle is generated by v0.1.0. A release that attaches such assets must first add a native component, source, build script, tests, and live validation.
- `default xml namespace` remains non-minifiable; there is no ES3-equivalent transform in this tool.
- Static checks do not prove Illustrator compatibility; the differential harness is the release gate.

---

## Credits

Built on UglifyJS 3.19.3 and Adobe ExtendScript engine behavior measured through Illustrator automation. ExtendScript host references: docsforadobe and the Illustrator COM/JSX probes in this workspace.

---

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
