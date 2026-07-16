# Contributing

Thanks for contributing to Beets Web Manager.

## Development

1. Create a branch for your change.
2. Keep changes focused and avoid unrelated refactors.
3. Run the relevant checks before opening a pull request.

```bash
python -m unittest discover -s tests -p "test_*.py"
cd frontend
npm ci
npm run typecheck
npm run lint
npm run build
```

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
