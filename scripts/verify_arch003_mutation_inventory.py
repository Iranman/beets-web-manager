"""CI gate for the ARCH-003 mutation inventory.

SEC-002 / ARCH-003 final closure review, findings #15-#27: the previous
version of this script only validated the hand-written inventory JSON's
internal consistency (spelling of classifications, existence of named
transaction families) and one hardcoded literal-string check for the old
`beet import` subprocess pattern -- it never looked at the actual source
code, so an inventory that only listed 8 entries passed as "0 unclassified"
regardless of what the repository actually contained.

This version re-runs the real AST discovery (`discover_mutation_sinks.py`)
against the current source and compares it against the checked-in
`security/arch003_mutation_inventory.json` by stable key. The gate is a
*ratchet*, not an all-or-nothing switch, because a first honest discovery
pass across this codebase found several hundred real candidate sinks and a
large fraction of them are genuine pre-existing ARCH-003 debt this single
review cannot respons­ibly migrate in one pass (see the design doc). Failing
CI on that entire pre-existing backlog would not make the repository safer
today; it would just make the gate impossible to land, which is how these
gates rot into `--no-verify` habits. Instead:

  - Every sink discovered in the current source MUST have a matching entry
    in the inventory (by exact key: file + enclosing function + a hash of
    the call text). A discovered sink with NO matching key at all is a
    genuinely NEW, completely unreviewed mutation site and fails the gate
    outright -- this is the real regression protection (finding #26).
  - The *count* of ARCH003_BLOCKER + NEEDS_REVIEW entries must not exceed
    the baseline recorded in the inventory file. It is fine to fix one and
    add a new (still-unclassified) one elsewhere in the same PR as long as
    the total doesn't grow; it is not fine to silently accumulate more
    unreviewed debt than existed before.
  - Every CONTROLLED_MEDIA_MUTATION entry must reference a transaction
    family that actually exists in `backend/transaction_engine.py`.
"""

import ast
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from discover_mutation_sinks import discover_all  # noqa: E402

ALLOWED_CLASSIFICATIONS = {
    "CONTROLLED_MEDIA_MUTATION",
    "ENGINE_NATIVE_BEETS",
    # SEC-002 / ARCH-003 Wave 23 final review, finding #4: "engine-owned"
    # is not automatically "architecture-safe" -- an operation that
    # executes inside the engine container can still bypass Plan/Review/
    # Apply/Verify/TransactionStore/Rollback entirely. These four
    # narrower classifications replace treating ENGINE_NATIVE_BEETS as a
    # universal exemption for anything inside the Control Agent.
    "ENGINE_CONTROLLED_TRANSACTION",  # backs a real _v1 Plan/Apply family
    "ENGINE_NATIVE_READ_ONLY",        # genuinely read-only (diagnostics, lookups)
    "ENGINE_ADMIN_MUTATION",          # deliberate, explicit, verified admin-style action -- not a transaction family, but not a silent bypass either
    "ENGINE_GENERIC_BYPASS",          # confirmed real production mutation with NO transaction boundary at all
    "ENGINE_CONFIG_STATE",            # engine-side config/job-state files, not library media
    "APP_STATE",
    "CONFIG_STATE",
    "CACHE_STATE",
    "STAGING_ONLY",
    "TRANSACTION_STATE",
    "NON_MEDIA_FILESYSTEM",
    "READ_ONLY_FALSE_POSITIVE",
    "TEST_ONLY",
    "DEAD_CODE",
    "NEEDS_REVIEW",
    "ARCH003_BLOCKER",
}
# Classifications that represent a genuine, real architectural gap and
# should count toward the unresolved/ratchet total alongside
# NEEDS_REVIEW/ARCH003_BLOCKER (finding #27: Control-Agent risks were
# previously invisible to the blocker count because everything there was
# blanket-classified as an exemption).
_UNRESOLVED_EXTRA = {"ENGINE_GENERIC_BYPASS"}
_UNRESOLVED = {"NEEDS_REVIEW", "ARCH003_BLOCKER"} | _UNRESOLVED_EXTRA


def _real_transaction_families(repo_root: Path) -> set:
    path = repo_root / "backend" / "transaction_engine.py"
    if not path.exists():
        return set()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return set()
    families = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "mutation_family" and isinstance(node.value, ast.Constant):
                    families.add(node.value.value)
        # Also catch string literals assigned to a "mutation_family" dict key.
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.endswith("_v1"):
            families.add(node.value)
    return families


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
    warnings = []

    by_key = {}
    for entry in inventory:
        key = entry.get("key")
        classification = entry.get("classification")
        family = entry.get("transaction_family")
        symbol = entry.get("function")
        file_path = entry.get("file")

        if classification not in ALLOWED_CLASSIFICATIONS:
            errors.append(f"Invalid classification '{classification}' for {symbol} in {file_path}")
        if classification == "CONTROLLED_MEDIA_MUTATION":
            real_families = _real_transaction_families(repo_root)
            if family not in real_families:
                errors.append(f"Unknown/stale transaction family '{family}' for controlled entry {symbol} in {file_path}")
        if key:
            by_key[key] = entry

    # Real discovery: every current sink must have a matching inventory key.
    discovered = discover_all(repo_root)
    new_unreviewed = []
    for sink in discovered:
        if sink.key not in by_key:
            new_unreviewed.append(sink)

    if new_unreviewed:
        errors.append(f"{len(new_unreviewed)} newly discovered mutation sink(s) have no inventory entry at all:")
        for s in new_unreviewed[:20]:
            errors.append(f"    NEW: {s.file}:{s.function}:{s.lineno} -- {s.call_text[:90]}")
        errors.append("  Run `python scripts/generate_arch003_mutation_inventory.py` and classify the new entries.")

    # SEC-002 / ARCH-003 Wave 23 final review, finding #21: NEEDS_REVIEW=0
    # must be *earned* through real triage, not mechanically enforced as a
    # hard gate -- a hard "NEEDS_REVIEW must be 0" check creates exactly
    # the pressure that produces fake precision (force every ambiguous
    # sink into some other bucket just to make the number disappear,
    # which is indistinguishable from guessing). NEEDS_REVIEW is included
    # in the ratchet's unresolved-count baseline below, so it still can't
    # silently grow -- it just isn't required to hit zero before merge.
    needs_review_count = sum(1 for e in inventory if e.get("classification") == "NEEDS_REVIEW")

    unresolved_count = sum(1 for e in inventory if e.get("classification") in _UNRESOLVED)
    baseline = data.get("unresolved_baseline", data.get("classification_counts", {}).get("ARCH003_BLOCKER", 0)
                         + data.get("classification_counts", {}).get("NEEDS_REVIEW", 0))
    if unresolved_count > baseline:
        errors.append(f"Unresolved sink count grew: {unresolved_count} now vs baseline {baseline}. "
                       f"Classify/fix at least enough entries to keep the total from increasing.")
    elif unresolved_count < baseline:
        warnings.append(f"Unresolved sink count improved: {unresolved_count} vs baseline {baseline} "
                         f"-- update 'unresolved_baseline' in the inventory to lock in the improvement.")

    for w in warnings:
        print(f"NOTE: {w}")

    if errors:
        print("ARCH-003 Mutation Inventory Check Failed:")
        for err in errors:
            print(f" - {err}")
        return False

    print(f"ARCH-003 Mutation Inventory Check Passed: {len(inventory)} entries, "
          f"{unresolved_count} unresolved (baseline {baseline}), "
          f"{len(discovered)} sinks discovered in current source, all matched to inventory.")
    return True


def main():
    check_mode = "--check" in sys.argv
    repo_root = Path(__file__).parent.parent.resolve()
    success = verify_mutation_inventory(repo_root, check_mode=check_mode)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
