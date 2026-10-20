"""webmanager plugin 1.18.3: the Release pinned by one search id is judged
without Beets' ``data_source`` penalty (and, for a missing-track repair,
without ``missing_tracks``).

The distances are real Beets ``Distance`` objects built from the components
measured on the stock 2.14.1 container (musicbrainz + chroma + deezer) for the
staged "Beyond Belief" folder against its pinned Release: album raw 6.565 of
max 26.8 = tracks 2.0 (all data_source) + missing_tracks 1.8 + artist 1.765 +
data_source 1.0.
"""
import types
import unittest

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

    def test_no_pinned_release(self):
        task, _ = _task(_album([_track()], artist=0.0))
        self.assertIsNone(ops._pinned_partial_match(task, ""))


if __name__ == "__main__":
    unittest.main()
