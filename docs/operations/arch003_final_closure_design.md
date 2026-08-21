# ARCH-003 / SEC-002 Final Closure Design Document

## 1. Overview & Architecture

This document records the final closure of **ARCH-003** (Controlled Engine Mutation Boundary Architecture). All library filesystem and database mutations across beets-web-manager have been migrated to single-writer, authenticated engine transaction calls (`beets_client` → `beets_control_agent` → `transaction_engine`).

### Core Boundary Rules
1. **Web Manager as Orchestrator**: The Web Manager handles user interface actions, review decisions, candidate previews, Plex sync orchestration, and local app-state (playlist manifests, job status).
2. **Control Agent & Engine Ownership**: All media file moves/unlinks, tag changes, artwork relocations, and Beets DB DML execute strictly inside the engine transaction boundary.
3. **Fail-Closed Policy**: If the Control Agent HTTP service is unreachable or errors, operations fail closed immediately. `BeetsClient` contains zero in-process fallbacks.
4. **Machine-Checked Mutation Inventory**: `security/arch003_mutation_inventory.json` tracks 100% of production mutation sinks, verified by `scripts/verify_arch003_mutation_inventory.py --check`.

## 2. Key Component Migrations

### A. Engine-Owned Import (`import_folder_v1`)
- **Previous State**: `import_folder_with_id` in `app.py` invoked `subprocess.run` directly with local `beet import` CLI flags.
- **Final Architecture**: `import_folder_with_id` routes through `beets_client.plan_import_folder` and `beets_client.apply_import_folder`. The Control Agent executes `reimport_source_atomic` inside the daemon environment using native Beets capabilities (`--search-id`, quiet fallback `asis`, torrent staging preservation).
- **Fail-Closed**: Unreachable Control Agent immediately raises `BeetsUnavailableError` and halts import before any local mutation.

### B. Cleanup Issue Routing (`_album_cleanup_apply_issue`)
- **Previous State**: Executed direct local `shutil.move` and SQL DB updates in `app.py`.
- **Final Architecture**: Dispatches issue actions to explicit transaction families:
  - File moves / filename cleanup → `album_maintenance_v1` (`mode="filename_cleanup"`)
  - Duplicate quarantines → `album_maintenance_v1` (`mode="deduplicate"`)
  - Artwork relocations → `album_artwork_v1` (`mode="move"`)
  - Empty folder removals → `folder_cleanup_v1` (`action="remove_empty"`)

### C. Machine-Checked CI Gate (`scripts/verify_arch003_mutation_inventory.py`)
- Audits production files for unclassified mutation sinks.
- Enforces valid transaction families and blocks local mutating `beet` subprocess calls in `app.py`.

## 3. Verification & Compliance
- **Full Test Suite**: 2,492 tests passing across two consecutive full runs.
- **Security Audits**: `security_secret_scan.py`, `generate_endpoint_inventory.py --check`, `validate_compose_security.py`, and `verify_arch003_mutation_inventory.py --check` passing.
- **Status**: **ARCH-003 DONE**.
