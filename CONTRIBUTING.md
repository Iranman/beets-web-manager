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

## Versioning and releases

The project uses Semantic Versioning. While the version is `0.x`:

- **Minor release (`0.Y.0`)** when the change needs operator attention: a CHANGELOG **Upgrade Notes** item that asks the operator to act, something that used to work is now refused, a migration of persisted state or user files, a webmanager plugin minor/major or protocol change (Beets must be restarted), a renamed or removed Compose/environment/mount setting, or a new user-facing feature.
- **Patch release (`0.y.Z`)** for fixes and dependency or security bumps that need no operator action and no migration.
- When in doubt, take the higher level.

Every change adds an entry under `## Unreleased` in `CHANGELOG.md`, with an **Upgrade Notes** subsection whenever operators must do something. A release PR retitles `## Unreleased` to `## vX.Y.Z - YYYY-MM-DD` and bumps `VERSION` together; CI (`release-metadata` in `docker-build.yml`) fails if they disagree, and on a tag it also requires the tag to be `v` + `VERSION`. Pushing the tag publishes the image and creates the GitHub Release from that CHANGELOG section. The maintainer's full steps are in `AGENTS.md` ("Release and deploy").

## Security

Do not include real credentials, tokens, cookies, private logs, private music-library data, or screenshots containing secrets in issues, pull requests, tests, or fixtures. Report vulnerabilities privately using `SECURITY.md`.
