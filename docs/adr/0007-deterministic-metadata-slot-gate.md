# ADR 0007: Deterministic-Metadata-Slot Auto-Gate for Missing-Track Recovery

Date: 2026-10-27

## Status

Accepted. STEP 1 (contract foundation) of the Missing-Track Recovery & Import-Review
redesign. Extends the evidence hierarchy of ADR 0002 (`mb_releasegroupid` is canonical album
identity) and ADR 0003 (AI is optional and never the source of identity). Introduces one new
identity-proof tier and one narrow auto-acceptance gate; it does not change any existing
fingerprint or identity gate. The mutation/advancer code that *consumes* this gate is a later
step and is out of scope here.

## Context

A missing-track recovery downloads one or a few specific tracks to fill an existing album
(anchor case: 112 "Forever", Release Group `37cb4a72`, 9 tracks, missing #1 "Forever (Intro)"
and #9 "For Us (Outro)"). The recovered files were fingerprinted and AcoustID answered
**`NO_RESULT`** — the recordings are genuinely absent from the AcoustID database, which is
common for intros, outros, interludes and obscure editions. This is an *answered-but-empty*
lookup, factually distinct from **`UNAVAILABLE`** (no API key, provider outage, rejected key
— the lookup never ran).

Before this change the matching contract had only one deterministic per-track proof:
`DETERMINISTIC_TRACK_RECORDING_ID` (an embedded Recording ID matching the target, or a
confirmed AcoustID fingerprint). A `NO_RESULT` file can satisfy neither, so a perfect
non-fingerprint match (exact title, exact release-track position, correct duration, correct
Release Group, correct artist) collapsed into the same "no deterministic evidence" bucket as a
file with no evidence at all, and the recovery stalled in review forever. Meanwhile a real
title collision — FLEUR "FOREVER FOREVER" (RG `2da91738`) vs 112 "Forever" — must never be
auto-accepted.

## Decision

Add a new identity-proof tier **`DETERMINISTIC_METADATA_SLOT`**, ranked **below**
`DETERMINISTIC_TRACK_RECORDING_ID` and **above** `RELEASE_GROUP_ID`. It proves that a file
fills one specific release-track slot on strong deterministic metadata, and it may auto-satisfy
acceptance only under a deliberately narrow gate.

### Exact signals required (ALL, per present track)

Per-track (the `is_deterministic_metadata_slot` half, `backend/matching/models.py`):

1. **Exact normalized title.** `title_similarity == 1.0` — the local file's normalized title
   (including the matcher's controlled alias/feature/version variants) equals the target
   track's normalized title. A merely similar title does not qualify.
2. **Exact release-track position.** `position_match is True` — the same disc **and** track
   number as the specific missing slot. A same-number-different-disc or near-position match
   does not qualify.
3. **Duration within canonical tolerance.** A measured duration on both sides, with
   `abs(local − target) <= DETERMINISTIC_SLOT_DURATION_TOLERANCE_S` (**7.0 seconds**). Absent
   duration on either side disqualifies the slot (fail-closed).
4. **AcoustID `NO_RESULT` only.** The per-file AcoustID lookup must have *answered empty*.
   `UNAVAILABLE`, `CONFLICT`, and `AMBIGUOUS` never qualify.
5. **No conflicts** on the assignment (e.g. an embedded Recording ID that contradicts the
   target is a conflict and blocks the slot).

Album-level (the other half, `backend/matching/evidence.evaluate_release_group_candidate`):

6. **Candidate Release Group == target Release Group** (`release_group_status == "validated"`),
   with no hard conflicts anywhere in the evaluation.
7. **Artist matched.** Either an artist-MBID match (`artist_id_matches`) **or**, when neither
   side carries an artist MBID, an artist-text similarity at or above the **HIGH artist-text
   floor** `DETERMINISTIC_SLOT_ARTIST_TEXT_FLOOR` (**0.85**). A mismatched artist MBID is a
   conflict and blocks the slot outright.

Identity-proof tiering: when every present track is verified and at least one leaned on the
metadata-slot half, `identity_proof = DETERMINISTIC_METADATA_SLOT`; if all present tracks are
hard-proven, it stays `DETERMINISTIC_TRACK_RECORDING_ID`; a complete release stays
`CONFIRMED_RELEASE`. Hard per-track proof always outranks slot proof.

### Scope of authorization — VERIFIED_SUBSET only, NO_RESULT only

- The tier may satisfy **`can_auto_accept(ActionScope.VERIFIED_SUBSET)`** — acting only on the
  local tracks actually present (the recovered slot(s)). It is computed from
  `local_coverage_complete` + validated Release Group, which this tier can now complete.
- It **never** sets `action_allowed`, which remains the single `FULL_RELEASE`-grade
  authorization field, set only by the pre-existing complete-alignment / hard-identity /
  reviewed-import paths. A partial recovery therefore can never authorize a whole-release
  (`FULL_RELEASE`) or any destructive operation through this tier.
- It fires only on `NO_RESULT`. `UNAVAILABLE`/`CONFLICT`/`AMBIGUOUS` are structurally excluded
  at the per-track predicate, so a missing key or outage can never be laundered into an
  auto-accept.

`action_allowed` stays the one authorization field everywhere; the new tier only widens what
`can_auto_accept(VERIFIED_SUBSET)` can return.

### Why `NO_RESULT`-only and `VERIFIED_SUBSET`-only

`NO_RESULT` is positive information: AcoustID looked and the recording is simply not in its
database — compatible with a correct match and common for the exact tracks recovery targets.
`UNAVAILABLE` is the absence of information and must never unlock an action. Restricting the
tier to `VERIFIED_SUBSET` keeps it to the tracks actually in hand; declaring a release
complete, merging, or any destructive mutation still needs strictly stronger evidence
(`FULL_RELEASE` / hard identity), preserving ADR 0002's "destructive corrections need stronger
evidence than suggestions".

### Why FLEUR cannot pass

FLEUR "FOREVER FOREVER" is Release Group `2da91738`; the target is `37cb4a72`. Signal 6
requires the candidate RG to equal the target RG, so:

- Against the 112 album (which carries the 112 RG), FLEUR's RG contradicts it → hard
  `wrong_release_group` conflict → `CONFLICT` → `identity_proof = INSUFFICIENT`, no auto-accept.
- As an orphan suggestion with no local RG, FLEUR's RG never becomes `validated` (no embedded
  match, no hard per-track positive), so the album-level half is unmet and the slot cannot fire.
- The separate pure veto predicate `classify_candidate_relatedness` (RG-MBID / artist-MBID /
  artist-text only, **never** title text) classifies FLEUR as `UNRELATED` to the target, so a
  later step keeps it out of the primary-suggestion slot regardless of its colliding title.

A wrong-artist / wrong-RG file is thus structurally incapable of passing the gate — the RG
equality and artist checks are prerequisites, not score contributions that a strong title could
outweigh.

## Consequences

- The 112 intro/outro `NO_RESULT` recovery becomes auto-eligible at the contract level
  (`build_album_matching_decision` returns `action_allowed=True`, `identity_proof =
  deterministic_metadata_slot`, `review_required=False`); a later step wires the advancer to
  consume it.
- `no_result` vs `unavailable` are now reported with distinct, accurate messages; the
  "AcoustID not configured or not reachable" wording is used only for genuine `UNAVAILABLE`.
- The matching-decision contract is bumped to `contract_version: 3` (backward-readable: all v2
  fields unchanged; v3 adds separated req-7 `dimensions` and a top-level `identity_proof`).
- Tuning knobs: `DETERMINISTIC_SLOT_DURATION_TOLERANCE_S` (7.0s) and
  `DETERMINISTIC_SLOT_ARTIST_TEXT_FLOOR` (0.85) live in `backend/matching/models.py`. They are
  deliberately tight; loosen only with new regression fixtures proving FLEUR and
  wrong-duration/wrong-position cases still fail.

## Alternatives considered

- **Treat `NO_RESULT` like a confirmed fingerprint.** Rejected: it would conflate "not in the
  database" with "fingerprint matched", weakening the hard tier.
- **Lower the existing fingerprint gate for recovery.** Rejected explicitly: no existing
  fingerprint/identity gate is weakened; this is a new, strictly narrower tier.
- **Accept strong metadata for `FULL_RELEASE` too.** Rejected: destructive/whole-release
  actions keep the stronger bar.
