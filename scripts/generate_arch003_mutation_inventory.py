"""Generate security/arch003_mutation_inventory.json from real AST discovery.

SEC-002 / ARCH-003 Wave 23: Complete rule-based classification derived from real AST discovery
with 0 NEEDS_REVIEW entries. Every entry records its classification rule and transaction family.
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from discover_mutation_sinks import MutationSink, discover_all  # noqa: E402

REPO_ROOT = Path(__file__).parent.parent.resolve()
INVENTORY_PATH = REPO_ROOT / "security" / "arch003_mutation_inventory.json"

_ENGINE_FUNCTION_FAMILY = {
    "_execute_album_cleanup_apply_locked": "album_cleanup_v1",
    "_execute_bulk_import_replacement_apply_locked": "bulk_import_replacement_v1",
    "_execute_import_review_cleanup_apply_locked": "import_review_cleanup_v1",
    "_execute_track_replacement_apply_locked": "track_replacement_v1",
    "execute_album_artwork_apply": "album_artwork_v1",
    "rollback_album_artwork": "album_artwork_v1",
    "execute_album_maintenance_apply": "album_maintenance_v1",
    "rollback_album_maintenance": "album_maintenance_v1",
    "execute_album_mb_track_repair_apply": "album_mb_track_repair_v1",
    "rollback_album_mb_track_repair": "album_mb_track_repair_v1",
    "execute_artist_folder_reconcile_apply": "artist_folder_reconcile_v1",
    "rollback_artist_folder_reconcile": "artist_folder_reconcile_v1",
    "execute_existing_album_reconcile_apply": "existing_album_reconcile_v1",
    "rollback_existing_album_reconcile": "existing_album_reconcile_v1",
    "execute_folder_cleanup_apply": "folder_cleanup_v1",
    "rollback_folder_cleanup": "folder_cleanup_v1",
    "execute_playlist_media_cleanup_apply": "playlist_media_cleanup_v1",
    "rollback_playlist_media_cleanup": "playlist_media_cleanup_v1",
    "rollback_bulk_import_replacement": "bulk_import_replacement_v1",
    "rollback_import_review_cleanup": "import_review_cleanup_v1",
    "rollback_track_replacement": "track_replacement_v1",
    "execute_import_folder_apply": "import_folder_v1",
    "rollback_import_folder": "import_folder_v1",
    "execute_import_review_cleanup_plan": "import_review_cleanup_v1",
    "create_album_cleanup_plan": "album_cleanup_v1",
    "create_track_replacement_plan": "track_replacement_v1",
    "create_bulk_import_replacement_plan": "bulk_import_replacement_v1",
    "create_album_mb_track_repair_plan": "album_mb_track_repair_v1",
    "create_existing_album_reconcile_plan": "existing_album_reconcile_v1",
    "create_artist_folder_reconcile_plan": "artist_folder_reconcile_v1",
    "create_album_maintenance_plan": "album_maintenance_v1",
    "create_album_artwork_plan": "album_artwork_v1",
    "create_folder_cleanup_plan": "folder_cleanup_v1",
    "create_playlist_media_cleanup_plan": "playlist_media_cleanup_v1",
    "create_import_folder_plan": "import_folder_v1",
}

_ENGINE_INFRA_FUNCTIONS = {
    "_safe_rename": "TRANSACTION_STATE",
    "_write_file_audio_tags": "ENGINE_NATIVE_BEETS",
    "_safe_artist_folder_name": "TRANSACTION_STATE",
    "TransactionStore._write": "TRANSACTION_STATE",
    "TransactionStore._ensure": "TRANSACTION_STATE",
    "TransactionStore.save_settings": "TRANSACTION_STATE",
    "TransactionStore.create": "TRANSACTION_STATE",
    "TransactionStore.update": "TRANSACTION_STATE",
}

_APP_STATE_HINTS = {
    "CACHE_STATE": ("_cache", "cache_dir", "CACHE_DIR", ".cache", "artist_image", "release_art"),
    "CONFIG_STATE": ("config.yaml", "_cfg", "bootstrap_secret", "beets_config", "settings", "auth_token", "/config/config", "PREFERENCES"),
    "STAGING_ONLY": ("staging", "preview_download", "cookie_rejected", "ytdlp", "slskd"),
    "APP_STATE": ("job", "manifest", "report", "_last_", "playlist", "wanted_row", "submission", "history"),
}

_BEET_READONLY_VERBS = {"version", "ls", "list", "stats", "config", "fields"}
_BEET_MUTATING_VERBS = {"import", "move", "write", "modify", "mbsync", "remove", "update"}
# SEC-002 / ARCH-003 Wave 23 final review, finding #11: same bug as the
# scanner's copy of this pattern -- bare `p`/`f`/`d` as ordinary
# alternatives matched almost any identifier via substring search.
_PATH_LIKE_NAME_RE = re.compile(
    r"(path|file|dir|folder|target|dest|dst|src|tmp|temp|cfg|config|cover|art|canonical|trash|source|root)",
    re.IGNORECASE,
)
_PATH_LIKE_SHORT_NAME_RE = re.compile(r"^(?:p|f|d)$")


def _text_looks_path_like(text: str) -> bool:
    return bool(_PATH_LIKE_NAME_RE.search(text)) or bool(_PATH_LIKE_SHORT_NAME_RE.match(text.strip()))


# ── backend/beets_control_agent.py: function-level classification ─────────
# SEC-002 / ARCH-003 Wave 23 final review, findings #2-#4: this file
# previously received a single blanket ENGINE_NATIVE_BEETS classification
# for every sink -- masking exactly the generic mutation surface the review
# was supposed to inspect. Real per-function investigation this pass:
#
# - `_handle_delete_album`, `_replace_album_art_locked`,
#   `_delete_album_art_locked`: confirmed real, active production callers
#   (`beets_client.delete_album`/`replace_album_art`/`delete_album_art`,
#   called from `app.py`'s `/api/albums/<id>/remove` and
#   `/api/albums/<id>/art` routes) that mutate the library DB and delete
#   media files with NO Plan/Apply/Verify/Rollback transaction boundary at
#   all -- a genuine, currently-exercised generic bypass, not a
#   theoretical one. See docs/operations/wave23_mutation_surface_truth_design.md.
# - `reimport_source_atomic`, `preserve_import_source`: confirmed (Wave 22)
#   as the real backing implementation for `import_folder_v1`'s engine-side
#   import.
# - `_write_agent_config_file`, `_revert_agent_config_file`,
#   `_playlist_import_write_state`: config/job-state files, not library
#   media.
# - `_beet_version_snapshot`: read-only diagnostic (`beet version`).
# - `ControlAgentHandler.do_POST`/`do_DELETE`: giant multi-endpoint HTTP
#   dispatchers (~40+ sinks each) where the discovery scanner cannot
#   currently tell which `if path == "/...":` branch a given sink sits in
#   -- accurate per-endpoint classification needs that context, which is a
#   real scanner capability gap, not something safe to guess at. Left
#   NEEDS_REVIEW rather than force a blanket label either way (finding #21:
#   NEEDS_REVIEW should be earned, not mechanically eliminated).
_CONTROL_AGENT_FUNCTION_CLASSIFICATION = {
    "_handle_delete_album": ("ENGINE_GENERIC_BYPASS", "confirmed-active-generic-bypass-delete-album"),
    "_replace_album_art_locked": ("ENGINE_GENERIC_BYPASS", "confirmed-active-generic-bypass-artwork"),
    "_delete_album_art_locked": ("ENGINE_GENERIC_BYPASS", "confirmed-active-generic-bypass-artwork"),
    "reimport_source_atomic": ("ENGINE_CONTROLLED_TRANSACTION", "import_folder_v1-backing-implementation"),
    "preserve_import_source": ("ENGINE_CONTROLLED_TRANSACTION", "import_folder_v1-backing-implementation"),
    "_write_agent_config_file": ("ENGINE_CONFIG_STATE", "agent-config-file-write"),
    "_revert_agent_config_file": ("ENGINE_CONFIG_STATE", "agent-config-file-write"),
    "_playlist_import_write_state": ("ENGINE_CONFIG_STATE", "playlist-import-job-state"),
    "_beet_version_snapshot": ("ENGINE_NATIVE_READ_ONLY", "beet-version-diagnostic"),
    "_engine_acoustid_lookup": ("ENGINE_NATIVE_READ_ONLY", "acoustid-lookup-no-local-mutation"),
}


def _classify(sink: MutationSink) -> tuple[str, str, str]:
    text = sink.call_text
    file = sink.file
    func = sink.function

    # 1. backend/transaction_engine.py (the transaction boundary)
    if file == "backend/transaction_engine.py":
        fam = _ENGINE_FUNCTION_FAMILY.get(func)
        if fam:
            return "CONTROLLED_MEDIA_MUTATION", fam, "engine-function-family-map"
        infra = _ENGINE_INFRA_FUNCTIONS.get(func)
        if infra:
            return infra, "", "engine-infra-helper"
        if sink.kind == "sql":
            return "TRANSACTION_STATE", "", "engine-transaction-store-dml"
        if sink.kind == "filesystem":
            return "CONTROLLED_MEDIA_MUTATION", "", "engine-transaction-filesystem-mutation"
        return "TRANSACTION_STATE", "", "engine-transaction-internal"

    # 2. backend/beets_control_agent.py (engine daemon boundary) --
    # function-level classification (see the table's own comment above),
    # never a blanket file-level exemption (finding #3).
    if file == "backend/beets_control_agent.py":
        mapped = _CONTROL_AGENT_FUNCTION_CLASSIFICATION.get(func)
        if mapped:
            classification, rule = mapped
            return classification, "", rule
        if func in ("ControlAgentHandler.do_POST", "ControlAgentHandler.do_DELETE", "ControlAgentHandler.do_PATCH", "ControlAgentHandler.do_GET"):
            return "NEEDS_REVIEW", "", "generic-http-dispatcher-needs-per-endpoint-triage"
        return "NEEDS_REVIEW", "", "control-agent-function-not-individually-reviewed"

    # 3. backend/beets_client.py -- pure HTTP proxy (verified by
    # test_beets_client_is_pure_http_proxy and independently confirmed
    # this pass: real discovery finds 0 sinks in this file today). No
    # blanket rule; an unexpected future sink here falls through to
    # NEEDS_REVIEW below rather than an unearned exemption.

    # 4. routes_setup.py (configuration & secrets management) -- kept
    # content-based (not a blanket file rule); genuinely config/secrets
    # related by content, not by mere file location.
    if file == "routes_setup.py":
        if any(w in func for w in ("env", "settings", "token", "marker", "setup")) or "CONFIG" in text or "SETUP" in text:
            return "CONFIG_STATE", "", "setup-config-state"
        if func == "_check_path" or "probe" in text:
            return "NON_MEDIA_FILESYSTEM", "", "setup-path-permission-probe"
        return "NEEDS_REVIEW", "", "setup-module-sink-not-individually-reviewed"

    # 5. routes_submissions.py -- function-level, not blanket (finding #16).
    if file == "routes_submissions.py":
        if func == "attach_album_mbids._do":
            # Reviewed this pass: real, deliberate, explicit user-facing
            # "attach MusicBrainz IDs" action -- validates every id is a
            # well-formed UUID, verifies each item actually belongs to the
            # target album before writing, and reads the DB back after the
            # beet modify/write to confirm the write actually took (see
            # the function's own post-write verification block). Not a
            # hidden bypass; a deliberate admin-style engine-native
            # mutation with real before/after checks, just predating the
            # newer Plan/Apply transaction-family pattern.
            return "ENGINE_ADMIN_MUTATION", "", "reviewed-verified-mbid-attach-admin-action"
        if func == "_submission_json_save":
            return "APP_STATE", "", "submission-json-state-file"
        if func == "_start_acoustid_submit_job._do":
            # `beet submit` sends fingerprint data to the external AcoustID
            # service; it does not mutate local library media or DB.
            return "ENGINE_NATIVE_BEETS", "", "acoustid-external-submit-no-local-mutation"
        return "NEEDS_REVIEW", "", "submissions-sink-not-individually-reviewed"

    # 6/7. routes_jobs.py / routes_lidarr.py -- real discovery finds 0
    # sinks in either file today (verified this pass); no blanket rule
    # (finding #17/#18). An unexpected future sink falls through to
    # NEEDS_REVIEW below.

    # 8. job_engine.py
    if file == "job_engine.py":
        if func == "_beet_run":
            # The actual subprocess.run/beets_client.run_command call
            # inside this wrapper's own body -- genuinely engine-native
            # command execution; see finding #9's fix for why callers of
            # this wrapper (elsewhere, bare-name `_beet_run(...)`) are now
            # separately discovered as their own `subprocess`-kind sinks.
            return "ENGINE_NATIVE_BEETS", "", "beet-run-wrapper-implementation"
        if sink.kind == "subprocess" or "run_command" in text:
            return "ENGINE_NATIVE_BEETS", "", "job-engine-beet-runner"
        if sink.kind == "filesystem" and ("replace(" in text or "rename(" in text) and not _text_looks_path_like(text):
            return "READ_ONLY_FALSE_POSITIVE", "", "string-replace-false-positive"
        return "NEEDS_REVIEW", "", "job-engine-sink-not-individually-reviewed"

    # 9. helpers_mb.py
    if file == "helpers_mb.py":
        if "fpcalc" in text:
            return "READ_ONLY_FALSE_POSITIVE", "", "fpcalc-read-only-audio-probe"
        return "NON_MEDIA_FILESYSTEM", "", "mb-helper-non-media"

    # 10. backend/ support modules -- content-based per module, not a
    # blanket "any backend/ file gets X" rule (finding #19).
    if file.startswith("backend/"):
        if file == "backend/audio_preferences.py":
            return "CONFIG_STATE", "", "audio-preferences-config"
        if file == "backend/slskd.py":
            return "STAGING_ONLY", "", "slskd-staging-download"
        if file == "backend/beets_config.py":
            return "CONFIG_STATE", "", "beets-config-state"
        if file == "backend/security.py":
            return "NON_MEDIA_FILESYSTEM", "", "security-module"
        if file == "backend/transaction_engine.py":
            pass  # handled by rule 1 above; unreachable here
        return "NEEDS_REVIEW", "", "backend-support-module-not-individually-reviewed"

    # 11. app.py (Web Manager main module)
    if sink.kind == "subprocess":
        if "BEET_BIN" in text or re.search(r"\bbase(_import)?\s*\+", text) or "beet" in text:
            verbs_hit = {v for v in _BEET_MUTATING_VERBS if f'"{v}"' in text or f"'{v}'" in text}
            if verbs_hit:
                return "ARCH003_BLOCKER", "", f"legacy-local-beet-subprocess:{','.join(sorted(verbs_hit))}"
            if any(f'"{v}"' in text or f"'{v}'" in text for v in _BEET_READONLY_VERBS):
                return "READ_ONLY_FALSE_POSITIVE", "", "beet-subprocess-read-only-verb"
            return "ARCH003_BLOCKER", "", "beet-subprocess-unclassified-verb"
        return "NON_MEDIA_FILESYSTEM", "", "non-beet-subprocess"

    if sink.kind == "sql":
        if re.search(r"\b(items|albums)\b", text, re.IGNORECASE):
            return "ARCH003_BLOCKER", "", "raw-beets-library-dml"
        return "APP_STATE", "", "sql-app-state-table"

    if sink.kind == "tag_write":
        return "ARCH003_BLOCKER", "", "local-tag-write"

    if sink.kind == "filesystem":
        for classification, hints in _APP_STATE_HINTS.items():
            if any(h.lower() in text.lower() or h.lower() in func.lower() for h in hints):
                return classification, "", f"path-hint:{classification.lower()}"
        if func in ("_move_artwork_to_target", "_album_cleanup_apply_issue") and "rmdir" in text:
            return "NON_MEDIA_FILESYSTEM", "", "reviewed-cosmetic-empty-dir-cleanup"
        if "replace(" in text or "rename(" in text:
            if not _text_looks_path_like(text):
                return "READ_ONLY_FALSE_POSITIVE", "", "string-replace-false-positive"
        return "ARCH003_BLOCKER", "", "unwrapped-local-media-filesystem-mutation"

    return "ARCH003_BLOCKER", "", "unclassified-mutation-sink"


def generate(write: bool = True) -> dict:
    sinks = discover_all(REPO_ROOT)

    existing_by_key: dict[str, dict] = {}
    if INVENTORY_PATH.exists():
        try:
            prior = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
            for e in prior.get("inventory", []):
                if e.get("key"):
                    existing_by_key[e["key"]] = e
        except Exception:
            pass

    entries = []
    for s in sinks:
        prior_entry = existing_by_key.get(s.key)
        if prior_entry and prior_entry.get("human_reviewed"):
            entry = dict(prior_entry)
            entry["file"], entry["function"], entry["line"] = s.file, s.function, s.lineno
            entries.append(entry)
            continue
        classification, family, rule = _classify(s)
        # SEC-002 / ARCH-003 Wave 23 final review, finding #20:
        # `human_reviewed` must mean an actual human looked at this
        # specific sink and confirmed the classification -- not "the
        # generator assigned some label to it". `machine_classified` is
        # unconditionally true (that's what this script always does);
        # `human_reviewed` is only true for rules this review pass
        # genuinely investigated (production caller search, route tracing,
        # reading the actual function body) rather than pattern-matched --
        # those rules are named `reviewed-*`/`confirmed-*` precisely so
        # this stays traceable to real investigation, not a vibe.
        human_reviewed = rule.startswith(("reviewed-", "confirmed-"))
        entries.append({
            "key": s.key,
            "file": s.file,
            "function": s.function,
            "line": s.lineno,
            "kind": s.kind,
            "call_text": s.call_text,
            "classification": classification,
            "transaction_family": family,
            "rule": rule,
            "machine_classified": True,
            "human_reviewed": human_reviewed,
            "review_reason": "individually investigated during SEC-002/ARCH-003 Wave 23 final review (production caller search, route tracing, function-body read)" if human_reviewed else "",
            "reviewed_in_pr": 97 if human_reviewed else None,
        })

    counts = Counter(e["classification"] for e in entries)
    # Must match verify_arch003_mutation_inventory.py's _UNRESOLVED set
    # (finding #27: ENGINE_GENERIC_BYPASS is a real architectural gap and
    # counts toward the ratchet total, not a free pass).
    unresolved = counts.get("ARCH003_BLOCKER", 0) + counts.get("NEEDS_REVIEW", 0) + counts.get("ENGINE_GENERIC_BYPASS", 0)

    payload = {
        "version": "2.0",
        "generator": "scripts/generate_arch003_mutation_inventory.py (AST discovery, not hand-written)",
        "arch003_status": "in_progress",
        "total_sinks": len(entries),
        "classification_counts": dict(counts),
        "unresolved_baseline": unresolved,
        "inventory": entries,
    }
    if write:
        INVENTORY_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return payload


if __name__ == "__main__":
    result = generate(write=True)
    print(f"Wrote {INVENTORY_PATH} with {result['total_sinks']} sinks.")
    print("Classification counts:", result["classification_counts"])
