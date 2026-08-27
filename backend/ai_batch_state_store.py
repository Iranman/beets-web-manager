"""Durable, versioned, compare-and-swap (CAS) state store for AI Batch Import and AI review workflows.

Implements ARCH-010:
- Schema versioning (schema_version = 1)
- Revision tracking (revision = 1, 2, 3...)
- Compare-And-Swap (CAS) optimistic concurrency control across workers/processes
- Crash-safe, ACID-backed SQLite persistence with WAL mode
- One-way migration of legacy unversioned JSON state files
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


class AiBatchStateConflictError(RuntimeError):
    """Raised when a compare-and-swap (CAS) update fails due to a stale revision,
    OR two writers race to create the same new row (normalized to the same
    conflict contract as a stale-revision update -- the caller should not
    need to handle a raw sqlite3.IntegrityError differently)."""
    pass


class AiBatchStateUnsupportedSchemaError(RuntimeError):
    """Raised when a persisted row's schema_version is not one this reader
    version supports -- a genuinely unsupported/future format that must be
    rejected explicitly rather than decoded as if it were the current
    schema."""
    pass


# Wave 26 / ARCH-010: the set of schema_version values this reader knows how
# to load (as-is or via an explicit migration step). A row whose
# schema_version is not in this set is refused -- never silently decoded
# under the current schema's assumptions. There is exactly one schema today
# (CURRENT_SCHEMA_VERSION); when a second version is ever introduced,
# SUPPORTED_SCHEMA_VERSIONS grows to include it and the reader gains an
# explicit migration branch for it, rather than the set silently
# encompassing "whatever integer happens to be in the row."
CURRENT_SCHEMA_VERSION = 1
SUPPORTED_SCHEMA_VERSIONS = {1}


def _check_supported_schema(entity: str, ident: str, schema_version: Any) -> None:
    try:
        version = int(schema_version)
    except (TypeError, ValueError):
        raise AiBatchStateUnsupportedSchemaError(
            f"{entity} {ident!r} has a non-integer schema_version ({schema_version!r})"
        )
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise AiBatchStateUnsupportedSchemaError(
            f"{entity} {ident!r} has unsupported schema_version={version} "
            f"(supported: {sorted(SUPPORTED_SCHEMA_VERSIONS)})"
        )


class AiBatchStateStore:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_db(self) -> None:
        with self._get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS ai_batch_state (
                    id TEXT PRIMARY KEY,
                    schema_version INTEGER NOT NULL DEFAULT 1,
                    revision INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL,
                    job_id TEXT,
                    payload_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    writer_id TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS ai_review_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    schema_version INTEGER NOT NULL DEFAULT 1,
                    revision INTEGER NOT NULL DEFAULT 1,
                    album_id INTEGER,
                    mb_albumid TEXT,
                    decision TEXT,
                    payload_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS ai_suggestions_cache (
                    album_id INTEGER PRIMARY KEY,
                    schema_version INTEGER NOT NULL DEFAULT 1,
                    mb_albumid TEXT,
                    payload_json TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)
            # Wave 26 / ARCH-010: source_key makes legacy-migration inserts
            # idempotent (see migrate_legacy_files) -- normal runtime calls
            # to record_review_decision() never pass one, so ordinary
            # decisions remain a pure append-only log (SQLite does not
            # treat multiple NULLs as conflicting under a UNIQUE index).
            # Added via ALTER rather than only in CREATE TABLE so a
            # database created by an earlier build of this same feature
            # (before this column existed) still gets it.
            existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(ai_review_decisions)").fetchall()}
            if "source_key" not in existing_cols:
                conn.execute("ALTER TABLE ai_review_decisions ADD COLUMN source_key TEXT")
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_ai_review_decisions_source_key "
                "ON ai_review_decisions(source_key)"
            )

    def get_batch_state(self, batch_job_id: str) -> Optional[Dict[str, Any]]:
        ident = str(batch_job_id or "").strip()
        if not ident:
            return None
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM ai_batch_state WHERE id = ? OR job_id = ?",
                (ident, ident),
            ).fetchone()
            if not row:
                return None
            _check_supported_schema("Batch", row["id"], row["schema_version"])
            try:
                state = json.loads(row["payload_json"])
            except Exception:
                state = {}
            if not isinstance(state, dict):
                state = {}
            state["batch_job_id"] = row["id"]
            state["job_id"] = row["job_id"] or state.get("job_id", "")
            state["status"] = row["status"]
            state["schema_version"] = row["schema_version"]
            state["revision"] = row["revision"]
            state["created_at"] = row["created_at"]
            state["updated_at"] = row["updated_at"]
            return state

    def list_batch_states(self) -> List[Dict[str, Any]]:
        with self._get_conn() as conn:
            rows = conn.execute("SELECT * FROM ai_batch_state ORDER BY updated_at DESC").fetchall()
            results = []
            for row in rows:
                try:
                    _check_supported_schema("Batch", row["id"], row["schema_version"])
                except AiBatchStateUnsupportedSchemaError:
                    # A listing spans every batch -- one row with an
                    # unsupported future schema must not take down
                    # visibility into every other batch. Excluded, not
                    # decoded: this is not "silently loading the payload",
                    # it is refusing to load it at all.
                    continue
                try:
                    state = json.loads(row["payload_json"])
                except Exception:
                    state = {}
                if isinstance(state, dict):
                    state["batch_job_id"] = row["id"]
                    state["job_id"] = row["job_id"] or state.get("job_id", "")
                    state["status"] = row["status"]
                    state["schema_version"] = row["schema_version"]
                    state["revision"] = row["revision"]
                    state["created_at"] = row["created_at"]
                    state["updated_at"] = row["updated_at"]
                    results.append(state)
            return results

    def save_batch_state(
        self,
        state: Dict[str, Any],
        expected_revision: Optional[int] = None,
        writer_id: str = "",
    ) -> Dict[str, Any]:
        state_copy = dict(state)
        batch_job_id = str(state_copy.get("batch_job_id") or state_copy.get("id") or "").strip()
        if not batch_job_id:
            raise ValueError("State must contain a valid batch_job_id")

        job_id = str(state_copy.get("job_id") or "").strip()
        status = str(state_copy.get("status") or "queued").strip()
        now = time.time()
        created_at = float(state_copy.get("created_at") or now)

        # Prepare payload dictionary excluding DB metadata fields
        payload = dict(state_copy)
        payload.pop("schema_version", None)
        payload.pop("revision", None)

        payload_json = json.dumps(payload, indent=2, sort_keys=True)

        with self._get_conn() as conn:
            row = conn.execute("SELECT revision, schema_version FROM ai_batch_state WHERE id = ?", (batch_job_id,)).fetchone()
            if row:
                # Wave 26 / ARCH-010 correction: expected_revision was
                # previously optional even against an existing row -- when
                # omitted, the code silently trusted whatever revision it
                # had *just* read in this same call as the caller's
                # "expected" value. That defeats CAS for the actual failure
                # mode it exists to prevent: a caller holding a genuinely
                # stale in-memory snapshot (e.g. its own `revision` field
                # was accidentally dropped somewhere upstream) would have
                # its stale payload written on top of a concurrent writer's
                # newer revision, silently. An update against an existing
                # row now REQUIRES the caller to state which revision it
                # believes it is updating -- omitting it is a caller bug,
                # not a valid "just overwrite whatever is there" request.
                if expected_revision is None:
                    raise ValueError(
                        f"expected_revision is required to update existing batch {batch_job_id} "
                        "(pass the revision this state was last read at; omitting it would risk "
                        "silently overwriting a concurrent writer's change)"
                    )
                _check_supported_schema("Batch", batch_job_id, row["schema_version"])
                current_rev = int(row["revision"])
                if expected_revision != current_rev:
                    raise AiBatchStateConflictError(
                        f"State conflict for batch {batch_job_id}: expected revision {expected_revision}, current revision is {current_rev}"
                    )
                new_rev = current_rev + 1
                cur = conn.execute(
                    """
                    UPDATE ai_batch_state
                    SET revision = ?, status = ?, job_id = ?, payload_json = ?, updated_at = ?, writer_id = ?
                    WHERE id = ? AND revision = ?
                    """,
                    (new_rev, status, job_id, payload_json, now, writer_id, batch_job_id, current_rev),
                )
                if cur.rowcount == 0:
                    raise AiBatchStateConflictError(
                        f"State conflict for batch {batch_job_id}: concurrent update occurred"
                    )
                state_copy["revision"] = new_rev
            else:
                if expected_revision is not None and expected_revision != 0:
                    raise AiBatchStateConflictError(
                        f"State conflict for new batch {batch_job_id}: expected revision {expected_revision}, but batch does not exist"
                    )
                new_rev = 1
                try:
                    conn.execute(
                        """
                        INSERT INTO ai_batch_state
                        (id, schema_version, revision, status, job_id, payload_json, created_at, updated_at, writer_id)
                        VALUES (?, 1, 1, ?, ?, ?, ?, ?, ?)
                        """,
                        (batch_job_id, status, job_id, payload_json, created_at, now, writer_id),
                    )
                except sqlite3.IntegrityError:
                    # Wave 26 / ARCH-010: two workers can both observe "row
                    # does not exist" (the SELECT above) and both attempt
                    # this INSERT -- the loser must receive the same
                    # conflict contract as a stale-revision update, not a
                    # raw sqlite3.IntegrityError the caller has to know to
                    # special-case.
                    raise AiBatchStateConflictError(
                        f"State conflict for new batch {batch_job_id}: concurrent create occurred"
                    )
                state_copy["revision"] = 1

            state_copy["schema_version"] = 1
            state_copy["updated_at"] = now
            return state_copy

    def create_batch_state(self, state: Dict[str, Any], writer_id: str = "") -> Dict[str, Any]:
        """Explicit create semantic for a brand-new batch: fails with
        AiBatchStateConflictError if a row for this batch_job_id already
        exists (including a concurrent creator that won the race), rather
        than silently updating it. Equivalent to
        save_batch_state(state, expected_revision=0, ...) -- provided as a
        clearer, harder-to-misuse spelling for the common "this is a new
        batch" call site."""
        return self.save_batch_state(state, expected_revision=0, writer_id=writer_id)

    def record_review_decision(self, entry: Dict[str, Any], source_key: Optional[str] = None) -> Dict[str, Any]:
        """Record an AI review decision. `source_key` is used ONLY by
        migrate_legacy_files() to make re-running migration idempotent
        (a UNIQUE index on this column rejects a decision already migrated
        by a prior, crashed attempt) -- normal runtime callers never pass
        it, so this remains a plain append-only log for real decisions.
        Raises sqlite3.IntegrityError if source_key is supplied and a
        decision with that exact source_key already exists."""
        entry_copy = dict(entry)
        album_id = int(entry_copy.get("album_id") or 0)
        mb_albumid = str(entry_copy.get("mb_albumid") or "").strip()
        decision = str(entry_copy.get("decision") or "").strip()
        now = time.time()
        payload_json = json.dumps(entry_copy, indent=2, sort_keys=True)

        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT INTO ai_review_decisions
                (schema_version, revision, album_id, mb_albumid, decision, payload_json, created_at, source_key)
                VALUES (1, 1, ?, ?, ?, ?, ?, ?)
                """,
                (album_id, mb_albumid, decision, payload_json, now, source_key),
            )
        entry_copy["schema_version"] = 1
        entry_copy["revision"] = 1
        entry_copy["created_at"] = now
        return entry_copy

    def list_review_decisions(self, limit: int = 500) -> List[Dict[str, Any]]:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT id, schema_version, payload_json FROM ai_review_decisions ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            decisions = []
            for row in rows:
                try:
                    _check_supported_schema("Review decision", row["id"], row["schema_version"])
                except AiBatchStateUnsupportedSchemaError:
                    continue
                try:
                    dec = json.loads(row["payload_json"])
                    if isinstance(dec, dict):
                        decisions.append(dec)
                except Exception:
                    pass
            return decisions

    def save_suggestions_cache(self, suggestions: Dict[str, Any]) -> None:
        now = time.time()
        with self._get_conn() as conn:
            for aid_str, payload in suggestions.items():
                try:
                    aid = int(aid_str)
                except Exception:
                    continue
                if isinstance(payload, dict):
                    mb_albumid = str(payload.get("mb_albumid") or "").strip()
                    payload_json = json.dumps(payload, indent=2)
                    conn.execute(
                        """
                        INSERT INTO ai_suggestions_cache (album_id, schema_version, mb_albumid, payload_json, updated_at)
                        VALUES (?, 1, ?, ?, ?)
                        ON CONFLICT(album_id) DO UPDATE SET
                            schema_version = 1,
                            mb_albumid = excluded.mb_albumid,
                            payload_json = excluded.payload_json,
                            updated_at = excluded.updated_at
                        """,
                        (aid, mb_albumid, payload_json, now),
                    )

    def load_suggestions_cache(self) -> Dict[str, Any]:
        with self._get_conn() as conn:
            rows = conn.execute("SELECT album_id, payload_json FROM ai_suggestions_cache").fetchall()
            results = {}
            for row in rows:
                try:
                    results[str(row["album_id"])] = json.loads(row["payload_json"])
                except Exception:
                    pass
            return results

    def migrate_legacy_files(
        self,
        state_dir: Path,
        decisions_file: Optional[Path] = None,
        suggestions_file: Optional[Path] = None,
    ) -> Dict[str, Any]:
        """One-way migration of legacy unversioned JSON state into this
        store. Returns {"migrated": <count>, "errors": [{"file", "error"}, ...]}.

        Crash-idempotent: safe to call repeatedly, including immediately
        after a crash mid-migration.
        - Batch state: a row is inserted only if get_batch_state() shows
          none exists yet; the source file is renamed to *.migrated.bak
          only after that check/insert succeeds. A retry after a crash
          between insert and rename finds the row already present and
          skips straight to the (idempotent) rename.
        - Review decisions: each decision is migrated with a deterministic
          `source_key` (the source file's name + its index + a hash of its
          own content) enforced UNIQUE by the schema. A retry that
          re-reads an as-yet-unrenamed decisions file re-attempts the same
          inserts; the UNIQUE index rejects whichever decisions a prior
          crashed attempt already committed (sqlite3.IntegrityError,
          treated as "already migrated", not an error) -- no duplicate
          review decisions are ever created.
        - Suggestions cache: save_suggestions_cache() is a plain UPSERT
          keyed by album_id, already naturally idempotent.

        A file that cannot be parsed/understood, or a decision that fails
        to migrate for a real reason, is left in place -- never deleted,
        never silently discarded -- and reported in `errors` instead."""
        migrated_count = 0
        errors: List[Dict[str, str]] = []
        state_dir = Path(state_dir)

        if state_dir.exists():
            for json_path in sorted(state_dir.glob("*.json")):
                if json_path.name.endswith(".migrated.bak") or json_path.name == "ai_batch_state.db":
                    continue
                try:
                    text = json_path.read_text(encoding="utf-8")
                    state = json.loads(text)
                except Exception as ex:
                    errors.append({"file": str(json_path), "error": f"unreadable/corrupt batch state file, left in place: {ex}"})
                    continue
                try:
                    if isinstance(state, dict) and (state.get("batch_job_id") or state.get("job_id")):
                        batch_id = str(state.get("batch_job_id") or state.get("job_id"))
                        existing = self.get_batch_state(batch_id)
                        if not existing:
                            self.save_batch_state(state, expected_revision=None, writer_id="migration")
                            migrated_count += 1
                        bak_path = json_path.with_suffix(".json.migrated.bak")
                        os.replace(str(json_path), str(bak_path))
                    else:
                        errors.append({"file": str(json_path), "error": "not a recognizable batch state record, left in place"})
                except Exception as ex:
                    errors.append({"file": str(json_path), "error": f"migration failed, left in place: {ex}"})

        if decisions_file and Path(decisions_file).exists():
            dec_path = Path(decisions_file)
            if not dec_path.name.endswith(".migrated.bak"):
                items: Optional[list] = None
                try:
                    text = dec_path.read_text(encoding="utf-8")
                    items = json.loads(text)
                except Exception as ex:
                    errors.append({"file": str(dec_path), "error": f"unreadable/corrupt review decisions file, left in place: {ex}"})
                if isinstance(items, list):
                    all_ok = True
                    inserted_here = 0
                    for idx, item in enumerate(reversed(items)):
                        if not isinstance(item, dict):
                            continue
                        source_key = f"{dec_path.name}:{idx}:" + hashlib.sha256(
                            json.dumps(item, sort_keys=True, default=str).encode("utf-8")
                        ).hexdigest()
                        try:
                            self.record_review_decision(item, source_key=source_key)
                            inserted_here += 1
                        except sqlite3.IntegrityError:
                            # Already migrated by a prior (possibly
                            # crashed) attempt -- not an error, already done.
                            pass
                        except Exception as ex:
                            all_ok = False
                            errors.append({"file": str(dec_path), "error": f"decision at index {idx} failed to migrate: {ex}"})
                    migrated_count += inserted_here
                    if all_ok:
                        try:
                            bak_path = dec_path.with_suffix(".json.migrated.bak")
                            os.replace(str(dec_path), str(bak_path))
                        except Exception as ex:
                            errors.append({"file": str(dec_path), "error": f"migrated but could not rename source file, left in place: {ex}"})
                elif items is not None:
                    errors.append({"file": str(dec_path), "error": "not a list of review decisions, left in place"})

        if suggestions_file and Path(suggestions_file).exists():
            sug_path = Path(suggestions_file)
            if not sug_path.name.endswith(".migrated.bak"):
                try:
                    text = sug_path.read_text(encoding="utf-8")
                    data = json.loads(text)
                    if isinstance(data, dict):
                        self.save_suggestions_cache(data)
                        migrated_count += len(data)
                        bak_path = sug_path.with_suffix(".json.migrated.bak")
                        os.replace(str(sug_path), str(bak_path))
                    else:
                        errors.append({"file": str(sug_path), "error": "not a suggestions-cache dict, left in place"})
                except Exception as ex:
                    errors.append({"file": str(sug_path), "error": f"unreadable/corrupt suggestions cache file, left in place: {ex}"})

        return {"migrated": migrated_count, "errors": errors}
