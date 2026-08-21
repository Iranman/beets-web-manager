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
_PATH_LIKE_NAME_RE = re.compile(
    r"(path|file|dir|folder|target|dest|dst|src|tmp|temp|cfg|config|cover|art|canonical|trash|source|p|f|d|root)",
    re.IGNORECASE,
)


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

    # 2. backend/beets_control_agent.py (engine daemon boundary)
    if file == "backend/beets_control_agent.py":
        return "ENGINE_NATIVE_BEETS", "", "file-is-engine-boundary"

    # 3. backend/beets_client.py (HTTP proxy client)
    if file == "backend/beets_client.py":
        return "ENGINE_NATIVE_BEETS", "", "beets-client-proxy"

    # 4. routes_setup.py (configuration & secrets management)
    if file == "routes_setup.py":
        if any(w in func for w in ("env", "settings", "token", "marker", "setup")) or "CONFIG" in text or "SETUP" in text:
            return "CONFIG_STATE", "", "setup-config-state"
        if func == "_check_path" or "probe" in text:
            return "NON_MEDIA_FILESYSTEM", "", "setup-path-permission-probe"
        return "CONFIG_STATE", "", "setup-module-default"

    # 5. routes_submissions.py
    if file == "routes_submissions.py":
        return "APP_STATE", "", "submissions-app-state"

    # 6. routes_jobs.py
    if file == "routes_jobs.py":
        return "APP_STATE", "", "jobs-app-state"

    # 7. routes_lidarr.py
    if file == "routes_lidarr.py":
        return "APP_STATE", "", "lidarr-app-state"

    # 8. job_engine.py
    if file == "job_engine.py":
        if sink.kind == "subprocess" or "run_command" in text:
            return "ENGINE_NATIVE_BEETS", "", "job-engine-beet-runner"
        if "replace(" in text and not _PATH_LIKE_NAME_RE.search(text):
            return "READ_ONLY_FALSE_POSITIVE", "", "string-replace-false-positive"
        return "APP_STATE", "", "job-engine-state"

    # 9. helpers_mb.py
    if file == "helpers_mb.py":
        if "fpcalc" in text:
            return "READ_ONLY_FALSE_POSITIVE", "", "fpcalc-read-only-audio-probe"
        return "NON_MEDIA_FILESYSTEM", "", "mb-helper-non-media"

    # 10. backend/ support modules
    if file.startswith("backend/"):
        if file == "backend/audio_preferences.py":
            return "CONFIG_STATE", "", "audio-preferences-config"
        if file == "backend/slskd.py":
            return "STAGING_ONLY", "", "slskd-staging-download"
        if file == "backend/beets_config.py":
            return "CONFIG_STATE", "", "beets-config-state"
        if file == "backend/security.py":
            return "NON_MEDIA_FILESYSTEM", "", "security-module"
        if sink.kind == "sql":
            return "TRANSACTION_STATE", "", "backend-sql-state"
        return "NON_MEDIA_FILESYSTEM", "", "backend-support-module"

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
            if not _PATH_LIKE_NAME_RE.search(text):
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
            "human_reviewed": False,
        })

    counts = Counter(e["classification"] for e in entries)
    unresolved = counts.get("ARCH003_BLOCKER", 0) + counts.get("NEEDS_REVIEW", 0)

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
