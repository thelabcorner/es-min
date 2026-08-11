#!/usr/bin/env node
/**
 * run-static-tests.mjs — static validation for the esmin repository.
 * No Illustrator execution here; the LIVE validation is the Python harness.
 */
import fs from 'node:fs';
import path from 'node:path';
import { spawnSync } from 'node:child_process';

const root = process.cwd();
const failures = [];
const check = (cond, label) => {
  if (!cond) failures.push(label);
  else console.log('PASS:', label);
};

// 1. Repository/package structure
const pkg = JSON.parse(fs.readFileSync('package.json', 'utf8'));
check(pkg.name === 'es-min', 'package name is es-min');
check(pkg.license === 'GPL-3.0-or-later', 'package declares GPL-3.0-or-later');
check(pkg.bin && pkg.bin.esmin === 'bin/esmin.mjs', 'package exposes esmin CLI');
check(pkg.dependencies && pkg.dependencies['uglify-js'] === '3.19.3', 'uglify-js is a runtime dependency');
check(fs.existsSync('README.md'), 'README.md exists');
check(fs.existsSync('LICENSE'), 'LICENSE exists');

// 2. Skill compatibility document stays present for agents that still load it.
const skill = fs.readFileSync('SKILL.md', 'utf8');
check(skill.startsWith('---\n'), 'SKILL.md starts with YAML frontmatter');
check(/^name:\s*adobe-extendscript-minification/m.test(skill), 'skill name correct');
check(/^description:/m.test(skill), 'skill has description');

// 3. Configs are valid JSON and worker-enforced invariants hold
for (const cfg of ['baseline', 'conservative', 'aggressive']) {
  const data = JSON.parse(fs.readFileSync(`configs/${cfg}.json`, 'utf8'));
  check(data.output && data.output.extendscript === true,
    `${cfg}.json forces output.extendscript`);
  check(data.output && data.output.quote_keys === true,
    `${cfg}.json forces quote_keys (reserved-word keys are ES3-illegal unquoted)`);
  check(data.output && data.output.semicolons === true,
    `${cfg}.json forces semicolons (switch-case ASI bug)`);
}
for (const cfg of ['matrix-output', 'matrix-mangle', 'matrix-compress']) {
  const data = JSON.parse(fs.readFileSync(`configs/${cfg}.json`, 'utf8'));
  check(Array.isArray(data.variants) && data.variants.length > 0,
    `${cfg}.json is a variant matrix`);
}

// 4. All fixture JSONs are well-formed and reference valid modes
const fixtureDir = 'fixtures';
let fixtureCount = 0;
for (const file of fs.readdirSync(fixtureDir)) {
  if (!file.endsWith('.json')) continue;
  const fx = JSON.parse(fs.readFileSync(path.join(fixtureDir, file), 'utf8'));
  fixtureCount++;
  check(typeof fx.id === 'string' && fx.id.length > 0, `${file}: id`);
  check(typeof fx.source === 'string', `${file}: source`);
  check(['eval', 'file', 'parse-guard'].includes(fx.mode || 'eval'), `${file}: mode`);
  if (fx.mode === 'file') {
    check(!fx.source.trim().endsWith(';') || /__extminResult/.test(fx.source),
      `${file}: file-mode must use the global result sentinel`);
  }
  if (fx.minifiable === false) {
    check(fx.note || fx.hasE4X, `${file}: not-minifiable needs a note`);
  }
}
console.log(`\n${fixtureCount} fixtures validated`);

// 5. Reserved-name list matches the runtime probe evidence
const reserved = JSON.parse(fs.readFileSync('configs/reserved-adobe.json', 'utf8')).globals;
for (const name of ['app', 'File', 'Folder', 'BridgeTalk', 'ExternalObject', 'ScriptUI',
  'XML', 'XMLList', 'Namespace', 'QName', 'SaveOptions', 'UserInteractionLevel']) {
  check(reserved.includes(name), `reserved-adobe.json contains ${name}`);
}

// 6. Harness modules compile
for (const mod of ['scripts/extendscript_preprocess.py', 'scripts/e4x_to_xml_string.py',
  'scripts/test_extendscript_minification.py', 'scripts/report-generator.py']) {
  const r = spawnSync('python', ['-m', 'py_compile', mod], { encoding: 'utf8' });
  check(r.status === 0, `${mod} compiles`);
}
for (const mod of ['bin/esmin.mjs', 'scripts/minify-worker.mjs', 'scripts/fuzz-generate.mjs']) {
  const r = spawnSync(process.execPath, ['--check', mod], { encoding: 'utf8' });
  check(r.status === 0, `${mod} parses`);
}

if (failures.length) {
  console.error('\nFAILURES:\n- ' + failures.join('\n- '));
  process.exit(1);
}
console.log('\nAll static checks passed. Live validation requires the Python harness '
  + 'and a running Illustrator (see README.md / SKILL.md).');
