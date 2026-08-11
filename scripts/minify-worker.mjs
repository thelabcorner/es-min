#!/usr/bin/env node
/**
 * minify-worker.mjs — UglifyJS minification worker for the ExtendScript
 * compatibility harness.
 *
 * Usage:
 *   node minify-worker.mjs --config <config.json> --in <source.js> --out <result.json>
 *
 * The input source is a FUNCTION BODY (the same shape the Illustrator COM
 * wrapper executes: `(function(){ <source> })()`). Since UglifyJS does not
 * parse top-level `return`, the worker wraps the source in
 * `function __extmin_main(){ ... }`, minifies, and then extracts the minified
 * function body back out via the AST. The wrapper function is never called, so
 * compression cannot change fixture semantics through it.
 *
 * Output JSON:
 *   { "ok": true,  "code": "...", "stats": { inBytes, outBytes,
 *     "timeMs" } }
 *   { "ok": false, "error": { "name", "message", "line", "col" } }
 */
import fs from "node:fs";
import path from "node:path";
import process from "node:process";
import UglifyJS from "uglify-js";

const WRAP_OPEN = "function __extmin_main(){";

function fail(message) {
  console.error(`minify-worker: ${message}`);
  process.exit(2);
}

function parseArgs(argv) {
  const out = {};
  for (let i = 0; i < argv.length; i += 2) {
    const key = argv[i];
    const value = argv[i + 1];
    if (!key.startsWith("--")) fail(`unexpected argument: ${key}`);
    out[key.slice(2)] = value;
  }
  if (!out.config) fail("missing --config");
  if (!out.in) fail("missing --in");
  if (!out.out) fail("missing --out");
  return out;
}

function stripMeta(options) {
  // Remove harness-only keys before passing to UglifyJS.
  const cleaned = {};
  for (const [key, value] of Object.entries(options)) {
    if (key === "_id" || key === "comment") continue;
    cleaned[key] = value;
  }
  // uglify-js minify defaults options.module to true when unset, which enables
  // strict-mode parsing and REJECTS `with` (a legal ExtendScript construct).
  // ExtendScript is a script, never a module: force module:false unless a
  // config explicitly requests otherwise.
  cleaned.parse = { module: false, html5_comments: false, ...(cleaned.parse || {}) };
  // JSON cannot carry RegExp objects: convert string-form property-mangle
  // regexes ("^_" -> /^_/).
  const props = cleaned.mangle && cleaned.mangle.properties;
  if (props && typeof props.regex === "string") {
    props.regex = new RegExp(props.regex);
  }
  return cleaned;
}

function minifyFunctionBody(source, options) {
  const wrapped = WRAP_OPEN + source + "\n}";
  const result = UglifyJS.minify(wrapped, options);
  if (result.error) return result;
  let parsed;
  try {
    parsed = UglifyJS.parse(result.code, options.parse || {});
  } catch (err) {
    return { error: err };
  }
  const fn = parsed.body[0];
  if (!fn || !Array.isArray(fn.body)) {
    return { error: new Error("wrapper extraction failed: unexpected AST shape") };
  }
  const top = new UglifyJS.AST_Toplevel({ body: fn.body });
  const code = top.print_to_string(options.output || {});
  return { code };
}

const args = parseArgs(process.argv.slice(2));
const configPath = path.resolve(args.config);
const inPath = path.resolve(args.in);
const outPath = path.resolve(args.out);

let rawConfig;
try {
  rawConfig = JSON.parse(fs.readFileSync(configPath, "utf8"));
} catch (err) {
  fail(`cannot read config ${configPath}: ${err.message}`);
}

const optionSets = Array.isArray(rawConfig) ? rawConfig : [rawConfig];
const source = fs.readFileSync(inPath, "utf8");
const inBytes = Buffer.byteLength(source, "utf8");

const started = Date.now();
let lastError = null;
let resultCode = null;
let usedId = null;

const variantArg = args.variant;
const candidates = variantArg === undefined ? optionSets : [optionSets[Number(variantArg)]];

for (const options of candidates) {
  const clean = stripMeta(options);
  usedId = options._id || null;
  const result = minifyFunctionBody(source, clean);
  if (result.error) {
    lastError = {
      name: result.error.name || "Error",
      message: result.error.message || String(result.error),
      line: result.error.line ?? -1,
      col: result.error.col ?? -1,
    };
    continue;
  }
  resultCode = result.code;
  break;
}

const timeMs = Date.now() - started;

if (resultCode === null) {
  const payload = { ok: false, error: lastError, timeMs, configId: usedId };
  fs.writeFileSync(outPath, JSON.stringify(payload, null, 2), "utf8");
  process.exit(0);
}

const outBytes = Buffer.byteLength(resultCode, "utf8");
const payload = {
  ok: true,
  code: resultCode,
  stats: { inBytes, outBytes, reductionBytes: inBytes - outBytes, timeMs },
  configId: usedId || path.basename(configPath, ".json"),
};
fs.writeFileSync(outPath, JSON.stringify(payload), "utf8");
process.exit(0);
