import os
import shutil
import tempfile
import time
import json
import uuid
import subprocess
import urllib.request
import urllib.parse
import urllib.error
import unittest

from beets.library import Library, Item, Album
from beetsplug.web import app as beets_web_app
from beetsplug.webmanager import WebManagerPlugin
from beetsplug.webmanager.auth import set_api_key_file
import beetsplug.webmanager.operations as ops_mod

STOCK_BEETS_IMAGE = "lscr.io/linuxserver/beets:latest"


def _docker_with_retry(cmd, attempts=3, backoff=5):
    """Run a docker command up to `attempts` times; return None on success, else the last stderr.

    `cmd` is an argv list, or a callable returning one (re-evaluated per attempt).
    """
    err = ""
    for attempt in range(attempts):
        if attempt:
            time.sleep(backoff * attempt)
        argv = cmd() if callable(cmd) else cmd
        res = subprocess.run(argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        if res.returncode == 0:
            return None
        err = f"attempt {attempt + 1}/{attempts}: exit {res.returncode}: {res.stderr.strip()}"
    return err


def _create_synthetic_audio(file_path: str, title: str, artist: str, album: str):
    """Generate a minimal valid audio file with tags for importer testing."""
    import wave
    import struct
    import mutagen.wave
    import mutagen.id3

    with wave.open(file_path, "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(struct.pack("<h", 0) * 44100)

    try:
        mw = mutagen.wave.WAVE(file_path)
        mw.add_tags()
        mw.tags.add(mutagen.id3.TIT2(encoding=3, text=[title]))
        mw.tags.add(mutagen.id3.TPE1(encoding=3, text=[artist]))
        mw.tags.add(mutagen.id3.TALB(encoding=3, text=[album]))
        mw.save()
    except Exception:
        pass


class StockBeetsInProcessAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.music_dir = os.path.join(self.td, "music")
        self.downloads_dir = os.path.join(self.td, "downloads")
        self.config_dir = os.path.join(self.td, "config")
        os.makedirs(self.music_dir, exist_ok=True)
        os.makedirs(self.downloads_dir, exist_ok=True)
        os.makedirs(self.config_dir, exist_ok=True)

        self.dbpath = os.path.join(self.config_dir, "musiclibrary.blb")
        self.lib = Library(self.dbpath, directory=self.music_dir)

        # Set up 64-hex API key (256-bit entropy)
        self.key_file = os.path.join(self.config_dir, ".webmanager_api_key")
        self.token = "a" * 64
        with open(self.key_file, "w", encoding="utf-8") as f:
            f.write(self.token + "\n")
        set_api_key_file(self.key_file)
        ops_mod.set_allowed_roots([self.music_dir, self.downloads_dir])
        ops_mod.set_import_roots([self.downloads_dir])

        # Configure Beets web app
        self.plugin = WebManagerPlugin()
        beets_web_app.config["lib"] = self.lib
        beets_web_app.config["INCLUDE_PATHS"] = True
        beets_web_app.config["READONLY"] = True
        beets_web_app.config["TESTING"] = True
        from beets import config as beets_config
        beets_config["web"]["readonly"] = True

        # Pre-populate sample library data
        self.album = Album(
            album="Echoes of Time",
            albumartist="Chronos",
            year=2023,
            genre="Ambient",
        )
        self.lib.add(self.album)

        self.sample_file = os.path.join(self.music_dir, "track1.mp3")
        with open(self.sample_file, "wb") as f:
            f.write(b"ID3\x03\x00\x00\x00\x00\x00\x00MP3_DATA_PAYLOAD")

        self.item1 = Item(
            title="Sands of Time",
            artist="Chronos",
            album="Echoes of Time",
            albumartist="Chronos",
            album_id=self.album.id,
            track=1,
            genre="Ambient",
            year=2023,
            path=self.sample_file.encode("utf-8"),
        )
        self.lib.add(self.item1)
        self.lib._connection().commit()

        self.client = beets_web_app.test_client()

    def tearDown(self):
        ops_mod.set_allowed_roots(None)
        ops_mod.set_import_roots(None)
        set_api_key_file(None)
        try:
            self.lib._connection().close()
        except Exception:
            pass
        shutil.rmtree(self.td, ignore_errors=True)

    def test_native_upstream_reads(self):
        """Verify native upstream endpoints match the expected Beets REST API contracts."""
        # 1. GET /stats
        res = self.client.get("/stats")
        self.assertEqual(res.status_code, 200)
        stats = res.get_json()
        self.assertEqual(stats["items"], 1)
        self.assertEqual(stats["albums"], 1)

        # 2. GET /artist/
        res = self.client.get("/artist/")
        self.assertEqual(res.status_code, 200)
        artists = res.get_json()
        self.assertIn("artist_names", artists)
        self.assertIn("Chronos", artists["artist_names"])

        # 3. GET /item/
        res = self.client.get("/item/")
        self.assertEqual(res.status_code, 200)
        items_data = res.get_json()
        self.assertIn("items", items_data)
        self.assertEqual(len(items_data["items"]), 1)
        self.assertEqual(items_data["items"][0]["title"], "Sands of Time")

        # 4. GET /item/<id>
        res = self.client.get(f"/item/{self.item1.id}")
        self.assertEqual(res.status_code, 200)
        item_res = res.get_json()
        self.assertEqual(item_res["title"], "Sands of Time")

        # 5. GET /album/
        res = self.client.get("/album/")
        self.assertEqual(res.status_code, 200)
        albums_data = res.get_json()
        self.assertIn("albums", albums_data)
        self.assertEqual(len(albums_data["albums"]), 1)
        self.assertEqual(albums_data["albums"][0]["album"], "Echoes of Time")

        # 6. GET /album/<id>?expand
        res = self.client.get(f"/album/{self.album.id}?expand")
        self.assertEqual(res.status_code, 200)
        expanded = res.get_json()
        self.assertEqual(expanded["album"], "Echoes of Time")
        self.assertIn("items", expanded)
        self.assertEqual(len(expanded["items"]), 1)
        self.assertEqual(expanded["items"][0]["title"], "Sands of Time")

        # 7. GET /item/values/<key>
        res = self.client.get("/item/values/artist")
        self.assertEqual(res.status_code, 200)
        values = res.get_json()
        self.assertIn("values", values)
        self.assertIn("Chronos", values["values"])

    def test_native_file_stream(self):
        """Verify native audio binary streaming endpoint."""
        res = self.client.get(f"/item/{self.item1.id}/file")
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"MP3_DATA_PAYLOAD", res.data)

    def test_webmanager_plugin_security_and_operations(self):
        """Verify WebManager plugin authentication, status handshake, modification, and path containment."""
        # 1. Unauthenticated request must fail
        res = self.client.get("/webmanager/status")
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.get_json()["error_code"], "UNAUTHORIZED")

        # 2. Authenticated status check
        auth_headers = {"Authorization": f"Bearer {self.token}"}
        res = self.client.get("/webmanager/status", headers=auth_headers)
        self.assertEqual(res.status_code, 200)
        status_data = res.get_json()
        self.assertEqual(status_data["protocol_version"], "1.0")
        self.assertEqual(status_data["plugin_version"], "1.8.0")
        self.assertTrue(status_data["upstream_web_readonly"])
        self.assertTrue(status_data["plugin_mutations_enabled"])
        self.assertIn("import", status_data["capabilities"])
        # 1.6.0: path diagnostics exposed only to authenticated callers (401 checked above)
        self.assertIn("allowed_roots", status_data)
        self.assertIn("import_roots", status_data)

        # 3. Path containment verification on /webmanager/import
        outside_path = os.path.join(self.td, "..", "etc", "passwd")
        res = self.client.post(
            "/webmanager/import",
            headers=auth_headers,
            json={"paths": [outside_path]},
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.get_json()["error_code"], "PATH_NOT_ALLOWED")

        # Root self-import rejected
        res_root = self.client.post(
            "/webmanager/import",
            headers=auth_headers,
            json={"paths": [self.downloads_dir]},
        )
        self.assertEqual(res_root.status_code, 400)
        self.assertEqual(res_root.get_json()["error_code"], "PATH_NOT_ALLOWED")

        # /music is an allowed_root but NOT an import_root -- must be rejected as an import source
        music_child = os.path.join(self.music_dir, "not_a_valid_import_source")
        os.makedirs(music_child, exist_ok=True)
        res_music = self.client.post(
            "/webmanager/import",
            headers=auth_headers,
            json={"paths": [music_child]},
        )
        self.assertEqual(res_music.status_code, 400)
        self.assertEqual(res_music.get_json()["error_code"], "PATH_NOT_ALLOWED")

        # 4. Modify tags
        res = self.client.post(
            "/webmanager/modify",
            headers=auth_headers,
            json={
                "item_ids": [self.item1.id],
                "fields": {"title": "Sands of Time (Remastered)", "genre": "Chillout"},
                "write": False,
                "move": False,
            },
        )
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json()["success"])

        # 5. Verify modification through native upstream read
        res = self.client.get(f"/item/{self.item1.id}")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["title"], "Sands of Time (Remastered)")
        self.assertEqual(res.get_json()["genre"], "Chillout")


class StockBeetsDockerAcceptanceTests(unittest.TestCase):
    @unittest.skipIf(not shutil.which("docker"), "Docker CLI not available on host")
    def test_real_stock_docker_container_acceptance(self):
        """Real Docker Acceptance Test booting unmodified lscr.io/linuxserver/beets:latest.

        Tests:
        1. Stock image startup with web & webmanager plugins mounted in /config
        2. Native Beets GET /stats and GET /item/
        3. WebManager plugin authentication (rejecting invalid token, accepting 64-hex token)
        4. Non-interactive import of a synthetic tagged audio file from /downloads into /music
        5. Concurrent idempotency test (two simultaneous requests with same key converging safely)
        6. Operation ID collision safety (rejecting conflicting payload with 409 Conflict)
        7. Upstream native verification of the imported file and fields
        8. Disposable mutation modification and upstream verification
        """
        import concurrent.futures

        # Verify docker daemon is responsive before proceeding
        try:
            res_daemon = subprocess.run(["docker", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if res_daemon.returncode != 0:
                self.skipTest("Docker daemon not running or responsive")
        except Exception:
            self.skipTest("Docker CLI not functional")

        container_name = f"stock-beets-acc-{uuid.uuid4().hex[:8]}"
        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

        td = tempfile.mkdtemp()
        try:
            config_dir = os.path.join(td, "config")
            music_dir = os.path.join(td, "music")
            downloads_dir = os.path.join(td, "downloads")
            os.makedirs(config_dir, exist_ok=True)
            os.makedirs(music_dir, exist_ok=True)
            os.makedirs(downloads_dir, exist_ok=True)
            if os.name != "nt":
                try:
                    os.chmod(td, 0o777)
                    os.chmod(config_dir, 0o777)
                    os.chmod(music_dir, 0o777)
                    os.chmod(downloads_dir, 0o777)
                except Exception:
                    pass

            # 1. Provision webmanager plugin into config_dir/beetsplug/webmanager
            target_plugin_dir = os.path.join(config_dir, "beetsplug", "webmanager")
            os.makedirs(target_plugin_dir, exist_ok=True)
            src_plugin_dir = os.path.join(repo_root, "beetsplug", "webmanager")
            for f in ["__init__.py", "compat.py", "auth.py", "schemas.py", "operations.py", "version.py", "plugin_ops.py", "engine_common.py", "replace_ops.py", "remove_ops.py", "merge_ops.py", "untracked_ops.py", "folder_ops.py"]:
                shutil.copy2(os.path.join(src_plugin_dir, f), os.path.join(target_plugin_dir, f))

            # 2. Provision 64-hex secret API key file (256-bit entropy)
            token = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
            key_file = os.path.join(config_dir, ".webmanager_api_key")
            with open(key_file, "w", encoding="utf-8") as f:
                f.write(token + "\n")
            if os.name != "nt":
                try:
                    os.chmod(key_file, 0o666)
                except Exception:
                    pass

            # 3. Write config.yaml
            config_yaml = f"""plugins: web webmanager
pluginpath:
  - /config/beetsplug
directory: /music
library: /config/musiclibrary.blb
import:
  write: yes
  copy: no
  move: yes
  quiet: yes
  autotag: no
web:
  host: 0.0.0.0
  port: 8337
  readonly: yes
  include_paths: yes
webmanager:
  api_key_file: /config/.webmanager_api_key
  allowed_roots:
    - /music
    - /downloads
  import_roots:
    - /downloads
"""
            with open(os.path.join(config_dir, "config.yaml"), "w", encoding="utf-8") as f:
                f.write(config_yaml)
            if os.name != "nt":
                try:
                    os.chmod(os.path.join(config_dir, "config.yaml"), 0o666)
                except Exception:
                    pass

            # 4. Generate synthetic tagged audio file in downloads
            track_name = "Synthetic_Acceptance_Track.wav"
            track_path = os.path.join(downloads_dir, track_name)
            _create_synthetic_audio(
                track_path,
                title="Synthetic Anthem",
                artist="Acceptance Bot",
                album="Docker Test LP",
            )
            if os.name != "nt":
                try:
                    os.chmod(track_path, 0o666)
                except Exception:
                    pass

            # 5. Pre-pull image and start stock Beets container bound strictly to loopback 127.0.0.1.
            # Registry pulls and loopback port picks can flake in CI (`docker run` exit 125), so both
            # are retried a bounded number of times; the last failure's stderr goes into the assertion.
            pull_err = _docker_with_retry(["docker", "pull", STOCK_BEETS_IMAGE])
            cached = subprocess.run(["docker", "image", "inspect", STOCK_BEETS_IMAGE], capture_output=True).returncode == 0
            if pull_err is not None and not cached:
                self.fail(f"docker pull {STOCK_BEETS_IMAGE} failed after retries:\n{pull_err}")

            import socket

            # Ensure urllib opener and environment permit requests to local acceptance test container
            _raw_urlopen = getattr(urllib.request, "_beets_original_urlopen", urllib.request.urlopen)
            orig_allowlist = os.environ.get("BEETS_OUTBOUND_ALLOWLIST")

            def _run_stock_container():
                nonlocal host_port
                # A failed `docker run` can leave a created container holding the name.
                subprocess.run(["docker", "rm", "-f", container_name],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                sock = socket.socket()
                sock.bind(("127.0.0.1", 0))
                host_port = sock.getsockname()[1]
                sock.close()
                return [
                    "docker", "run", "-d",
                    "--name", container_name,
                    "-p", f"127.0.0.1:{host_port}:8337",
                    "-v", f"{config_dir}:/config",
                    "-v", f"{music_dir}:/music",
                    "-v", f"{downloads_dir}:/downloads",
                    "-e", "PUID=1000",
                    "-e", "PGID=1000",
                    STOCK_BEETS_IMAGE,
                ]

            host_port = None
            run_err = _docker_with_retry(_run_stock_container)
            if run_err is not None:
                subprocess.run(["docker", "rm", "-f", container_name],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.fail(f"docker run {STOCK_BEETS_IMAGE} failed after retries:\n{run_err}")
            os.environ["BEETS_OUTBOUND_ALLOWLIST"] = f"127.0.0.1:{host_port},localhost:{host_port},beets:8338,127.0.0.1:8338"

            try:
                base_url = f"http://127.0.0.1:{host_port}"
                # Wait for Beets web server to become responsive (up to 90s for image pull + s6-overlay init)
                responsive = False
                for _ in range(90):
                    time.sleep(1)
                    try:
                        req = urllib.request.Request(f"{base_url}/stats")
                        with _raw_urlopen(req, timeout=2) as resp:
                            if resp.status == 200:
                                responsive = True
                                break
                    except Exception:
                        pass

                if not responsive:
                    logs = subprocess.run(["docker", "logs", container_name], capture_output=True, text=True)
                    self.fail(f"Timed out waiting for stock Beets container at {base_url}.\nContainer logs:\nSTDOUT:\n{logs.stdout}\nSTDERR:\n{logs.stderr}")

                # Step 1: Upstream Native Read Check
                req = urllib.request.Request(f"{base_url}/stats")
                with _raw_urlopen(req, timeout=5) as resp:
                    stats = json.loads(resp.read().decode("utf-8"))
                    self.assertIn("items", stats)

                # Step 2: WebManager Auth Check
                # Invalid token must return 401
                try:
                    req = urllib.request.Request(
                        f"{base_url}/webmanager/status",
                        headers={"Authorization": "Bearer 0000000000000000000000000000000000000000000000000000000000000000"},
                    )
                    _raw_urlopen(req, timeout=5)
                    self.fail("Expected 401 for wrong token")
                except urllib.error.HTTPError as ex:
                    self.assertEqual(ex.code, 401)

                # Valid token must return 200 OK with handshake schema
                auth_header = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
                req = urllib.request.Request(f"{base_url}/webmanager/status", headers=auth_header)
                with _raw_urlopen(req, timeout=5) as resp:
                    status_res = json.loads(resp.read().decode("utf-8"))
                    self.assertEqual(status_res["protocol_version"], "1.0")
                    self.assertEqual(status_res["plugin_version"], "1.8.0")
                    self.assertTrue(status_res["upstream_web_readonly"])
                    self.assertTrue(status_res["plugin_mutations_enabled"])
                    self.assertIn("import", status_res["capabilities"])

                # Step 2b: import_roots must reject the bare import root itself and any
                # /music child, even though /music is an allowed_root for other
                # operations -- it must never become a valid import source.
                for rejected_path in ("/downloads", f"/music/{track_name}"):
                    req_reject = urllib.request.Request(
                        f"{base_url}/webmanager/import",
                        data=json.dumps({"paths": [rejected_path]}).encode("utf-8"),
                        headers=auth_header,
                        method="POST",
                    )
                    try:
                        _raw_urlopen(req_reject, timeout=10)
                        self.fail(f"Expected import from {rejected_path!r} to be rejected")
                    except urllib.error.HTTPError as ex:
                        self.assertEqual(ex.code, 400)
                        body = json.loads(ex.read().decode("utf-8"))
                        self.assertEqual(body["error_code"], "PATH_NOT_ALLOWED")

                # Step 3: Concurrent Idempotency Test
                # Execute two simultaneous POST requests with same key and payload
                idemp_key = "test-concurrent-idemp-docker-001"
                import_payload = {
                    "paths": [f"/downloads/{track_name}"],
                    "autotag": False,
                    "duplicate_action": "skip",
                    "singletons": True,
                    "copy": False,
                    "move": True,
                    "write": True,
                }

                def _send_concurrent_import():
                    h = dict(auth_header)
                    h["Idempotency-Key"] = idemp_key
                    r = urllib.request.Request(
                        f"{base_url}/webmanager/import",
                        data=json.dumps(import_payload).encode("utf-8"),
                        headers=h,
                        method="POST",
                    )
                    with _raw_urlopen(r, timeout=15) as resp_obj:
                        return resp_obj.status, json.loads(resp_obj.read().decode("utf-8"))

                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                    f1 = executor.submit(_send_concurrent_import)
                    f2 = executor.submit(_send_concurrent_import)
                    status1, res1 = f1.result()
                    status2, res2 = f2.result()

                self.assertIn(status1, (200, 202))
                self.assertIn(status2, (200, 202))

                # Step 4: Verify Operation Status Polling and Public Schema
                req_op = urllib.request.Request(
                    f"{base_url}/webmanager/operations/{idemp_key}",
                    headers=auth_header,
                )
                with _raw_urlopen(req_op, timeout=5) as resp:
                    op_data = json.loads(resp.read().decode("utf-8"))
                    self.assertEqual(op_data["operation_id"], idemp_key)
                    self.assertEqual(op_data["status"], "succeeded")
                    self.assertNotIn("_fingerprint", op_data)  # internal fingerprint hidden

                # Step 5: Collision Check (Same Idempotency-Key with DIFFERENT Payload)
                conflicting_payload = dict(import_payload)
                conflicting_payload["duplicate_action"] = "remove"
                conflicting_headers = dict(auth_header)
                conflicting_headers["Idempotency-Key"] = idemp_key
                req_conflict = urllib.request.Request(
                    f"{base_url}/webmanager/import",
                    data=json.dumps(conflicting_payload).encode("utf-8"),
                    headers=conflicting_headers,
                    method="POST",
                )
                try:
                    _raw_urlopen(req_conflict, timeout=15)
                    self.fail("Expected 409 Conflict for differing payload with same idempotency key")
                except urllib.error.HTTPError as ex:
                    self.assertEqual(ex.code, 409)

                # Step 6: Verify imported track in native upstream Beets Web API
                req_items = urllib.request.Request(f"{base_url}/item/")
                with _raw_urlopen(req_items, timeout=5) as resp:
                    items_res = json.loads(resp.read().decode("utf-8"))
                    items = items_res.get("items", [])
                    self.assertGreaterEqual(len(items), 1)
                    imported_item = items[0]
                    self.assertEqual(imported_item["title"], "Synthetic Anthem")
                    self.assertEqual(imported_item["artist"], "Acceptance Bot")
                    # include_paths shows the path. Beets 2.x stores it relative to
                    # the library directory (/music) when the importing thread has
                    # the library's music-dir context -- as /webmanager requests now
                    # do -- so accept library-relative or absolute-under-/music.
                    shown = imported_item.get("path", "")
                    self.assertTrue(shown, "include_paths must show the item path")
                    resolved = shown if shown.startswith("/") else "/music/" + shown
                    self.assertTrue(resolved.startswith("/music/"), shown)
                    self.assertNotIn("..", shown.split("/"))
                    self.assertTrue(resolved.endswith("Synthetic Anthem.wav"), shown)

                # Step 7: Exercise Disposable Mutation (POST /webmanager/modify)
                modify_payload = {
                    "item_ids": [imported_item["id"]],
                    "fields": {"genre": "Synthesized Electro"},
                    "write": False,
                    "move": False,
                }
                req_mod = urllib.request.Request(
                    f"{base_url}/webmanager/modify",
                    data=json.dumps(modify_payload).encode("utf-8"),
                    headers=auth_header,
                    method="POST",
                )
                with _raw_urlopen(req_mod, timeout=10) as resp:
                    mod_res = json.loads(resp.read().decode("utf-8"))
                    self.assertTrue(mod_res["success"])

                # Step 8: Verify mutation through native upstream GET /item/<id>
                req_verify = urllib.request.Request(f"{base_url}/item/{imported_item['id']}")
                with _raw_urlopen(req_verify, timeout=5) as resp:
                    ver_res = json.loads(resp.read().decode("utf-8"))
                    self.assertEqual(ver_res["genre"], "Synthesized Electro")

                # Step 9: quarantine-remove on a real Werkzeug request thread
                # (library-relative DB paths must resolve to the real file).
                container_path = resolved
                sha = subprocess.run(
                    ["docker", "exec", container_name, "sha256sum", container_path],
                    capture_output=True, text=True, check=True,
                ).stdout.split()[0]
                req_q = urllib.request.Request(
                    f"{base_url}/webmanager/quarantine-remove-items",
                    data=json.dumps({"items": [{"item_id": imported_item["id"], "sha256": sha}]}).encode("utf-8"),
                    headers=auth_header,
                    method="POST",
                )
                with _raw_urlopen(req_q, timeout=30) as resp:
                    q_res = json.loads(resp.read().decode("utf-8"))
                    self.assertTrue(q_res["success"], q_res)
                gone = subprocess.run(["docker", "exec", container_name, "test", "-e", container_path])
                self.assertNotEqual(gone.returncode, 0, "file must have moved into the engine quarantine")
                with self.assertRaises(urllib.error.HTTPError) as missing:
                    _raw_urlopen(urllib.request.Request(f"{base_url}/item/{imported_item['id']}"), timeout=5)
                self.assertEqual(missing.exception.code, 404)

                # Step 10: rollback from the engine's own manifest.
                req_rb = urllib.request.Request(
                    f"{base_url}/webmanager/quarantine-remove-items/rollback",
                    data=json.dumps({"quarantine_id": q_res["quarantine_id"]}).encode("utf-8"),
                    headers=auth_header,
                    method="POST",
                )
                with _raw_urlopen(req_rb, timeout=30) as resp:
                    rb_res = json.loads(resp.read().decode("utf-8"))
                    [restored] = rb_res["restored"]
                back = subprocess.run(["docker", "exec", container_name, "test", "-f", container_path])
                self.assertEqual(back.returncode, 0, "file must be back at its original path")
                with _raw_urlopen(urllib.request.Request(f"{base_url}/item/{restored['new_item_id']}"), timeout=5) as resp:
                    self.assertEqual(json.loads(resp.read().decode("utf-8"))["title"], "Synthetic Anthem")

                # Steps 11-14: album-row merge + rollback, untracked attach /
                # quarantine + rollback, and idempotent replay -- all against
                # the real stock container (ARCH-020 / ARCH-021).
                self._accept_merge_and_untracked(base_url, auth_header, container_name, downloads_dir, music_dir,
                                                 _raw_urlopen)

            finally:
                if orig_allowlist is not None:
                    os.environ["BEETS_OUTBOUND_ALLOWLIST"] = orig_allowlist
                else:
                    os.environ.pop("BEETS_OUTBOUND_ALLOWLIST", None)
                # Clean up container
                subprocess.run(["docker", "rm", "-f", container_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        finally:
            if os.name != "nt":
                subprocess.run(
                    ["docker", "run", "--rm", "-v", f"{td}:/work", "alpine", "chmod", "-R", "777", "/work"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            shutil.rmtree(td, ignore_errors=True)


    def _accept_merge_and_untracked(self, base_url, auth_header, container_name, downloads_dir, music_dir, urlopen):
        rel = "45347542-db98-422a-a307-ae95d5371f60"
        rg = "ef4b6576-ac7c-4f72-bee6-e7a6b6cf019d"

        def call(method, path, body=None, key=None, expect=None):
            headers = dict(auth_header)
            if key:
                headers["Idempotency-Key"] = key
            req = urllib.request.Request(f"{base_url}{path}", method=method, headers=headers,
                                         data=json.dumps(body).encode("utf-8") if body is not None else None)
            try:
                with urlopen(req, timeout=60) as resp:
                    return resp.status, json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as ex:
                raw = ex.read().decode("utf-8", errors="replace")
                try:
                    return ex.code, json.loads(raw or "{}")
                except ValueError:  # stock Beets answers 404 with an HTML page
                    return ex.code, {"raw": raw[:200]}

        def container_path(shown):
            return shown if shown.startswith("/") else "/music/" + shown

        def csha(path):
            out = subprocess.run(["docker", "exec", container_name, "sha256sum", path],
                                 capture_output=True, text=True, check=True).stdout
            return out.split()[0]

        def exists(path):
            return subprocess.run(["docker", "exec", container_name, "test", "-e", path]).returncode == 0

        # Two album rows of one release: import two folders as albums, then
        # give both rows the same Release Group + Release ID via modify.
        album_ids = []
        for name, tracks in (("MergeA", (1, 2)), ("MergeB", (3,))):
            folder = os.path.join(downloads_dir, name)
            os.makedirs(folder, exist_ok=True)
            for t in tracks:
                _create_synthetic_audio(os.path.join(folder, f"{t:02d}.wav"), title=f"Merge {t}",
                                        artist="Merge Bot", album=f"Merge LP {name}")
            if os.name != "nt":
                os.chmod(folder, 0o777)
                for f in os.listdir(folder):
                    os.chmod(os.path.join(folder, f), 0o666)
            key = f"accept-import-{name}-{uuid.uuid4().hex[:6]}"
            status, _ = call("POST", "/webmanager/import", {"paths": [f"/downloads/{name}"], "autotag": False,
                                                            "singletons": False, "move": True, "write": False,
                                                            "duplicate_action": "keep"}, key=key)
            self.assertIn(status, (200, 202))
            for _ in range(60):
                _s, op = call("GET", f"/webmanager/operations/{key}")
                if op.get("status") in ("succeeded", "failed"):
                    break
                time.sleep(1)
            self.assertEqual(op.get("status"), "succeeded", op)
            _s, albums = call("GET", "/album/")
            album_ids.append(next(a["id"] for a in albums["albums"] if a["album"] == f"Merge LP {name}"))
        status, _ = call("POST", "/webmanager/modify", {"album_ids": album_ids, "write": False, "move": False,
                                                        "fields": {"mb_albumid": rel, "mb_releasegroupid": rg}})
        self.assertEqual(status, 200)
        target_id, source_id = album_ids
        _s, source_album = call("GET", f"/album/{source_id}?expand")
        source_items = source_album.get("items") or []
        _s, target_album = call("GET", f"/album/{target_id}?expand")
        for idx, it in enumerate((target_album.get("items") or []) + source_items, start=1):
            status, _ = call("POST", "/webmanager/modify", {"item_ids": [it["id"]], "write": False, "move": False,
                                                            "fields": {"track": idx, "disc": 1,
                                                                       "mb_trackid": f"00000000-0000-0000-0000-{idx:012d}"}})
            self.assertEqual(status, 200)
        _s, source_album = call("GET", f"/album/{source_id}?expand")
        items = [{"item_id": it["id"], "source_album_id": source_id, "sha256": csha(container_path(it["path"])),
                  "mb_trackid": it["mb_trackid"], "disc": it["disc"], "track": it["track"]}
                 for it in source_album["items"]]
        merge_key = f"accept-merge-{uuid.uuid4().hex[:8]}"
        body = {"target_album_id": target_id, "source_album_ids": [source_id], "expected_release_group_id": rg,
                "expected_release_id": rel, "items": items}
        status, merged = call("POST", "/webmanager/album-row-merge", body, key=merge_key)
        self.assertEqual(status, 200, merged)
        self.assertEqual(call("GET", f"/album/{source_id}")[0], 404)
        for it in items:
            self.assertEqual(call("GET", f"/item/{it['item_id']}")[1]["album_id"], target_id)
        status, replay = call("POST", "/webmanager/album-row-merge", body, key=merge_key)
        self.assertTrue(replay.get("replayed"), replay)
        status, rolled = call("POST", "/webmanager/album-row-merge/rollback", {"merge_id": merged["merge_id"]})
        self.assertEqual(status, 200, rolled)
        self.assertEqual(call("GET", f"/album/{source_id}")[0], 200)  # the original album id is back
        for it in items:
            self.assertEqual(call("GET", f"/item/{it['item_id']}")[1]["album_id"], source_id)
        self.assertTrue(call("POST", "/webmanager/album-row-merge/rollback",
                             {"merge_id": merged["merge_id"]})[1].get("replayed"))

        # Partial move (ARCH-020 legacy callers): one item of the two-item
        # row moves, that row stays with the other; rollback restores it.
        _s, target_album = call("GET", f"/album/{target_id}?expand")
        t_items = target_album["items"]
        one = t_items[0]
        partial_body = {"target_album_id": source_id, "source_album_ids": [target_id], "partial": True,
                        "expected_release_group_id": rg, "expected_release_id": rel,
                        "items": [{"item_id": one["id"], "source_album_id": target_id,
                                   "sha256": csha(container_path(one["path"])), "mb_trackid": one["mb_trackid"],
                                   "disc": one["disc"], "track": one["track"]}]}
        status, refused = call("POST", "/webmanager/album-row-merge", {**partial_body, "partial": False},
                               key=f"accept-partial-{uuid.uuid4().hex[:8]}")
        self.assertEqual((status, refused.get("error_code")), (409, "SOURCE_NOT_FULLY_COVERED"), refused)
        status, part = call("POST", "/webmanager/album-row-merge", partial_body,
                            key=f"accept-partial-{uuid.uuid4().hex[:8]}")
        self.assertEqual(status, 200, part)
        self.assertEqual(part["retired_album_ids"], [])
        self.assertEqual(call("GET", f"/item/{one['id']}")[1]["album_id"], source_id)
        self.assertEqual(call("GET", f"/album/{target_id}")[0], 200)  # still holds its other item
        status, rolled = call("POST", "/webmanager/album-row-merge/rollback", {"merge_id": part["merge_id"]})
        self.assertEqual(status, 200, rolled)
        self.assertEqual(call("GET", f"/item/{one['id']}")[1]["album_id"], target_id)

        # Reviewed cleanup of a copy that is the only item of a duplicate row
        # of the same release: never emptied implicitly; retired only when
        # named with a sibling keeper; rollback restores row and item ids.
        [dup] = items
        [keeper] = [it for it in (call("GET", f"/album/{target_id}?expand")[1].get("items") or [])
                    if it["track"] == 1]
        status, _ = call("POST", "/webmanager/modify", {"item_ids": [dup["item_id"]], "write": False, "move": False,
                                                        "fields": {"track": 1}})
        self.assertEqual(status, 200)
        dup_sha = csha(container_path(call("GET", f"/item/{dup['item_id']}")[1]["path"]))
        status, refused = call("POST", "/webmanager/quarantine-remove-items",
                               {"items": [{"item_id": dup["item_id"], "sha256": dup_sha}]},
                               key=f"accept-qr-{uuid.uuid4().hex[:8]}")
        self.assertEqual((status, refused.get("error_code")), (409, "ALBUM_WOULD_EMPTY"), refused)
        status, retired = call("POST", "/webmanager/quarantine-remove-items",
                               {"items": [{"item_id": dup["item_id"], "sha256": dup_sha, "retire_album_id": source_id,
                                           "sibling_keeper_item_id": keeper["id"]}]},
                               key=f"accept-qr-{uuid.uuid4().hex[:8]}")
        self.assertEqual(status, 200, retired)
        self.assertEqual(retired["retired_album_ids"], [source_id])
        self.assertEqual(call("GET", f"/album/{source_id}")[0], 404)
        self.assertEqual(call("GET", f"/item/{dup['item_id']}")[0], 404)
        self.assertEqual(call("GET", f"/item/{keeper['id']}")[1]["album_id"], target_id)
        status, back = call("POST", "/webmanager/quarantine-remove-items/rollback",
                            {"quarantine_id": retired["quarantine_id"]}, key=f"accept-qrb-{uuid.uuid4().hex[:8]}")
        self.assertEqual(status, 200, back)
        [restored] = back["restored"]
        self.assertEqual((restored["new_item_id"], restored["album_id"]), (dup["item_id"], source_id))
        self.assertEqual(call("GET", f"/album/{source_id}")[1].get("mb_albumid"), rel)
        self.assertEqual(csha(container_path(call("GET", f"/item/{dup['item_id']}")[1]["path"])), dup_sha)

        # Untracked: attach a tagged loose file as a singleton, roll back;
        # quarantine it, roll back.
        from mediafile import MediaFile
        loose_dir = os.path.join(music_dir, "Loose")
        os.makedirs(loose_dir, exist_ok=True)
        host_file = os.path.join(loose_dir, "loose.wav")
        _create_synthetic_audio(host_file, title="Loose", artist="Loose Bot", album="Loose LP")
        mf = MediaFile(host_file)
        mf.mb_trackid, mf.mb_albumid, mf.track, mf.disc = "00000000-0000-0000-0000-000000000099", rel, 9, 1
        mf.save()
        if os.name != "nt":
            os.chmod(loose_dir, 0o777)
            os.chmod(host_file, 0o666)
        path = "/music/Loose/loose.wav"
        digest = csha(path)
        attach_key = f"accept-attach-{uuid.uuid4().hex[:8]}"
        status, attached = call("POST", "/webmanager/untracked/attach",
                                {"path": path, "sha256": digest, "album_id": None,
                                 "expected": {"mb_trackid": "00000000-0000-0000-0000-000000000099", "mb_albumid": rel,
                                              "disc": 1, "track": 9}}, key=attach_key)
        self.assertEqual(status, 200, attached)
        self.assertEqual(call("GET", f"/item/{attached['item_id']}")[0], 200)
        self.assertTrue(call("POST", "/webmanager/untracked/attach", {"path": path}, key=attach_key)[1].get("replayed"))
        status, rb = call("POST", "/webmanager/untracked/rollback", {"record_id": attached["record_id"]})
        self.assertEqual(status, 200, rb)
        self.assertEqual(call("GET", f"/item/{attached['item_id']}")[0], 404)
        self.assertTrue(exists(path))
        status, quarantined = call("POST", "/webmanager/untracked/quarantine",
                                   {"files": [{"path": path, "sha256": digest}]},
                                   key=f"accept-quarantine-{uuid.uuid4().hex[:8]}")
        self.assertEqual(status, 200, quarantined)
        self.assertFalse(exists(path))
        status, rb = call("POST", "/webmanager/untracked/rollback", {"record_id": quarantined["record_id"]})
        self.assertEqual(status, 200, rb)
        self.assertEqual(csha(path), digest)

        # New album row from untracked files of a release Beets does not have
        # yet, in place (ARCH-021): refused for a release that has a row;
        # applied; replayed by key; rolled back with the files untouched.
        new_rel, new_rg = "9d3c1b2a-7e4f-4c6d-8a1b-2f3e4d5c6b7a", "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
        new_dir = os.path.join(music_dir, "NewRelease")
        os.makedirs(new_dir, exist_ok=True)
        album_files = []
        for t in (1, 2):
            host = os.path.join(new_dir, f"{t:02d}.wav")
            _create_synthetic_audio(host, title=f"New {t}", artist="New Bot", album="New LP")
            mf = MediaFile(host)
            mf.mb_trackid, mf.mb_albumid, mf.mb_releasegroupid = f"00000000-0000-0000-0000-{200 + t:012d}", new_rel, new_rg
            mf.track, mf.disc = t, 1
            mf.save()
            if os.name != "nt":
                os.chmod(host, 0o666)
            cpath = f"/music/NewRelease/{t:02d}.wav"
            album_files.append({"path": cpath, "sha256": csha(cpath),
                                "expected": {"mb_trackid": f"00000000-0000-0000-0000-{200 + t:012d}",
                                             "disc": 1, "track": t}})
        if os.name != "nt":
            os.chmod(new_dir, 0o777)
        status, refused = call("POST", "/webmanager/untracked/attach-album",
                               {"release_id": rel, "release_group_id": rg, "files": album_files},
                               key=f"accept-album-{uuid.uuid4().hex[:8]}")
        self.assertEqual((status, refused.get("error_code")), (409, "ALBUM_ROW_EXISTS"), refused)
        album_key = f"accept-album-{uuid.uuid4().hex[:8]}"
        album_body = {"release_id": new_rel, "release_group_id": new_rg, "files": album_files}
        status, made = call("POST", "/webmanager/untracked/attach-album", album_body, key=album_key)
        self.assertEqual(status, 200, made)
        status, new_album = call("GET", f"/album/{made['album_id']}?expand")
        self.assertEqual((new_album["mb_albumid"], new_album["mb_releasegroupid"]), (new_rel, new_rg))
        self.assertEqual(sorted(i["track"] for i in new_album["items"]), [1, 2])
        self.assertEqual([csha(f["path"]) for f in album_files], [f["sha256"] for f in album_files])
        self.assertTrue(call("POST", "/webmanager/untracked/attach-album", album_body, key=album_key)[1].get("replayed"))
        status, rb = call("POST", "/webmanager/untracked/rollback", {"record_id": made["record_id"]})
        self.assertEqual(status, 200, rb)
        self.assertTrue(rb["files_untouched"])
        self.assertEqual(call("GET", f"/album/{made['album_id']}")[0], 404)
        for iid in made["item_ids"]:
            self.assertEqual(call("GET", f"/item/{iid}")[0], 404)
        self.assertEqual([csha(f["path"]) for f in album_files], [f["sha256"] for f in album_files])


if __name__ == "__main__":
    unittest.main()
