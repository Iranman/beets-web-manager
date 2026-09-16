# AGENTS.md

Orientation for coding agents and contributors working in this repository.

- Read `docs/ARCHITECTURE.md` first — current system shape, non-negotiable product rules, and intended dependency direction.
- Read `docs/DEVELOPMENT.md` for setup, validation commands, and engineering constraints.
- Read `docs/TECHNICAL_DEBT.md` before touching an area with known open debt.
- Use `REVIEW.md` as the review checklist for any change touching matching, jobs, filesystem mutation, or provider integrations.
- Follow `CONTRIBUTING.md` for branching, commit style, and the PR process.

Beets remains the library backend. MusicBrainz/AcoustID are the primary identity evidence, and the MusicBrainz release-group ID is the canonical album identity. Library mutations always go through the controlled transaction workflow in `backend/transaction_engine.py`. See `docs/ARCHITECTURE.md`'s "Non-Negotiable Rules" section and `docs/adr/` for the full, binding version of these rules — this file is an index, not a second copy.
