// Guards the Cloud Shell clipboard wrapper in src/app/azure/auth/page.tsx.
// The wrapper is a heredoc, and a heredoc is easy to break in ways that only
// surface in a customer's shell: an unquoted delimiter would expand the script's
// own $(...) locally, and a missing trailing newline would leave the delimiter
// mid-line so the paste hangs waiting for input.
//
// Run: node client/src/app/azure/auth/cloudshell-wrapper.check.mjs
// ponytail: mirrors the wrapper expression rather than importing it; the page is
// a React client component and there is no client test runner here. Keep the
// template below in sync with copyFullScript.

import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const wrap = (script) =>
  `cat > aurora-setup.sh <<'AURORA_SCRIPT_EOF'\n${script.replace(/\n*$/, '\n')}AURORA_SCRIPT_EOF\nbash aurora-setup.sh\n`;

const here = dirname(fileURLToPath(import.meta.url));
const scriptPath = join(here, '../../../../../server/connectors/azure_connector/setup-aurora-access.sh');
const script = readFileSync(scriptPath, 'utf8');

// 1. The delimiter must not occur in the body, or the heredoc ends early and the
//    rest of the script is executed as shell commands.
assert.ok(!script.includes('AURORA_SCRIPT_EOF'), 'delimiter collides with script body');

// 2. The delimiter must start its own line for both trailing-newline and
//    no-trailing-newline inputs.
for (const body of [script, 'echo hi', 'echo hi\n\n\n']) {
  assert.match(wrap(body), /\nAURORA_SCRIPT_EOF\nbash aurora-setup\.sh\n$/, 'delimiter not alone on its line');
}

// 3. Quoting is what protects the script's own expansions; assert the body really
//    contains some, so check 4 is not vacuous.
assert.ok(script.includes('$(') && script.includes('${'), 'expected expansions in script');

// 4. Round-trip through a real shell: the written file must be byte-identical, so
//    nothing was expanded or dropped in transit. Only the heredoc runs -- the
//    trailing `bash aurora-setup.sh` is stripped, since executing it would create
//    real service principals in whatever tenant the CLI is pointed at.
const dir = mkdtempSync(join(tmpdir(), 'aurora-cloudshell-'));
const lines = wrap(script).split('\n');
assert.equal(lines.pop(), '', 'expected trailing newline');
assert.equal(lines.pop(), 'bash aurora-setup.sh', 'expected invocation on the last line');
writeFileSync(join(dir, 'write_only.sh'), lines.join('\n') + '\n');
execFileSync('bash', ['write_only.sh'], { cwd: dir });

assert.equal(readFileSync(join(dir, 'aurora-setup.sh'), 'utf8'), script, 'heredoc did not reproduce the script exactly');

// 5. The reconstructed file must still be valid bash.
execFileSync('bash', ['-n', 'aurora-setup.sh'], { cwd: dir });

console.log('cloudshell wrapper OK: exact round-trip, delimiter safe, valid bash');
