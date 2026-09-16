---
name: matching-music
description: Handles album/track identity, MusicBrainz release-group matching, AcoustID/fingerprint evidence, AI suggestions, import candidate scoring, and track-list reconciliation. Use when imports suggest the wrong album, tracks fail to match, MBIDs conflict, or matching confidence is wrong.
---

# Matching Music

Use deterministic identity evidence first and keep matching explainable.

## Identity Rules

- Canonical album identity is MusicBrainz Release Group ID: `mb_releasegroupid`.
- A MusicBrainz Release ID is edition-level supporting data, never a substitute for a Release Group ID.
- MusicBrainz recording IDs and AcoustID/fingerprint evidence are stronger than text-only title matching.
- AI may rank or explain candidates already discovered from deterministic sources. It must not invent or independently verify identity.
- AI failure, missing credentials, rate limits, or timeouts must not stop MusicBrainz/AcoustID matching.
- Conflicting or incomplete strong evidence goes to review.

## Track Reconciliation

Evaluate evidence by track, not only by album title or track count:
- recording identity,
- AcoustID/fingerprint evidence,
- duration tolerance,
- track/disc position,
- normalized title/artist,
- filename/tag evidence.

A correct release-group candidate must not be rejected only because one displayed title differs when fingerprint/recording identity, duration, and position strongly agree.

A matching track count alone must not override contradictory fingerprint or recording evidence.

## Debug Workflow

1. Capture the exact input metadata and selected/suggested candidate.
2. Compare the candidate Release Group ID against the intended album.
3. Inspect per-track evidence and the reason each track matched or failed.
4. Identify whether the defect is:
   - candidate discovery,
   - release-group selection,
   - track reconciliation,
   - confidence aggregation,
   - AI override/fallback,
   - presentation only.
5. Fix the owning layer, not the symptom.
6. Add focused tests for the failure and at least one conflict/negative case.

Read `docs/AI_ENGINEERING_RULES.md` only if changing the shared matching result contract, mutation eligibility, or architecture boundary.

## Mutation Boundary

Matching may recommend an action; it does not silently rename, move, delete, retag, replace, or import files. Any applied mutation must use the project's controlled preview/apply/audit/recovery path.

## Report

Return:
- Failed Matching Stage
- Root Cause
- Evidence Used
- Fix
- Regression Tests
