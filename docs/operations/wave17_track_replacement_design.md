# SEC-002 / ARCH-003 Wave 17: Track Replacement Controlled Mutation Boundary

**Status note (Claude final technical review, PR #90):** this document originally described AGY's initial design and made several claims that did not match the actual implementation -- an unmigrated caller matrix (only one of four claimed callers was actually touched), a "move replacement onto destination" step that never existed in the pre-Wave-17 code and was a genuine behavioral regression, and an AcoustID-verification claim the engine did not actually implement. This revision describes the corrected implementation as independently verified against the code, not as originally proposed. See `docs/TECHNICAL_DEBT.md` ARCH-003 for the change log and remaining debt.

## 1. Overview & Objectives

Wave 17 adds one new mutation family, `track_replacement_v1`, to the engine-owned Plan -> Review -> Apply -> Verify -> Transaction boundary established in Waves 15-16, and migrates **one** existing production mutation path onto it: the automatic music-format replacement job's original-file cleanup step (`_music_format_remove_original_after_replacement`). A second, new manual entry point (human-reviewed, `/api/items/<iid>/replacement/plan` + `/replacement/apply`) also uses this same family. See section 2 for exactly what is, and is not, migrated.

### Architectural Invariants

1. **Engine Ownership**: `backend/transaction_engine.py`, invoked through `beets_control_agent.py`, is the sole owner of the original file's quarantine move and the corresponding Beets DB row removal for `track_replacement_v1` transactions. `app.py` performs no direct file or SQLite mutation for this mutation family.
2. **Mutation Family**: All track-replacement transactions carry `mutation_family == "track_replacement_v1"`; both Apply and Rollback verify this explicitly rather than trusting the calling route.
3. **Corrected destination semantics**: Apply **never moves or touches the replacement candidate file**. It only quarantines the original and removes its DB row. The replacement candidate is expected to already exist as its own tracked library item via the normal import pipeline -- this matches the pre-Wave-17 behavior of `_music_format_remove_original_after_replacement`. AGY's implementation had introduced a "move replacement onto the original's path" step that does not reflect this and would have silently changed production semantics; it was removed.
4. **Non-destructive quarantine**: the original file is moved (never deleted) to a server-derived quarantine root (`REPLACEMENT_QUARANTINE_DIR`, read server-side only -- a client-supplied `quarantine_root` in the Plan payload is ignored) under `<quarantine-root>/<date>/<operation_id>/<filename>`, using the transaction's own durable ID (not a second, discarded ID generated before `store.create()`).
5. **Role-specific roots**: original-file candidates must resolve under the music-library root; replacement candidates must resolve under an approved staging/acquisition root (downloads, playlist-downloads, torrents, tempdir). These are two disjoint root sets, not one combined `allowed_roots` list -- an "original" cannot be satisfied by a file under a staging root, and vice versa.
6. **Item-ID-bound original resolution**: `original_item_id` is the sole authority for which file is "the original". A client-supplied `original_path` is treated only as expected-state evidence that must exactly match the engine's own DB-read record (`item_path_mismatch` if it disagrees) -- never as an independent selector.
7. **TOCTOU & stat verification**: both the original and the replacement candidate are fully re-verified (inode, device, size, `mtime_ns`, non-symlink at every path component, root containment) immediately before Apply performs any mutation, not only at Plan time.
8. **Matching authority is required, not optional**: Plan requires a `matching_contract` with `identity_source` set to something other than AI-only evidence, and a `replacement_recording_id`. When both original and replacement Recording IDs, or both Release Group IDs, are known, they must agree. AI-only evidence (`identity_source in {"", "ai", "ai_suggestion", "ai_only"}`) is rejected outright (`ai_only_evidence_rejected`).
9. **DB failure can never report success**: a DB exception, a zero-row update, or a post-Apply DB re-read that still finds the row present is a fail-closed structured error (`db_update_failed` / `db_rowcount_mismatch` / `verification_failed`) with `mutated: true`, never a silent `except: pass` followed by `Completed`.
10. **Durable per-step crash recovery**: Apply persists `apply_steps` progress (`original_quarantined`, `db_row_removed`) after each meaningful stage, so a retry after a crash mid-Apply resumes correctly instead of re-quarantining an already-moved file or losing track of partial state.
11. **Rollback ordering**: the quarantined original is moved to a transaction-owned `.rollback-holding` location first, then into the (confirmed-vacant) original path -- nothing is destroyed before restoration is confirmed. Rollback never touches the replacement file (Apply never moved it in the first place).
12. **Concurrency**: item-ID-keyed locking (`_get_resource_lock(f"item:{item_id}")`, acquired outside the existing per-operation lock) serializes two transactions racing on the *same* Beets item; transactions for different items remain fully parallel.

---

## 2. Corrected Production Caller Matrix

This replaces the original design document's caller matrix, which claimed four migrated workflows. Independent review of the actual diff found only one real caller change; the other three claims did not match the code and have been corrected here rather than forced into scope to make the matrix "look complete."

| Production symbol | Source file | Mutation type | Uses `track_replacement_v1`? | Production-path test coverage | Notes |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `_music_format_remove_original_after_replacement` (called from `_music_format_replace_rows`, the existing automatic background replacement job) | `app.py` | Original-file quarantine + DB row removal, after a replacement has already been separately imported and AcoustID-verified | **Yes** | `tests/test_sec002_wave17_track_replacement.py` (`RealEndToEndRouteTests`, plus the full engine-level suite) | Migrated this wave. Matching evidence is threaded through from the job's own pre-existing, already-computed AcoustID fingerprint check (`_acoustid_fingerprint_match`/`_acoustid_fingerprint_ids`) via `_music_format_replacement_matching_contract()` -- no new fingerprint stack was built. |
| `POST /api/items/<iid>/replacement/plan` + `/replacement/apply` (new manual, human-reviewed entry point) | `app.py` | Same engine mutation as above, triggered by an operator selecting a specific candidate file for a specific track in the Web Manager UI | **Yes** | `tests/test_sec002_wave17_track_replacement.py` | New this wave. Requires a real, server-side AcoustID fingerprint match between the supplied candidate and the existing track before a Plan can even be created (`candidate_not_verified` otherwise); AI is not consulted for this decision at all. This is the "Review" step's real entry point -- Plan and Apply are separate HTTP calls, and the frontend (`TrackReplacementModal.tsx`) requires an explicit "Apply Replacement" click between them. |
| `_import_and_tag_disk` / `replace_existing_item_ids` | `app.py` | Direct `DELETE FROM items WHERE id IN (...)` for import-time item replacement | **No -- intentionally out of scope** | Unchanged, pre-existing coverage only | This is a distinct mutation shape (bulk replacement of items *during import*, not post-import single-track replacement of an already-tracked item) with its own existing safety handling. Forcing it through `track_replacement_v1`'s single-original/single-candidate contract would not fit its actual semantics; not migrated this wave. Remains ARCH-003 debt. |
| `repair_album_mb_tracks` | `app.py` | Direct SQLite `UPDATE items`/`UPDATE albums` + local `beet write` subprocess | **No -- intentionally out of scope** | Unchanged, pre-existing coverage only | Tag/metadata repair for tracks already correctly matched to a recording, not a file-replacement operation -- no original/candidate file pair exists in this workflow, so `track_replacement_v1`'s contract does not apply. Remains ARCH-003 debt (tracked separately; also touched narrowly by Wave 14's RGID-conflict guard). |
| `POST /api/items/<iid>/attach-recording` (`item_attach_recording`) | `app.py` | MusicBrainz recording attachment (tag/metadata write, not a file-replacement swap) | **No -- correctly out of scope** | Its own, independently reviewed suite (Wave 14) | Per this review's explicit scope instruction: this endpoint already has its own reviewed MusicBrainz attachment transaction semantics (`action_eligibility`, `decision_version` audit trail) and is not a track-replacement operation. It was never actually touched by the AGY diff despite being listed as "migrated" in the original matrix; that claim is removed rather than the endpoint being force-fit into this family. |

**Automatic vs. manual, and why:** `_music_format_replace_rows` (the pre-existing automatic background job) is a legitimate, already-authorized automatic replacement policy -- it only proceeds when its own AcoustID fingerprint check finds a verified match, and it now uses the same authoritative Plan/Apply engine contract as the manual path rather than a separate ad hoc mutation. Every other track-level replacement in this repository requires an explicit human "Apply Replacement" click via `TrackReplacementModal.tsx`. Neither path allows AI-only evidence to authorize a mutation.

---

## 3. Engine Transaction Flow (`track_replacement_v1`)

```
[Web Manager: automatic job, or operator via TrackReplacementModal]
       │
       │ 1. POST /tracks/replacement/plan  (beets_control_agent.py -> transaction_engine.create_track_replacement_plan)
       ▼
[Transaction Engine]
       ├── Resolve original file from original_item_id (DB read), cross-check any client-supplied original_path exactly
       ├── Validate candidate under an approved staging root; validate original under the music-library root (disjoint root sets)
       ├── Full symlink-component walk (normalize-before-resolve, so a symlink is never transparently erased before it can be caught) + stat capture (inode/dev/size/mtime_ns) for both files
       ├── Require + cross-verify matching_contract (Recording ID, Release Group ID when known, AI-only evidence rejected)
       ├── Compute quarantine destination under the server-derived REPLACEMENT_QUARANTINE_DIR (client-supplied quarantine_root ignored)
       └── store.create(...) -> real transaction ID used for both the quarantine path and the returned operation_id (no second, discarded ID)
       │
       │ 2. Human review (frontend) OR automatic job's own pre-check, presenting existing-track identity, candidate identity, why-authorized evidence, planned quarantine path, rollback availability
       │
       │ 3. POST /tracks/replacement/apply  (only reachable with a real operation_id from step 1)
       ▼
[Transaction Engine]
       ├── Acquire item-ID lock, then the per-operation lock (item lock outer, deadlock-free since each operation's lock is unique to it)
       ├── Idempotency check: if already Completed, return the completed state without re-mutating
       ├── Resume from durable apply_steps if this is a crash-recovery retry (skip already-completed steps, sanity-check consistency)
       ├── Re-verify full TOCTOU signature + root containment + symlink-component safety for both files (not just the candidate)
       ├── Move the original to quarantine via the hardened _safe_rename (EXDEV-safe) primitive -- the replacement candidate file is never moved
       ├── Remove the original's Beets DB row (rowcount verified == 1; any exception fails closed with mutated: true, never swallowed)
       ├── Re-read the DB to confirm the row is actually gone, and confirm the quarantined file exists and is non-empty
       └── Only then mark status="Completed"
```

---

## 4. Idempotency, Crash Recovery & Rollback

1. **Idempotency**: repeated Apply calls for the same `operation_id` are serialized by the item + operation lock pair and short-circuit to the already-`Completed` result rather than re-mutating.
2. **Crash recovery**: `apply_steps` metadata is persisted after each meaningful stage. A retry that arrives after a crash mid-Apply checks what already happened (including a physical check that a claimed-quarantined file is actually present) before deciding whether to repeat a step.
3. **Rollback** (`rollback_track_replacement`): requires `mutation_family == "track_replacement_v1"` and `status == "Completed"`; re-validates the quarantine path's root/symlink safety and refuses if the original path is unexpectedly already occupied. Moves the quarantined file to a `.rollback-holding` sibling first, then to the original path -- if the second move fails, it is moved back to its quarantine slot and the transaction reports `Failed`, never a false `Rolled Back`. The Beets DB row is restored from a full-row capture taken at Plan time; if the item ID has since been reused, or the DB restore itself fails, the transaction reports `Partially Rolled Back` (filesystem restored, DB not) rather than overclaiming. Only a fully successful restore reports `Rolled Back`. Repeated rollback attempts on an already-rolled-back transaction are refused, not silently re-run.
4. **Generic rollback dispatch**: the shared `/api/transactions/<id>/rollback` route looks up the transaction's actual `mutation_family` via `beets_client.get_transaction()` and dispatches to `rollback_track_replacement` / `rollback_import_review_cleanup` / a clean refusal for other known families / 404 for unknown IDs -- it no longer assumes every unrecognized ID belongs to Import Review cleanup.

---

## 5. What remains open (ARCH-003 debt after this wave)

- `_import_and_tag_disk`/`replace_existing_item_ids`, `repair_album_mb_tracks` still perform direct SQLite/local-subprocess mutation and are not part of this mutation family (see section 2 for why). ARCH-003 remains **In Progress**, not Done.
- No engine-side AcoustID fingerprinting was added; the design intentionally reuses the existing `_acoustid_fingerprint_match`/`_acoustid_fingerprint_ids` infrastructure that the automatic background job already used, rather than adding a second fingerprint stack.
