## v1.0.0 — 2026-08-10

**SemVer: major** — first public `es-min` release establishes the package, CLI, config, fixture, and harness surfaces.

Gate: `npm test` and `npm pack --dry-run` passed on the tagged commit; live Illustrator corpus regenerated through `agent-skills/illustrator-com-automation-skill/comtool/ILLUSTRATOR_COM_TOOL.py` with 104 pass / 5 documented divergences / 1 skip; generated fuzz passed 60/60 (seed 42).

### Added
- `es-min` npm package with an `esmin` CLI wrapper around `scripts/minify-jsx.py` — accepts the existing `--in`, `--config`, `--out`, `--skip-includes`, and `--check-only` flags.
- Repository packaging for configs, fixtures, references, scripts, and static tests.
- README in the es-family evidence-first structure, including the current release-asset constraint.

### Release assets
- Attach the `es-min-1.0.0.tgz` npm package or source archive after the gate passes.
- Do **not** attach a DLL for v1.0.0: this repository currently has no native minification accelerator, no native source, and no native validation gate.
- Do **not** attach ESPACK accel bundles for v1.0.0: no `ESMIN.accel.jsx` / `.min.jsx` is produced or certified by this repository yet.

### Compatibility
- Build-time tooling: Node.js >=18, Python 3, UglifyJS 3.19.3.
- Live validation target: Adobe Illustrator 30.6.0 / Windows x86-64.
