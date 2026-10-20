import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
try:  # ARCH-001: app.py module family (works under discovery and tests.<module> runs)
    from _app_ast_cache import app_family_source  # noqa: E402
except ImportError:  # pragma: no cover
    from tests._app_ast_cache import app_family_source  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
REQ = (ROOT / "requirements.txt").read_text(encoding="utf-8")
CONFIG = (ROOT / "config.yaml.example").read_text(encoding="utf-8")
APP = app_family_source()
SETUP = (ROOT / "routes_setup.py").read_text(encoding="utf-8")
COMPOSE = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
FULL_COMPOSE = (ROOT / "docker-compose.full.yml").read_text(encoding="utf-8")


class BeetsFreshInstallPackagingTests(unittest.TestCase):
    def test_beets_version_and_runtime_dependencies_are_coherent(self):
        self.assertNotIn("beets[", REQ)
        self.assertIn("pylast==7.2.0", REQ)
        self.assertNotIn("beets==2.2.0", REQ)
        self.assertNotIn("python3-discogs-client==", REQ)
        # ADR 0006: Beets runs in-process, pinned to the version the
        # embedded engine is tested against, with chroma's pyacoustid.
        from backend.beets_embedded import PINNED_BEETS_VERSION
        self.assertRegex(REQ, rf"(?m)^beets=={PINNED_BEETS_VERSION}$")
        self.assertRegex(REQ, r"(?m)^pyacoustid==\d+\.\d+\.\d+$")

    def test_default_plugin_list_is_importable_model(self):
        first_line = CONFIG.splitlines()[0]
        self.assertIn("plugins:", first_line)
        for plugin in ("musicbrainz", "lastgenre", "listenbrainz", "discpath", "replaygain", "chroma"):
            self.assertIn(plugin, first_line)
        self.assertNotIn("discogs", first_line)
        self.assertNotIn("plexsync", first_line)
        self.assertIn("Add \"discogs\" to plugins only after setting discogs.user_token.", CONFIG)

    def test_pluginpath_uses_user_then_bundled_directory(self):
        self.assertIn("/config/beetsplug", CONFIG)

    def test_replaygain_uses_installed_ffmpeg_backend(self):
        self.assertIn("replaygain:\n    auto: no\n    backend: ffmpeg", CONFIG.replace("\r\n", "\n"))
        self.assertNotIn("command: mp3gain", CONFIG)

    def test_compose_ai_and_metadata_env_are_optional(self):
        combined = COMPOSE + "\n" + FULL_COMPOSE
        for var in (
            "OPENAI_API_KEY", "OPENROUTER_API_KEY", "AI_API_KEY", "AI_BASE_URL", "AI_MODEL",
            "ACOUSTID_API_KEY", "ACOUSTID_USER_KEY", "ACOUSTID_KEY", "DISCOGS_TOKEN", "DISCOGS_USER_TOKEN", "LISTENBRAINZ_TOKEN",
        ):
            self.assertIn(var, combined)
        self.assertNotIn("OPENAI_API_KEY:?", combined)
        self.assertNotIn("AI_API_KEY:?", combined)

    def test_compose_beets_service_is_stock_image_with_no_custom_build(self):
        # Dockerfile.beets (the legacy custom Beets engine image) was
        # deleted outright as part of the stock-Beets migration -- the sole
        # authoritative Beets runtime is the published
        # lscr.io/linuxserver/beets image, started with no `build:`
        # directive anywhere. There is no "existing vs. fresh library"
        # startup-behavior override to forward, because Web Manager never
        # builds or owns a Beets image in the first place.
        self.assertFalse((ROOT / "Dockerfile.beets").exists())
        for compose_text in (COMPOSE, FULL_COMPOSE):
            beets_start = compose_text.index("\n  beets:")
            beets_end = compose_text.index("\n\n", beets_start)
            beets_block = compose_text[beets_start:beets_end]
            self.assertIn("lscr.io/linuxserver/beets:", beets_block)
            self.assertNotIn("build:", beets_block)
            self.assertNotIn("Dockerfile.beets", beets_block)
            self.assertNotIn("BEETS_EXPECT_EXISTING_LIBRARY", beets_block)
    def test_compose_host_port_does_not_change_container_listener(self):
        combined = "\n".join(
            (ROOT / name).read_text(encoding="utf-8")
            for name in ("docker-compose.yml", "docker-compose.full.yml", "docker-compose.dev.yml")
        )
        self.assertIn("${WEBCONTROL_PORT:-8337}:8337", combined)
        self.assertNotIn("WEBCONTROL_PORT: ${WEBCONTROL_PORT:-8337}", combined)
        self.assertGreaterEqual(combined.count("WEBCONTROL_PORT: 8337"), 3)

    def test_beetsplug_has_no_package_initializer(self):
        # A beetsplug/__init__.py makes /app/beetsplug a regular package,
        # which shadows (rather than merges with) the real beetsplug
        # namespace package installed in site-packages -- the exact cause of
        # "ModuleNotFoundError: No module named 'beetsplug.fetchart'" (and
        # every other bundled Beets plugin) once /app is on sys.path.
        # beetsplug must stay an implicit PEP 420 namespace package so
        # Python merges the bundled and installed directories instead of
        # one exclusively winning.
        self.assertFalse((ROOT / "beetsplug" / "__init__.py").exists())
        self.assertTrue((ROOT / "beetsplug" / "discpath.py").exists())

    def test_setup_status_reports_plugin_diagnostics(self):
        self.assertIn("def _beets_plugin_diagnostics", SETUP)
        self.assertIn('"beets": diagnostics', SETUP)
        for key in ("musicbrainz", "acoustid", "discogs", "lastgenre", "listenbrainz", "discpath", "fetchart", "replaygain", "plex", "lidarr", "slskd"):
            self.assertIn(f'"{key}"', SETUP)
        self.assertIn("dependency_plugin_missing", SETUP)
        self.assertIn("installed_but_disabled", SETUP)


class BeetsFreshInstallStockDockerSmokeTests(unittest.TestCase):
    """#355: a fresh install's config (setup.sh copies config.yaml.example to
    beets/config.yaml) loads every listed plugin in the stock
    lscr.io/linuxserver/beets image, with the bundled plugins provisioned
    into /config/beetsplug, and a network-free import + fetchart run works.
    Runs wherever Docker is available (CI's unit-tests and security jobs
    pre-pull the image); stock-beets-acceptance covers the HTTP protocol."""

    SMOKE_OVERLAY = """\
paths:
  default: Smoke/$title
  singleton: Smoke/$title
  comp: Smoke/$title
import:
  write: no
  copy: no
  move: no
fetchart:
  auto: no
  sources: filesystem
chroma:
  auto: no
embedart:
  auto: no
lastgenre:
  auto: no
scrub:
  auto: no
replaygain:
  auto: no
"""

    SHELL = """
set -eu
beet -vv version 2>&1
fpcalc -version
mkdir -p /tmp/import-smoke
ffmpeg -hide_banner -loglevel error -f lavfi -i sine=frequency=440:duration=1 -ac 2 -ar 44100 /tmp/import-smoke/smoke.wav
ffmpeg -hide_banner -loglevel error -f lavfi -i color=c=blue:s=16x16 -frames:v 1 /tmp/import-smoke/cover.jpg
beet -c /config/smoke.yaml import -q -A /tmp/import-smoke 2>&1
beet -c /config/smoke.yaml fetchart 2>&1
beet -c /config/smoke.yaml ls -a -f 'ART=$artpath' 2>&1
"""

    @unittest.skipIf(not shutil.which("docker"), "Docker CLI not available on host")
    def test_stock_image_loads_default_config_plugins(self):
        try:  # same dual import as _app_ast_cache above
            from test_stock_beets_acceptance import STOCK_BEETS_IMAGE, _docker_with_retry
        except ImportError:  # pragma: no cover
            from tests.test_stock_beets_acceptance import STOCK_BEETS_IMAGE, _docker_with_retry
        if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
            self.skipTest("Docker daemon not running or responsive")
        if subprocess.run(["docker", "image", "inspect", STOCK_BEETS_IMAGE], capture_output=True).returncode != 0:
            err = _docker_with_retry(["docker", "pull", STOCK_BEETS_IMAGE])
            if err is not None:
                self.fail(f"docker pull {STOCK_BEETS_IMAGE} failed:\n{err}")
        with tempfile.TemporaryDirectory() as config_dir, tempfile.TemporaryDirectory() as music_dir:
            plugdir = Path(config_dir, "beetsplug")
            shutil.copytree(ROOT / "beetsplug" / "webmanager", plugdir / "webmanager")
            shutil.copy2(ROOT / "beetsplug" / "discpath.py", plugdir / "discpath.py")
            Path(config_dir, "config.yaml").write_text(CONFIG, encoding="utf-8")
            Path(config_dir, "smoke.yaml").write_text(self.SMOKE_OVERLAY, encoding="utf-8")
            Path(config_dir, ".webmanager_api_key").write_text("0" * 64 + "\n", encoding="utf-8")
            # Run as the invoking user so the temp dirs stay removable.
            user = ["--user", f"{os.getuid()}:{os.getgid()}"] if hasattr(os, "getuid") else []
            proc = subprocess.run([
                "docker", "run", "--rm", *user,
                "-e", "BEETSDIR=/config", "-e", "HOME=/tmp",
                "-v", f"{config_dir}:/config",
                "-v", f"{music_dir}:/music",
                "--entrypoint", "sh",
                STOCK_BEETS_IMAGE, "-c", self.SHELL,
            ], capture_output=True, text=True, timeout=300)
        output = (proc.stdout or "") + "\n" + (proc.stderr or "")
        self.assertEqual(proc.returncode, 0, output)
        for bad in ("error loading plugin", "modulenotfounderror", "no module named", "traceback"):
            self.assertNotIn(bad, output.lower(), output)
        plugins_line = next((ln for ln in output.splitlines() if ln.startswith("plugins:")), "plugins:")
        loaded = {p.strip() for p in plugins_line.split(":", 1)[1].split(",")}
        self.assertLessEqual(set(CONFIG.splitlines()[0].split(":", 1)[1].split()), loaded, output)
        # fetchart ran and stored the cover found beside the track (art_filename: albumart).
        self.assertIn("ART=/tmp/import-smoke/albumart.jpg", output)


if __name__ == "__main__":
    unittest.main()
