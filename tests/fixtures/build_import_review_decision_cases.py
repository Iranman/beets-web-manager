"""Regenerate frontend/tests/fixtures/import_review_decision_cases.json.

The file lives under frontend/ so the frontend suite can load it from a
frontend-only build context; the backend suite reads the same file.

Each case is a decision input; ``expected`` is what the backend authority
(backend/import_review_decision.decide) returns for it. The frontend mirror
(frontend/src/features/importReview/importReviewDecision.ts) must return the
same payload for the same input (frontend/tests/importReviewDecision.test.ts).

Run from the repository root after changing a decision rule ON BOTH SIDES:

    python tests/fixtures/build_import_review_decision_cases.py
"""

import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backend.import_review_decision import decide  # noqa: E402

RG = "11111111-2222-3333-4444-555555555555"
REL = "66666666-7777-8888-9999-aaaaaaaaaaaa"
OTHER = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"


def item(**overrides):
    base = {"id": "review-1", "type": "pending_ai", "status": "Needs review", "reason": "", "path": "/downloads/a",
            "mb_valid": True, "mb_albumid": REL, "existing_album_ids": []}
    base.update(overrides)
    return base


def rows(*statuses):
    return [{"num": n, "local_title": f"t{n}", "mb_title": f"T{n}", "mb_trackid": "", "status": status,
             "source_path": f"/downloads/a/{n:02d}.flac" if status not in ("missing",) else ""}
            for n, status in enumerate(statuses, 1)]


def match(**overrides):
    base = {"release_group_id": RG, "representative_release_id": REL, "artist": "A", "album": "B", "year": "2000",
            "track_mapping": rows("matched", "verified_match", "fuzzy"), "preflight_status": "passed",
            "preflight_reason": "", "is_release_group_usable": True, "is_importable": True,
            "is_partial_import": False, "auto_fix_eligible": False, "source": "candidate"}
    base.update(overrides)
    return base


def preview(count=3, **overrides):
    base = {"ok": True, "safe": True, "status": "safe", "blocked_reasons": [], "warnings": [],
            "tracks_to_import_count": count, "real_conflict_count": 0, "tracks": []}
    base.update(overrides)
    return {"status": "ready", "key": "k", "preview": base}


CASES = [
    ("ready import", item(), RG, match(), preview()),
    ("auto-fix eligible import", item(), RG, match(auto_fix_eligible=True), preview()),
    ("no ID entered, album", item(), "", match(), preview()),
    ("no ID entered, item target", item(type="library_no_mb", target_kind="item"), "  ", None, None),
    ("library album without ID", item(type="library_no_mb", target_kind="album"), RG, None, None),
    ("no match selected", item(), RG, None, None),
    ("field and match out of sync", item(), OTHER, match(), preview()),
    ("ID differs only by case and spaces", item(), f"  {RG.upper()} ", match(), preview()),
    ("release group is not a UUID", item(), "not-a-uuid", match(release_group_id="not-a-uuid"), preview()),
    ("no representative release", item(), RG, match(representative_release_id=""), preview()),
    ("identity not validated", item(), RG, match(identity_validated=False), preview()),
    ("identity not validated with reason", item(), RG,
     match(identity_validated=False, candidate_identity_error="Release belongs to another Release Group."), preview()),
    ("track comparison not finished", item(), RG, match(track_mapping=[]), preview()),
    ("preflight not run", item(), RG, match(preflight_status="not_run"), preview()),
    ("preflight stale", item(), RG, match(preflight_status="stale"), preview()),
    ("preflight failed with reason", item(), RG,
     match(preflight_status="failed", preflight_reason="Only 2 of 12 tracks matched."), preview()),
    ("preflight failed without reason", item(), RG, match(preflight_status="failed"), preview()),
    ("not importable", item(), RG, match(is_importable=False, preflight_reason="Tracklist too short."), preview()),
    ("preview missing", item(), RG, match(), None),
    ("preview idle", item(), RG, match(), {"status": "idle", "key": "k"}),
    ("preview loading", item(), RG, match(), {"status": "loading", "key": "k"}),
    ("preview error with message", item(), RG, match(), {"status": "error", "key": "k", "error": "Preview timed out."}),
    ("preview error without message", item(), RG, match(), {"status": "error", "key": "k"}),
    ("preview unsafe with reason", item(), RG, match(),
     preview(safe=False, status="blocked", blocked_reasons=["target folder holds another release"])),
    ("preview unsafe without reason", item(), RG, match(), preview(safe=False, status="blocked")),
    ("preview needs cleanup", item(), RG, match(),
     preview(safe=False, status="blocked", next_action="verify_or_cleanup_unmatched")),
    ("preview count differs from selection", item(), RG, match(), preview(count=2)),
    ("no importable rows", item(), RG, match(track_mapping=rows("different", "missing", "extra")), preview(count=0)),
    ("preview without a count uses the selection", item(), RG, match(), preview(count=None)),
    ("conflicting target file is not imported", item(), RG, match(),
     preview(count=2, tracks=[{"source_path": "/downloads/a/01.flac", "target_conflict": True}])),
    ("real conflict keeps it out of ready", item(), RG, match(), preview(real_conflict_count=1)),
    ("partial import", item(), RG, match(is_partial_import=True, track_mapping=rows("matched", "missing", "fuzzy")),
     preview(count=2)),
    ("partial import of one track", item(), RG,
     match(is_partial_import=True, track_mapping=rows("matched", "missing")), preview(count=1)),
    ("repair of an existing album", item(existing_album_id=42), RG, match(auto_fix_eligible=True), None),
    ("repair with failed preflight", item(existing_album_ids=[42]), RG, match(preflight_status="failed"), None),
    ("partial repair", item(existing_album_id=42), RG,
     match(is_partial_import=True, track_mapping=rows("matched", "missing")), preview(count=1)),
    ("stored blocked reason, no match", item(blocked_reason="Format policy rejected this source.",
                                           blocked_next_action="Pick another source."), RG, None, None),
    ("stored reason mentions blocking", item(status="Blocked", reason="Target path conflict blocked the import."),
     RG, None, None),
    ("blocked status key", item(status_key="target-conflict"), RG, None, None),
    ("failed status", item(status="Import Failed"), RG, match(), preview()),
    ("audio mismatch evidence", item(evidence={"preflight": {"ok": False, "acoustid_mismatch": True}}), RG, match(),
     preview()),
    ("stored preflight failure", item(evidence={"preflight": {"ok": False}}), RG, match(), preview()),
    ("skipped item", item(type="skipped", blocked_reason="Skipped by operator."), RG, match(), preview()),
    ("no candidate", item(mb_valid=False, mb_albumid=""), "", None, None),
    ("other type with a valid ID", item(type="needs_review", mb_valid=True), RG, None, None),
]


def main() -> None:
    out = []
    for name, it, mbid, selected, state in CASES:
        entry = {"item": it, "mbid": mbid, "selected_match": selected, "target_preview_state": state}
        out.append({"name": name, "input": entry, "expected": decide(copy.deepcopy(entry))})
    target = ROOT / "frontend" / "tests" / "fixtures" / "import_review_decision_cases.json"
    target.write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {len(out)} cases to {target}")


if __name__ == "__main__":
    main()
