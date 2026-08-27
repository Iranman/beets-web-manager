"""Wave 26 AI Batch Import acceptance seeding helper (PR #101, section 24).

Runs INSIDE the beets-web-manager container (copied in via `docker cp` by
scripts/verify_two_service_docker_acceptance.py, never baked into the
image). It talks to the exact same `backend.ai_batch_state_store.
AiBatchStateStore` / DB path the running Flask app itself uses (resolved
the identical way `app.py`'s `_get_ai_batch_store()` does: `AI_BATCH_STATE_DIR`
env var, defaulting to `WEB_MANAGER_DATA_DIR/ai_batch_jobs`), so every write
this script makes is visible to the real running app on its very next
request -- no separate/parallel state store, no mock of application logic.

This is the acceptance run's one deliberate stub of the AI provider
boundary: real AI batch import normally reaches OpenAI once per folder via
`batch_ai_suggest`/`_ai_batch_run_suggestions` before a folder is eligible
for the decision-processing/auto-import step this scenario actually wants
to exercise. Seeding a folder directly into `status="ai_completed"` with
its `ai_result` already populated (exactly the shape
`_ai_batch_run_suggestions` itself would have written) makes the real
worker's own recover path -- `if status == "ai_completed" and
folder.get("ai_result"): decision_ready += 1; continue` in
`_run_ai_batch_import` -- skip the AI call entirely and fall straight
through to the real, unmocked `_ai_batch_process_decisions` ->
`_ai_import_folder` -> `confirmed_import_v1` composition against the real
engine. Nothing about transaction planning, engine IPC, or state
durability is stubbed; only the OpenAI round trip is replaced with its own
already-known result, the same way a unit test mocks an HTTP client.

Usage:
    python3 ai_batch_seed.py create <batch_job_id> <container_folder_path> <scan_root> <release_id> <releasegroup_id> <artist> <album>
    python3 ai_batch_seed.py reseed_folder <batch_job_id> <container_folder_path> <release_id> <releasegroup_id> <artist> <album>
    python3 ai_batch_seed.py bump <batch_job_id>
    python3 ai_batch_seed.py get <batch_job_id> <field>
    python3 ai_batch_seed.py attempt_stale_write <batch_job_id> <stale_revision>
    python3 ai_batch_seed.py dump_folder_states <batch_job_id>
"""
import hashlib
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, "/app")

from backend.ai_batch_state_store import AiBatchStateStore  # noqa: E402


def _store() -> AiBatchStateStore:
    web_manager_data_dir = Path(os.environ.get("WEB_MANAGER_DATA_DIR", "/web-manager-data"))
    active_dir = Path(os.environ.get("AI_BATCH_STATE_DIR", str(web_manager_data_dir / "ai_batch_jobs")))
    return AiBatchStateStore(active_dir / "ai_batch_state.db")


def _folder_id(source_folder: str) -> str:
    # Matches app.py's own _ai_batch_folder_id() exactly -- not load-bearing
    # for the recover path (it trusts whatever keys are already in
    # folder_states, it never recomputes them), but kept identical so any
    # future code that DOES recompute it still finds this folder.
    raw = source_folder.strip().replace("\\", "/").rstrip("/")
    return hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()[:16]


def cmd_create(batch_job_id: str, container_folder_path: str, scan_root: str,
               release_id: str, releasegroup_id: str, artist: str, album: str) -> None:
    now = time.time()
    fid = _folder_id(container_folder_path)
    state = {
        "batch_job_id": batch_job_id,
        "job_id": "",
        "source_path": scan_root,
        "status": "queued",
        "current_step": "queued",
        "total_folders_found": 1,
        "folders_processed": 0,
        "folders_queued": 1,
        "folders_running": 0,
        "folders_completed": 0,
        "folders_failed": 0,
        "folders_skipped": 0,
        "heartbeat_at": now,
        "started_at": now,
        "updated_at": now,
        "completed_at": None,
        "current_folder_names": [],
        "last_completed_folder": "",
        "last_failed_folder": "",
        "last_failed_reason": "",
        "last_error": "",
        "retry_count": 0,
        "ai_max_parallel": 1,
        "ai_timeout_seconds": 120,
        "folder_states": {
            fid: {
                "folder_id": fid,
                "batch_job_id": batch_job_id,
                "source_folder": container_folder_path,
                "status": "ai_completed",
                "current_step": "AI suggestion ready (acceptance-seeded at the provider boundary)",
                "ai_suggest_status": "completed",
                "ai_suggest_started_at": now,
                "ai_suggest_completed_at": now,
                "ai_suggest_error": "",
                "review_item_id": "",
                "detected_artist": artist,
                "detected_album": album,
                "suggested_release_group_id": releasegroup_id,
                "failure_reason": "",
                "retry_count": 0,
                "ai_result": {
                    "ok": True,
                    "suggestion": {
                        "artist": artist,
                        "album": album,
                        "albumartist": artist,
                        "confidence": "high",
                        "mb_albumid": release_id,
                        "mb_releasegroupid": releasegroup_id,
                        "mb_valid": True,
                        "year": "",
                        "reason": "acceptance-seeded high-confidence match",
                    },
                },
            },
        },
    }
    created = _store().create_batch_state(state)
    print(created.get("revision"))


def cmd_reseed_folder(batch_job_id: str, container_folder_path: str,
                       release_id: str, releasegroup_id: str, artist: str, album: str) -> None:
    """Re-establish the cached, provider-boundary-stubbed ai_result on an
    EXISTING batch's folder after a real retry_failed requeue wiped it.

    Found live (Wave 26 Docker acceptance round): _run_ai_batch_import's
    own retry_failed reconciliation correctly (and, for real production
    use, desirably) resets a retryable folder to status="ai_queued" with
    ai_suggest_status="queued" -- discarding any previous ai_result so a
    genuinely fresh AI suggestion pass runs on retry, in case the earlier
    suggestion was itself bad. That is correct production behavior, but
    it means a plain retry_failed call in this acceptance script would go
    on to make a REAL OpenAI call with this environment's dummy
    OPENAI_API_KEY and fail for an unrelated reason. This re-applies the
    same provider-boundary stub cmd_create used originally, without
    needing to create a whole new batch (which would fail: the row
    already exists, and create_batch_state() requires expected_revision=0)."""
    store = _store()
    state = store.get_batch_state(batch_job_id)
    if not state:
        raise SystemExit(f"no such batch_job_id: {batch_job_id}")
    now = time.time()
    fid = _folder_id(container_folder_path)
    folder_states = state.setdefault("folder_states", {})
    folder_states[fid] = {
        "folder_id": fid,
        "batch_job_id": batch_job_id,
        "source_folder": container_folder_path,
        "status": "ai_completed",
        "current_step": "AI suggestion ready (acceptance-reseeded at the provider boundary)",
        "ai_suggest_status": "completed",
        "ai_suggest_started_at": now,
        "ai_suggest_completed_at": now,
        "ai_suggest_error": "",
        "review_item_id": "",
        "detected_artist": artist,
        "detected_album": album,
        "suggested_release_group_id": releasegroup_id,
        "failure_reason": "",
        "retry_count": 0,
        "ai_result": {
            "ok": True,
            "suggestion": {
                "artist": artist,
                "album": album,
                "albumartist": artist,
                "confidence": "high",
                "mb_albumid": release_id,
                "mb_releasegroupid": releasegroup_id,
                "mb_valid": True,
                "year": "",
                "reason": "acceptance-reseeded high-confidence match",
            },
        },
    }
    updated = store.save_batch_state(state, expected_revision=state.get("revision"))
    print(updated.get("revision"))


def cmd_bump(batch_job_id: str) -> None:
    store = _store()
    state = store.get_batch_state(batch_job_id)
    if not state:
        raise SystemExit(f"no such batch_job_id: {batch_job_id}")
    # A legitimate extra write from a distinct caller -- simulates "another
    # writer already committed a newer revision" between this scenario's
    # own earlier read and its next action, exactly what section 24's
    # "stale AI review blocked on revision change" case needs to prove.
    state["current_step"] = "bumped by acceptance seed (simulated concurrent writer)"
    updated = store.save_batch_state(state, expected_revision=state.get("revision"))
    print(updated.get("revision"))


def cmd_get(batch_job_id: str, field: str) -> None:
    state = _store().get_batch_state(batch_job_id)
    if not state:
        raise SystemExit(f"no such batch_job_id: {batch_job_id}")
    print(state.get(field))


def cmd_dump_folder_states(batch_job_id: str) -> None:
    """Diagnostic-only: print the raw folder_states dict as JSON, to
    ground-truth what the store actually persisted (as opposed to what
    the code is assumed/expected to have written)."""
    state = _store().get_batch_state(batch_job_id)
    if not state:
        raise SystemExit(f"no such batch_job_id: {batch_job_id}")
    print(json.dumps(state.get("folder_states") or {}, default=str))


def cmd_attempt_stale_write(batch_job_id: str, stale_revision: str) -> None:
    """Direct, unambiguous CAS proof for section 24's "stale AI review
    blocked on revision change" -- deliberately does NOT re-read the
    store's current state first (every real app.py caller, e.g.
    ai_batch_pause, self-heals by reading fresh immediately before its own
    commit, which is correct production behavior but makes it impossible
    to demonstrate a stale write through that route without an actual
    thread race). This directly exercises the same
    AiBatchStateStore.save_batch_state() CAS contract with an
    intentionally-stale expected_revision, exactly the scenario CAS exists
    to reject."""
    from backend.ai_batch_state_store import AiBatchStateConflictError

    store = _store()
    state = store.get_batch_state(batch_job_id)
    if not state:
        raise SystemExit(f"no such batch_job_id: {batch_job_id}")
    state["current_step"] = "attempted stale write (should be rejected)"
    try:
        store.save_batch_state(state, expected_revision=int(stale_revision))
        print("accepted")
    except AiBatchStateConflictError:
        print("rejected")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        raise SystemExit(__doc__)
    cmd, rest = args[0], args[1:]
    if cmd == "create":
        cmd_create(*rest)
    elif cmd == "reseed_folder":
        cmd_reseed_folder(*rest)
    elif cmd == "bump":
        cmd_bump(*rest)
    elif cmd == "get":
        cmd_get(*rest)
    elif cmd == "attempt_stale_write":
        cmd_attempt_stale_write(*rest)
    elif cmd == "dump_folder_states":
        cmd_dump_folder_states(*rest)
    else:
        raise SystemExit(f"unknown command: {cmd}")
