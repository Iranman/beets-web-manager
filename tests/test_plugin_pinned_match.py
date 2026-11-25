"""webmanager plugin 1.18.3: the Release pinned by one search id is judged
without Beets' ``data_source`` penalty (and, for a missing-track repair,
without ``missing_tracks``).

The distances are real Beets ``Distance`` objects built from the components
measured on the stock 2.14.1 container (musicbrainz + chroma + deezer) for the
staged "Beyond Belief" folder against its pinned Release: album raw 6.565 of
max 26.8 = tracks 2.0 (all data_source) + missing_tracks 1.8 + artist 1.765 +
data_source 1.0.
"""
import sys
import types
import unittest
from unittest import mock

from beets.autotag.distance import Distance
import beetsplug.webmanager.operations as ops

REL = "48f3a653-6b80-47de-8de9-d921e06dbd33"
THRESH = 0.04  # Beets' default match.strong_rec_thresh


def _track(title=0.0, length=0.0, data_source=0.5):
    """One mapped track as measured: only data_source 0.5 (= 0.125 of max 8)."""
    t = Distance()
    t.add("track_title", title)
    t.add("track_index", 0.0)
    t.add("track_length", length)
    if data_source is not None:
        t.add("data_source", data_source)
    return t


def _album(tracks, missing=2, artist=1.765 / 3.0, album=0.0, data_source=0.5):
    d = Distance()
    d.add("artist", artist)
    d.add("album", album)
    d.add("year", 0.0)
    d.tracks = {}
    for i, t in enumerate(tracks):
        d.tracks[i] = t
        d.add("tracks", t.distance)
    for _ in range(missing):
        d.add("missing_tracks", 1.0)
    if data_source is not None:
        d.add("data_source", data_source)
    return d


def _task(dist, missing=2, extra_items=(), album_id=REL):
    match = types.SimpleNamespace(
        info=types.SimpleNamespace(album_id=album_id), distance=dist,
        extra_tracks=[object()] * missing, extra_items=list(extra_items))
    return types.SimpleNamespace(candidates=[match]), match


def _old_rule(dist, missing):
    """Plugin 1.18.2: only the missing-track penalty set aside."""
    w = missing * 0.9
    return (dist.raw_distance - w) / (dist.max_distance - w)


class PinnedMatchDistanceTests(unittest.TestCase):
    def test_measured_beyond_belief_components(self):
        dist = _album([_track() for _ in range(8)])
        self.assertAlmostEqual(dist.raw_distance, 6.565, places=3)
        self.assertAlmostEqual(dist.max_distance, 26.8, places=3)
        raw, mx = ops._distance_without(dist, ops._PINNED_ALBUM_EXEMPT)
        # data_source gone from the album and from every track; artist stays.
        self.assertAlmostEqual(raw, 1.765, places=3)
        self.assertAlmostEqual(mx, 23.0, places=3)
        self.assertGreaterEqual(raw / mx, THRESH)
        task, _ = _task(dist)
        self.assertIsNone(ops._pinned_partial_match(task, REL))

    def test_partial_source_with_matching_artist_now_passes(self):
        dist = _album([_track() for _ in range(8)], artist=0.0)
        self.assertGreaterEqual(_old_rule(dist, 2), THRESH)  # 3.0 / 25.0: skipped before
        task, match = _task(dist)
        self.assertIs(ops._pinned_partial_match(task, REL), match)

    def test_complete_fully_tagged_source_now_passes(self):
        """Measured on stock 2.14.1: a complete, fully tagged copy of the
        Release scores 0.0656 (tracks 1.429 + data_source 1.0 of 37.0), over
        the strong threshold, so a full pinned import was skipped too."""
        dist = _album([_track() for _ in range(10)], missing=0, artist=0.0)
        self.assertGreaterEqual(float(dist), THRESH)
        task, match = _task(dist, missing=0)
        self.assertIs(ops._pinned_partial_match(task, REL, allow_missing=False), match)

    def test_no_data_source_penalty_is_unchanged(self):
        dist = _album([_track(data_source=None) for _ in range(8)], artist=0.0, data_source=None)
        task, match = _task(dist)
        self.assertIs(ops._pinned_partial_match(task, REL), match)

    def test_everything_else_keeps_the_bar(self):
        tracks = [_track() for _ in range(7)]
        for name, dist, kw, allow in (
            ("wrong title", _album(tracks + [_track(title=1.0)], artist=0.0), {}, True),
            ("wrong lengths", _album([_track(length=1.0) for _ in range(8)], artist=0.0), {}, True),
            ("wrong album", _album(tracks + [_track()], artist=0.0, album=0.5), {}, True),
            ("extra items", _album(tracks + [_track()], artist=0.0), {"extra_items": [object()]}, True),
            ("missing without allow", _album(tracks + [_track()], artist=0.0), {}, False),
            ("not the pinned release", _album(tracks + [_track()], artist=0.0),
             {"album_id": "b6a6e718-3d8e-4c3e-9eaa-7b54af639bb9"}, True),
        ):
            with self.subTest(name):
                task, _ = _task(dist, **kw)
                self.assertIsNone(ops._pinned_partial_match(task, REL, allow_missing=allow))

    def test_max_rec_data_source_cap_is_honoured(self):
        """N1: data_source is left out of the distance, not out of an
        operator's explicit match.max_rec cap."""
        from beets import config as beets_config
        task, match = _task(_album([_track() for _ in range(8)], artist=0.0))
        self.assertIs(ops._pinned_partial_match(task, REL), match)
        beets_config["match"]["max_rec"]["data_source"] = "medium"
        try:
            self.assertIsNone(ops._pinned_partial_match(task, REL))
        finally:
            beets_config["match"]["max_rec"]["data_source"] = "strong"  # confuse cannot delete; strong = no cap

    def test_no_pinned_release(self):
        task, _ = _task(_album([_track()], artist=0.0))
        self.assertIsNone(ops._pinned_partial_match(task, ""))


class _Item:
    """A source file: hashable like a Beets Item, keyed by path in chroma."""

    def __init__(self, n):
        self.path = b"/downloads/Beyond Belief/%02d.mp3" % n


TRACK_IDS = ["00000000-0000-4000-8000-0000000000%02d" % n for n in range(1, 11)]
SOURCE = [1, 2, 3, 4, 5, 7, 9, 10]  # the staged folder's eight tracks


class _Lib:
    def __init__(self, albums):
        self.albums = albums

    def get_album(self, album_id):
        return self.albums.get(album_id)


class PinnedArtistRelaxationTests(unittest.TestCase):
    """webmanager plugin 1.18.4 (part b): a missing-track repair may set the
    album-level artist penalty aside when the source artist is part of the
    Release's credit and AcoustID matched every file to its mapped track."""

    def _case(self, cur_artist="38 Spesh", artists=("38 Spesh", "Harry Fraud"), artist="38 Spesh, Harry Fraud",
              va=False, hits=None, album=0.0, length=0.0, extra_items=(), existing_album_id=5,
              lib_release=REL, chroma=True):
        items = [_Item(n) for n in SOURCE]
        dist = _album([_track(length=length) for _ in SOURCE], album=album)
        info = types.SimpleNamespace(album_id=REL, va=va, artists=list(artists), artists_credit=[], artist=artist)
        match = types.SimpleNamespace(
            info=info, distance=dist, extra_tracks=[object(), object()], extra_items=list(extra_items),
            mapping={it: types.SimpleNamespace(track_id=TRACK_IDS[n - 1]) for it, n in zip(items, SOURCE)})
        task = types.SimpleNamespace(candidates=[match], items=items, cur_artist=cur_artist,
                                     paths=[b"/downloads/Beyond Belief"])
        if hits is None:
            hits = {it.path: ([TRACK_IDS[n - 1]], [REL]) for it, n in zip(items, SOURCE)}
        lib = _Lib({5: {"mb_albumid": lib_release}})
        modules = {"beetsplug.chroma": types.SimpleNamespace(_matches=hits)} if chroma else {}
        with mock.patch.dict(sys.modules, modules):
            if not chroma:
                sys.modules.pop("beetsplug.chroma", None)
            got = ops._pinned_partial_match(task, REL, True, lib, existing_album_id)
        return got, match, task, items

    def assertAccepted(self, **kw):
        got, match, task, _ = self._case(**kw)
        self.assertIs(got, match, kw)
        self.assertTrue(getattr(task, "webmanager_partial_artist_relaxed", False), kw)

    def assertRejected(self, **kw):
        got, _, task, _ = self._case(**kw)
        self.assertIsNone(got, kw)
        self.assertFalse(getattr(task, "webmanager_partial_artist_relaxed", False), kw)

    def test_1_beyond_belief_shape_is_accepted_and_flagged(self):
        dist = _album([_track() for _ in SOURCE])
        raw, mx = ops._distance_without(dist, ops._PINNED_ALBUM_EXEMPT)
        self.assertGreaterEqual(raw / mx, THRESH)  # part (a) alone: 0.0767, still skipped
        self.assertAccepted()

    def test_2_different_recordings_are_rejected(self):
        items = [_Item(n) for n in SOURCE]
        other = {it.path: (["99999999-0000-4000-8000-0000000000%02d" % n], []) for it, n in zip(items, SOURCE)}
        self.assertRejected(hits=other)

    def test_3_one_file_without_its_recording_is_rejected(self):
        items = [_Item(n) for n in SOURCE]
        hits = {it.path: ([TRACK_IDS[n - 1]], []) for it, n in zip(items, SOURCE)}
        missing = dict(hits)
        missing.pop(items[3].path)
        self.assertRejected(hits=missing)
        wrong = dict(hits)
        wrong[items[3].path] = ([TRACK_IDS[0]], [])
        self.assertRejected(hits=wrong)

    def test_4_chroma_not_loaded_is_rejected(self):
        self.assertRejected(chroma=False)

    def test_5_artist_outside_the_credit_is_rejected(self):
        self.assertRejected(cur_artist="Stove God Cooks")

    def test_6_blank_artist_and_various_artists_are_rejected(self):
        self.assertRejected(cur_artist="")
        self.assertRejected(cur_artist="   ")
        self.assertRejected(va=True)

    def test_7_album_title_mismatch_is_rejected(self):
        self.assertRejected(album=0.5)

    def test_8_track_lengths_over_the_threshold_are_rejected(self):
        self.assertRejected(length=1.0)

    def test_9_repaired_album_must_be_on_the_pinned_release(self):
        self.assertRejected(existing_album_id=0)
        self.assertRejected(existing_album_id=6)
        self.assertRejected(lib_release="b6a6e718-3d8e-4c3e-9eaa-7b54af639bb9")

    def test_10_extra_items_are_rejected(self):
        self.assertRejected(extra_items=[object()])

    def test_11_credit_variants(self):
        for name in ("38 Spesh & Harry Fraud", "38 Spesh feat. Harry Fraud", "38 SPESH",
                     "\uff13\uff18 Spesh", "38\u00a0Spesh  x  Harry Fraud", "Harry Fraud / 38 Spesh"):
            with self.subTest(name):
                self.assertAccepted(cur_artist=name)
        # The credit taken from the joined artist string when no list is given.
        self.assertAccepted(cur_artist="38 Spesh & Harry Fraud", artists=())
        for artists in (("38 Spesh", "Harry Fraud"), ()):
            with self.subTest(artists=artists):
                self.assertRejected(cur_artist="38 Spesh, Unknown Guest", artists=artists)

    def test_complete_source_is_never_relaxed(self):
        """Only a missing-track repair may set the artist penalty aside."""
        items = [_Item(n) for n in range(1, 11)]
        dist = _album([_track() for _ in items], missing=0)
        info = types.SimpleNamespace(album_id=REL, va=False, artists=["38 Spesh", "Harry Fraud"])
        match = types.SimpleNamespace(info=info, distance=dist, extra_tracks=[], extra_items=[],
                                      mapping={it: types.SimpleNamespace(track_id=TRACK_IDS[i])
                                               for i, it in enumerate(items)})
        task = types.SimpleNamespace(candidates=[match], items=items, cur_artist="38 Spesh")
        hits = {it.path: ([TRACK_IDS[i]], []) for i, it in enumerate(items)}
        with mock.patch.dict(sys.modules, {"beetsplug.chroma": types.SimpleNamespace(_matches=hits)}):
            self.assertIsNone(ops._pinned_partial_match(task, REL, True, _Lib({5: {"mb_albumid": REL}}), 5))


MEDIUM = 0.25  # Beets' default match.medium_rec_thresh


class PinnedHumanConfirmedFingerprintPrimaryTests(unittest.TestCase):
    """webmanager plugin 1.22.0: on the explicit human Confirm/Import-with-ID
    path only, complete recording-level fingerprint identity (AcoustID matched
    every file to its mapped recording) accepts the pinned Release IRRESPECTIVE
    of Beets' text distance -- the text ceiling is overridden, not merely raised
    to MEDIUM (which #817 did). The John Williams classical shape (album title
    text score 0.807, and here track titles also divergent so ratio exceeds
    MEDIUM) imports because every track is fingerprint-confirmed."""

    def _case(self, album=0.807, track_title=0.0, length=0.0, human_confirmed=True,
              chroma=True, drop_hit=False, wrong_hit=False, extra_items=(), album_id=REL,
              missing=0, allow_missing=False, ntracks=8):
        items = [_Item(k) for k in range(1, ntracks + 1)]
        dist = _album([_track(title=track_title, length=length, data_source=None) for _ in items],
                      missing=missing, artist=0.0, album=album, data_source=None)
        info = types.SimpleNamespace(album_id=album_id, va=False, artists=[], artists_credit=[], artist="")
        match = types.SimpleNamespace(
            info=info, distance=dist, extra_tracks=[object()] * missing, extra_items=list(extra_items),
            mapping={it: types.SimpleNamespace(track_id=TRACK_IDS[k - 1]) for k, it in enumerate(items, 1)})
        task = types.SimpleNamespace(candidates=[match], items=items, cur_artist="",
                                     paths=[b"/downloads/Williams"])
        hits = {it.path: ([TRACK_IDS[k - 1]], [REL]) for k, it in enumerate(items, 1)}
        if drop_hit:
            hits.pop(items[-1].path)
        if wrong_hit:
            hits[items[-1].path] = ([TRACK_IDS[0]], [REL])  # a recording the file is not mapped to
        modules = {"beetsplug.chroma": types.SimpleNamespace(_matches=hits)} if chroma else {}
        with mock.patch.dict(sys.modules, modules):
            if not chroma:
                sys.modules.pop("beetsplug.chroma", None)
            got = ops._pinned_partial_match(task, REL, allow_missing, None, 0, human_confirmed)
        return got, match, task

    def test_john_williams_over_medium_is_accepted_by_fingerprint_identity(self):
        # DECISIVE positive: text distance is pushed OVER medium (album title +
        # track titles divergent), yet every file is fingerprint-confirmed, so
        # it imports. This FAILS on #817 (medium ceiling) -- non-vacuous.
        got, match, task = self._case(album=0.807, track_title=0.5, human_confirmed=True)
        raw, mx = ops._distance_without(match.distance, ops._PINNED_ALBUM_EXEMPT)
        self.assertGreaterEqual(raw / mx, MEDIUM)   # over medium: #817 skipped it
        self.assertIs(got, match)
        self.assertTrue(getattr(task, "webmanager_confirmed_fingerprint_primary", False))
        # Over medium, so the #817 back-compat sub-flag is NOT set.
        self.assertFalse(getattr(task, "webmanager_confirmed_relaxed", False))

    def test_a_under_medium_sets_both_primary_and_backcompat_flags(self):
        # The common sub-case (text also under medium) keeps the #817 flag too.
        got, match, task = self._case(album=0.807, human_confirmed=True)
        raw, mx = ops._distance_without(match.distance, ops._PINNED_ALBUM_EXEMPT)
        self.assertGreaterEqual(raw / mx, THRESH)   # over strong (Beets skipped it)
        self.assertLess(raw / mx, MEDIUM)           # but under medium
        self.assertIs(got, match)
        self.assertTrue(getattr(task, "webmanager_confirmed_fingerprint_primary", False))
        self.assertTrue(getattr(task, "webmanager_confirmed_relaxed", False))

    def test_b_without_fingerprint_hits_fails_closed_to_strong(self):
        # chroma not loaded, a dropped file, and a wrong recording all refuse,
        # even though the text distance (album 0.807) is under medium -- the
        # text score cannot carry it without fingerprint proof.
        for name, kw in (("no chroma", {"chroma": False}),
                         ("one file lacks a hit", {"drop_hit": True}),
                         ("one file mapped to another recording", {"wrong_hit": True})):
            with self.subTest(name):
                got, _, task = self._case(human_confirmed=True, **kw)
                self.assertIsNone(got)
                self.assertFalse(getattr(task, "webmanager_confirmed_fingerprint_primary", False))
                self.assertFalse(getattr(task, "webmanager_confirmed_relaxed", False))

    def test_c_auto_path_still_refuses_the_text_distant_match(self):
        # The automatic gate is byte-for-byte unchanged: same distance, same
        # fingerprint hits, human_confirmed False -> still skipped, no flags.
        got, _, task = self._case(human_confirmed=False)
        self.assertIsNone(got)
        self.assertFalse(getattr(task, "webmanager_confirmed_fingerprint_primary", False))
        self.assertFalse(getattr(task, "webmanager_confirmed_relaxed", False))

    def test_c2_auto_path_refuses_even_over_medium(self):
        # Same John Williams over-medium folder on the auto path still skips.
        got, _, task = self._case(album=0.807, track_title=0.5, human_confirmed=False)
        self.assertIsNone(got)
        self.assertFalse(getattr(task, "webmanager_confirmed_fingerprint_primary", False))

    def test_d_strong_match_is_unchanged_on_both_paths(self):
        # A genuinely strong pinned match (ratio under 0.04) still imports via
        # the strong gate with no relax flags, whether human_confirmed or not.
        for human in (True, False):
            with self.subTest(human_confirmed=human):
                got, match, task = self._case(album=0.3, human_confirmed=human)
                raw, mx = ops._distance_without(match.distance, ops._PINNED_ALBUM_EXEMPT)
                self.assertLess(raw / mx, THRESH)
                self.assertIs(got, match)
                self.assertFalse(getattr(task, "webmanager_confirmed_fingerprint_primary", False))
                self.assertFalse(getattr(task, "webmanager_confirmed_relaxed", False))

    def test_f_structural_guards_still_hold_under_human_confirmed(self):
        # Fingerprint identity overrides only the TEXT ceiling; every structural
        # guard (no extra items; best candidate is the pinned release) still
        # refuses even a fully fingerprint-confirmed, human-confirmed folder.
        for name, kw in (("extra items", {"extra_items": [object()]}),
                         ("not the pinned release", {"album_id": "b6a6e718-3d8e-4c3e-9eaa-7b54af639bb9"})):
            with self.subTest(name):
                got, _, task = self._case(human_confirmed=True, **kw)
                self.assertIsNone(got)
                self.assertFalse(getattr(task, "webmanager_confirmed_fingerprint_primary", False))

    def test_g_human_confirmed_subset_needs_allow_missing(self):
        # A deliberate 8-of-10 subset: missing tracks are allowed only with
        # allow_missing (step 3); only then is the folder fingerprint-accepted.
        got, match, task = self._case(missing=2, allow_missing=True, human_confirmed=True)
        self.assertIs(got, match)
        self.assertTrue(getattr(task, "webmanager_confirmed_fingerprint_primary", False))
        # Without allow_missing the missing tracks refuse it before acceptance.
        got2, _, _ = self._case(missing=2, allow_missing=False, human_confirmed=True)
        self.assertIsNone(got2)


class _PathItem:
    """A source file at an arbitrary path (for path-key / basename tests)."""

    def __init__(self, path: bytes):
        self.path = path


class PinnedConfirmedEvidenceBranchTests(unittest.TestCase):
    """webmanager plugin 1.23.0: the human-confirmed override accepts the pinned
    Release from Web Manager's OWN per-file AcoustID evidence
    (``fingerprint_confirmed``), NOT chroma's in-session ``_matches`` (which a
    pinned ``--search-id`` run does not reliably populate). Chroma is NOT loaded
    in any of these cases, proving acceptance stands on the threaded evidence
    alone -- the live path the #819 unit tests never exercised."""

    def _case(self, evidence, items=None, album=0.807, track_title=0.5,
              human_confirmed=True, album_id=REL, ntracks=8):
        items = items if items is not None else [_Item(k) for k in range(1, ntracks + 1)]
        dist = _album([_track(title=track_title, data_source=None) for _ in items],
                      missing=0, artist=0.0, album=album, data_source=None)
        info = types.SimpleNamespace(album_id=album_id, va=False, artists=[], artists_credit=[], artist="")
        match = types.SimpleNamespace(
            info=info, distance=dist, extra_tracks=[], extra_items=[],
            mapping={it: types.SimpleNamespace(track_id=TRACK_IDS[k - 1]) for k, it in enumerate(items, 1)})
        task = types.SimpleNamespace(candidates=[match], items=items, cur_artist="",
                                     paths=[b"/downloads/Williams"])
        # chroma deliberately absent: evidence must carry it on its own.
        with mock.patch.dict(sys.modules, {}):
            sys.modules.pop("beetsplug.chroma", None)
            got = ops._pinned_partial_match(task, album_id, False, None, 0,
                                            human_confirmed, evidence)
        return got, match, task

    def _evidence_one_id_per_file(self, items):
        # Exactly one confirmed recording id per file (as production supplies),
        # keyed by the item's own path -- the mapped track's recording.
        return {it.path: [TRACK_IDS[k - 1]] for k, it in enumerate(items, 1)}

    def test_correct_evidence_accepts_over_medium_without_chroma(self):
        items = [_Item(k) for k in range(1, 9)]
        raw, mx = None, None
        got, match, task = self._case(self._evidence_one_id_per_file(items), items=items)
        raw, mx = ops._distance_without(match.distance, ops._PINNED_ALBUM_EXEMPT)
        self.assertGreaterEqual(raw / mx, MEDIUM)  # over medium: text alone would skip
        self.assertIs(got, match)
        self.assertTrue(getattr(task, "webmanager_confirmed_fingerprint_primary", False))

    def test_wrong_recording_id_is_refused(self):
        items = [_Item(k) for k in range(1, 9)]
        ev = self._evidence_one_id_per_file(items)
        ev[items[3].path] = ["99999999-0000-4000-8000-000000000099"]  # not the mapped recording
        got, _, task = self._case(ev, items=items)
        self.assertIsNone(got)
        self.assertFalse(getattr(task, "webmanager_confirmed_fingerprint_primary", False))

    def test_a_file_absent_from_evidence_fails_closed(self):
        items = [_Item(k) for k in range(1, 9)]
        ev = self._evidence_one_id_per_file(items)
        ev.pop(items[2].path)  # one file has no confirmed recording
        got, _, task = self._case(ev, items=items)
        self.assertIsNone(got)
        self.assertFalse(getattr(task, "webmanager_confirmed_fingerprint_primary", False))

    def test_basename_fallback_matches_a_diverged_path(self):
        # Evidence keyed by a differently-rooted path (symlink/realpath
        # divergence) still matches a unique basename.
        items = [_PathItem(b"/music/lib/Williams/%02d.flac" % k) for k in range(1, 3)]
        ev = {b"/downloads/Williams/%02d.flac" % k: [TRACK_IDS[k - 1]] for k in range(1, 3)}
        got, match, _ = self._case(ev, items=items, ntracks=2)
        self.assertIs(got, match)

    def test_n2_duplicate_basename_across_discs_cannot_borrow(self):
        # Asymmetric multi-disc: disc1/01.flac is confirmed, disc2/01.flac is
        # NOT, and both mapped recordings are the SAME id. The basename "01.flac"
        # is NOT unique across task items, so disc2 must not borrow disc1's
        # evidence -> fail closed (this is the N2 fail-open the fix closes).
        items = [_PathItem(b"/dl/Album/disc1/01.flac"), _PathItem(b"/dl/Album/disc2/01.flac")]
        dist = _album([_track(title=0.5, data_source=None) for _ in items],
                      missing=0, artist=0.0, album=0.807, data_source=None)
        info = types.SimpleNamespace(album_id=REL, va=False, artists=[], artists_credit=[], artist="")
        # Both files mapped to the SAME recording id, so a borrow WOULD pass.
        match = types.SimpleNamespace(
            info=info, distance=dist, extra_tracks=[], extra_items=[],
            mapping={it: types.SimpleNamespace(track_id=TRACK_IDS[0]) for it in items})
        task = types.SimpleNamespace(candidates=[match], items=items, cur_artist="",
                                     paths=[b"/dl/Album"])
        evidence = {b"/dl/Album/disc1/01.flac": [TRACK_IDS[0]]}  # only disc1 confirmed
        with mock.patch.dict(sys.modules, {}):
            sys.modules.pop("beetsplug.chroma", None)
            got = ops._pinned_partial_match(task, REL, False, None, 0, True, evidence)
        self.assertIsNone(got)
        self.assertFalse(getattr(task, "webmanager_confirmed_fingerprint_primary", False))

    def test_2a_evidence_for_a_non_task_file_cannot_be_borrowed(self):
        # #821 2a: a task file whose ONLY same-basename evidence entry belongs
        # to a file OUTSIDE this task's import root (task.paths) -- an
        # existing-album library item, or another split task's file. The
        # basename fallback is now scoped to task-local evidence, so that entry
        # cannot be borrowed -> fail closed (before the fix this false-accepted).
        item = _PathItem(b"/dl/Album/01.flac")
        dist = _album([_track(title=0.5, data_source=None)],
                      missing=0, artist=0.0, album=0.807, data_source=None)
        info = types.SimpleNamespace(album_id=REL, va=False, artists=[], artists_credit=[], artist="")
        match = types.SimpleNamespace(
            info=info, distance=dist, extra_tracks=[], extra_items=[],
            mapping={item: types.SimpleNamespace(track_id=TRACK_IDS[0])})
        task = types.SimpleNamespace(candidates=[match], items=[item], cur_artist="",
                                     paths=[b"/dl/Album"])
        # Only a NON-task file is confirmed, same basename + same recording id,
        # but it lives under /library, not under the task root /dl/Album.
        evidence = {b"/library/Old/01.flac": [TRACK_IDS[0]]}
        with mock.patch.dict(sys.modules, {}):
            sys.modules.pop("beetsplug.chroma", None)
            got = ops._pinned_partial_match(task, REL, False, None, 0, True, evidence)
        self.assertIsNone(got)
        self.assertFalse(getattr(task, "webmanager_confirmed_fingerprint_primary", False))

    def test_auto_path_ignores_evidence(self):
        # human_confirmed False: even complete, correct evidence does not relax
        # the text bar (the automatic gate is unchanged).
        items = [_Item(k) for k in range(1, 9)]
        got, _, task = self._case(self._evidence_one_id_per_file(items), items=items,
                                  human_confirmed=False)
        self.assertIsNone(got)
        self.assertFalse(getattr(task, "webmanager_confirmed_fingerprint_primary", False))

    def test_evidence_takes_priority_over_a_misleading_chroma(self):
        # Evidence present but a stale/empty chroma _matches is also loaded:
        # the backend evidence is the authority, so acceptance still stands.
        items = [_Item(k) for k in range(1, 9)]
        ev = self._evidence_one_id_per_file(items)
        dist = _album([_track(title=0.5, data_source=None) for _ in items],
                      missing=0, artist=0.0, album=0.807, data_source=None)
        info = types.SimpleNamespace(album_id=REL, va=False, artists=[], artists_credit=[], artist="")
        match = types.SimpleNamespace(
            info=info, distance=dist, extra_tracks=[], extra_items=[],
            mapping={it: types.SimpleNamespace(track_id=TRACK_IDS[k - 1]) for k, it in enumerate(items, 1)})
        task = types.SimpleNamespace(candidates=[match], items=items, cur_artist="",
                                     paths=[b"/downloads/Williams"])
        with mock.patch.dict(sys.modules, {"beetsplug.chroma": types.SimpleNamespace(_matches={})}):
            got = ops._pinned_partial_match(task, REL, False, None, 0, True, ev)
        self.assertIs(got, match)
        self.assertTrue(getattr(task, "webmanager_confirmed_fingerprint_primary", False))


if __name__ == "__main__":
    unittest.main()
