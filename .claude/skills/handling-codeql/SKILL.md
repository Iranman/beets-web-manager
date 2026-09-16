---
name: handling-codeql
description: Resolves specific CodeQL/security findings with evidence-driven, rule-scoped analysis. Use for CodeQL alerts, taint-flow findings, secret exposure, injection, unsafe filesystem/process/network behavior, or security PR cleanup. Avoids repeated repository-wide security audits for one alert.
---

# Handling CodeQL Findings

Work alert-by-alert or rule-family-by-rule-family.

## Workflow

1. Identify the exact rule, source, sink, path, and affected file/function.
2. Determine whether the finding is:
   - a real vulnerability,
   - unreachable/dead path,
   - already sanitized/validated,
   - generated/test-only behavior,
   - a modeling false positive.
3. For real issues, fix the root data-flow or trust-boundary problem.
4. For safe dispositions, document concrete code evidence; do not hand-wave.
5. Add a focused regression/security test when practical.
6. Re-run the narrowest relevant test/check first.
7. Run repository-wide security validation once at final review/release, not after every alert.

## Security Invariants

- Never expose tokens, cookies, auth headers, signed URLs, credentials, or secrets.
- Validate untrusted path/process/network inputs at the boundary.
- Redact exceptions/subprocess output before browser/API exposure when secrets may be present.
- Do not weaken an existing security boundary just to silence CodeQL.
- Do not hard-code allowlists or values only to satisfy a test.

If the alert crosses a documented architecture or mutation boundary, read the relevant part of `docs/AI_ENGINEERING_RULES.md`; otherwise keep scope to the data flow.

## Report

Return:
- Rule / Finding
- Exploitability or Safe Disposition Evidence
- Fix, if required
- Regression Check
- Remaining Alerts In Scope
