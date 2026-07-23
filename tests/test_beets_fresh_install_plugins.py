import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQ = (ROOT / "requirements.txt").read_text(encoding="utf-8")
CONFIG = (ROOT / "config.yaml.example").read_text(encoding="utf-8")
APP = (ROOT / "app.py").read_text(encoding="utf-8")
SETUP = (ROOT / "routes_setup.py").read_text(encoding="utf-8")
COMPOSE = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
ARR_COMPOSE = (ROOT / "docker-compose.arrs.yml").read_text(encoding="utf-8")


class BeetsFreshInstallPackagingTests(unittest.TestCase):
    def test_beets_version_and_runtime_dependencies_are_coherent(self):
        self.assertIn("beets[chroma,discogs,embedart,fetchart,lastgenre,lastimport,lyrics,scrub]==2.12.0", REQ)
        self.assertIn("pylast==7.1.0", REQ)
        self.assertNotIn("beets==2.2.0", REQ)
        self.assertNotIn("python3-discogs-client==", REQ)
        self.assertNotIn("pyacoustid==", REQ)

    def test_default_plugin_list_is_importable_model(self):
        first_line = CONFIG.splitlines()[0]
        self.assertIn("plugins:", first_line)
        for plugin in ("musicbrainz", "lastgenre", "listenbrainz", "discpath", "replaygain", "chroma"):
            self.assertIn(plugin, first_line)
        self.assertNotIn("discogs", first_line)
        self.assertNotIn("plexsync", first_line)
        self.assertIn("Add \"discogs\" to plugins only after setting discogs.user_token.", CONFIG)

    def test_pluginpath_uses_user_then_bundled_directory(self):
        self.assertIn("pluginpath:\n  - /config/beetsplug\n  - /app/beetsplug", CONFIG.replace("\r\n", "\n"))
        self.assertIn("_BEETS_PLUGINPATH_CONFIG", APP)
        self.assertIn('"  - /config/beetsplug\\n"', APP)
        self.assertIn('"  - /app/beetsplug\\n"', APP)
        self.assertNotIn('"pluginpath: /config/beetsplug\\n"', APP)

    def test_replaygain_uses_installed_ffmpeg_backend(self):
        self.assertIn("replaygain:\n    auto: no\n    backend: ffmpeg", CONFIG.replace("\r\n", "\n"))
        self.assertNotIn("command: mp3gain", CONFIG)

    def test_compose_ai_and_metadata_env_are_optional(self):
        combined = COMPOSE + "\n" + ARR_COMPOSE
        for var in (
            "OPENAI_API_KEY", "OPENROUTER_API_KEY", "AI_API_KEY", "AI_BASE_URL", "AI_MODEL",
            "ACOUSTID_API_KEY", "ACOUSTID_KEY", "DISCOGS_TOKEN", "DISCOGS_USER_TOKEN", "LISTENBRAINZ_TOKEN",
        ):
            self.assertIn(var, combined)
        self.assertNotIn("OPENAI_API_KEY:?", combined)
        self.assertNotIn("AI_API_KEY:?", combined)

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


@unittest.skipUnless(os.environ.get("RUN_DOCKER_SMOKE") == "1", "set RUN_DOCKER_SMOKE=1 to build and run the Docker fresh-install smoke test")
class BeetsFreshInstallDockerSmokeTests(unittest.TestCase):
    def test_fresh_image_loads_default_plugins(self):
        if not shutil.which("docker"):
            self.skipTest("docker executable not available")
        image = os.environ.get("DOCKER_SMOKE_IMAGE", "beets-web-manager-review-fix")
        subprocess.run(["docker", "build", "--no-cache", "-t", image, "."], cwd=ROOT, check=True, text=True)
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMP", None)) as config_dir, \
             tempfile.TemporaryDirectory(dir=os.environ.get("TMP", None)) as music_dir, \
             tempfile.TemporaryDirectory(dir=os.environ.get("TMP", None)) as downloads_dir:
            shell = """
set -eu
cp /app/config.yaml.example /config/config.yaml
cat >> /config/config.yaml <<'YAML'
paths:
  default: Smoke/$title
  singleton: Smoke/$title
  comp: Smoke/$title
import:
  write: no
  copy: yes
  move: no
  quiet_fallback: asis
fetchart:
  auto: no
  sources: filesystem
embedart:
  auto: no
lyrics:
  auto: no
lastgenre:
  auto: no
scrub:
  auto: no
YAML
test "$(id -u)" != "0"
beet -c /config/config.yaml version
beet -c /config/config.yaml config
# Direct, unambiguous proof fetchart resolves from the installed Beets
# distribution (not shadowed by the bundled/mounted beetsplug directories)
# and its runtime dependencies import -- this is the exact failure mode a
# `beetsplug/__init__.py` package initializer previously caused: it made
# /app/beetsplug a regular package, which shadows the real beetsplug
# namespace package in site-packages instead of merging with it.
python -c "
import beetsplug.fetchart, beetsplug.discpath
print('fetchart module:', beetsplug.fetchart.__file__)
print('discpath module:', beetsplug.discpath.__file__)
assert 'site-packages' in beetsplug.fetchart.__file__, 'fetchart did not resolve from the installed Beets distribution'
assert '/app/beetsplug' in beetsplug.discpath.__file__, 'bundled discpath plugin was not found'
"
python -c "import requests, PIL; print('fetchart dependencies importable')"
# Exact command /api/setup/status now runs as its plugin-loader probe
# (routes_setup.py's _BEET_LOADER_PROBE_ARGS) -- `beet plugins` does not
# exist in Beets 2.12.0, so this is the supported, real replacement. Assert
# its exit code explicitly (not just via the overall `set -eu` script exit)
# so a regression here fails with an unambiguous marker.
if beet -c /config/config.yaml -vv version; then
  echo "SETUP_DIAGNOSTIC_LOADER_PROBE_OK"
else
  echo "SETUP_DIAGNOSTIC_LOADER_PROBE_FAILED"
  exit 1
fi
python -c "import discogs_client; print('discogs client importable')"
beet -c /config/config.yaml help >/tmp/beet-help.txt
fpcalc -version
mkdir -p /tmp/import-smoke
ffmpeg -hide_banner -loglevel error -f lavfi -i sine=frequency=440:duration=1 -ac 2 -ar 44100 /tmp/import-smoke/smoke.wav
ffmpeg -hide_banner -loglevel error -f lavfi -i color=c=blue:s=16x16 -frames:v 1 /tmp/import-smoke/cover.jpg
beet -c /config/config.yaml import -q --quiet-fallback asis /tmp/import-smoke
# Real, network-free fetchart run against the just-imported album -- proves
# the plugin actually loads and executes, not just that the loader probe
# above tolerates it. sources: filesystem finds the cover.jpg placed
# beside the smoke track, so no network access is needed or attempted.
if beet -c /config/config.yaml fetchart -q; then
  echo "FETCHART_RUN_OK"
else
  echo "FETCHART_RUN_FAILED"
  exit 1
fi
"""
            proc = subprocess.run([
                "docker", "run", "--rm",
                "--read-only",
                "--security-opt", "no-new-privileges:true",
                "--cap-drop", "ALL",
                "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=256m",
                "--tmpfs", "/run:rw,nosuid,nodev,size=64m",
                "-v", f"{config_dir}:/config",
                "-v", f"{music_dir}:/data/media/music",
                "-v", f"{downloads_dir}:/data/torrents",
                "--entrypoint", "sh",
                image,
                "-lc", shell,
            ], cwd=ROOT, capture_output=True, text=True, timeout=300)
        output = (proc.stdout or "") + "\n" + (proc.stderr or "")
        self.assertEqual(proc.returncode, 0, output)
        for bad in ("error loading plugin", "ModuleNotFoundError", "replaygain initialization failed", "No module named", "mp3gain", "plexsync"):
            self.assertNotIn(bad.lower(), output.lower())
        for expected in ("lastgenre", "listenbrainz", "musicbrainz", "discpath", "replaygain", "ffmpeg", "discogs client importable"):
            self.assertIn(expected, output.lower())
        # Proves the exact command setup diagnostics now runs (`beet -c
        # ... -vv version`) exits zero against a real fresh-install image
        # with the default plugin set loaded -- and never uses the
        # unsupported `beet plugins` command.
        self.assertIn("SETUP_DIAGNOSTIC_LOADER_PROBE_OK", output)
        self.assertNotIn("SETUP_DIAGNOSTIC_LOADER_PROBE_FAILED", output)
        self.assertNotIn("beet -c /config/config.yaml plugins", output)
        # Direct FetchArt packaging/execution assertions (Issue: FetchArt
        # plugin load error in production) -- not merely inferred from the
        # general loader probe above.
        self.assertIn("fetchart module: /usr/local/lib/python3", output)
        self.assertIn("discpath module: /app/beetsplug/discpath.py", output)
        self.assertIn("fetchart dependencies importable", output)
        self.assertIn("FETCHART_RUN_OK", output)
        self.assertNotIn("FETCHART_RUN_FAILED", output)


if __name__ == "__main__":
    unittest.main()
