#!/usr/bin/env node
/**
 * fuzz-generate.mjs — grammar-aware ES3 test program generator.
 *
 * Generates deterministic (seeded) ES3 programs that stress the constructs
 * most likely to interact badly with UglifyJS: nested conditionals, comma
 * expressions, assignments, closures, switch, try/catch/finally, loops,
 * scope shadowing, and property access.
 *
 * Usage:
 *   node scripts/fuzz-generate.mjs --count 60 --seed 42 --out reports/fuzz
 *
 * Output: one JSON fixture per program (differential-only: no expect) plus a
 * manifest.json listing the seeds. Every program is bounded (no recursion,
 * fixed small loop counts) so execution cannot hang Illustrator.
 */
import fs from "node:fs";
import path from "node:path";
import process from "node:process";

// ---------------------------------------------------------------------------
// Deterministic PRNG (mulberry32)
// ---------------------------------------------------------------------------
function mulberry32(seed) {
  let a = seed >>> 0;
  return function () {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

// ---------------------------------------------------------------------------
// Program generator
// ---------------------------------------------------------------------------
class Gen {
  constructor(rng) {
    this.rng = rng;
    this.varCounter = 0;
    this.fnCounter = 0;
  }
  pick(arr) {
    return arr[Math.floor(this.rng() * arr.length)];
  }
  int(min, max) {
    return min + Math.floor(this.rng() * (max - min + 1));
  }
  freshVar() {
    return "v" + this.varCounter++;
  }
  freshFn() {
    return "f" + this.fnCounter++;
  }
  // -- atoms ----------------------------------------------------------------
  number() {
    const mode = this.int(0, 4);
    if (mode === 0) return String(this.int(-100, 100));
    if (mode === 1) return String(this.int(0, 20) * 0.5);
    if (mode === 2) return String(this.int(0, 3) === 0 ? 0 : this.int(1, 255));
    return String(this.int(0, 1));
  }
  string() {
    const words = ["alpha", "beta", "gamma", "x1", "a b", "q'q", 'd"d', "line\n", "\\t"];
    return JSON.stringify(this.pick(words));
  }
  bool() {
    return this.rng() < 0.5 ? "true" : "false";
  }
  value() {
    const r = this.rng();
    if (r < 0.25) return this.number();
    if (r < 0.45) return this.string();
    if (r < 0.55) return this.bool();
    return "null";
  }
  // -- expressions ----------------------------------------------------------
  binaryExpr(depth) {
    const ops = ["+", "-", "*", "/", "%", "&", "|", "^", "<<", ">>",
      "&&", "||", "==", "!=", "<", ">", "<=", ">="];
    return `(${this.expr(depth + 1)} ${this.pick(ops)} ${this.expr(depth + 1)})`;
  }
  ternaryExpr(depth) {
    return `(${this.expr(depth + 1)} ? ${this.expr(depth + 1)} : ${this.expr(depth + 1)})`;
  }
  arrayExpr(depth) {
    const n = this.int(1, 4);
    const parts = [];
    for (let i = 0; i < n; i++) parts.push(this.expr(depth + 1));
    return `[${parts.join(",")}]`;
  }
  objectExpr(depth) {
    const keys = this.pick([["a", "b"], ["x", "y"], ["k1", "k2", "k3"], ["if", "else"]]);
    const parts = [];
    for (const k of keys) {
      parts.push(`${JSON.stringify(k)}:${this.expr(depth + 1)}`);
    }
    return `{${parts.join(",")}}`;
  }
  unaryExpr(depth) {
    const ops = ["!", "~", "-", "+", "typeof", "void "];
    return `${this.pick(ops)}(${this.expr(depth + 1)})`;
  }
  callExpr(depth) {
    return `${this.freshFn()}(${this.expr(depth + 1)},${this.expr(depth + 1)})`;
  }
  expr(depth) {
    if (depth > 3) return this.value();
    const r = this.rng();
    if (r < 0.18) return this.binaryExpr(depth);
    if (r < 0.34) return this.ternaryExpr(depth);
    if (r < 0.46) return this.arrayExpr(depth);
    if (r < 0.56) return this.objectExpr(depth);
    if (r < 0.64) return this.unaryExpr(depth);
    if (r < 0.72) return this.callExpr(depth);
    if (r < 0.86) return this.freshVar();
    return this.value();
  }
  // -- statements -----------------------------------------------------------
  varStmt() {
    const v = this.freshVar();
    return `var ${v} = ${this.expr(0)};`;
  }
  assignStmt() {
    // Declared with var: bare assignments (v6 = v7) would create IMPLICIT
    // GLOBALS in ExtendScript that leak across fixtures in the same session
    // and make results order-dependent.
    return `var ${this.freshVar()} = ${this.expr(0)};`;
  }
  ifStmt() {
    return `if (${this.expr(0)}) { ${this.stmt()} } else { ${this.stmt()} }`;
  }
  forStmt() {
    const n = this.int(1, 5);
    const v = this.freshVar();
    return `for (var ${v} = 0; ${v} < ${n}; ${v}++) { ${this.stmt()} }`;
  }
  whileStmt() {
    const n = this.int(1, 4);
    const v = this.freshVar();
    return `var ${v} = 0; while (${v} < ${n}) { ${this.stmt()} ${v}++; }`;
  }
  doStmt() {
    const n = this.int(1, 4);
    const v = this.freshVar();
    return `var ${v} = 0; do { ${this.stmt()} ${v}++; } while (${v} < ${n});`;
  }
  switchStmt() {
    const cases = [];
    const n = this.int(1, 3);
    for (let i = 0; i < n; i++) {
      cases.push(`case ${i}: ${this.stmt()} break;`);
    }
    return `switch (${this.int(0, n)}) { ${cases.join(" ")} default: ${this.stmt()} }`;
  }
  tryStmt() {
    // VERIFIED ENGINE QUIRK (Illustrator 30.6.0): an error thrown inside a
    // finally clause SILENTLY TERMINATES the whole script without propagating
    // (no exception surfaces; execution just stops). The finally body must
    // therefore be error-free: only pure value statements.
    const fv = this.freshVar();
    return `try { ${this.stmt()} } catch (e) { ${this.stmt()} } finally { var ${fv} = ${this.value()}; }`;
  }
  fnDecl() {
    const name = this.freshFn();
    const v1 = this.freshVar();
    const v2 = this.freshVar();
    return `function ${name}(${v1}, ${v2}) { return ${this.expr(0)}; }`;
  }
  closureStmt() {
    const v = this.freshVar();
    const inner = this.freshVar();
    return `var ${v} = (function(){ var ${inner} = ${this.expr(0)}; return function(){ return ${inner}; }; })();`;
  }
  stmt() {
    const r = this.rng();
    if (r < 0.18) return this.varStmt();
    if (r < 0.30) return this.assignStmt();
    if (r < 0.42) return this.ifStmt();
    if (r < 0.52) return this.forStmt();
    if (r < 0.60) return this.whileStmt();
    if (r < 0.66) return this.doStmt();
    if (r < 0.74) return this.switchStmt();
    if (r < 0.82) return this.tryStmt();
    if (r < 0.90) return this.fnDecl();
    return this.closureStmt();
  }
  program() {
    const body = [];
    const n = this.int(6, 14);
    for (let i = 0; i < n; i++) body.push(this.stmt());
    const result = this.expr(0);
    body.push(`return ${result};`);
    return body.join("\n");
  }
}

// ---------------------------------------------------------------------------
// CLI
// ---------------------------------------------------------------------------
function parseArgs(argv) {
  const out = {};
  for (let i = 0; i < argv.length; i += 2) {
    out[argv[i].replace(/^--/, "")] = argv[i + 1];
  }
  return out;
}

const args = parseArgs(process.argv.slice(2));
const count = Number(args.count || 60);
const seed = Number(args.seed || 42);
const outDir = path.resolve(args.out || "reports/fuzz");
fs.mkdirSync(outDir, { recursive: true });

const manifest = [];
for (let i = 0; i < count; i++) {
  const s = (seed + i * 7919) >>> 0;
  const gen = new Gen(mulberry32(s));
  const source = gen.program();
  const id = `fuzz-${s.toString(16)}`;
  const fixture = {
    id,
    description: `generated ES3 program, seed 0x${s.toString(16)} (differential only)`,
    tags: ["fuzz", "generated"],
    source,
    minifiable: true,
    timeoutSec: 30,
  };
  fs.writeFileSync(path.join(outDir, `${id}.json`), JSON.stringify(fixture, null, 2));
  manifest.push({ id, seed: s });
  console.log(`${id} (${source.length} B)`);
}
fs.writeFileSync(path.join(outDir, "manifest.json"),
  JSON.stringify({ count, baseSeed: seed, programs: manifest }, null, 2));
console.log(`wrote ${count} fixtures to ${outDir}`);
