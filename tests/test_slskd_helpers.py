import tempfile
import unittest
from pathlib import Path

from backend.slskd import (
    build_album_candidates,
    cleanup_failed_candidate_files,
    file_remote_name,
    slskd_download_candidate_roots,
    stage_selected_audio_files,
)


AUDIO_EXTS = {".flac", ".mp3", ".m4a", ".ogg", ".opus", ".wav"}


class SlskdHelperTests(unittest.TestCase):
    def test_file_remote_name_preserves_response_directory_separator(self):
        response = {"directory": r"MP3-ARCHIVE\A-D\Album"}
        file_info = {"filename": "01 Intro.mp3", "size": 123}

        self.assertEqual(
            file_remote_name(file_info, response),
            r"MP3-ARCHIVE\A-D\Album\01 Intro.mp3",
        )

    def test_build_album_candidates_prefers_complete_album_over_partial_flac(self):
        responses = [
            {
                "username": "partial",
                "directory": "Music/Artist/Album",
                "hasFreeUploadSlot": True,
                "files": [
                    {"filename": "01 Song.flac"},
                    {"filename": "02 Song.flac"},
                ],
            },
            {
                "username": "complete",
                "directory": "MP3/Artist/Album (2011)",
                "hasFreeUploadSlot": False,
                "files": [
                    {"filename": f"{idx:02d} Track.mp3"}
                    for idx in range(1, 6)
                ],
            },
        ]

        candidates, skipped = build_album_candidates(
            responses, "Artist", "Album", "2011", 5, AUDIO_EXTS
        )

        self.assertEqual(skipped, 0)
        self.assertEqual(candidates[0]["username"], "complete")
        self.assertEqual(len(candidates[0]["files"]), 5)

    def test_build_album_candidates_skips_failed_peer_folder(self):
        responses = [
            {
                "username": "badpeer",
                "directory": "Music/Artist/Album",
                "files": [{"filename": f"{idx:02d} Track.mp3"} for idx in range(1, 4)],
            },
            {
                "username": "goodpeer",
                "directory": "Music/Artist/Album",
                "files": [{"filename": f"{idx:02d} Track.mp3"} for idx in range(1, 4)],
            },
        ]

        candidates, skipped = build_album_candidates(
            responses,
            "Artist",
            "Album",
            "",
            3,
            AUDIO_EXTS,
            skip_candidates={("badpeer", "music/artist/album")},
        )

        self.assertEqual(skipped, 1)
        self.assertEqual(candidates[0]["username"], "goodpeer")

    def test_candidate_roots_preserve_remote_directory_structure(self):
        with tempfile.TemporaryDirectory() as tmp:
            downloads = Path(tmp) / "downloads"
            remote = (
                r"MP3-ARCHIVE\A-D\2_Chainz-T.R.U._REALigion_"
                r"(Hosted_By_DJ_Drama)-MIXTAPE-2011-WEB\01-2_chainz-intro.mp3"
            )

            roots = slskd_download_candidate_roots(downloads, "seymourkitty", [remote])

            self.assertEqual(
                roots[0],
                downloads
                / "seymourkitty"
                / "MP3-ARCHIVE"
                / "A-D"
                / "2_Chainz-T.R.U._REALigion_(Hosted_By_DJ_Drama)-MIXTAPE-2011-WEB",
            )
            self.assertIn(
                downloads / "2_Chainz-T.R.U._REALigion_(Hosted_By_DJ_Drama)-MIXTAPE-2011-WEB",
                roots,
            )

    def test_stage_selected_audio_files_copies_only_selected_audio(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            downloads = root / "downloads"
            source = root / "source"
            source.mkdir()
            selected_one = source / "01 Intro.mp3"
            selected_two = source / "02 Got One.mp3"
            extra_audio = source / "03 Manual Leftover.mp3"
            cover = source / "cover.jpg"
            for path in (selected_one, selected_two, extra_audio, cover):
                path.write_text(path.name, encoding="utf-8")
            log = []

            staged = Path(stage_selected_audio_files(
                downloads,
                AUDIO_EXTS,
                str(source),
                [selected_one, selected_two],
                "2 Chainz",
                "T.R.U. REALigion",
                log,
                stage_prefix="teststage",
            ))

            self.assertNotEqual(staged, source)
            self.assertTrue((staged / "01 Intro.mp3").exists())
            self.assertTrue((staged / "02 Got One.mp3").exists())
            self.assertFalse((staged / "03 Manual Leftover.mp3").exists())
            self.assertFalse((staged / "cover.jpg").exists())
            self.assertIn("Staged 2 selected audio file", "\n".join(log))

    def test_stage_selected_audio_files_keeps_source_when_all_audio_selected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            downloads = root / "downloads"
            source = root / "source"
            source.mkdir()
            track_one = source / "01 Intro.mp3"
            track_two = source / "02 Got One.mp3"
            cover = source / "cover.jpg"
            for path in (track_one, track_two, cover):
                path.write_text(path.name, encoding="utf-8")

            staged = stage_selected_audio_files(
                downloads,
                AUDIO_EXTS,
                str(source),
                [track_one, track_two],
                "2 Chainz",
                "T.R.U. REALigion",
                [],
                stage_prefix="unused",
            )

            self.assertEqual(staged, str(source))
            self.assertFalse((downloads / "_beets_missing_import").exists())

    def test_stage_selected_audio_files_preserves_disc_subfolders(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            downloads = root / "downloads"
            source = root / "source"
            disc_one = source / "CD 01"
            disc_two = source / "CD 02"
            disc_one.mkdir(parents=True)
            disc_two.mkdir(parents=True)
            selected = disc_two / "02 Skydive.flac"
            extra = disc_one / "01 Plentiful.flac"
            selected.write_text("selected", encoding="utf-8")
            extra.write_text("extra", encoding="utf-8")

            staged = Path(stage_selected_audio_files(
                downloads,
                AUDIO_EXTS,
                str(source),
                [selected],
                "Alicia Keys",
                "KEYS",
                [],
                stage_prefix="discstage",
            ))

            self.assertTrue((staged / "CD 02" / "02 Skydive.flac").exists())
            self.assertFalse((staged / "02 Skydive.flac").exists())
            self.assertFalse((staged / "CD 01" / "01 Plentiful.flac").exists())

    def test_stage_selected_audio_files_uses_target_track_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            downloads = root / "downloads"
            source = root / "source"
            wrong_disc = source / "CD 01"
            wrong_disc.mkdir(parents=True)
            selected = wrong_disc / "02 Skydive.flac"
            selected.write_text("selected", encoding="utf-8")

            staged = Path(stage_selected_audio_files(
                downloads,
                AUDIO_EXTS,
                str(source),
                [selected],
                "Alicia Keys",
                "KEYS",
                [],
                stage_prefix="targetstage",
                target_tracks=[{"disc": 2, "track": 2, "title": "Skydive"}],
            ))

            self.assertTrue((staged / "CD 02" / "02 Skydive.flac").exists())
            self.assertFalse((staged / "CD 01" / "02 Skydive.flac").exists())

    def test_failed_candidate_cleanup_removes_only_queued_audio(self):
        with tempfile.TemporaryDirectory() as tmp:
            downloads = Path(tmp) / "downloads"
            album_dir = (
                downloads
                / "seymourkitty"
                / "MP3-ARCHIVE"
                / "A-D"
                / "Album"
            )
            album_dir.mkdir(parents=True)
            queued_one = album_dir / "01 Intro.mp3"
            queued_two = album_dir / "02 Got One.flac"
            unqueued = album_dir / "03 Keep Me.mp3"
            cover = album_dir / "cover.jpg"
            for path in (queued_one, queued_two, unqueued, cover):
                path.write_text(path.name, encoding="utf-8")
            remote_files = [
                r"MP3-ARCHIVE\A-D\Album\01 Intro.mp3",
                r"MP3-ARCHIVE\A-D\Album\02 Got One.flac",
            ]
            log = []

            removed = cleanup_failed_candidate_files(
                downloads,
                "seymourkitty",
                remote_files,
                AUDIO_EXTS,
                log,
            )

            self.assertEqual(removed, 2)
            self.assertFalse(queued_one.exists())
            self.assertFalse(queued_two.exists())
            self.assertTrue(unqueued.exists())
            self.assertTrue(cover.exists())
            self.assertIn("Removed 2 partial file", "\n".join(log))


if __name__ == "__main__":
    unittest.main()
