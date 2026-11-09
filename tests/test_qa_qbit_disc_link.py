"""QA: a Disc subfolder of a queued qBittorrent unit swapped for a link after
discovery (same files, same inodes) is refused at processing time."""
import os
from tests.test_import_qbittorrent_source import SourceRecheckTests, _dir_link, setUpModule  # noqa: F401


class DiscLinkRecheckTests(SourceRecheckTests):
    def test_disc_subfolder_swapped_for_a_link_is_refused(self):
        self.torrent("Album", ["CD1/01.flac", "CD2/01.flac"])

        def swap():
            os.rename(self.dl / "Album" / "CD1", self.outside / "CD1")  # same inodes, outside the roots
            _dir_link(self.outside / "CD1", self.dl / "Album" / "CD1")

        with self.after_discovery(swap):
            out, folders = self.run_batch()
        self.assert_refused(out, folders, "source_unsafe")
        self.reimport.assert_not_called()


del SourceRecheckTests
