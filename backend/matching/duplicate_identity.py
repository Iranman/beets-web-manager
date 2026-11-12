"""Is a folder Beets skipped as ``duplicate`` really already in the library?

Beets' duplicate check is name-only (albumartist + album), so a ``duplicate``
skip alone proves nothing. The webmanager plugin attaches
``duplicate_evidence`` (what the task matched, and the recordings of the
library albums it collided with). The folder counts as already in the
library only when Beets matched it strongly to a MusicBrainz release, every
file is mapped to a track with a Recording ID, and every one of those
recordings is already in a library album of the same Release Group.
Thresholds are owned by music identity; they are fixed, not read from the
user's Beets config. Pure: no I/O. Any missing or malformed field is "no".
"""

from __future__ import annotations

import math
from typing import Any, Mapping, Tuple

from .library_gaps import _has_id

STRONG_DISTANCE = 0.04


def _lower(value: Any) -> str:
    return str(value or "").strip().lower()


def _count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("not a count")
    return value


def duplicate_is_already_in_library(ev: Any) -> Tuple[bool, str]:
    if not isinstance(ev, Mapping):
        return False, "no_duplicate_evidence"
    if ev.get("choice") != "APPLY":
        return False, "not_a_beets_match"
    if ev.get("data_source") != "MusicBrainz":
        return False, "non_musicbrainz_match"
    distance = ev.get("distance")
    if isinstance(distance, bool) or not isinstance(distance, (int, float)) or not 0 <= distance <= STRONG_DISTANCE:
        return False, "match_not_strong"
    rg = _lower(ev.get("release_group_id"))
    if not _has_id(rg):
        return False, "match_has_no_release_group"
    try:
        extra_items, extra_tracks = _count(ev.get("extra_items")), _count(ev.get("extra_tracks"))
        mapped, items = _count(ev.get("mapped_count")), _count(ev.get("task_item_count"))
    except ValueError:
        return False, "torrent_not_fully_mapped"
    if extra_items or extra_tracks or mapped != items or not mapped:
        return False, "torrent_not_fully_mapped"
    raw = ev.get("recording_ids")
    recs = {_lower(r) for r in raw} if isinstance(raw, list) else set()
    if len(recs) != mapped or not all(_has_id(r) for r in recs):
        return False, "recording_ids_incomplete"
    dups = ev.get("duplicates") if isinstance(ev.get("duplicates"), list) else []
    same_rg = [d for d in dups if isinstance(d, Mapping) and _lower(d.get("mb_releasegroupid")) == rg]
    if not same_rg:
        return False, "name_collision_other_release_group"
    lib_recs = {_lower(r) for d in same_rg if isinstance(d.get("recording_ids"), list)
                for r in d["recording_ids"] if _has_id(_lower(r))}
    if recs <= lib_recs:
        return True, "recordings_present_in_release_group"
    return False, "torrent_has_recordings_not_in_library"


# --- Local fingerprint evidence (supporting only) ---------------------------
#
# Compares two raw Chromaprint fingerprints (``fpcalc -raw``) to say whether
# two files carry the same audio. SUPPORTING evidence for duplicate grouping
# only: it never overrides a MusicBrainz or AcoustID identity and never on
# its own authorises a merge or delete -- those still need the MB/AcoustID
# identity gates (e.g. ``same_recording_proof``). Pure: no I/O.
#
# The offset vote is adapted from ``_match_fingerprints`` in pyacoustid
# (https://github.com/beetbox/pyacoustid, acoustid.py). Changed here: the
# window is symmetric, too few votes is "no alignment" (inconclusive), and
# the best offset is scored by bit-error rate into a three-way verdict.
# pyacoustid's LICENSE, verbatim:
#
#   The MIT License
#
#   Copyright (c) 2011-2019 Adrian Sampson
#
#   Permission is hereby granted, free of charge, to any person obtaining a copy
#   of this software and associated documentation files (the "Software"), to deal
#   in the Software without restriction, including without limitation the rights
#   to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
#   copies of the Software, and to permit persons to whom the Software is
#   furnished to do so, subject to the following conditions:
#
#   The above copyright notice and this permission notice shall be included in
#   all copies or substantial portions of the Software.
#
#   THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
#   IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
#   FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
#   AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
#   LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
#   OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
#   THE SOFTWARE.

SAME_AUDIO = "same_audio"
DIFFERENT_AUDIO = "different_audio"
INCONCLUSIVE = "inconclusive"

#: Bit-error rate at or below which two aligned fingerprints are the same audio
#: (re-encodes of one master land well under this; unrelated audio is ~0.5).
SAME_AUDIO_MAX_BIT_ERROR_RATE = 0.15
#: Bit-error rate at or above which two aligned fingerprints are different audio.
DIFFERENT_AUDIO_MIN_BIT_ERROR_RATE = 0.35
#: Fewest overlapping fingerprint items (~0.124 s each) worth judging.
MIN_OVERLAP_ITEMS = 40
#: Offset search window (items, both directions) and per-item vote tolerance
#: (pyacoustid's values).
MAX_ALIGN_OFFSET = 120
MAX_ITEM_BIT_ERROR = 2
#: Fewest near-identical item pairs at one offset to call the pair aligned.
#: Fewer means the true offset may lie outside the window (or one file is a
#: clip from elsewhere in the other), so no verdict is given.
MIN_ALIGNMENT_VOTES = 10
#: Mean per-bit binary entropy below which a fingerprint carries too little
#: information to judge (silence or a constant tone; real music is ~0.9).
MIN_BIT_ENTROPY = 0.5


def _clean_fp(fp: Any) -> list:
    if not isinstance(fp, (list, tuple)):
        return []
    out = []
    for v in fp:
        if isinstance(v, bool) or not isinstance(v, int):
            return []
        out.append(v & 0xFFFFFFFF)
    return out


def _bit_entropy(fp: list) -> float:
    """Mean binary entropy of the 32 bit positions across the fingerprint."""
    total = 0.0
    for bit in range(32):
        p = sum((v >> bit) & 1 for v in fp) / len(fp)
        if 0 < p < 1:
            total -= p * math.log2(p) + (1 - p) * math.log2(1 - p)
    return total / 32


def _best_offset(a: list, b: list) -> Tuple[int, int]:
    """(offset ``i - j``, votes) with the most near-identical item pairs."""
    counts: dict = {}
    for i, x in enumerate(a):
        for j in range(max(0, i - MAX_ALIGN_OFFSET), min(len(b), i + MAX_ALIGN_OFFSET + 1)):
            if (x ^ b[j]).bit_count() <= MAX_ITEM_BIT_ERROR:
                counts[i - j] = counts.get(i - j, 0) + 1
    if not counts:
        return 0, 0
    best = max(counts, key=lambda k: (counts[k], -abs(k)))
    return best, counts[best]


def compare_raw_fingerprints(fp_a: Any, fp_b: Any) -> Tuple[str, dict]:
    """Return (verdict, details) for two raw fingerprints. Malformed input,
    low-information audio, no alignment or too little overlap is
    ``inconclusive``, never an error and never a guess."""
    a, b = _clean_fp(fp_a), _clean_fp(fp_b)
    if not a or not b:
        return INCONCLUSIVE, {"reason": "fingerprint_missing"}
    if min(_bit_entropy(a), _bit_entropy(b)) < MIN_BIT_ENTROPY:
        return INCONCLUSIVE, {"reason": "low_entropy"}
    offset, votes = _best_offset(a, b)
    details = {"offset": offset, "votes": votes}
    if votes < MIN_ALIGNMENT_VOTES:
        return INCONCLUSIVE, {**details, "reason": "no_alignment"}
    pairs = [a[i] ^ b[i - offset] for i in range(max(0, offset), min(len(a), len(b) + offset))]
    details["overlap"] = len(pairs)
    if len(pairs) < MIN_OVERLAP_ITEMS:
        return INCONCLUSIVE, {**details, "reason": "overlap_too_short"}
    ber = sum(x.bit_count() for x in pairs) / (32 * len(pairs))
    details["bit_error_rate"] = round(ber, 4)
    if ber <= SAME_AUDIO_MAX_BIT_ERROR_RATE:
        return SAME_AUDIO, details
    if ber >= DIFFERENT_AUDIO_MIN_BIT_ERROR_RATE:
        return DIFFERENT_AUDIO, details
    return INCONCLUSIVE, {**details, "reason": "bit_error_rate_between_thresholds"}
