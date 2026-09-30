"""The authoritative Import Review action decision (ARCH-005).

For one review item, the ID the operator entered, the match they selected and
the target path preview, this module decides: which filter bucket the item
is in, whether the apply action is blocked and why, what to do next, which
source files an import would take, and what the action is called.

The frontend mirrors these rules in
``frontend/src/features/importReview/importReviewDecision.ts`` so the page
can react instantly; both implementations are run against the same cases
(``frontend/tests/fixtures/import_review_decision_cases.json``) in CI, and the apply
path asks this module (``POST /api/import-review/decision``) for the final
verdict. The import itself is still gated again by the import workflow.

Everything here is a pure function of its input: no I/O, no library access.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

_MB_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
_BLOCK_WORD_RE = re.compile(r"\bblock(?:ed|ing)?\b", re.IGNORECASE)
_STATUS_SEPARATORS_RE = re.compile(r"[\s-]+")

IMPORTABLE_TRACK_STATUSES = frozenset({"matched", "fuzzy", "verified_match", "acoustid_verified"})
FAILED_STATUS_KEYS = frozenset({"auto_enqueue_failed", "import_failed", "import_failed_needs_reconcile",
                                "preflight_failed", "failed"})
BLOCKED_STATUS_KEYS = frozenset({"blocked", "not_importable", "target_conflict", "purge_required", "duplicate_only",
                                 "duplicate_cleanup", "no_verified_tracks", "format_policy_rejected"})


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _count(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def same_mbid(left: Any, right: Any) -> bool:
    a, b = _text(left), _text(right)
    return bool(a and b and a.strip().lower() == b.strip().lower())


def is_musicbrainz_uuid(value: Any) -> bool:
    return bool(_MB_UUID_RE.match(_text(value).strip()))


def existing_album_id(item: Dict[str, Any]) -> int:
    ids = item.get("existing_album_ids") or []
    return int(_count(item.get("existing_album_id") or (ids[0] if ids else 0) or 0))


def has_audio_mismatch_evidence(item: Dict[str, Any]) -> bool:
    preflight = (item.get("evidence") or {}).get("preflight") or {}
    return bool(preflight.get("acoustid_mismatch"))


def item_match_bucket(item: Dict[str, Any]) -> str:
    """The bucket the item's own stored state puts it in."""
    if has_audio_mismatch_evidence(item):
        return "audio_mismatch"
    status_key = _STATUS_SEPARATORS_RE.sub("_", _text(item.get("status_key") or item.get("status")).strip().lower())
    if status_key in FAILED_STATUS_KEYS:
        return "failed"
    if item.get("blocked_reason") or status_key in BLOCKED_STATUS_KEYS:
        return "blocked"
    if item.get("type") == "library_no_mb":
        return "needs_id"
    if item.get("type") != "pending_ai":
        return "ready" if item.get("mb_valid") else "no_candidate"
    if not item.get("mb_valid") and not item.get("mb_albumid"):
        return "no_candidate"
    preflight = (item.get("evidence") or {}).get("preflight")
    if preflight and preflight.get("ok") is False:
        return "failed"
    return "ready"


def selected_import_source_files(match: Optional[Dict[str, Any]], preview: Optional[Dict[str, Any]] = None) -> List[str]:
    """The source files an import of this match would take: importable mapped
    rows, minus files the target preview reports as conflicting."""
    if not match or match.get("identity_validated") is False:
        return []
    conflicted = {t.get("source_path") for t in ((preview or {}).get("tracks") or [])
                  if t.get("target_conflict") and t.get("source_path")}
    files: List[str] = []
    for row in match.get("track_mapping") or []:
        path = row.get("source_path") or ""
        if row.get("status") in IMPORTABLE_TRACK_STATUSES and path and path not in conflicted and path not in files:
            files.append(path)
    return files


def _preview_count(preview: Dict[str, Any], selected_count: int) -> float:
    value = preview.get("tracks_to_import_count")
    return float(selected_count) if value is None else _count(value)


def target_preview_block_reason(item: Dict[str, Any], match: Optional[Dict[str, Any]],
                                preview_state: Optional[Dict[str, Any]]) -> str:
    import_like = item.get("type") == "pending_ai" and not existing_album_id(item)
    if not import_like or not match:
        return ""
    if not match.get("is_importable"):
        return ""
    status = (preview_state or {}).get("status")
    if not preview_state or status == "idle":
        return "Import blocked until the target path preview is available."
    if status == "loading":
        return "Import blocked until the target path preview finishes."
    if status == "error":
        return preview_state.get("error") or "Import blocked because the target path preview failed."
    preview = preview_state.get("preview")
    if not preview:
        return "Import blocked until the target path preview is available."
    if not preview.get("safe"):
        if preview.get("next_action") == "verify_or_cleanup_unmatched":
            return ("Import blocked: no verified tracks selected after automatic verification; purge/quarantine the "
                    "unmatched source file or choose another match.")
        reasons = preview.get("blocked_reasons") or []
        if reasons and reasons[0]:
            return f"Import blocked by target path preview: {reasons[0]}."
        return "Import blocked because the target path preview is not safe."
    selected_count = len(selected_import_source_files(match, preview))
    preview_count = _preview_count(preview, selected_count)
    if preview_count < 1 or selected_count < 1:
        return "Import blocked: no verified tracks selected for import."
    if preview_count != selected_count:
        return "Import blocked: selected file count does not match target preview."
    return ""


def apply_block_reason(item: Dict[str, Any], mbid: Any, match: Optional[Dict[str, Any]],
                       preview_state: Optional[Dict[str, Any]]) -> str:
    """Why the apply action may not run for this selection; "" when it may."""
    release_group_id = _text(mbid).strip()
    if not release_group_id:
        if item.get("target_kind") == "item":
            return "Enter or select a MusicBrainz recording ID first."
        return "Enter or select a MusicBrainz Release Group ID first."
    import_like = item.get("type") == "pending_ai" and not existing_album_id(item)
    if not import_like:
        if match and match.get("preflight_status") == "failed":
            return "Import blocked because this candidate failed tracklist preflight."
        return ""
    if not match:
        return "Select the visible MusicBrainz match first so its track comparison controls the import."
    if not same_mbid(match.get("release_group_id"), release_group_id):
        return "Import blocked because the visible match and Release Group ID field are out of sync."
    if not is_musicbrainz_uuid(match.get("release_group_id")):
        return "Import blocked because this candidate does not include a valid MusicBrainz Release Group ID."
    if not is_musicbrainz_uuid(match.get("representative_release_id")):
        return ("Import blocked because this candidate does not include a representative release for tracklist "
                "comparison.")
    if match.get("identity_validated") is False:
        return (match.get("candidate_identity_error")
                or "Import blocked: representative release does not belong to selected Release Group.")
    if not (match.get("track_mapping") or []):
        return "Import blocked until the visible candidate track comparison finishes."
    if match.get("preflight_status") in ("not_run", "stale"):
        return "Import blocked until preflight is refreshed for the selected visible candidate."
    if match.get("preflight_status") == "failed":
        return match.get("preflight_reason") or "Import blocked because this candidate failed tracklist preflight."
    if not match.get("is_importable"):
        return match.get("preflight_reason") or "Import blocked because the selected match is not importable."
    return target_preview_block_reason(item, match, preview_state)


def stored_blocked_reason(item: Dict[str, Any]) -> str:
    if item.get("blocked_reason"):
        return _text(item.get("blocked_reason"))
    text = " ".join(_text(v) for v in (item.get("status"), item.get("reason")) if v)
    if _BLOCK_WORD_RE.search(text):
        return _text(item.get("reason") or item.get("status")) or "Import blocked."
    return ""


def action_block_reason_for_filter(item: Dict[str, Any], mbid: Any, match: Optional[Dict[str, Any]],
                                   preview_state: Optional[Dict[str, Any]]) -> str:
    selected = apply_block_reason(item, mbid, match, preview_state) if match else ""
    return selected or stored_blocked_reason(item)


def should_show_blocked_bucket(item: Dict[str, Any], mbid: Any, match: Optional[Dict[str, Any]],
                               preview_state: Optional[Dict[str, Any]]) -> bool:
    if item.get("type") == "skipped" or has_audio_mismatch_evidence(item):
        return False
    return bool(action_block_reason_for_filter(item, mbid, match, preview_state))


def should_show_ready_bucket(item: Dict[str, Any], mbid: Any, match: Optional[Dict[str, Any]],
                             preview_state: Optional[Dict[str, Any]]) -> bool:
    if item.get("type") == "skipped" or has_audio_mismatch_evidence(item):
        return False
    if should_show_blocked_bucket(item, mbid, match, preview_state):
        return False
    if not match or not match.get("is_importable"):
        return False
    if not same_mbid(match.get("release_group_id"), mbid):
        return False
    if not is_musicbrainz_uuid(match.get("release_group_id")):
        return False
    if not is_musicbrainz_uuid(match.get("representative_release_id")):
        return False
    if match.get("preflight_status") != "passed":
        return False
    preview = (preview_state or {}).get("preview") if (preview_state or {}).get("status") == "ready" else None
    if not preview or not preview.get("safe"):
        return False
    if _count(preview.get("real_conflict_count")) > 0:
        return False
    selected_count = len(selected_import_source_files(match, preview))
    preview_count = _preview_count(preview, selected_count)
    return selected_count > 0 and preview_count > 0 and selected_count == preview_count


def blocked_action_hint(reason: Any) -> str:
    value = _text(reason).lower()
    if "music format preferences" in value or "format policy" in value:
        return "Choose another source or update Music Format Preferences before retrying."
    if "target path" in value:
        return "Fix the target path conflict, then retry this item."
    if "out of sync" in value or "visible musicbrainz match" in value:
        return "Select the visible candidate again so the ID field and comparison agree."
    if "release group id" in value or "valid musicbrainz" in value:
        return "Use Find Match or enter a valid MusicBrainz Release or Release Group ID."
    if "preflight" in value or "tracklist" in value or "not importable" in value:
        return "Choose a release that matches the files, or delete the source folder if the audio is wrong."
    if "no verified tracks" in value or "selected file count" in value:
        return "Adjust the selected track mapping before importing."
    return "Resolve this block before importing; uncertain audio stays in review."


def action_label(item: Dict[str, Any], match: Optional[Dict[str, Any]] = None,
                 preview: Optional[Dict[str, Any]] = None) -> str:
    if item.get("type") == "library_no_mb":
        return "Attach recording ID" if item.get("target_kind") == "item" else "Match album"
    selected_count = len(selected_import_source_files(match, preview)) if match else 0
    preview_count = _preview_count(preview or {}, selected_count)
    existing = existing_album_id(item)
    if match and match.get("identity_validated") is False:
        return "Import blocked"
    if match and match.get("is_partial_import"):
        n = int(max(0, min(selected_count or preview_count, preview_count)))
        plural = "" if n == 1 else "s"
        return f"Repair {n} matched track{plural}" if existing else f"Import {n} matched track{plural}"
    if match and match.get("auto_fix_eligible"):
        return "Complete Verified Repair" if existing else "Complete Verified Import"
    return "Repair with ID" if existing else "Import with ID"


def decide(entry: Dict[str, Any]) -> Dict[str, Any]:
    """The full decision for one entry
    ``{"item", "mbid", "selected_match", "target_preview_state"}``."""
    item = entry.get("item") or {}
    mbid = _text(entry.get("mbid"))
    match = entry.get("selected_match") or None
    preview_state = entry.get("target_preview_state") or None
    preview = (preview_state or {}).get("preview") or None

    apply_reason = apply_block_reason(item, mbid, match, preview_state)
    stored_reason = stored_blocked_reason(item)
    block_reason = apply_reason or stored_reason
    if apply_reason:
        next_action = blocked_action_hint(apply_reason)
    else:
        next_action = _text(item.get("blocked_next_action")) or (blocked_action_hint(block_reason) if block_reason else "")
    return {
        "match_bucket": item_match_bucket(item),
        "blocked": should_show_blocked_bucket(item, mbid, match, preview_state),
        "ready": should_show_ready_bucket(item, mbid, match, preview_state),
        "can_apply": not apply_reason,
        "apply_block_reason": apply_reason,
        "block_reason": block_reason,
        "next_action": next_action,
        "action_label": action_label(item, match, preview),
        "selected_source_files": selected_import_source_files(match, preview),
    }
