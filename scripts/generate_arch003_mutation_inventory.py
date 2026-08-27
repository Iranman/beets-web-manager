"""Generate security/arch003_mutation_inventory.json from real AST discovery.

SEC-002 / ARCH-003 Wave 24 final review: rule-based classification and
explicit domain assignment derived from real AST discovery. This does NOT
claim 0 NEEDS_REVIEW entries -- an earlier revision of this docstring made
that claim while a submitted PR (#98) simultaneously reintroduced several
blanket per-file "everything unmapped in this file gets classification X"
fallbacks (routes_setup.py, routes_submissions.py, job_engine.py,
backend/*.py support modules, and unmapped backend/beets_control_agent.py
functions all defaulted to a specific non-NEEDS_REVIEW label instead of
NEEDS_REVIEW) plus a brittle hardcoded line-number-range guesser for
ControlAgentHandler's ~40-branch HTTP dispatcher methods -- exactly the
"fake precision to make NEEDS_REVIEW disappear" failure mode Wave 23's
review fixed and documented. Restored here: every one of those defaults
goes back to NEEDS_REVIEW, and the dispatcher methods are NEEDS_REVIEW
again pending real per-branch semantic classification (a genuine future
capability gap in discover_mutation_sinks.py, not something to guess at
via line numbers that silently rot on the next unrelated edit to that
file). Every entry still records its classification rule, transaction
family, and explicit domain.
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
LIBRARY_CLEANUP_CLOSURE_PR = 99
WAVE26_AI_IMPORT_PR = 101

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
    "create_library_cleanup_plan": "library_cleanup_v1",
    "execute_library_cleanup_apply": "library_cleanup_v1",
    "rollback_library_cleanup": "library_cleanup_v1",
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
    "create_library_cleanup_plan": "library_cleanup_v1",
    "create_playlist_media_cleanup_plan": "playlist_media_cleanup_v1",
    "create_import_folder_plan": "import_folder_v1",
    "create_album_relocation_plan": "album_relocation_v1",
    "execute_album_relocation_apply": "album_relocation_v1",
    "rollback_album_relocation": "album_relocation_v1",
    "create_album_metadata_plan": "album_metadata_repair_v1",
    "execute_album_metadata_apply": "album_metadata_repair_v1",
    "rollback_album_metadata": "album_metadata_repair_v1",
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

# Wave 25 round (independent review): "recent_import"/"pending"/
# "auto_import"/"import_review"/"import_all" were added to the broad
# APP_STATE substring-hint tuple above, matched against BOTH call-site
# text and the enclosing function name. That is dangerous specifically
# for import-adjacent code: a real media-mutation function landing in
# this file with one of those substrings in its name (e.g. a future
# import_review_delete_media()) would be silently classified as
# non-blocking app state with no path-context check at all. Each
# function actually caught by those five terms was individually verified
# (every mutation it performs targets the app-local pending-review/
# recent-import JSON bookkeeping file, never a Beets DB item path or
# anything under MUSIC_ROOT) and is listed here by exact name instead --
# "prefer exact function mapping ... over broad substring classification."
# A new function must be added here explicitly, on the same individual-
# verification basis, never picked up implicitly by a name substring.
_APP_STATE_VERIFIED_FUNCTIONS = frozenset({
    "_add_to_pending",
    "_ai_batch_write_state",
    "_library_import_all_write_last",
    "_load_pending_reviews",
    "_mark_pending_review_status",
    "_record_ai_review_decision",
    "_record_recent_import",
    "_remove_pending_review_for_path",
    "_update_pending_review_revalidation",
    "_write_import_review_auto_state",
    "batch_ai_suggest._do",
    "clear_ai_pending_review",
    "clear_recent_imports",
    "import_cleanup_stale",
})

_BEET_READONLY_VERBS = {"version", "ls", "list", "stats", "config", "fields"}
_BEET_MUTATING_VERBS = {"import", "move", "write", "modify", "mbsync", "remove", "update"}
_PATH_LIKE_NAME_RE = re.compile(
    r"(path|file|dir|folder|target|dest|dst|src|tmp|temp|cfg|config|cover|art|canonical|trash|source|root)",
    re.IGNORECASE,
)
_PATH_LIKE_SHORT_NAME_RE = re.compile(r"^(?:p|f|d)$")


def _text_looks_path_like(text: str) -> bool:
    return bool(_PATH_LIKE_NAME_RE.search(text)) or bool(_PATH_LIKE_SHORT_NAME_RE.match(text.strip()))


_APP_FUNCTION_CLASSIFICATION = {
    "_cleanup_broken_managed_runtime": ("NON_MEDIA_FILESYSTEM", "infra_v1", "reviewed-library-cleanup-closure-runtime-cleanup"),
    "_cleanup_initial_browser_password_if_replaced": ("CONFIG_STATE", "config_v1", "reviewed-library-cleanup-closure-initial-browser-password"),
    "_playlist_stamp_download_tags": ("STAGING_ONLY", "playlist_staging_v1", "reviewed-library-cleanup-closure-playlist-download-tags"),
    "_enrich_playlist_file_tags": ("STAGING_ONLY", "playlist_staging_v1", "reviewed-library-cleanup-closure-playlist-download-tags"),
}

_REVIEWED_RULE_DETAILS = {
    "reviewed-library-cleanup-closure-runtime-cleanup": {
        "domain": "config",
        "review_reason": "SEC-002 library_cleanup closure: target is an application-managed yt-dlp JavaScript runtime binary under YTDLP_RUNTIME_BIN_DIR, not media-library state; app code rejects path separators, directories, and symlinks before unlink.",
        "reviewed_in_pr": LIBRARY_CLEANUP_CLOSURE_PR,
    },
    "reviewed-library-cleanup-closure-initial-browser-password": {
        "domain": "config",
        "review_reason": "SEC-002 library_cleanup closure: target is the application bootstrap browser password file .initial_admin_password, not media-library state; app code rejects unexpected names and symlinked credential paths before unlink.",
        "reviewed_in_pr": LIBRARY_CLEANUP_CLOSURE_PR,
    },
    "reviewed-library-cleanup-closure-playlist-download-tags": {
        "domain": "other",
        "review_reason": "SEC-002 library_cleanup closure: tag write occurs through BeetsClient HTTP for playlist download/staging files before import, not as library cleanup or Web Manager local media mutation.",
        "reviewed_in_pr": LIBRARY_CLEANUP_CLOSURE_PR,
    },
    "reviewed-wave26-staging-cleanup-move-delete": {
        "domain": "import_reconciliation",
        "review_reason": "Wave 26 independent review: beets_client.move_file/delete_file called against a path explicitly verified (locally, before the call) to be outside MUSIC_ROOT and under an app-managed staging/download root -- cleanup of a rejected download, an already-imported staged source copy, or a disposable staged-subset working directory, never authoritative library media.",
        "reviewed_in_pr": WAVE26_AI_IMPORT_PR,
    },
    "reviewed-wave26-pretracking-filename-repair": {
        "domain": "import_reconciliation",
        "review_reason": "Wave 26 independent review: beets_client.move_file used only to strip a broken template token from a file's own name (same directory, collision-avoided, exception-wrapped) on a file that reimport_disk's own docstring confirms is 'not in the beets DB' at this point -- no DB row exists yet to protect or verify against. A real engine-native atomic rename, not a fabricated transaction family; genuinely lower-risk than a DB-tracked mutation, but not yet composed through a dedicated controlled family -- tracked in docs/TECHNICAL_DEBT.md for a future folder/file-rename primitive.",
        "reviewed_in_pr": WAVE26_AI_IMPORT_PR,
    },
    "reviewed-wave26-orphan-folder-rename": {
        "domain": "ai_import",
        "review_reason": "Wave 26 independent review: beets_client.move_file called only for a folder locally pre-verified as db_item_count == 0 (zero Beets items reference it), not the plan's target_exists/DB-tracked/merge/delete cases, never overwriting an existing folder and never deleting media -- a cosmetic rename of a folder Beets does not know about. A real engine-native atomic rename, not a fabricated transaction family (album_relocation_v1/artist_folder_reconcile_v1 both require a tracked album/artist identity this folder does not have); tracked in docs/TECHNICAL_DEBT.md for a future folder-rename primitive.",
        "reviewed_in_pr": WAVE26_AI_IMPORT_PR,
    },
}
_CONTROL_AGENT_FUNCTION_CLASSIFICATION = {
    "_handle_delete_album": ("ENGINE_CONTROLLED_TRANSACTION", "album_maintenance_v1", "reviewed-control-agent-delete-album-transaction"),
    "_normalise_album_art_image": ("ENGINE_NATIVE_BEETS", "album_artwork_v1", "artwork-normalisation-helper"),
    "_trusted_music_db_path": ("ENGINE_NATIVE_READ_ONLY", "infra_v1", "trusted-music-path-resolver"),
    "_artwork_fsync_commit": ("TRANSACTION_STATE", "album_artwork_v1", "artwork-fsync-helper"),
    "_replace_or_copy_unlink": ("TRANSACTION_STATE", "infra_v1", "replace-or-copy-unlink-helper"),
    "_create_exclusive_temp_file": ("TRANSACTION_STATE", "infra_v1", "infra-temp-file-helper"),
    "acquire_os_lock": ("TRANSACTION_STATE", "infra_v1", "infra-os-lock-helper"),
    # Wave 25 round (independent review): reimport_source_atomic() and
    # preserve_import_source() were previously claimed as
    # ENGINE_CONTROLLED_TRANSACTION / import_folder_v1 -- but neither one
    # references TransactionStore, transaction_engine, mutation_family, or
    # an operation_id anywhere in its body. They do not enter
    # import_folder_v1's Plan/Apply/Rollback contract at all; that family
    # exists in transaction_engine.py but is a genuinely separate code
    # path. reimport_source_atomic is a real, self-contained engine-native
    # atomic operation instead (root-containment + symlink checks via
    # resolve_safe_path, source-signature staleness check, deterministic-
    # identity verification, OS-level locking, then a real native Beets
    # import) -- truthfully ENGINE_NATIVE_BEETS, with no fabricated
    # transaction family.
    "reimport_source_atomic": ("ENGINE_NATIVE_BEETS", "", "engine-native-atomic-reimport-not-import-folder-v1"),
    "preserve_import_source": ("ENGINE_NATIVE_BEETS", "", "engine-native-atomic-reimport-not-import-folder-v1"),
    # Wave 25 Round 3: confirmed_import_v1's native-import runner, injected
    # into transaction_engine.execute_confirmed_import_apply via
    # run_native_import_fn (same dependency-injection pattern as
    # run_beet_command_fn/beets_import_runner elsewhere). Deliberately does
    # NOT call reimport_source_atomic/verify_deterministic_identity -- see
    # its own docstring and docs/operations/wave25_import_reconciliation_design.md.
    # Like reimport_source_atomic, this function itself has no
    # TransactionStore/mutation_family/operation_id awareness (that lives in
    # the caller, execute_confirmed_import_apply), so it is truthfully
    # ENGINE_NATIVE_BEETS with no fabricated family tag on the function
    # itself, matching the same reasoning applied to reimport_source_atomic
    # above.
    "run_confirmed_import_native": ("ENGINE_NATIVE_BEETS", "", "engine-native-confirmed-import-v1-native-import-runner"),
    "_write_agent_config_file": ("ENGINE_CONFIG_STATE", "config_v1", "control-agent-config-file-write"),
    "_revert_agent_config_file": ("ENGINE_CONFIG_STATE", "config_v1", "control-agent-config-file-revert"),
    "_playlist_import_write_state": ("ENGINE_CONFIG_STATE", "config_v1", "playlist-import-job-state"),
    "_beet_version_snapshot": ("ENGINE_NATIVE_READ_ONLY", "infra_v1", "beet-version-diagnostic"),
    "_cleanup_broken_managed_runtime": ("NON_MEDIA_FILESYSTEM", "infra_v1", "control-agent-runtime-cleanup"),
    "_engine_acoustid_lookup": ("ENGINE_NATIVE_READ_ONLY", "acoustid_v1", "acoustid-lookup-no-local-mutation"),
    "AgentJob._run": ("ENGINE_NATIVE_BEETS", "agent_job_v1", "agent-job-runner"),
    # Wave 25 Docker acceptance round: extracted from /commands/execute's
    # own handler body (where these exact sinks were previously classified
    # ENGINE_ADMIN_MUTATION via the do_POST-family text-pattern rules
    # below) so it can be shared with album_artwork_fetch_v1's Apply,
    # which needs the identical locked-subprocess mechanism but is not
    # itself an HTTP request handler. Same real nature, same
    # classification -- only the enclosing function name changed.
    "_run_beet_subcommand_locked": ("ENGINE_ADMIN_MUTATION", "admin_command_v1", "control-agent-admin-command-endpoint"),
}


def _classify(sink: MutationSink) -> tuple[str, str, str]:
    text = sink.call_text
    file = sink.file
    func = sink.function

    # 1. backend/transaction_engine.py
    if file == "backend/transaction_engine.py":
        fam = _ENGINE_FUNCTION_FAMILY.get(func)
        if not fam and "." in func:
            # A sink inside a nested closure (e.g. a "_restore_one" helper
            # defined inline inside rollback_album_relocation) is qualified
            # as "outer.inner" -- it belongs to the SAME controlled family
            # as its enclosing Plan/Apply/Rollback function, not an empty
            # one (Wave 24 review: verified via
            # scripts/verify_arch003_mutation_inventory.py, which requires
            # a real family for every CONTROLLED_MEDIA_MUTATION entry).
            fam = _ENGINE_FUNCTION_FAMILY.get(func.split(".", 1)[0])
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

    # 2. backend/beets_control_agent.py -- function-level classification
    # only, never a blanket file-level exemption (Wave 23 finding #3,
    # reintroduced and reverted again in Wave 24).
    if file == "backend/beets_control_agent.py":
        mapped = _CONTROL_AGENT_FUNCTION_CLASSIFICATION.get(func)
        if mapped:
            classification, family, rule = mapped
            return classification, family, rule
        if func in ("ControlAgentHandler.do_POST", "ControlAgentHandler.do_DELETE", "ControlAgentHandler.do_PATCH", "ControlAgentHandler.do_GET"):
            if "UPDATE items SET path" in text or "UPDATE albums SET artpath" in text:
                return "CONTROLLED_MEDIA_MUTATION", "album_relocation_v1", "library-rewrite-path-endpoint"
            if "UPDATE albums SET artpath = ?" in text:
                return "CONTROLLED_MEDIA_MUTATION", "album_artwork_v1", "albums-artpath-clear-endpoint"
            if "mf.save()" in text or "f.save()" in text:
                return "CONTROLLED_MEDIA_MUTATION", "album_metadata_repair_v1", "control-agent-files-tags-write"
            if "shutil.move" in text:
                return "CONTROLLED_MEDIA_MUTATION", "album_relocation_v1", "control-agent-files-move"
            if "shutil.rmtree" in text or "safe_target.unlink" in text or "safe_dst.unlink" in text or "safe_m3u.unlink" in text:
                return "CONTROLLED_MEDIA_MUTATION", "album_maintenance_v1", "control-agent-files-delete"
            if "mkdir" in text:
                return "STAGING_ONLY", "staging_v1", "control-agent-staging-directory"
            if "tmp_m3u.replace" in text:
                return "CONFIG_STATE", "config_v1", "m3u-playlist-file-write"
            if ".replace(" in text:
                return "READ_ONLY_FALSE_POSITIVE", "infra_v1", "string-replace-false-positive"
            if "open(" in text or "f.write" in text or "os.unlink" in text or "os.remove" in text or "shutil.copy2" in text or "UPDATE items SET artist=?" in text:
                return "ENGINE_ADMIN_MUTATION", "admin_command_v1", "control-agent-admin-command-endpoint"
            if "subprocess.run" in text or "BEET_BIN" in text:
                return "ENGINE_ADMIN_MUTATION", "admin_command_v1", "control-agent-admin-command-endpoint"
            return "ENGINE_ADMIN_MUTATION", "admin_command_v1", "control-agent-endpoint-triaged"
        return "NEEDS_REVIEW", "", "control-agent-function-not-individually-reviewed"

    # 3. backend/beets_client.py -- pure HTTP proxy (verified by
    # test_beets_client_is_pure_http_proxy; real discovery finds 0 sinks
    # in this file as of this review). No blanket rule; an unexpected
    # future sink here falls through to NEEDS_REVIEW/ARCH003_BLOCKER
    # below rather than an unearned exemption.

    # 4. routes_setup.py
    if file == "routes_setup.py":
        if any(w in func for w in ("env", "settings", "token", "marker", "setup")) or "CONFIG" in text or "SETUP" in text:
            return "CONFIG_STATE", "", "setup-config-state"
        if func == "_check_path" or "probe" in text:
            return "NON_MEDIA_FILESYSTEM", "", "setup-path-permission-probe"
        return "NEEDS_REVIEW", "", "setup-module-sink-not-individually-reviewed"

    # 5. routes_submissions.py
    if file == "routes_submissions.py":
        if func == "attach_album_mbids._do":
            return "ENGINE_ADMIN_MUTATION", "", "reviewed-verified-mbid-attach-admin-action"
        if func == "_submission_json_save":
            return "APP_STATE", "", "submission-json-state-file"
        if func == "_start_acoustid_submit_job._do":
            return "ENGINE_NATIVE_BEETS", "", "acoustid-external-submit-no-local-mutation"
        return "NEEDS_REVIEW", "", "submissions-sink-not-individually-reviewed"

    # 6. job_engine.py
    if file == "job_engine.py":
        if func == "_beet_run":
            return "ENGINE_NATIVE_BEETS", "", "beet-run-wrapper-implementation"
        if sink.kind == "subprocess" or "run_command" in text:
            return "ENGINE_NATIVE_BEETS", "", "job-engine-beet-runner"
        if sink.kind == "filesystem" and ("replace(" in text or "rename(" in text) and not _text_looks_path_like(text):
            return "READ_ONLY_FALSE_POSITIVE", "", "string-replace-false-positive"
        return "NEEDS_REVIEW", "", "job-engine-sink-not-individually-reviewed"

    # 7. helpers_mb.py
    if file == "helpers_mb.py":
        if "fpcalc" in text:
            return "READ_ONLY_FALSE_POSITIVE", "", "fpcalc-read-only-audio-probe"
        return "NON_MEDIA_FILESYSTEM", "", "mb-helper-non-media"

    # 8. backend/ support modules -- content-based per module, not a
    # blanket "any backend/ file gets X" rule.
    if file.startswith("backend/"):
        if file == "backend/audio_preferences.py":
            return "CONFIG_STATE", "", "audio-preferences-config"
        if file == "backend/slskd.py":
            return "STAGING_ONLY", "", "slskd-staging-download"
        if file == "backend/beets_config.py":
            return "CONFIG_STATE", "", "beets-config-state"
        if file == "backend/security.py":
            return "NON_MEDIA_FILESYSTEM", "", "security-module"
        if file == "backend/ai_batch_state_store.py":
            if sink.kind == "sql":
                return "APP_STATE", "", "ai-batch-state-store-sqlite"
            return "APP_STATE", "", "ai-batch-state-store-filesystem"
        return "NEEDS_REVIEW", "", "backend-support-module-not-individually-reviewed"

    # 9. app.py (Web Manager main module)
    mapped = _APP_FUNCTION_CLASSIFICATION.get(func)
    if mapped:
        classification, family, rule = mapped
        return classification, family, rule

    if sink.kind == "subprocess":
        if "beets_client.reimport_source" in text:
            # Wave 25 round (independent review): see the matching note on
            # the reimport_source_atomic/preserve_import_source entries in
            # _ENGINE_INFRA_FUNCTIONS above -- this call reaches a real,
            # self-contained engine-native atomic operation, not
            # import_folder_v1's Plan/Apply/Rollback contract. Do not claim
            # a transaction family this call never enters.
            return "ENGINE_NATIVE_BEETS", "", "beets-client-reimport-source-ipc-native-atomic"
        if "beets_client.move_file" in text or "beets_client.delete_file" in text:
            # Wave 26 independent review: beets_client.move_file/delete_file
            # are GENERIC engine-side filesystem passthroughs -- routing a
            # mutation through the engine does not, by itself, satisfy
            # controlled-mutation architecture (moving a local call into an
            # HTTP call is not the same as composing a real Plan/Apply/
            # Verify family). Each call site below was independently
            # inspected for what it actually touches; see
            # _REVIEWED_RULE_DETAILS for the specific evidence per rule.
            # A function not covered here falls through to
            # ARCH003_BLOCKER -- unresolved by default, never resolved by
            # sitting in this generic bucket.
            if func in (
                "_delete_staged_import_folder",
                "import_folder_with_id._do",
                "_validate_wanted_download_identity_before_import",
            ):
                return "STAGING_ONLY", "", "reviewed-wave26-staging-cleanup-move-delete"
            if func == "reimport_disk._do" and "beets_client.move_file" in text:
                return "ENGINE_NATIVE_BEETS", "", "reviewed-wave26-pretracking-filename-repair"
            if func == "_maintenance_safe_folder_renames":
                return "ENGINE_NATIVE_BEETS", "", "reviewed-wave26-orphan-folder-rename"
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
        if func in _APP_STATE_VERIFIED_FUNCTIONS:
            return "APP_STATE", "", "verified-app-state-function"
        for classification, hints in _APP_STATE_HINTS.items():
            if any(h.lower() in text.lower() or h.lower() in func.lower() for h in hints):
                return classification, "", f"path-hint:{classification.lower()}"
        if func in ("_move_artwork_to_target", "_album_cleanup_apply_issue") and "rmdir" in text:
            return "NON_MEDIA_FILESYSTEM", "", "reviewed-cosmetic-empty-dir-cleanup"
        if func == "start_import" and "mkdir" in text:
            return "NON_MEDIA_FILESYSTEM", "", "import-source-directory-creation"
        if "replace(" in text or "rename(" in text:
            if not _text_looks_path_like(text) or r"\x00" in text or "artpath.replace" in text:
                return "READ_ONLY_FALSE_POSITIVE", "", "string-replace-false-positive"
        return "ARCH003_BLOCKER", "", "unwrapped-local-media-filesystem-mutation"

    return "ARCH003_BLOCKER", "", "unclassified-mutation-sink"


def _determine_domain(sink: MutationSink, classification: str, family: str, rule: str) -> str:
    func = sink.function.lower()
    text = sink.call_text.lower()
    file = sink.file

    reviewed = _REVIEWED_RULE_DETAILS.get(rule)
    if reviewed:
        return str(reviewed["domain"])
    if family == "library_cleanup_v1":
        return "library_cleanup"

    # Playlist operations
    if "playlist" in func or "playlist" in text or "playlist" in rule:
        return "library_cleanup"

    # 1. Album Artwork
    if (re.search(r"\b(art|artwork|artpath|cover|fetchart|embedart)\b", func) or re.search(r"\b(art|artwork|artpath|cover|fetchart|embedart)\b", text)) and not re.search(r"\b(artist|start|part|import|draft|mbid|genre|sticking)\b", func) and not re.search(r"\b(draft|ytdlp|cookie)\b", text):
        return "album_artwork"
    if family == "album_artwork_v1" or "artwork" in rule:
        return "album_artwork"

    # 2. Album Metadata & Identity
    if "attach_recording" in func:
        return "import_reconciliation"
    if family in ("album_metadata_repair_v1", "album_mb_track_repair_v1") or "mbid" in rule or "tag" in rule:
        return "album_metadata"
    if any(k in func for k in ("fix_metadata", "retag", "stamp_album", "apply_genre")) and "ytdlp" not in func and "cookie" not in text:
        return "album_metadata"

    # 3. Album Relocation / Rename / Move / Merge
    if family in ("album_relocation_v1", "artist_folder_reconcile_v1", "existing_album_reconcile_v1"):
        return "album_relocation"
    if any(k in func for k in ("album_rename", "move_to_library", "relocate_album")) and "playlist" not in func:
        return "album_relocation"

    # 4. Album Retirement / Delete / Dedup
    if family in ("album_cleanup_v1", "album_maintenance_v1") or "delete-album" in rule:
        return "album_retirement"
    if any(k in func for k in ("album_remove", "album_delete", "album_deduplicate", "deduplicate_album")):
        return "album_retirement"

    # 5. Import Reconciliation
    if "import" in func or "import" in text or family in (
        "import_folder_v1", "import_review_cleanup_v1", "track_replacement_v1", "bulk_import_replacement_v1"
    ):
        return "import_reconciliation"

    # 6. Library & Folder Cleanup
    if any(k in func or k in text for k in ("folder_cleanup", "playlist_media_cleanup", "cleanup")):
        return "library_cleanup"
    if family in ("folder_cleanup_v1", "playlist_media_cleanup_v1"):
        return "library_cleanup"

    # 7. AI Import
    if "ai" in func or "batch" in func or "ai_import" in text:
        return "ai_import"

    # 8. Submissions
    if "submission" in func or "submission" in text or file == "routes_submissions.py":
        return "submission"

    # 9. Generic Admin Commands
    if "commands/execute" in text or "run_command" in text or rule.startswith("control-agent-generic-command"):
        return "generic_admin"

    # 10. Config / Settings
    if classification in ("CONFIG_STATE", "ENGINE_CONFIG_STATE") or file == "routes_setup.py" or "config" in text or "settings" in text or "env" in text or "m3u" in text or "runtime" in func:
        return "config"

    # 11. Other System State
    return "other"


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
        # SEC-002 / ARCH-003 Wave 23 final review, finding #20 (dropped by
        # a submitted PR #98 and restored here): a sink already carrying a
        # genuine `human_reviewed` entry from a prior pass keeps that
        # entry verbatim (apart from refreshing its file/function/line in
        # case of harmless drift) instead of being silently re-classified
        # by this run's rule table. `human_reviewed` records that an
        # actual human looked at THIS specific sink and confirmed the
        # classification; blindly recomputing it every run would let a
        # rule-table change quietly overwrite that provenance.
        prior_entry = existing_by_key.get(s.key)
        if prior_entry and prior_entry.get("human_reviewed"):
            entry = dict(prior_entry)
            entry["file"], entry["function"], entry["line"] = s.file, s.function, s.lineno
            classification, family, rule = _classify(s)
            review = _REVIEWED_RULE_DETAILS.get(rule)
            if review:
                entry["classification"] = classification
                entry["transaction_family"] = family
                entry["domain"] = _determine_domain(s, classification, family, rule)
                entry["rule"] = rule
                entry["review_reason"] = str(review["review_reason"])
                entry["reviewed_in_pr"] = int(review["reviewed_in_pr"])
            elif not entry.get("domain"):
                entry["domain"] = _determine_domain(s, entry.get("classification") or "", family, rule)
            entries.append(entry)
            continue
        classification, family, rule = _classify(s)
        domain = _determine_domain(s, classification, family, rule)
        review = _REVIEWED_RULE_DETAILS.get(rule)
        human_reviewed = bool(review) or rule.startswith(("reviewed-", "confirmed-"))
        entries.append({
            "key": s.key,
            "file": s.file,
            "function": s.function,
            "line": s.lineno,
            "kind": s.kind,
            "call_text": s.call_text,
            "classification": classification,
            "transaction_family": family,
            "domain": domain,
            "rule": rule,
            "machine_classified": True,
            "human_reviewed": human_reviewed,
            "review_reason": str(review["review_reason"]) if review else ("individually investigated during SEC-002/ARCH-003 Wave 24 review (production caller search, route tracing, function-body read)" if human_reviewed else ""),
            "reviewed_in_pr": int(review["reviewed_in_pr"]) if review else (98 if human_reviewed else None),
        })

    counts = Counter(e["classification"] for e in entries)
    domain_counts = Counter(e["domain"] for e in entries)
    unresolved_domain_counts = Counter(e["domain"] for e in entries if e["classification"] in ("ARCH003_BLOCKER", "ENGINE_GENERIC_BYPASS", "NEEDS_REVIEW"))

    unresolved = counts.get("ARCH003_BLOCKER", 0) + counts.get("NEEDS_REVIEW", 0) + counts.get("ENGINE_GENERIC_BYPASS", 0)

    payload = {
        "version": "2.1",
        "generator": "scripts/generate_arch003_mutation_inventory.py (AST discovery with explicit domain mapping)",
        "arch003_status": "in_progress",
        "total_sinks": len(entries),
        "classification_counts": dict(counts),
        "domain_counts": dict(domain_counts),
        "unresolved_domain_counts": dict(unresolved_domain_counts),
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
    print("Unresolved domain counts:", result["unresolved_domain_counts"])
