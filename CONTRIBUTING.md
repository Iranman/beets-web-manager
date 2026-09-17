# Contributing

Thanks for contributing to Beets Web Manager. See `docs/DEVELOPMENT.md` for project layout, setup, and the full validation command set, and `docs/ARCHITECTURE.md` for current system shape and non-negotiable product rules.

## Development

1. Create a branch for your change; do not commit directly to `main`.
2. Keep changes focused and avoid unrelated refactors. When a change uncovers a larger design issue, record it in `docs/TECHNICAL_DEBT.md` rather than expanding scope to fix it.
3. Run the relevant checks before opening a pull request (see `docs/DEVELOPMENT.md` for the complete list):

```bash
python -m unittest discover -s tests -p "test_*.py"
cd frontend
npm ci
npm run typecheck
npm run lint
npm run build
```

4. Use `REVIEW.md` as the review checklist for any change touching matching, jobs, filesystem mutation, or provider integrations.

## Commit Messages

Use concise conventional prefixes:

- `feat:` new user-visible behavior
- `fix:` bug fix
- `docs:` documentation-only change
- `test:` test-only change
- `build:` build or dependency change
- `ci:` GitHub Actions or automation
- `chore:` maintenance with no behavior change

## Security

Do not include real credentials, tokens, cookies, private logs, private music-library data, or screenshots containing secrets in issues, pull requests, tests, or fixtures. Report vulnerabilities privately using `SECURITY.md`.
