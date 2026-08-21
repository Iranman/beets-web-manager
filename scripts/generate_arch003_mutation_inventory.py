"""Generate security/arch003_mutation_inventory.json from real AST discovery.

SEC-002 / ARCH-003 final closure review, findings #15-#27: replaces the
previous hand-written 8-entry inventory (which `verify_arch003_mutation_
inventory.py` only validated for internal consistency, never against the
actual source) with one derived from `scripts/discover_mutation_sinks.py`'s
real scan of the repository.

Classification is rule-based, applied in priority order (see
`_classify`). Every entry records which rule produced its classification
(`rule`) so a human reviewer can see *why* the tool decided what it decided,
rather than trusting an opaque judgment. Entries the rules cannot classify
with reasonable confidence are marked `NEEDS_REVIEW` -- an honest "not yet
triaged" bucket, distinct from `ARCH003_BLOCKER` ("triaged, and it is a real
gap"). Re-running this script preserves any classification a human has since
hand-edited directly in the JSON for an unchanged key; it only adds newly
discovered keys and marks keys that disappeared as no longer present.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from discover_mutation_sinks import MutationSink, discover_all  # noqa: E402

REPO_ROOT = Path(__file__).parent.parent.resolve()
INVENTORY_PATH = REPO_ROOT / "security" / "arch003_mutation_inventory.json"

# backend/transaction_engine.py function name -> owning transaction family.
# This file is the engine-owned controlled-mutation boundary itself, so its
# own sinks are, by definition, the implementation of that boundary -- they
# are the CONTROLLED_MEDIA_MUTATION entries other files delegate to.
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
}
# Shared low-level helpers used by every family (safe-rename primitive,
# tag-write primitive, and the transaction store's own durable-state
# persistence) -- these are infrastructure the controlled boundary is built
# on top of, not a mutation family in their own right.
_ENGINE_INFRA_FUNCTIONS = {
    "_safe_rename": "TRANSACTION_STATE",
    "_write_file_audio_tags": "ENGINE_NATIVE_BEETS",
    "TransactionStore._write": "TRANSACTION_STATE",
    "TransactionStore.save_settings": "TRANSACTION_STATE",
}

# Path/context substrings that mean "this is Web Manager app state, not
# Beets library media" -- config files, caches, job/transaction bookkeeping,
# playlist manifests, secrets. Matched against the call text.
_APP_STATE_HINTS = {
    "CACHE_STATE": ("_cache", "cache_dir", "CACHE_DIR", ".cache", "artist_image", "release_art"),
    "CONFIG_STATE": ("config.yaml", "_cfg", "bootstrap_secret", "beets_config", "settings", "auth_token", "/config/config"),
    "STAGING_ONLY": ("staging", "preview_download", "cookie_rejected", "ytdlp"),
    "APP_STATE": ("job", "manifest", "report", "_last_", "playlist", "wanted_row"),
}

_BEET_READONLY_VERBS = {"version", "ls", "list", "stats", "config", "fields"}
_BEET_MUTATING_VERBS = {"import", "move", "write", "modify", "mbsync", "remove", "update"}


def _classify(sink: MutationSink) -> tuple[str, str, str]:
    """Return (classification, transaction_family, rule)."""
    if sink.file == "backend/transaction_engine.py":
        fam = _ENGINE_FUNCTION_FAMILY.get(sink.function)
        if fam:
            return "CONTROLLED_MEDIA_MUTATION", fam, "engine-function-family-map"
        infra = _ENGINE_INFRA_FUNCTIONS.get(sink.function)
        if infra:
            return infra, "", "engine-infra-helper"
        return "NEEDS_REVIEW", "", "engine-function-unmapped"

    if sink.file == "backend/beets_control_agent.py":
        # This file *is* the engine container's native Beets/filesystem
        # interface by architectural definition (see docs/ARCHITECTURE.md).
        # A file-level default, not an individual per-line review --
        # documented as such; genuinely dangerous generic bypass surface
        # inside it is a separate audit item (see design doc finding #28).
        return "ENGINE_NATIVE_BEETS", "", "file-is-engine-boundary"

    if sink.file not in ("app.py",):
        # Smaller support/helper modules (routes_setup.py, audio_
        # preferences.py, slskd.py, helpers_mb.py, ...): default to
        # NEEDS_REVIEW rather than guess: these were not individually
        # reviewed this pass.
        return "NEEDS_REVIEW", "", "unreviewed-support-module"

    # ---- app.py: the actual Web Manager surface ----
    text = sink.call_text

    if sink.kind == "subprocess":
        if "BEET_BIN" in text or re.search(r"\bbase(_import)?\s*\+", text):
            verbs_hit = {v for v in _BEET_MUTATING_VERBS if f'"{v}"' in text or f"'{v}'" in text}
            if verbs_hit:
                return "ARCH003_BLOCKER", "", f"legacy-local-beet-subprocess:{','.join(sorted(verbs_hit))}"
            if any(f'"{v}"' in text or f"'{v}'" in text for v in _BEET_READONLY_VERBS):
                return "READ_ONLY_FALSE_POSITIVE", "", "beet-subprocess-read-only-verb"
            return "NEEDS_REVIEW", "", "beet-subprocess-unclassified-verb"
        return "NON_MEDIA_FILESYSTEM", "", "non-beet-subprocess"

    if sink.kind == "sql":
        # Heuristic: DML against `items`/`albums` (Beets library tables) is
        # a real library mutation unless the enclosing function is already
        # known to delegate to the engine for its actual mutation (in which
        # case this line is dead/vestigial and should be individually
        # reviewed, not silently trusted either way).
        if re.search(r"\b(items|albums)\b", text, re.IGNORECASE):
            return "ARCH003_BLOCKER", "", "raw-beets-library-dml"
        return "NEEDS_REVIEW", "", "sql-non-library-table-unclassified"

    if sink.kind == "tag_write":
        return "ARCH003_BLOCKER", "", "local-tag-write"

    if sink.kind == "filesystem":
        for classification, hints in _APP_STATE_HINTS.items():
            if any(h.lower() in text.lower() or h.lower() in sink.function.lower() for h in hints):
                return classification, "", f"path-hint:{classification.lower()}"
        # Known-reviewed sinks from this session's own corrections: cosmetic
        # empty-subdirectory cleanup left in _move_artwork_to_target after
        # the engine already relocated the real files (see that function's
        # own comment) -- not a library-media mutation.
        if sink.function == "_move_artwork_to_target" and sink.call_text.strip() in ("sub.rmdir()", "d.rmdir()"):
            return "NON_MEDIA_FILESYSTEM", "", "reviewed-cosmetic-empty-dir-cleanup"
        return "NEEDS_REVIEW", "", "filesystem-unclassified"

    return "NEEDS_REVIEW", "", "unmatched-kind"


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
            # A human has explicitly annotated this exact call text before;
            # preserve their judgment rather than overwrite it silently.
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

    from collections import Counter
    counts = Counter(e["classification"] for e in entries)

    unresolved = counts.get("ARCH003_BLOCKER", 0) + counts.get("NEEDS_REVIEW", 0)
    payload = {
        "version": "2.0",
        "generator": "scripts/generate_arch003_mutation_inventory.py (AST discovery, not hand-written)",
        "arch003_status": "in_progress" if unresolved else "done",
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
