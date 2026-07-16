import unittest
from pathlib import Path


class AcquisitionActionFlagTests(unittest.TestCase):
    def test_acquire_recommends_download_or_review_only(self):
        app_path = Path(__file__).resolve().parents[1] / "app.py"
        source = app_path.read_text(encoding="utf-8")

        self.assertIn("def _acq_can_import_disk", source)
        self.assertIn("def _acq_can_download", source)
        self.assertIn('"can_download": can_download', source)
        self.assertIn('"can_import_disk": can_import_disk', source)
        self.assertNotIn('recommended = "import_disk"', source)
        self.assertIn('if can_download:', source)
        self.assertIn('recommended = "slskd"', source)
        self.assertIn('elif local and not_imported > 0 and missing <= 0:', source)
        self.assertIn('recommended = "review"', source)
        self.assertIn('if (row.get("actions") or {}).get("can_download")', source)

    def test_acquire_download_all_ignores_import_disk_rows(self):
        root = Path(__file__).resolve().parents[1]
        app_source = (root / "app.py").read_text(encoding="utf-8")
        ui_source = (
            root / "frontend" / "src" / "features" / "acquisition" / "AcquisitionPanel.tsx"
        ).read_text(encoding="utf-8")

        self.assertIn("Acquire Download All starting", app_source)
        self.assertIn("No downloadable Acquire rows matched the request", app_source)
        self.assertIn('if not can_download:', app_source)
        self.assertIn('"import_disk_count": 0', app_source)
        self.assertNotIn('"  action: Import Disk"', app_source)
        self.assertNotIn('prefix = f"import {idx}/{len(selected)}"', app_source)

        self.assertIn("Music downloads", ui_source)
        self.assertIn("missing music", ui_source)
        self.assertIn("wanted music", ui_source)
        self.assertIn("Download next", ui_source)
        self.assertIn("No downloadable rows", ui_source)
        self.assertIn("Missing first", ui_source)
        self.assertIn("Previous batch needs attention", ui_source)
        self.assertIn("Previous batch completed", ui_source)
        self.assertIn("Batch running", ui_source)
        self.assertIn("View in Jobs", ui_source)
        self.assertIn("Last update:", ui_source)
        self.assertIn("Job ID:", ui_source)
        self.assertNotIn("Import Disk", ui_source)
        self.assertNotIn("reimportDisk", ui_source)
        self.assertNotIn("Open running job", ui_source)
        self.assertNotIn("Download batch running", ui_source)
        self.assertNotIn('font-medium text-slate-300">Last batch</span>', ui_source)

    def test_source_fallback_applies_to_wanted_album_downloads(self):
        root = Path(__file__).resolve().parents[1]
        app_source = (root / "app.py").read_text(encoding="utf-8")
        ui_source = (
            root / "frontend" / "src" / "features" / "acquisition" / "AcquisitionPanel.tsx"
        ).read_text(encoding="utf-8")

        self.assertIn('if method == "slskd" and try_source_fallback:', app_source)
        self.assertNotIn("is_local_repair and try_source_fallback", app_source)
        self.assertIn("fallback_scope = (", app_source)
        self.assertIn('"the full album"', app_source)
        self.assertIn("SLSKD download failed; direct-source fallback also failed", app_source)
        self.assertNotIn("SLSKD missing-track download failed; direct-source fallback", app_source)

        self.assertIn("const sourceFallback = method === 'slskd' && ytdlpEnabled;", ui_source)
        self.assertNotIn("method === 'slskd' && isLocalRepair && ytdlpEnabled", ui_source)
        self.assertIn("function slskdActionLabel", ui_source)
        self.assertIn("return ytdlpEnabled ? 'SLSKD + sources' : 'SLSKD'", ui_source)
        self.assertIn("Fallback: SpotiFLAC -> YouTube -> SoundCloud", ui_source)


if __name__ == "__main__":
    unittest.main()

