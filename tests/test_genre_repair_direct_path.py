"""Direct-path coverage for the album genre repair rewire (modernization W2).

The wave-27 shim that formatted a fake beet-CLI return code is gone:
``backend.library_service.start_library_fix_genres`` and
``routes_library.album_fix_genre`` call the controlled engine op
``composite_workflows.repair_album_genre`` themselves. The bulk job body is
run here against fakes, so the per-album outcomes that operators depend on
(tagged / warned-and-continued / failed) are asserted as behavior rather than
as source shape. The single-album route's share of the rewire is pinned by
source inspection because its body is a closure handed to the job engine.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import backend.composite_workflows as composite_workflows
import backend.library_service as library_service

ROOT = Path(__file__).resolve().parents[1]


class _Album:
    def __init__(self, aid, artist="Artist", album="Album", genre=""):
        self.id = aid
        self.albumartist = artist
        self.album = album
        self.genre = genre


class _InlineJobs:
    """Runs the captured job body immediately, like the real engine would."""

    job_id = "inline-genre"

    def __init__(self):
        self.fn = None
        self.start_calls = []

    def start_python(self, fn, label="", metadata=None):
        self.fn = fn
        self.start_calls.append({"label": label, "metadata": metadata})
        return self


class FixGenresJobBodyTests(unittest.TestCase):
    def _run_body(self, albums, repair, genres_after, *, force=False, use_ai=False):
        jobs = _InlineJobs()
        log = []
        fake_lib = SimpleNamespace(albums=lambda *_: list(albums))
        repaired = []

        def repair_and_record(aid, **kw):
            outcome = repair(aid, **kw)
            repaired.append(int(aid))
            return outcome

        def genre_after(aid):
            # A genre only exists once the engine op has actually run for that
            # album, exactly like the live library.
            if int(aid) in repaired:
                return genres_after.get(int(aid), "")
            return ""

        with mock.patch.object(library_service, "lib", fake_lib), \
                mock.patch.object(library_service, "jobs", jobs), \
                mock.patch.object(library_service, "_invalidate_lib_cache"), \
                mock.patch.object(
                    library_service, "_album_genre_value_by_id", side_effect=genre_after
                ), \
                mock.patch.object(
                    composite_workflows, "repair_album_genre", side_effect=repair_and_record
                ) as repair_mock:
            body, status = library_service.start_library_fix_genres(
                {"force": force, "use_ai": use_ai}
            )
            jobs.fn(log)
        return body, status, log, repair_mock, jobs

    def test_job_calls_repair_album_genre_directly_and_counts_tagged(self):
        albums = [_Album(1), _Album(2, genre="Rock")]
        body, status, log, repair, jobs = self._run_body(
            albums, lambda aid, **kw: {"ok": True}, {1: "Jazz"}
        )

        self.assertEqual(status, 200)
        self.assertEqual(body, {"ok": True, "job_id": "inline-genre"})
        self.assertEqual(jobs.start_calls[0]["metadata"], {"type": "fix-genres", "force": False, "use_ai": False})
        # Without force only the album with no genre is a target.
        repair.assert_called_once_with(1, force=False, timeout=180.0)
        self.assertTrue(any("Genre: Jazz" in line for line in log), log)
        self.assertTrue(any("Last.fm tagged 1 album(s)" in line for line in log), log)

    def test_force_retags_every_album_through_the_same_direct_call(self):
        albums = [_Album(1, genre="Rock"), _Album(2, genre="Pop")]
        _, _, _, repair, _ = self._run_body(
            albums, lambda aid, **kw: {"ok": True}, {1: "Jazz", 2: "Pop"}, force=True
        )

        self.assertEqual(
            [call.args[0] for call in repair.call_args_list],
            [1, 2],
        )
        self.assertTrue(all(call.kwargs["force"] is True for call in repair.call_args_list))

    def test_result_error_warns_and_continues_to_the_next_album(self):
        albums = [_Album(1), _Album(2)]
        calls = []

        def repair(aid, **kw):
            calls.append(aid)
            if aid == 1:
                return {"ok": False, "error": "lastgenre exploded"}
            return {"ok": True}

        _, status, log, _, _ = self._run_body(albums, repair, {2: "Soul"})

        self.assertEqual(status, 200)
        self.assertEqual(calls, [1, 2])
        self.assertTrue(
            any("WARN lastgenre failed for album_id:1: lastgenre exploded" in line for line in log),
            log,
        )
        self.assertTrue(any("Genre: Soul" in line for line in log), log)
        self.assertTrue(any("1 failed" in line for line in log), log)

    def test_raised_exception_warns_and_continues_to_the_next_album(self):
        albums = [_Album(1), _Album(2)]
        calls = []

        def repair(aid, **kw):
            calls.append(aid)
            if aid == 1:
                raise RuntimeError("engine down")
            return {"ok": True}

        _, _, log, _, _ = self._run_body(albums, repair, {2: "Soul"})

        self.assertEqual(calls, [1, 2])
        self.assertTrue(
            any("WARN lastgenre failed for album_id:1: engine down" in line for line in log),
            log,
        )
        self.assertTrue(any("Genre: Soul" in line for line in log), log)


class AlbumFixGenreRouteWiringTests(unittest.TestCase):
    def test_single_album_route_calls_the_controlled_engine_op(self):
        source = (ROOT / "routes_library.py").read_text(encoding="utf-8")
        start = source.index('@app.post("/api/albums/<int:aid>/fix-genre")')
        body = source[start:source.index("@app.", start + 1)]

        self.assertIn(
            "composite_workflows.repair_album_genre(int(aid), force=False, timeout=180.0)",
            body,
        )
        self.assertIn('raise RuntimeError(f"lastgenre failed: {ex}") from ex', body)
        # No fake beet-CLI return-code facade may come back.
        self.assertNotIn("SimpleNamespace", body)
        self.assertNotIn("_require_beet_ok", body)


if __name__ == "__main__":
    unittest.main()
