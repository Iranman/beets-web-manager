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

        # Configure Beets web app
        self.plugin = WebManagerPlugin()
        beets_web_app.config["lib"] = self.lib
        beets_web_app.config["INCLUDE_PATHS"] = True
        beets_web_app.config["READONLY"] = True
        beets_web_app.config["TESTING"] = True

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
        self.assertEqual(status_data["plugin_version"], "0.1.0")
        self.assertTrue(status_data["upstream_web_readonly"])
        self.assertTrue(status_data["plugin_mutations_enabled"])
        self.assertIn("import", status_data["capabilities"])
        self.assertNotIn("allowed_roots", status_data)  # Internal paths not exposed

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

            # 1. Provision webmanager plugin into config_dir/beetsplug/webmanager
            target_plugin_dir = os.path.join(config_dir, "beetsplug", "webmanager")
            os.makedirs(target_plugin_dir, exist_ok=True)
            src_plugin_dir = os.path.join(repo_root, "beetsplug", "webmanager")
            for f in ["__init__.py", "compat.py", "auth.py", "schemas.py", "operations.py"]:
                shutil.copy2(os.path.join(src_plugin_dir, f), os.path.join(target_plugin_dir, f))

            # 2. Provision 64-hex secret API key file (256-bit entropy)
            token = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
            key_file = os.path.join(config_dir, ".webmanager_api_key")
            with open(key_file, "w", encoding="utf-8") as f:
                f.write(token + "\n")

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
"""
            with open(os.path.join(config_dir, "config.yaml"), "w", encoding="utf-8") as f:
                f.write(config_yaml)

            # 4. Generate synthetic tagged audio file in downloads
            track_name = "Synthetic_Acceptance_Track.wav"
            track_path = os.path.join(downloads_dir, track_name)
            _create_synthetic_audio(
                track_path,
                title="Synthetic Anthem",
                artist="Acceptance Bot",
                album="Docker Test LP",
            )

            # 5. Start stock Beets container bound strictly to loopback 127.0.0.1
            import socket
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            host_port = sock.getsockname()[1]
            sock.close()

            run_cmd = [
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

            subprocess.check_call(run_cmd)

            try:
                base_url = f"http://127.0.0.1:{host_port}"
                # Wait for Beets web server to become responsive (up to 90s for image pull + s6-overlay init)
                responsive = False
                for _ in range(90):
                    time.sleep(1)
                    try:
                        req = urllib.request.Request(f"{base_url}/stats")
                        with urllib.request.urlopen(req, timeout=2) as resp:
                            if resp.status == 200:
                                responsive = True
                                break
                    except Exception:
                        pass

                self.assertTrue(responsive, f"Timed out waiting for stock Beets container at {base_url}")

                # Step 1: Upstream Native Read Check
                req = urllib.request.Request(f"{base_url}/stats")
                with urllib.request.urlopen(req, timeout=5) as resp:
                    stats = json.loads(resp.read().decode("utf-8"))
                    self.assertIn("items", stats)

                # Step 2: WebManager Auth Check
                # Invalid token must return 401
                try:
                    req = urllib.request.Request(
                        f"{base_url}/webmanager/status",
                        headers={"Authorization": "Bearer 0000000000000000000000000000000000000000000000000000000000000000"},
                    )
                    urllib.request.urlopen(req, timeout=5)
                    self.fail("Expected 401 for wrong token")
                except urllib.error.HTTPError as ex:
                    self.assertEqual(ex.code, 401)

                # Valid token must return 200 OK with handshake schema
                auth_header = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
                req = urllib.request.Request(f"{base_url}/webmanager/status", headers=auth_header)
                with urllib.request.urlopen(req, timeout=5) as resp:
                    status_res = json.loads(resp.read().decode("utf-8"))
                    self.assertEqual(status_res["protocol_version"], "1.0")
                    self.assertEqual(status_res["plugin_version"], "0.1.0")
                    self.assertTrue(status_res["upstream_web_readonly"])
                    self.assertTrue(status_res["plugin_mutations_enabled"])
                    self.assertIn("import", status_res["capabilities"])

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
                    with urllib.request.urlopen(r, timeout=15) as resp_obj:
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
                with urllib.request.urlopen(req_op, timeout=5) as resp:
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
                    urllib.request.urlopen(req_conflict, timeout=15)
                    self.fail("Expected 409 Conflict for differing payload with same idempotency key")
                except urllib.error.HTTPError as ex:
                    self.assertEqual(ex.code, 409)

                # Step 6: Verify imported track in native upstream Beets Web API
                req_items = urllib.request.Request(f"{base_url}/item/")
                with urllib.request.urlopen(req_items, timeout=5) as resp:
                    items_res = json.loads(resp.read().decode("utf-8"))
                    items = items_res.get("items", [])
                    self.assertGreaterEqual(len(items), 1)
                    imported_item = items[0]
                    self.assertEqual(imported_item["title"], "Synthetic Anthem")
                    self.assertEqual(imported_item["artist"], "Acceptance Bot")
                    # Verify include_paths displays path in /music
                    self.assertIn("/music", imported_item.get("path", ""))

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
                with urllib.request.urlopen(req_mod, timeout=10) as resp:
                    mod_res = json.loads(resp.read().decode("utf-8"))
                    self.assertTrue(mod_res["success"])

                # Step 8: Verify mutation through native upstream GET /item/<id>
                req_verify = urllib.request.Request(f"{base_url}/item/{imported_item['id']}")
                with urllib.request.urlopen(req_verify, timeout=5) as resp:
                    ver_res = json.loads(resp.read().decode("utf-8"))
                    self.assertEqual(ver_res["genre"], "Synthesized Electro")

            finally:
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


if __name__ == "__main__":
    unittest.main()
