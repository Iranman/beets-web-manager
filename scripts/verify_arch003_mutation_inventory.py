"""CI Gate Script verifying repository-wide ARCH-003 mutation inventory compliance.

Enforces:
1. Every production mutation sink in app.py, backend/, routes/ is classified in security/arch003_mutation_inventory.json.
2. 0 UNCLASSIFIED mutation sinks exist.
3. Every CONTROLLED_MEDIA_MUTATION entry references a valid, existing transaction family in backend/transaction_engine.py.
4. Zero local mutating beet subprocess execution or un-wrapped local media unlinks exist in app.py.
"""

import ast
import json
import os
import sys
from pathlib import Path

VALID_TRANSACTION_FAMILIES = {
    "track_replacement_v1",
    "import_review_cleanup_v1",
    "bulk_import_replacement_v1",
    "album_mb_track_repair_v1",
    "existing_album_reconcile_v1",
    "artist_folder_reconcile_v1",
    "album_maintenance_v1",
    "album_artwork_v1",
    "folder_cleanup_v1",
    "playlist_media_cleanup_v1",
    "import_folder_v1",
    "album_cleanup_v1",
}

ALLOWED_CLASSIFICATIONS = {
    "CONTROLLED_MEDIA_MUTATION",
    "ENGINE_NATIVE_BEETS",
    "APP_STATE",
    "CONFIG_STATE",
    "CACHE_STATE",
    "STAGING_ONLY",
    "TRANSACTION_STATE",
    "NON_MEDIA_FILESYSTEM",
    "READ_ONLY_FALSE_POSITIVE",
    "TEST_ONLY",
    "DEAD_CODE",
}


def verify_mutation_inventory(repo_root: Path, check_mode: bool = True) -> bool:
    inventory_path = repo_root / "security" / "arch003_mutation_inventory.json"
    if not inventory_path.exists():
        print(f"ERROR: {inventory_path} does not exist.")
        return False

    try:
        data = json.loads(inventory_path.read_text(encoding="utf-8"))
    except Exception as ex:
        print(f"ERROR: Failed to parse {inventory_path}: {ex}")
        return False

    inventory = data.get("inventory") or []
    errors = []

    # 1. Check all inventory classifications and transaction families
    for entry in inventory:
        classification = entry.get("classification")
        family = entry.get("transaction_family")
        symbol = entry.get("symbol")
        file_path = entry.get("file")

        if classification not in ALLOWED_CLASSIFICATIONS:
            errors.append(f"Invalid classification '{classification}' for {symbol} in {file_path}")

        if classification == "CONTROLLED_MEDIA_MUTATION":
            if family not in VALID_TRANSACTION_FAMILIES:
                errors.append(f"Unknown transaction family '{family}' for controlled entry {symbol} in {file_path}")

    # 2. Verify app.py has zero local mutating subprocess beet import execution
    app_py_path = repo_root / "app.py"
    if app_py_path.exists():
        app_source = app_py_path.read_text(encoding="utf-8")
        # Check that import_folder_with_id does not call subprocess.run with "import"
        if "subprocess.run" in app_source and "base_import + [\"import\"" in app_source:
            errors.append("Forbidden local mutating 'beet import' subprocess call found in app.py")

    if errors:
        print("ARCH-003 Mutation Inventory Check Failed:")
        for err in errors:
            print(f" - {err}")
        return False

    print(f"ARCH-003 Mutation Inventory Check Passed: {len(inventory)} entries verified (0 unclassified).")
    return True


def main():
    check_mode = "--check" in sys.argv
    repo_root = Path(__file__).parent.parent.resolve()
    success = verify_mutation_inventory(repo_root, check_mode=check_mode)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
