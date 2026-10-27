"""Missing-track recovery carrier: the durable identity SPINE for the
download -> verify -> import-into-existing-album -> Plex-sync pipeline.

Phase 2 (ARCH mission: missing-track recovery). Phase-1 root cause: the
track->album identity (existing album, canonical Release Group, the expected
recording/release-track, track#, destination) lived only in the live
download-import job's Python closure and job metadata. At the one hand-off to
Import Review it was reduced to a boolean (`allow_existing`) plus a free-text
suggestion, and every consumer that re-entered from the folder on disk had to
re-derive identity from scratch -- which fails for a single-track
`<album> - <source> missing <ts>` folder and so a one-track recovery was
treated as a brand-new full-album import.

This module is the carrier: a durable, versioned, compare-and-swap record that
binds a staged download back to the album it is recovering a track for, so the
identity survives process restarts and job splits and is read back as
structured data -- never parsed from a folder name.

Design constraints (orchestrator / security):
* Persisted via the controlled-state substrate, mirroring
  ``backend.ai_batch_state_store`` (ARCH-010): schema-versioned, revision/CAS,
  crash-safe SQLite (WAL) under ``WEB_MANAGER_DATA_DIR`` -- NOT loose files in
  ``/downloads``.
* ``mb_releasegroupid`` is the CANONICAL album identity (ADR-0002). The
  supporting Release id is stored separately in ``release_id`` and is never
  substituted for the Release Group.
* ``load_for_folder`` keys on the exact staged folder PATH and returns the
  identity that was STORED at download time. It never trusts/derives identity
  from the folder name -- an unknown folder simply has no carrier.

This is the SPINE only. The specialist depth plugs in at the HOOK points
marked ``# HOOK(<owner>)`` across the pipeline:
* preflight single-track-recovery mode          -> music-identity-engineer
* verified-track insertion/merge via transaction -> library-transaction-engineer
* provider-specific carrier population/auto-import -> integrations-automation-engineer
* Review UI for a recovery vs a new-album import -> frontend-ux-engineer
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from backend.app_runtime import WEB_MANAGER_DATA_DIR, _s

logger = logging.getLogger(__name__)

CURRENT_SCHEMA_VERSION = 2
SUPPORTED_SCHEMA_VERSIONS = {1, 2}

# Carrier states that are TERMINAL for dedup/resume: a new request for the same
# server-derived identity must NOT resume one of these (it is done or discarded)
# -- it mints a fresh carrier. Every other state is a non-terminal recovery that
# a same-identity request RESUMES (recovery_service advancer, acquisition dedup).
TERMINAL_STATES = frozenset({"imported", "rolled_back"})

# States the bounded advancer (recovery_service) re-evaluates: a recovery that
# stalled in Review, or failed recoverably, or never advanced past staging. A
# claimed ('importing') or terminal carrier is owned by its job, never advanced.
ADVANCER_STATES = frozenset(
    {"review_required", "failed", "downloaded", "verifying", "created", "downloading"})

# Recovery/rollback lifecycle states. Ordered for readability only; the state
# machine is advanced explicitly by the pipeline, not implied by order.
CARRIER_STATES = (
    "created",        # identity captured, download not yet complete
    "downloading",
    "downloaded",     # files landed in the staged folder (folder bound)
    "verifying",      # HOOK(music-identity): fingerprint/MB verification
    "importing",      # HOOK(library-transaction): insertion/merge in progress
    "imported",       # merged into the existing album
    "review_required",  # could not auto-resolve; queued to Review WITH identity
    "failed",
    "rolled_back",
)

# Durable checkpoints for the single guarded recovery workflow (stage 16).
# A resume starts from the last reached checkpoint; the pipeline records them
# as it advances. Kept as data so the recovery job is resumable and idempotent.
CHECKPOINTS = (
    "CREATED",
    "DOWNLOADED",
    "STAGED",
    "VERIFIED",
    "IMPORTED",
    "PLEX_SYNCED",
)

# The one guarded, resumable recovery workflow (reuses job_engine +
# transaction_recovery/rollback_guard -- NOT a competing system). The
# acquisition layer applies this slug to ``job_contract.guarded(...)``.
RECOVERY_WORKFLOW_PREFIX = "missing-track-recovery"


def recovery_workflow_id(carrier_id: str) -> str:
    """Deterministic workflow id for a carrier's guarded recovery job, so a
    restart resumes the same workflow rather than starting a parallel one."""
    return f"{RECOVERY_WORKFLOW_PREFIX}-{_s(carrier_id).strip()}"


class RecoveryCarrierConflictError(RuntimeError):
    """Compare-and-swap failure: the carrier was updated by another writer
    since the caller read it (stale revision), or two writers raced to create
    the same carrier_id. Same contract as AiBatchStateConflictError."""


class RecoveryCarrierUnsupportedSchemaError(RuntimeError):
    """A persisted row's schema_version is not one this reader supports."""


def _folder_key(folder_path: str) -> str:
    """Stable lookup key for a staged folder: normalized absolute path
    (realpath best-effort), hashed. Case preserved (POSIX paths are
    case-sensitive). This keys the STORED identity to a path; it never
    encodes identity derived from the folder name."""
    raw = _s(folder_path).strip()
    if not raw:
        return ""
    try:
        norm = os.path.normpath(os.path.realpath(raw))
    except OSError:
        norm = os.path.normpath(os.path.abspath(raw))
    return hashlib.sha256(norm.encode("utf-8", "surrogatepass")).hexdigest()


def _track_signature(t: Any) -> str:
    """A stable per-track signature for the server-derived identity key.

    Prefer the MB recording id; else a (disc, track) position; else a
    normalized title. NEVER folder/source text (ADR-0002: identity is not
    reconstructed from a folder name). Returns "" for an unidentifiable track,
    which is dropped from the signature set so it cannot perturb the key."""
    if not isinstance(t, dict):
        return ""
    rid = _s(t.get("recording_id") or t.get("mb_trackid") or t.get("mb_recordingid")).strip().lower()
    if rid:
        return "r:" + rid
    try:
        trk = int(t.get("track") or t.get("track_no") or t.get("position") or 0)
        disc = int(t.get("disc") or t.get("disc_no") or t.get("medium") or 1)
    except (TypeError, ValueError):
        trk, disc = 0, 1
    if trk > 0:
        return f"p:{disc}:{trk}"
    title = _s(t.get("title")).strip().lower()
    return ("t:" + title) if title else ""


def compute_identity_key(existing_album_id: Any, mb_releasegroupid: Any,
                         wanted_tracks: Optional[List[Dict[str, Any]]]) -> str:
    """The stable, SERVER-derived recovery identity:
    ``sha256(existing_album_id | canonical rgid | sorted wanted-track sigs)``.

    Two independent "download missing" requests for the same album + canonical
    Release Group + same missing-track set yield the SAME key, so they dedupe
    onto one carrier/workflow/folder instead of a storm of per-request uuids.
    It is derived only from already-trusted structured inputs (never a client
    carrier id, never folder text), so it is safe as the dedup/resume anchor."""
    rgid = _s(mb_releasegroupid).strip().lower()
    sigs = sorted({s for s in (_track_signature(t) for t in (wanted_tracks or [])) if s})
    try:
        aid = int(existing_album_id or 0)
    except (TypeError, ValueError):
        aid = 0
    raw = "|".join([str(aid), rgid, *sigs])
    return hashlib.sha256(raw.encode("utf-8", "surrogatepass")).hexdigest()


def _check_supported_schema(carrier_id: str, schema_version: Any) -> None:
    try:
        version = int(schema_version)
    except (TypeError, ValueError):
        raise RecoveryCarrierUnsupportedSchemaError(
            f"carrier {carrier_id!r} has a non-integer schema_version ({schema_version!r})"
        )
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise RecoveryCarrierUnsupportedSchemaError(
            f"carrier {carrier_id!r} has unsupported schema_version={version} "
            f"(supported: {sorted(SUPPORTED_SCHEMA_VERSIONS)})"
        )


class RecoveryCarrierStore:
    """Crash-safe, versioned, CAS store for recovery carriers (one SQLite DB,
    WAL mode). Mirrors backend.ai_batch_state_store."""

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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS recovery_carrier (
                    carrier_id TEXT PRIMARY KEY,
                    schema_version INTEGER NOT NULL DEFAULT 1,
                    revision INTEGER NOT NULL DEFAULT 1,
                    state TEXT NOT NULL,
                    existing_album_id INTEGER NOT NULL DEFAULT 0,
                    mb_releasegroupid TEXT NOT NULL DEFAULT '',
                    release_id TEXT NOT NULL DEFAULT '',
                    destination_album_path TEXT NOT NULL DEFAULT '',
                    staged_folder_path TEXT NOT NULL DEFAULT '',
                    staged_folder_key TEXT NOT NULL DEFAULT '',
                    recovery_identity_key TEXT NOT NULL DEFAULT '',
                    origin_job_id TEXT NOT NULL DEFAULT '',
                    payload_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    writer_id TEXT
                )
                """
            )
            # One carrier -> MANY staged folders (every download attempt /
            # re-stage). Keyed by normalized-path hash so load_for_folder
            # resolves ANY attempt's folder to the same carrier instead of only
            # the last-bound one (the orphan-on-rebind root cause). An entry is
            # never deleted on a re-bind, so prior attempts keep resolving.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS recovery_staged_folder (
                    folder_key TEXT PRIMARY KEY,
                    carrier_id TEXT NOT NULL,
                    folder_path TEXT NOT NULL,
                    added_at REAL NOT NULL
                )
                """
            )
            # Migrate BEFORE creating any index on a v2-only column: on a real
            # v1 (base-schema) DB the recovery_carrier table already exists
            # without recovery_identity_key, so an index referencing it would
            # raise "no such column" unless the ALTER runs first. The migration
            # also writes recovery_staged_folder, so both tables must exist by
            # now (they do, created just above).
            self._migrate_v1_to_v2(conn)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_recovery_staged_folder_carrier "
                "ON recovery_staged_folder(carrier_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_recovery_carrier_folder "
                "ON recovery_carrier(staged_folder_key)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_recovery_carrier_identity "
                "ON recovery_carrier(recovery_identity_key)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_recovery_carrier_state "
                "ON recovery_carrier(state)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_recovery_carrier_job "
                "ON recovery_carrier(origin_job_id)"
            )

    def _migrate_v1_to_v2(self, conn: sqlite3.Connection) -> None:
        """Forward migration v1 -> v2 (idempotent). Adds recovery_identity_key
        and backfills it + the one-to-many recovery_staged_folder table from the
        existing rows, then stamps schema_version=2. A fresh DB created at v2
        (column already present, no v1 rows) is a no-op."""
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(recovery_carrier)")}
        if "recovery_identity_key" not in cols:
            conn.execute(
                "ALTER TABLE recovery_carrier "
                "ADD COLUMN recovery_identity_key TEXT NOT NULL DEFAULT ''")
        stale = conn.execute(
            "SELECT * FROM recovery_carrier WHERE schema_version < 2").fetchall()
        for row in stale:
            try:
                payload = json.loads(row["payload_json"])
                if not isinstance(payload, dict):
                    payload = {}
            except Exception:
                payload = {}
            key = compute_identity_key(
                row["existing_album_id"], row["mb_releasegroupid"],
                list(payload.get("wanted_tracks") or []) + list(payload.get("expected_tracks") or []))
            conn.execute(
                "UPDATE recovery_carrier SET recovery_identity_key = ?, schema_version = 2 "
                "WHERE carrier_id = ?", (key, row["carrier_id"]))
            legacy_folder = _s(row["staged_folder_path"])
            if legacy_folder.strip():
                self._record_staged_folder(conn, row["carrier_id"], legacy_folder)

    @staticmethod
    def _record_staged_folder(conn: sqlite3.Connection, carrier_id: str,
                              folder_path: str, folder_key: Optional[str] = None) -> None:
        """Map a staged folder to its carrier in the one-to-many side table.
        Idempotent (folder_key is the PK); never deletes a prior attempt, so
        every bound folder keeps resolving to the carrier. ``folder_key`` is
        passed precomputed from the CAS/upsert path so this does not call
        ``_folder_key`` a second time (keeps the single-call barrier semantics
        the CAS-race test relies on)."""
        path = _s(folder_path).strip()
        if not path:
            return
        key = folder_key if folder_key is not None else _folder_key(folder_path)
        cid = _s(carrier_id).strip()
        if not key or not cid:
            return
        conn.execute(
            "INSERT INTO recovery_staged_folder (folder_key, carrier_id, folder_path, added_at) "
            "VALUES (?,?,?,?) ON CONFLICT(folder_key) DO UPDATE SET "
            "carrier_id = excluded.carrier_id, folder_path = excluded.folder_path",
            (key, cid, _s(folder_path), time.time()))

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> Dict[str, Any]:
        _check_supported_schema(row["carrier_id"], row["schema_version"])
        try:
            payload = json.loads(row["payload_json"])
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        record = dict(payload)
        record.update(
            {
                "carrier_id": row["carrier_id"],
                "schema_version": row["schema_version"],
                "revision": row["revision"],
                "state": row["state"],
                "existing_album_id": row["existing_album_id"],
                "mb_releasegroupid": row["mb_releasegroupid"],
                "release_id": row["release_id"],
                "destination_album_path": row["destination_album_path"],
                "staged_folder_path": row["staged_folder_path"],
                "recovery_identity_key": (row["recovery_identity_key"]
                                          if "recovery_identity_key" in row.keys() else ""),
                "origin_job_id": row["origin_job_id"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        )
        return record

    def get(self, carrier_id: str) -> Optional[Dict[str, Any]]:
        ident = _s(carrier_id).strip()
        if not ident:
            return None
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM recovery_carrier WHERE carrier_id = ?", (ident,)
            ).fetchone()
            return self._row_to_record(row) if row else None

    def get_by_folder(self, folder_path: str) -> Optional[Dict[str, Any]]:
        """Resolve ANY staged-folder attempt (one-to-many side table) to its
        carrier. Falls back to the legacy last-bound column for a pre-v2 row
        the migration could not map (defensive; migration backfills the table)."""
        key = _folder_key(folder_path)
        if not key:
            return None
        with self._get_conn() as conn:
            link = conn.execute(
                "SELECT carrier_id FROM recovery_staged_folder WHERE folder_key = ?",
                (key,),
            ).fetchone()
            if link:
                row = conn.execute(
                    "SELECT * FROM recovery_carrier WHERE carrier_id = ?",
                    (link["carrier_id"],),
                ).fetchone()
                if row:
                    return self._row_to_record(row)
            row = conn.execute(
                "SELECT * FROM recovery_carrier WHERE staged_folder_key = ? "
                "ORDER BY updated_at DESC LIMIT 1",
                (key,),
            ).fetchone()
            return self._row_to_record(row) if row else None

    def get_by_identity(self, identity_key: str, *,
                        include_terminal: bool = False) -> Optional[Dict[str, Any]]:
        """The most-recently-updated carrier for a server-derived identity key,
        or None. By default a TERMINAL carrier (imported / rolled_back) is
        skipped, so a same-identity request resumes only a live/stuck recovery
        and never a finished one."""
        key = _s(identity_key).strip()
        if not key:
            return None
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM recovery_carrier WHERE recovery_identity_key = ? "
                "ORDER BY updated_at DESC",
                (key,),
            ).fetchall()
        for row in rows:
            rec = self._row_to_record(row)
            if include_terminal or _s(rec.get("state")) not in TERMINAL_STATES:
                return rec
        return None

    def list_by_states(self, states: Iterable[str], *, limit: int = 100) -> List[Dict[str, Any]]:
        """Carriers currently in any of ``states``, oldest-updated first (fair
        draining), capped at ``limit``. Used by the bounded advancer."""
        wanted = [s for s in (states or []) if s]
        if not wanted:
            return []
        placeholders = ",".join("?" for _ in wanted)
        with self._get_conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM recovery_carrier WHERE state IN ({placeholders}) "
                "ORDER BY updated_at ASC LIMIT ?",
                (*wanted, int(max(1, limit))),
            ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def folders_for_carrier(self, carrier_id: str) -> List[str]:
        """Every staged folder path ever bound to a carrier (newest first)."""
        cid = _s(carrier_id).strip()
        if not cid:
            return []
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT folder_path FROM recovery_staged_folder WHERE carrier_id = ? "
                "ORDER BY added_at DESC",
                (cid,),
            ).fetchall()
        return [_s(r["folder_path"]) for r in rows if _s(r["folder_path"]).strip()]

    def get_by_job(self, origin_job_id: str) -> Optional[Dict[str, Any]]:
        ident = _s(origin_job_id).strip()
        if not ident:
            return None
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM recovery_carrier WHERE origin_job_id = ? "
                "ORDER BY updated_at DESC LIMIT 1",
                (ident,),
            ).fetchone()
            return self._row_to_record(row) if row else None

    _COLUMN_FIELDS = (
        "state",
        "existing_album_id",
        "mb_releasegroupid",
        "release_id",
        "destination_album_path",
        "staged_folder_path",
        "recovery_identity_key",
        "origin_job_id",
    )

    def upsert(self, carrier_id: str, columns: Dict[str, Any],
               payload: Dict[str, Any], *, writer_id: str = "") -> Dict[str, Any]:
        """Create the carrier if absent, else replace it. Not CAS -- used only
        for the idempotent create-at-download (same carrier_id = same
        recovery). Concurrent updates after creation must use ``cas_update``."""
        ident = _s(carrier_id).strip() or uuid.uuid4().hex
        now = time.time()
        folder_key = _folder_key(columns.get("staged_folder_path", ""))
        with self._get_conn() as conn:
            existing = conn.execute(
                "SELECT created_at FROM recovery_carrier WHERE carrier_id = ?", (ident,)
            ).fetchone()
            created_at = existing["created_at"] if existing else now
            conn.execute(
                """
                INSERT INTO recovery_carrier
                    (carrier_id, schema_version, revision, state, existing_album_id,
                     mb_releasegroupid, release_id, destination_album_path,
                     staged_folder_path, staged_folder_key, recovery_identity_key,
                     origin_job_id, payload_json, created_at, updated_at, writer_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(carrier_id) DO UPDATE SET
                    revision = recovery_carrier.revision + 1,
                    state = excluded.state,
                    existing_album_id = excluded.existing_album_id,
                    mb_releasegroupid = excluded.mb_releasegroupid,
                    release_id = excluded.release_id,
                    destination_album_path = excluded.destination_album_path,
                    staged_folder_path = excluded.staged_folder_path,
                    staged_folder_key = excluded.staged_folder_key,
                    recovery_identity_key = excluded.recovery_identity_key,
                    origin_job_id = excluded.origin_job_id,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at,
                    writer_id = excluded.writer_id
                """,
                (
                    ident, CURRENT_SCHEMA_VERSION, 1,
                    _s(columns.get("state") or "created"),
                    int(columns.get("existing_album_id") or 0),
                    _s(columns.get("mb_releasegroupid")),
                    _s(columns.get("release_id")),
                    _s(columns.get("destination_album_path")),
                    _s(columns.get("staged_folder_path")),
                    folder_key,
                    _s(columns.get("recovery_identity_key")),
                    _s(columns.get("origin_job_id")),
                    json.dumps(payload or {}),
                    created_at, now, _s(writer_id),
                ),
            )
            self._record_staged_folder(conn, ident, _s(columns.get("staged_folder_path")), folder_key)
        got = self.get(ident)
        assert got is not None
        return got

    def cas_update(self, carrier_id: str, expected_revision: Optional[int],
                   columns: Dict[str, Any], payload: Dict[str, Any], *,
                   writer_id: str = "") -> Dict[str, Any]:
        """Merge ``columns`` + ``payload`` into an existing carrier. When
        ``expected_revision`` is given the write is a compare-and-swap and
        raises ``RecoveryCarrierConflictError`` on a stale revision."""
        ident = _s(carrier_id).strip()
        if not ident:
            raise RecoveryCarrierConflictError("missing carrier_id")
        now = time.time()
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM recovery_carrier WHERE carrier_id = ?", (ident,)
            ).fetchone()
            if not row:
                raise RecoveryCarrierConflictError(f"carrier {ident!r} does not exist")
            _check_supported_schema(ident, row["schema_version"])
            if expected_revision is not None and int(row["revision"]) != int(expected_revision):
                raise RecoveryCarrierConflictError(
                    f"carrier {ident!r} revision is {row['revision']}, "
                    f"expected {expected_revision}"
                )
            merged_cols = {f: row[f] for f in self._COLUMN_FIELDS}
            for f in self._COLUMN_FIELDS:
                if f in columns and columns[f] is not None:
                    merged_cols[f] = columns[f]
            try:
                base_payload = json.loads(row["payload_json"])
            except Exception:
                base_payload = {}
            if not isinstance(base_payload, dict):
                base_payload = {}
            base_payload.update(payload or {})
            folder_key = _folder_key(merged_cols.get("staged_folder_path", ""))
            cur = conn.execute(
                """
                UPDATE recovery_carrier SET
                    revision = revision + 1,
                    state = ?, existing_album_id = ?, mb_releasegroupid = ?,
                    release_id = ?, destination_album_path = ?,
                    staged_folder_path = ?, staged_folder_key = ?,
                    recovery_identity_key = ?, origin_job_id = ?,
                    payload_json = ?, updated_at = ?, writer_id = ?
                WHERE carrier_id = ? AND revision = ?
                """,
                (
                    _s(merged_cols.get("state") or "created"),
                    int(merged_cols.get("existing_album_id") or 0),
                    _s(merged_cols.get("mb_releasegroupid")),
                    _s(merged_cols.get("release_id")),
                    _s(merged_cols.get("destination_album_path")),
                    _s(merged_cols.get("staged_folder_path")),
                    folder_key,
                    _s(merged_cols.get("recovery_identity_key")),
                    _s(merged_cols.get("origin_job_id")),
                    json.dumps(base_payload),
                    now, _s(writer_id),
                    ident, int(row["revision"]),
                ),
            )
            # Atomic CAS backstop (mirrors ai_batch_state_store L246): the
            # Python pre-check above is only a fast path. Two WAL connections
            # can both SELECT the same revision before either commits, both
            # pass that pre-check, then one UPDATE matches 1 row (winner) and
            # the other matches 0 rows WITHOUT raising. Without this check the
            # loser would fall through to ``get(ident)`` and be handed the
            # WINNER's record as a false success -- defeating the
            # ``_recovery_claim_importing`` serializer and allowing two
            # confirmers to import the same track. A genuine 0-row UPDATE of an
            # existing row is a lost race, including on an expected_revision=None
            # blind write (the UPDATE still targets the revision we just read).
            if cur.rowcount == 0:
                raise RecoveryCarrierConflictError(
                    f"carrier {ident!r} revision changed concurrently")
            # One-to-many: record this (possibly new) staged folder so every
            # attempt resolves to the carrier, without clearing prior attempts.
            self._record_staged_folder(conn, ident, _s(merged_cols.get("staged_folder_path")), folder_key)
        got = self.get(ident)
        if got is None:
            raise RecoveryCarrierConflictError(f"carrier {ident!r} vanished during update")
        return got


# ── store registry (one store per resolved DB path; tests/deploys vary it) ──
_store_registry_lock = threading.Lock()
_store_registry: Dict[str, RecoveryCarrierStore] = {}


def _store() -> RecoveryCarrierStore:
    active_dir = Path(
        os.environ.get(
            "RECOVERY_CARRIER_STATE_DIR",
            str(WEB_MANAGER_DATA_DIR / "recovery_carriers"),
        )
    )
    db_path = active_dir / "recovery_carriers.db"
    key = os.path.normpath(os.path.abspath(str(db_path)))
    with _store_registry_lock:
        store = _store_registry.get(key)
        if store is None:
            store = RecoveryCarrierStore(db_path)
            _store_registry[key] = store
        return store


def _normalise_expected_tracks(expected: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """One entry per expected (missing) track. Canonical album identity is the
    Release Group on the carrier; each entry carries the recording-level
    evidence the verifier (music-identity) needs to prove a file IS the track."""
    out: List[Dict[str, Any]] = []
    for t in expected or []:
        if not isinstance(t, dict):
            continue
        out.append(
            {
                "recording_id": _s(t.get("recording_id") or t.get("mb_recordingid")),
                "track": t.get("track") or t.get("track_no") or t.get("position"),
                "disc": t.get("disc") or t.get("disc_no") or t.get("medium"),
                "duration": t.get("duration") or t.get("length"),
                "title": _s(t.get("title")),
            }
        )
    return out


# ───────────────────────── published accessor API ─────────────────────────
# These four are the contract the downstream specialists consume:
#   create_carrier(...)        create at download (stage 5)
#   load_for_folder(path)      preflight / AI-batch / review lookup by folder
#   load_for_job(job_id)       recovery-job resume lookup
#   advance_state(...)         checkpointed state transitions (stage 16)


def create_carrier(
    *,
    carrier_id: str = "",
    existing_album_id: int,
    mb_releasegroupid: str,
    release_id: str = "",
    destination_album_path: str = "",
    wanted_tracks: Optional[List[Dict[str, Any]]] = None,
    expected_tracks: Optional[List[Dict[str, Any]]] = None,
    source: Optional[Dict[str, Any]] = None,
    origin_job_id: str = "",
    staged_folder_path: str = "",
    state: str = "created",
) -> Dict[str, Any]:
    """Create (or idempotently refresh, by ``carrier_id``) a recovery carrier.

    ``mb_releasegroupid`` is the canonical album identity and is required;
    ``release_id`` is the supporting Release and is stored separately. Returns
    the stored record (including its ``carrier_id``, ``revision`` and
    ``recovery_workflow_id``)."""
    rgid = _s(mb_releasegroupid).strip()
    if not rgid:
        # Canonical identity is non-negotiable (ADR-0002); without it this is
        # not a recovery carrier and must not be created as one.
        raise ValueError("recovery carrier requires a canonical mb_releasegroupid")
    ident = _s(carrier_id).strip() or uuid.uuid4().hex
    # Server-derived stable identity: computed here from the album + canonical
    # RG + the missing-track set (never from a client carrier id or folder
    # name), so two identical "download missing" requests dedupe onto it.
    identity_key = compute_identity_key(
        existing_album_id, rgid,
        list(wanted_tracks or []) + list(expected_tracks or []))
    columns = {
        "state": state if state in CARRIER_STATES else "created",
        "existing_album_id": int(existing_album_id or 0),
        "mb_releasegroupid": rgid,
        "release_id": _s(release_id).strip(),
        "destination_album_path": _s(destination_album_path),
        "staged_folder_path": _s(staged_folder_path),
        "recovery_identity_key": identity_key,
        "origin_job_id": _s(origin_job_id),
    }
    payload = {
        "wanted_tracks": list(wanted_tracks or []),
        "expected_tracks": _normalise_expected_tracks(expected_tracks),
        "source": dict(source or {}),
        "verification_evidence": {},   # HOOK(music-identity): filled on verify
        "checkpoint": "CREATED",
        "recovery_workflow_id": recovery_workflow_id(ident),
    }
    return _store().upsert(ident, columns, payload, writer_id=origin_job_id)


def get_carrier(carrier_id: str) -> Optional[Dict[str, Any]]:
    return _store().get(carrier_id)


def load_for_folder(folder_path: str) -> Optional[Dict[str, Any]]:
    """Return the carrier whose staged folder is exactly ``folder_path``, or
    None. The identity comes from the store (written at download), NEVER parsed
    from the folder name -- an unknown folder has no carrier."""
    return _store().get_by_folder(folder_path)


def load_for_job(origin_job_id: str) -> Optional[Dict[str, Any]]:
    return _store().get_by_job(origin_job_id)


def get_by_identity(existing_album_id: Any = None, mb_releasegroupid: Any = None,
                    wanted_tracks: Optional[List[Dict[str, Any]]] = None, *,
                    identity_key: str = "",
                    include_terminal: bool = False) -> Optional[Dict[str, Any]]:
    """Resume lookup by the SERVER-derived identity (not a client carrier id).
    Either pass a precomputed ``identity_key`` or the (album, RG, wanted-tracks)
    triple it is derived from. Returns the most-recent NON-terminal carrier for
    that identity by default, so a duplicate request resumes it instead of
    minting a new carrier/workflow/folder (the storm fix)."""
    key = _s(identity_key).strip() or compute_identity_key(
        existing_album_id, mb_releasegroupid, wanted_tracks)
    return _store().get_by_identity(key, include_terminal=include_terminal)


def list_carriers_by_state(states: Iterable[str], *, limit: int = 100) -> List[Dict[str, Any]]:
    """Carriers in any of ``states`` (oldest-updated first), for the bounded
    recovery advancer. A read-only listing; never mutates."""
    return _store().list_by_states(states, limit=limit)


def staged_folders_for(carrier_id: str) -> List[str]:
    """Every staged folder bound to a carrier (newest first)."""
    return _store().folders_for_carrier(carrier_id)


def bind_staged_folder(carrier_id: str, folder_path: str, *,
                       expected_revision: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """Record the staged download folder on the carrier so ``load_for_folder``
    (AI-batch scan, pending-review, preflight) can find it. Returns the updated
    record, or None if the carrier does not exist."""
    if not _s(folder_path).strip():
        return None
    try:
        return _store().cas_update(
            carrier_id, expected_revision,
            {"staged_folder_path": _s(folder_path)}, {}, writer_id=carrier_id,
        )
    except RecoveryCarrierConflictError:
        if _store().get(carrier_id) is None:
            return None
        raise


def advance_state(carrier_id: str, state: str, *, checkpoint: str = "",
                  evidence: Optional[Dict[str, Any]] = None,
                  payload_extra: Optional[Dict[str, Any]] = None,
                  expected_revision: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """Transition the carrier's recovery state and (optionally) record a
    durable checkpoint and verification evidence. Used by the single guarded
    recovery workflow (stage 16) so a restart resumes from the last
    checkpoint. Returns the updated record, or None if it does not exist.

    ``payload_extra`` merges extra workflow-owned fields (e.g. the best-effort
    Plex-sync outcome) into the carrier payload without a dedicated column,
    leaving ``checkpoint`` and the music-identity ``verification_evidence``
    untouched when they are not supplied here."""
    if state not in CARRIER_STATES:
        raise ValueError(f"unknown recovery state {state!r} (known: {CARRIER_STATES})")
    payload: Dict[str, Any] = {}
    if payload_extra:
        # Finding B (defense-in-depth): ``checkpoint``/``verification_evidence``/
        # ``recovery_workflow_id`` may ONLY be set through this function's
        # dedicated, validated kwargs (the checkpoint allow-list, the evidence
        # slot, the deterministic workflow id). A caller that smuggled one of
        # them through the free-form ``payload_extra`` would write it unchecked,
        # bypassing that validation -- so we drop the reserved keys (with a
        # warning) rather than merge them.
        _reserved = {"checkpoint", "verification_evidence", "recovery_workflow_id"}
        _bad = _reserved.intersection(payload_extra)
        if _bad:
            logger.warning(
                "advance_state: ignoring reserved payload_extra key(s) %s for "
                "carrier %r; use the dedicated checkpoint/evidence kwargs",
                sorted(_bad), _s(carrier_id))
            payload_extra = {k: v for k, v in payload_extra.items() if k not in _reserved}
        payload.update(payload_extra)
    if checkpoint:
        if checkpoint not in CHECKPOINTS:
            raise ValueError(f"unknown checkpoint {checkpoint!r} (known: {CHECKPOINTS})")
        payload["checkpoint"] = checkpoint
    if evidence is not None:
        # HOOK(music-identity): verification evidence is recorded here, never
        # used to lower a confidence/AcoustID threshold -- it only records what
        # was proven.
        payload["verification_evidence"] = evidence
    try:
        return _store().cas_update(
            carrier_id, expected_revision, {"state": state}, payload, writer_id=carrier_id,
        )
    except RecoveryCarrierConflictError:
        if _store().get(carrier_id) is None:
            return None
        raise


# ──────────────────────── user-facing workflow stage ──────────────────────
# A stable mapping from the internal carrier state + checkpoint onto the 7
# stages the Download-Missing status UI shows (Phase-6, frontend). This is a
# PURE derivation (no store/provider access) and carries NO secret or provider
# token: the staged-folder path, release ids and track identity live elsewhere
# on the carrier, never here. The frontend reads ``stage`` (the stable key),
# ``stage_index`` (0..6 along the linear path, -1 for an off-path terminal),
# ``label``, ``terminal`` and an optional ``note`` (e.g. why Plex was skipped).
STAGE_LABELS = {
    "missing": "Missing",
    "searching": "Searching",
    "downloading": "Downloading",
    "verifying": "Verifying",
    "importing": "Importing",
    "plex_sync": "Plex Sync",
    "complete": "Complete",
    "needs_review": "Needs Review",
    "failed": "Failed",
}
STAGE_ORDER = ("missing", "searching", "downloading", "verifying",
               "importing", "plex_sync", "complete")


def _stage(key: str, *, terminal: bool = False, note: str = "") -> Dict[str, Any]:
    return {
        "stage": key,
        "stage_index": STAGE_ORDER.index(key) if key in STAGE_ORDER else -1,
        "label": STAGE_LABELS.get(key, key),
        "terminal": terminal,
        "note": note,
    }


def workflow_stage(carrier: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Map a carrier's durable state + checkpoint onto the user-facing stage.
    Safe on None (returns the 'missing' stage: no carrier yet). Checkpoint
    (the durable position) wins over the transient state where both are set."""
    if not carrier:
        return _stage("missing")
    state = _s(carrier.get("state"))
    checkpoint = _s(carrier.get("checkpoint"))
    plex = carrier.get("plex_sync") if isinstance(carrier.get("plex_sync"), dict) else {}
    plex_status = _s(plex.get("status"))
    if state == "review_required":
        return _stage("needs_review", terminal=True)
    if state in ("failed", "rolled_back"):
        return _stage("failed", terminal=True)
    if checkpoint == "PLEX_SYNCED":
        return _stage("complete", terminal=True, note=plex_status or "synced")
    if state == "imported" or checkpoint == "IMPORTED":
        # The verified track landed on the album (the import IS complete). The
        # Plex refresh is a best-effort tail: when it synced, or was skipped by
        # config / not configured, the workflow is Complete; while it is still
        # pending or failed (import intact, retriable) we show the Plex stage.
        if plex_status in ("synced", "skipped"):
            return _stage("complete", terminal=True,
                          note=_s(plex.get("reason")) or plex_status)
        return _stage("plex_sync", note=plex_status)
    if state == "importing" or checkpoint == "VERIFIED":
        return _stage("importing")
    if state == "verifying" or checkpoint == "STAGED":
        return _stage("verifying")
    if state in ("downloading", "downloaded") or checkpoint == "DOWNLOADED":
        return _stage("downloading")
    return _stage("searching")


def contract_confidence(matching_decision: Optional[Dict[str, Any]]) -> str:
    """The SINGLE review-row confidence, derived from the matching_decision
    contract (not the stale coarse suggestion string). A deterministic proof
    that authorizes action is HIGH; any other authorized action is MEDIUM;
    anything the contract does not authorize is REVIEW_REQUIRED. Empty when no
    decision has been computed yet (caller keeps its own fallback)."""
    if not isinstance(matching_decision, dict) or not matching_decision:
        return ""
    if matching_decision.get("action_allowed"):
        proof = _s(matching_decision.get("identity_proof"))
        return "high" if proof.startswith("deterministic") else "medium"
    return "review_required"


def _per_track_status(carrier: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Per expected (missing) track: identity (title, disc, position, recording
    MBID) + its download/verification status. Sourced only from the stable
    carrier (expected_tracks + verification_evidence.proof_files + state),
    never from a folder name."""
    evidence = carrier.get("verification_evidence") or {}
    proof_rids = {
        _s(f.get("recording_id")).strip().lower()
        for f in (evidence.get("proof_files") or []) if isinstance(f, dict)
    }
    state = _s(carrier.get("state"))
    downloaded = bool(carrier.get("staged_folder_path")) or state in (
        "downloaded", "verifying", "importing", "imported", "review_required")
    out: List[Dict[str, Any]] = []
    for t in (carrier.get("expected_tracks") or []):
        if not isinstance(t, dict):
            continue
        rid = _s(t.get("recording_id") or t.get("mb_trackid")).strip()
        if state == "imported":
            status = "imported"
        elif rid and rid.lower() in proof_rids:
            status = "verified"
        elif downloaded:
            status = "downloaded"
        else:
            status = "pending"
        out.append({
            "recording_id": rid,
            "title": _s(t.get("title")),
            "disc": t.get("disc"),
            "position": t.get("track"),
            "status": status,
        })
    return out


def review_identity(carrier: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Flatten a carrier into the STRUCTURED identity + recovery PACKET the
    Review hand-off stores in place of the old `allow_existing` boolean +
    free-text suggestion. Safe on None (returns {}). Consumed by
    pending_review_store, the Review UI (frontend, Step 5) and preflight
    single-track mode (music-identity).

    The packet exposes: the existing-album DB identity + canonical Release
    Group, the expected missing tracks (title, disc, position, recording MBID)
    with per-track download+verification status, the acquisition provenance,
    the matching_decision contract and the single contract-derived confidence."""
    if not carrier:
        return {}
    matching_decision = carrier.get("matching_decision") if isinstance(
        carrier.get("matching_decision"), dict) else {}
    return {
        "recovery": True,
        "recovery_carrier_id": _s(carrier.get("carrier_id")),
        "recovery_identity_key": _s(carrier.get("recovery_identity_key")),
        "existing_album_id": int(carrier.get("existing_album_id") or 0),
        "mb_releasegroupid": _s(carrier.get("mb_releasegroupid")),
        "release_id": _s(carrier.get("release_id")),
        "destination_album_path": _s(carrier.get("destination_album_path")),
        "wanted_tracks": list(carrier.get("wanted_tracks") or []),
        "expected_tracks": list(carrier.get("expected_tracks") or []),
        "tracks": _per_track_status(carrier),
        "provenance": dict(carrier.get("source") or {}),
        "matching_decision": matching_decision,
        "confidence": contract_confidence(matching_decision),
        "review_reason": _s((carrier.get("verification_evidence") or {}).get("rejection_reason")),
        "recovery_stage": workflow_stage(carrier),
    }
