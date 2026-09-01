# Repository-Wide CodeQL Security Closure — Working Report

Status: **IN PROGRESS — NOT COMPLETE**. Do not read this document as a closure claim.

## 1. Baseline (established 2026-09-01, verified against live GitHub API, not PR-scoped)

| Metric | Baseline |
|---|---|
| `origin/main` SHA | `7b05844d58d657ce6f1ae6c5b1724f5ba70ee257` (merge of PR #103) |
| Open repository-wide CodeQL alerts (before this pass) | 171 |
| Critical | 2 |
| High | 164 |
| Medium | 5 |
| Python alerts | ~156 |
| JavaScript/TypeScript alerts | ~15 |
| GitHub Actions alerts | 0 observed in this query |
| All alerts confirmed on `refs/heads/main` (not PR noise) | 171/171 |
| Alerts older than 14 days | 152/171 |

Rule breakdown at baseline:

| Rule | Count | File(s) |
|---|---|---|
| `py/path-injection` | 121 | `app.py` (109), `backend/transaction_engine.py` (8), `backend/beets_control_agent.py` (4) |
| `py/polynomial-redos` | 17 | `app.py` |
| `js/incomplete-url-substring-sanitization` | 14 | `index.html` (9), `frontend/src/views/Playlists.tsx` (5) |
| `py/stack-trace-exposure` | 5 | `routes_setup.py` (3), `app.py` (2) |
| `py/clear-text-storage-sensitive-data` | 4 | `scripts/verify_first_run_acceptance.py` (2), 2 test files |
| `py/incomplete-url-substring-sanitization` | 3 | `app.py` |
| `py/command-line-injection` | 2 | `backend/beets_control_agent.py` |
| `js/xss-through-dom` | 2 | `index.html` |
| `py/bad-tag-filter` | 1 | `app.py` |
| `py/regex-injection` | 1 | `app.py` |
| `js/insecure-randomness` | 1 | `index.html` |

**Context established before any fix work**: this repository has ~27 prior local "wave" branches
(`beets-codeql-app-path-wave1` through `wave8`, `beets-sec002-*`, `beets-wave27-pr102`, etc.) attempting
this exact closure. None landed a 0-open-alert state on `main`. Investigation of the first 2 alerts
closed below indicates a likely root cause for part of that gap: real validation code for these sinks
already exists in `main` (anchored regexes, `resolve_safe_path`, capability gates, allowlists), but the
corresponding GitHub alerts were never individually verified and dismissed — i.e. some fraction of the
171 may be `SAFE_BUT_CODEQL_BLIND` bookkeeping debt rather than undone code work. This must be verified
per-alert, not assumed, per the mission rules (no blanket dismissal).

## 2. Alert dispositions

### Alert 1009 — `py/command-line-injection`, critical — `backend/beets_control_agent.py:2041`

- **Disposition**: `SAFE_BUT_CODEQL_BLIND`
- **Source**: `options.get("mb_albumid")` from the `/imports/reimport` control-agent HTTP body.
- **Sink**: `subprocess.run(full_cmd, ...)` inside `reimport_source_atomic()`, where `full_cmd` includes
  `["--search-id", mb_albumid]`.
- **Validation chain**: `backend/beets_control_agent.py:1977-1979` — `mb_albumid` is stripped, then
  rejected outright unless it fully matches
  `_MB_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)`
  (anchored both ends, defined at line 1905). A flag-injection payload (leading `-`, `--config=...`,
  embedded whitespace/newlines) cannot match this pattern, so it can never reach `argv`.
  `import_target_path` is derived from `resolve_safe_path(...)`-validated/staged paths, never raw
  request text. `config_override` is length-bounded and written to a fresh `uuid4()`-named, `0600` temp
  file; only that generated safe path (never attacker text) is placed on the command line.
- **Why CodeQL can't see it**: CodeQL's Python command-line-injection query does not model a full-string
  anchored regex match as a command-line sanitizer barrier for this data-flow path.
- **Regression evidence**: `tests/test_sec002_wave19_mb_track_repair.py` and sibling SEC-002 wave tests
  already exercise invalid `mb_albumid` rejection through this exact gate.
- **GitHub disposition**: dismissed 2026-09-01, reason `false positive`.

### Alert 1010 — `py/command-line-injection`, critical — `backend/beets_control_agent.py:2762`

- **Disposition**: `SAFE_BUT_CODEQL_BLIND`
- **Sink**: `subprocess.run(full_cmd, ...)` inside the shared helper `_run_beet_subcommand_locked()`,
  which by its own docstring performs *no* validation itself — callers are responsible for
  allow-listing `cmd_list[0]` and sanitizing the rest of `cmd_list`.
- **All current callers verified** (both are in this file; there are no others):
  1. `/commands/execute` (line 5260): `command` must be a member of the fixed `ALLOWED_COMMANDS` set
     (20 literal beet subcommand names) and pass `require_command_capability()`; `args` pass through
     `_sanitize_command_path_args()`, which explicitly rejects `-c`/`--config`/`-c=...`/`--config=...`
     (the one flag capable of loading an arbitrary beets config) and resolves any absolute-path argument
     through `resolve_safe_path()`. `source_path` is independently validated the same way.
  2. `_run_beet_command()` closure at line 4756 (used by `/albums/artwork/fetch/apply`): `command` is
     hardcoded to `"fetchart"`/`"embedart"` from a fixed tuple, never request-derived; `args` still pass
     through the same `_sanitize_command_path_args()`.
- **Why CodeQL can't see it**: the sanitizing call happens in a different function from the sink, and
  the allowlist/capability checks happen in yet another (the HTTP handler); CodeQL's intraprocedural/
  interprocedural taint tracking does not connect the three-function chain as a barrier for this sink.
- **Note for future work**: `_sanitize_command_path_args()` does not reject non-path, non-`-c` leading-
  hyphen arguments (e.g. an arbitrary `beet modify` flag). This is current intended behavior — the
  endpoint's documented purpose is a controlled beet-subcommand passthrough behind internal auth +
  capability gating + a fixed subcommand allowlist — not a gap this alert is about. Flagged in
  `docs/TECHNICAL_DEBT.md` as worth an explicit flag-allowlist per subcommand if this endpoint's
  exposure ever widens.
- **GitHub disposition**: dismissed 2026-09-01, reason `false positive`.

## 3. Remaining alerts

169 of 171 alerts remain **NEEDS_REVIEW** as of this checkpoint — see
`security/codeql_main_alert_inventory.json` for the full raw list. Work continues alert-by-alert (or
pattern-class-by-pattern-class for the 109 `py/path-injection` instances clustered in `app.py`, a
48,286-line file) rather than being declared closed. **Repository-wide CodeQL security closure is NOT
complete.**
