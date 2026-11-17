// High/critical npm-audit gate with a narrow, documented exception list.
//
// `npm audit --audit-level=high` has no native way to accept a single advisory,
// so when an advisory has NO patched version it blocks every branch with no
// clean remedy. This gate keeps the full high/critical audit armed for every
// package and advisory EXCEPT the GHSA ids listed below, each of which must be
// unfixable (no patched release) AND dev/build-tooling only (never shipped to
// the production runtime). Remove an entry the moment upstream publishes a fix.
//
// Exceptions are recorded in docs/TECHNICAL_DEBT.md. Keep the two in sync.
import { execSync } from 'node:child_process';

const ALLOW = new Set([
  // picomatch ReDoS (GHSA-fc5q-39xc-vjpf / CVE-2026-112366). Affects all
  // published picomatch (<=4.0.7); no patched version exists. Pulled only by
  // devDeps (vitest/vite/tinyglobby/fdir, typescript-eslint); absent from the
  // production tree (`npm ls picomatch --omit=dev` is empty). Reachable only
  // via attacker-controlled glob patterns, which build/test tooling never
  // receives. Re-check for a picomatch release > 4.0.7 and drop this entry.
  'GHSA-fc5q-39xc-vjpf',
]);

function auditJson() {
  try {
    return JSON.parse(execSync('npm audit --json', { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] }));
  } catch (e) {
    // npm audit exits non-zero when vulnerabilities exist; the JSON is on stdout.
    if (e.stdout) return JSON.parse(e.stdout);
    throw e;
  }
}

// Resolve a vulnerability to the GHSA ids actually behind it, following the
// string `via` references npm uses for transitively-vulnerable packages.
function advisoryIds(name, vulns, seen = new Set()) {
  if (seen.has(name)) return [];
  seen.add(name);
  const ids = [];
  for (const via of vulns[name]?.via ?? []) {
    if (typeof via === 'object' && via.url) ids.push(via.url.split('/').pop());
    else if (typeof via === 'string') ids.push(...advisoryIds(via, vulns, seen));
  }
  return ids;
}

export function offenders(report) {
  const vulns = report.vulnerabilities ?? {};
  const out = [];
  for (const [name, v] of Object.entries(vulns)) {
    if (v.severity !== 'high' && v.severity !== 'critical') continue;
    const ids = advisoryIds(name, vulns);
    const unlisted = ids.filter((id) => !ALLOW.has(id));
    // Fail safe: a high/critical package with no resolvable advisory id is treated
    // as offending rather than silently cleared.
    if (ids.length === 0 || unlisted.length > 0) {
      out.push(`${name} (${v.severity}): ${unlisted.join(', ') || 'no-advisory-id'}`);
    }
  }
  return out;
}

if (process.argv[2] === '--selftest') {
  const fixture = {
    vulnerabilities: {
      picomatch: { severity: 'high', via: [{ url: 'https://github.com/advisories/GHSA-fc5q-39xc-vjpf' }] },
      vitest: { severity: 'high', via: ['picomatch'] }, // transitive -> allowlisted
      evil: { severity: 'critical', via: [{ url: 'https://github.com/advisories/GHSA-aaaa-bbbb-cccc' }] },
      nostamp: { severity: 'high', via: ['missing-pkg'] }, // resolves to nothing -> fail safe
      low: { severity: 'low', via: [{ url: 'https://github.com/advisories/GHSA-zzzz-zzzz-zzzz' }] },
    },
  };
  const got = offenders(fixture).map((s) => s.split(' ')[0]).sort();
  const want = ['evil', 'nostamp'];
  if (JSON.stringify(got) !== JSON.stringify(want)) {
    console.error('SELFTEST FAIL: got', got, 'want', want);
    process.exit(1);
  }
  console.log('SELFTEST OK');
  process.exit(0);
}

const bad = offenders(auditJson());
if (bad.length) {
  console.error('High/critical npm advisories not on the documented allowlist:\n  ' + bad.join('\n  '));
  console.error('\nFix the advisory (bump/override to a patched version). Do not add to the');
  console.error('allowlist unless it is unfixable AND dev/build-tooling only -- see');
  console.error('frontend/scripts/audit-allowlist.mjs and docs/TECHNICAL_DEBT.md.');
  process.exit(1);
}
console.log('npm audit: no high/critical advisories outside the documented allowlist.');
