#!/usr/bin/env node
import { spawnSync } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const script = join(root, "scripts", "minify-jsx.py");

const pythonCandidates = process.platform === "win32"
  ? ["py", "python", "python3"]
  : ["python3", "python"];

let last = null;
for (const python of pythonCandidates) {
  const args = python === "py" ? ["-3", script, ...process.argv.slice(2)] : [script, ...process.argv.slice(2)];
  const result = spawnSync(python, args, { stdio: "inherit" });
  if (result.error && result.error.code === "ENOENT") {
    last = result.error;
    continue;
  }
  if (result.error) {
    console.error(`esmin: failed to launch ${python}: ${result.error.message}`);
    process.exit(2);
  }
  process.exit(result.status ?? 1);
}

console.error(`esmin: Python 3 not found (${last ? last.message : "no candidates worked"})`);
process.exit(2);
