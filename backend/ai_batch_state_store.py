"""Durable, versioned, compare-and-swap (CAS) state store for AI Batch Import and AI review workflows.

Implements ARCH-010:
- Schema versioning (schema_version = 1)
- Revision tracking (revision = 1, 2, 3...)
- Compare-And-Swap (CAS) optimistic concurrency control across workers/processes
- Crash-safe, ACID-backed SQLite persistence with WAL mode
- One-way migration of legacy unversioned JSON state files
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


class AiBatchStateConflictError(RuntimeError):
    """Raised when a compare-and-swap (CAS) update fails due to a stale revision."""
    pass


class AiBatchStateStore:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
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
            row = conn.execute("SELECT revision FROM ai_batch_state WHERE id = ?", (batch_job_id,)).fetchone()
            if row:
                current_rev = int(row["revision"])
                if expected_revision is not None and expected_revision != current_rev:
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
                conn.execute(
                    """
                    INSERT INTO ai_batch_state
                    (id, schema_version, revision, status, job_id, payload_json, created_at, updated_at, writer_id)
                    VALUES (?, 1, 1, ?, ?, ?, ?, ?, ?)
                    """,
                    (batch_job_id, status, job_id, payload_json, created_at, now, writer_id),
                )
                state_copy["revision"] = 1

            state_copy["schema_version"] = 1
            state_copy["updated_at"] = now
            return state_copy

    def record_review_decision(self, entry: Dict[str, Any]) -> Dict[str, Any]:
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
                (schema_version, revision, album_id, mb_albumid, decision, payload_json, created_at)
                VALUES (1, 1, ?, ?, ?, ?, ?)
                """,
                (album_id, mb_albumid, decision, payload_json, now),
            )
        entry_copy["schema_version"] = 1
        entry_copy["revision"] = 1
        entry_copy["created_at"] = now
        return entry_copy

    def list_review_decisions(self, limit: int = 500) -> List[Dict[str, Any]]:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM ai_review_decisions ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            decisions = []
            for row in rows:
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
    ) -> int:
        migrated_count = 0
        state_dir = Path(state_dir)

        if state_dir.exists():
            for json_path in state_dir.glob("*.json"):
                if json_path.name.endswith(".migrated.bak") or json_path.name == "ai_batch_state.db":
                    continue
                try:
                    text = json_path.read_text(encoding="utf-8")
                    state = json.loads(text)
                    if isinstance(state, dict) and (state.get("batch_job_id") or state.get("job_id")):
                        batch_id = str(state.get("batch_job_id") or state.get("job_id"))
                        existing = self.get_batch_state(batch_id)
                        if not existing:
                            self.save_batch_state(state, expected_revision=None, writer_id="migration")
                            migrated_count += 1
                        bak_path = json_path.with_suffix(".json.migrated.bak")
                        os.replace(str(json_path), str(bak_path))
                except Exception:
                    pass

        if decisions_file and Path(decisions_file).exists():
            dec_path = Path(decisions_file)
            if not dec_path.name.endswith(".migrated.bak"):
                try:
                    text = dec_path.read_text(encoding="utf-8")
                    items = json.loads(text)
                    if isinstance(items, list):
                        for item in reversed(items):
                            if isinstance(item, dict):
                                self.record_review_decision(item)
                        migrated_count += len(items)
                    bak_path = dec_path.with_suffix(".json.migrated.bak")
                    os.replace(str(dec_path), str(bak_path))
                except Exception:
                    pass

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
                except Exception:
                    pass

        return migrated_count
