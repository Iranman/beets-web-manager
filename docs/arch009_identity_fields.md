# ARCH-009 Identity Field Classification

Permanent identities: **Artist** = MusicBrainz Artist ID · **Album** = Release Group ID · **Edition** = Release ID · **Track** = Recording ID.

Rules enforced in code (`backend/identity_contract.py`, `backend/import_reconciliation.py`, engine `create_album_mb_track_repair_plan`):

- A Release ID is never copied into a Release Group field.
- A supplied Release ID is resolved to its **authoritative** Release Group from MusicBrainz. The pairing must agree with any stated Release Group. If the release group cannot be verified, the request is refused (fail closed).
- An unknown Release Group is never inherited from another row, title, disc/track position, or Release ID.
- Legacy aliases are accepted only where they have exactly one meaning, and they are normalized into explicit internal fields immediately.

Classes: `CANONICAL_ALBUM_IDENTITY`, `EDITION_IDENTITY`, `LOOKUP`, `DISPLAY`, `LEGACY_COMPATIBILITY`.

## Mutation paths

| Route | Field(s) | Class | Enforcement |
|---|---|---|---|
| `POST /api/albums/<id>/add-mbids` | `mb_releasegroupid` | CANONICAL_ALBUM_IDENTITY | required UUID |
| | `mb_albumid` | EDITION_IDENTITY | `verify_album_identity`: must belong to the RG (409 otherwise, fail closed) |
| `POST /api/submissions/albums/<id>/attach-mbids` | `mb_releasegroupid` / `mb_albumid` | CANONICAL / EDITION | same as above |
| `POST /api/clean/rgid-group/relink` | `mb_releasegroupid` / `mb_albumid` | CANONICAL / EDITION | written RG is always the release's authoritative RG; disagreement refused |
| `POST /api/clean/rgid-group/assign-representative-release` | `mb_albumid` | EDITION_IDENTITY | verified against the album's RG, fail closed (previously failed open on lookup failure) |
| `POST /api/clean/rgid-group/merge`, `keep-separate`, `undo-resolution` | `mb_releasegroupid` | CANONICAL_ALBUM_IDENTITY | required UUID; rows must belong to the group |
| `POST /api/albums/<id>/deduplicate` | `mb_albumid` | EDITION_IDENTITY | override verified against the album's RG before persisting; authoritative RG stored with it |
| `POST /api/albums/<id>/duplicate-resolver/apply` | `mb_albumid` | EDITION_IDENTITY | override verified against the album's RG before stamping |
| `POST /api/albums/<id>/repair-mb-tracks`, `fix-metadata` | `mb_albumid` | EDITION_IDENTITY | engine repair plan: release's MB Release Group must equal the album's (`repair_identity_mismatch`), and must be known (`repair_release_group_unverified`); caller-supplied tracks never inherit the album's RG |
| `POST /api/folders/import-with-id` | `mb_releasegroupid` / `release_group_id` / `release_group` | CANONICAL_ALBUM_IDENTITY | must equal the RG MusicBrainz reports for the release, otherwise the import is blocked |
| | `mb_albumid` / `mbid` | EDITION_IDENTITY; LEGACY_COMPATIBILITY when it carries a release-group URL or equals the stated RG | an RG value is resolved to a representative release before import; never written as a release |
| `POST /api/import-review/auto-enqueue`, `POST /api/folders/import-target-preview`, candidate comparison | `release_group_id` (alias `mb_releasegroupid`) | CANONICAL_ALBUM_IDENTITY (alias LEGACY_COMPATIBILITY) | normalized immediately; the import job re-verifies against MusicBrainz |
| | `representative_release_id` (alias `mb_albumid`) | EDITION_IDENTITY (alias LEGACY_COMPATIBILITY) | same |
| `POST /api/albums/reimport-disk` | `mb_albumid` | EDITION_IDENTITY | RG taken from MusicBrainz; engine deterministic-identity gate |
| import reconciliation (`_merge_imported_album_into_existing`) | album rows' `mb_releasegroupid` | CANONICAL_ALBUM_IDENTITY | both albums must share a known RG (`album_identity`); otherwise nothing moves and a review is recorded |
| `POST /api/import-reconciliation/reviews/<id>/resolve` | none from the client (IDs come from the stored review) | — | only engine reconcile / bulk-replacement transactions |

## Read / display paths

| Surface | Field(s) | Class |
|---|---|---|
| `POST /api/download/album`, `GET ...?mbid=`, `GET /api/albums/<id>/duplicate-resolver?mb_albumid=`, candidate comparison `?release_group_id=` | `mb_albumid`, `mbid`, `release_group_id` | LOOKUP |
| Recording candidates (`matching_contract`) | `release_group_id` + `release_id` (and `mb_*` mirrors) | DISPLAY — serialized as separate explicit fields |
| Reconciliation reviews | `release_group_id`, `release_id` | DISPLAY |
| Import Review generic "MusicBrainz ID" input (`mbids` state) | one UUID | LEGACY_COMPATIBILITY — sent as the representative release (`mb_albumid`); the Release Group is sent separately (`mb_releasegroupid`) and is never copied from it |

Regression coverage: `tests/test_arch009_identity_contract.py`, `tests/test_arch009_merge_identity.py`, `tests/test_import_reconciliation.py`, `tests/test_duplicate_resolver_identity.py`, `tests/test_beets_transaction_engine.py::...without_release_group_is_refused`.
