"""Unit and integration tests for beetsplug.webmanager plugin."""

import os
import time
import shutil
import tempfile
import json
import unittest
from unittest.mock import patch
import beets
from beets.library import Library, Item, Album
from beets import config as beets_config
from beetsplug.web import app as beets_web_app
from beetsplug.webmanager import WebManagerPlugin
from beetsplug.webmanager.schemas import (
    is_path_safe_and_allowed,
    is_strict_descendant,
    resolve_safe_descendant,
    validate_fields,
    ALLOWED_ITEM_FIELDS,
    ALLOWED_ALBUM_FIELDS,
)
from beetsplug.webmanager.auth import set_api_key_file, get_expected_api_key, verify_token
from beetsplug.webmanager.compat import register_webmanager_blueprint
import beetsplug.webmanager.operations as ops_mod

TEST_64_HEX_TOKEN = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"


class BeetsplugWebManagerTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.music_dir = os.path.join(self.td, "music")
        self.downloads_dir = os.path.join(self.td, "downloads")
        os.makedirs(self.music_dir, exist_ok=True)
        os.makedirs(self.downloads_dir, exist_ok=True)

        self.dbpath = os.path.join(self.td, "test_library.blb")
        self.lib = Library(self.dbpath, directory=self.td)

        self.key_file = os.path.join(self.td, ".webmanager_api_key")
        self.token = TEST_64_HEX_TOKEN
        with open(self.key_file, "w", encoding="utf-8") as f:
            f.write(self.token + "\n")

        self.plugin = WebManagerPlugin()
        set_api_key_file(self.key_file)
        ops_mod.set_allowed_roots([self.td, "/music", "/downloads", self.music_dir, self.downloads_dir])
        ops_mod.set_import_roots(None)

        beets_web_app.config["lib"] = self.lib
        beets_web_app.config["TESTING"] = True
        self.client = beets_web_app.test_client()

    def tearDown(self):
        ops_mod.set_allowed_roots(None)
        ops_mod.set_import_roots(None)
        set_api_key_file(None)
        try:
            beets_config["web"]["readonly"] = True
        except Exception:
            pass
        try:
            self.lib._connection().close()
        except Exception:
            pass
        shutil.rmtree(self.td, ignore_errors=True)

    def test_path_safe_and_allowed(self):
        allowed_roots = [self.td, "/music", "/downloads"]
        safe_path = os.path.join(self.td, "album", "track1.mp3")
        unsafe_path = os.path.join(self.td, "..", "etc", "shadow")
        nonexistent_root = "/var/secret/file.mp3"

        self.assertTrue(is_path_safe_and_allowed(safe_path, allowed_roots))
        self.assertFalse(is_path_safe_and_allowed(unsafe_path, allowed_roots))
        self.assertFalse(is_path_safe_and_allowed(nonexistent_root, allowed_roots))

    def test_is_strict_descendant(self):
        allowed = [self.music_dir, self.downloads_dir]

        # Root itself must be rejected
        self.assertFalse(is_strict_descendant(self.downloads_dir, allowed))
        self.assertFalse(is_strict_descendant(self.music_dir, allowed))

        # Strict child is accepted
        child_album = os.path.join(self.downloads_dir, "album")
        self.assertTrue(is_strict_descendant(child_album, allowed))

        # Traversal up
        self.assertFalse(is_strict_descendant(os.path.join(self.downloads_dir, "..", "config"), allowed))
        self.assertFalse(is_strict_descendant(os.path.join(self.music_dir, "..", "..", "etc"), allowed))

        # Prefix confusion
        self.assertFalse(is_strict_descendant(os.path.join(self.td, "downloads2"), allowed))
        self.assertFalse(is_strict_descendant(os.path.join(self.td, "music-old"), allowed))

        # Null bytes & invalid
        self.assertFalse(is_strict_descendant(os.path.join(self.downloads_dir, "valid\x00bad"), allowed))
        self.assertFalse(is_strict_descendant("", allowed))
        self.assertFalse(is_strict_descendant(None, allowed))

    def test_key_security_and_format_validation(self):
        """Verify 64-hex validation, symlink rejection, short/malformed keys fail closed."""
        valid_key = "a" * 64
        real_key_file = os.path.join(self.td, "valid.key")
        with open(real_key_file, "w", encoding="utf-8") as f:
            f.write(valid_key + "\n")

        try:
            set_api_key_file(real_key_file)
            self.assertEqual(get_expected_api_key(), valid_key)
            self.assertTrue(verify_token(valid_key))

            # Short key (< 64 chars)
            short_file = os.path.join(self.td, "short.key")
            with open(short_file, "w", encoding="utf-8") as f:
                f.write("a" * 32 + "\n")
            set_api_key_file(short_file)
            self.assertEqual(get_expected_api_key(), "")

            # Non-hex characters (64 chars but contains 'z')
            non_hex_file = os.path.join(self.td, "non_hex.key")
            with open(non_hex_file, "w", encoding="utf-8") as f:
                f.write("z" * 64 + "\n")
            set_api_key_file(non_hex_file)
            self.assertEqual(get_expected_api_key(), "")

            # Symlink rejection
            try:
                symlink_file = os.path.join(self.td, "symlink.key")
                os.symlink(real_key_file, symlink_file)
                set_api_key_file(symlink_file)
                self.assertEqual(get_expected_api_key(), "")
            except (OSError, NotImplementedError):
                pass
        finally:
            set_api_key_file(self.key_file)

    def test_field_validation(self):
        raw_item_fields = {
            "title": "Good Title",
            "artist": "Good Artist",
            "year": 2024,
            "malicious_sql_field": "DROP TABLE",
            "__class__": "invalid",
        }
        validated = validate_fields(raw_item_fields, is_album=False)
        self.assertIn("title", validated)
        self.assertIn("artist", validated)
        self.assertIn("year", validated)
        self.assertNotIn("malicious_sql_field", validated)
        self.assertNotIn("__class__", validated)

    def test_auth_rejection(self):
        # No auth header
        res = self.client.get("/webmanager/status")
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.get_json()["error_code"], "UNAUTHORIZED")

        # Invalid token format
        res = self.client.get(
            "/webmanager/status",
            headers={"Authorization": "Bearer wrong_short_token"},
        )
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.get_json()["error_code"], "UNAUTHORIZED")

    def test_status_endpoint(self):
        res = self.client.get(
            "/webmanager/status",
            headers={"Authorization": f"Bearer {self.token}"},
        )
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["protocol_version"], "1.0")
        self.assertEqual(data["plugin_version"], "1.0.0")
        self.assertTrue(data["plugin_mutations_enabled"])
        self.assertNotIn("allowed_roots", data)

    def test_version_consistency(self):
        """Plugin version must be identical in __init__.__version__, version.py, and status endpoint."""
        import beetsplug.webmanager
        from beetsplug.webmanager.version import PLUGIN_VERSION, PROTOCOL_VERSION

        self.assertEqual(beetsplug.webmanager.__version__, PLUGIN_VERSION)
        self.assertEqual(PLUGIN_VERSION, "1.0.0")
        self.assertEqual(PROTOCOL_VERSION, "1.0")

        res = self.client.get(
            "/webmanager/status",
            headers={"Authorization": f"Bearer {self.token}"},
        )
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["plugin_version"], PLUGIN_VERSION)
        self.assertEqual(data["protocol_version"], PROTOCOL_VERSION)

    def test_status_endpoint_reports_actual_readonly_true(self):
        """upstream_web_readonly must reflect the real beets_config['web']['readonly'] value, not a hardcoded constant."""
        beets_config["web"]["readonly"] = True
        res = self.client.get(
            "/webmanager/status",
            headers={"Authorization": f"Bearer {self.token}"},
        )
        self.assertTrue(res.get_json()["upstream_web_readonly"])

    def test_status_endpoint_reports_actual_readonly_false(self):
        """When Beets' own config says readonly: no, status must report False, not a hidden/hardcoded True."""
        beets_config["web"]["readonly"] = False
        res = self.client.get(
            "/webmanager/status",
            headers={"Authorization": f"Bearer {self.token}"},
        )
        self.assertFalse(res.get_json()["upstream_web_readonly"])

    def test_import_roots_separate_from_allowed_roots(self):
        """import_roots must gate /webmanager/import; allowed_roots (which includes /music) must not."""
        ops_mod.set_import_roots([self.downloads_dir])
        auth = {"Authorization": f"Bearer {self.token}"}

        # ACCEPT: a strict child of the configured import root
        accepted_album = os.path.join(self.downloads_dir, "Artist - Album")
        os.makedirs(accepted_album, exist_ok=True)
        res = self.client.post("/webmanager/import", headers=auth, json={"paths": [accepted_album]})
        self.assertIn(res.status_code, (200, 202))

        # REJECT: the import root itself
        res = self.client.post("/webmanager/import", headers=auth, json={"paths": [self.downloads_dir]})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.get_json()["error_code"], "PATH_NOT_ALLOWED")

        # REJECT: /music is an allowed_root (per setUp) but is NOT an import_root --
        # it must never become a valid import source just because it's allowed elsewhere.
        music_child = os.path.join(self.music_dir, "Some Album")
        os.makedirs(music_child, exist_ok=True)
        res = self.client.post("/webmanager/import", headers=auth, json={"paths": [music_child]})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.get_json()["error_code"], "PATH_NOT_ALLOWED")

        # REJECT: prefix confusion (a sibling directory that merely starts with the same prefix)
        confused_dir = self.downloads_dir + "2"
        os.makedirs(os.path.join(confused_dir, "album"), exist_ok=True)
        res = self.client.post(
            "/webmanager/import", headers=auth, json={"paths": [os.path.join(confused_dir, "album")]}
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.get_json()["error_code"], "PATH_NOT_ALLOWED")

        # REJECT: path traversal out of the import root
        traversal_path = os.path.join(self.downloads_dir, "..", "music")
        res = self.client.post("/webmanager/import", headers=auth, json={"paths": [traversal_path]})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.get_json()["error_code"], "PATH_NOT_ALLOWED")

        # REJECT: a symlink under the import root that resolves outside it
        outside_target = os.path.join(self.td, "outside_target")
        os.makedirs(outside_target, exist_ok=True)
        symlink_path = os.path.join(self.downloads_dir, "escape_link")
        try:
            os.symlink(outside_target, symlink_path)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks not supported on this platform")
        self.assertIsNone(resolve_safe_descendant(symlink_path, [self.downloads_dir]))

    def test_import_policy_validations(self):
        """Verify autotag=true rejected, root-self import rejected, invalid duplicate_action rejected."""
        auth = {"Authorization": f"Bearer {self.token}"}
        sample_album = os.path.join(self.downloads_dir, "album1")
        os.makedirs(sample_album, exist_ok=True)
        ops_mod.set_import_roots([self.downloads_dir])

        # 1. autotag: True must be rejected with 400
        res = self.client.post(
            "/webmanager/import",
            headers=auth,
            json={"paths": [sample_album], "autotag": True},
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.get_json()["error_code"], "AUTOTAG_NOT_ALLOWED")

        # 2. Root-self import must be rejected with 400
        res = self.client.post(
            "/webmanager/import",
            headers=auth,
            json={"paths": [self.downloads_dir]},
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.get_json()["error_code"], "PATH_NOT_ALLOWED")

        # 3. Invalid duplicate_action must be rejected with 400
        res = self.client.post(
            "/webmanager/import",
            headers=auth,
            json={"paths": [sample_album], "duplicate_action": "invalid_action"},
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.get_json()["error_code"], "INVALID_DUPLICATE_ACTION")

        # 4. Non-existent path must be rejected with 400
        res = self.client.post(
            "/webmanager/import",
            headers=auth,
            json={"paths": [os.path.join(self.downloads_dir, "nonexistent")]},
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.get_json()["error_code"], "SOURCE_NOT_FOUND")

    def test_importer_config_snapshot_restoration(self):
        """Verify beets config is snapshot and restored even when an error occurs."""
        auth = {"Authorization": f"Bearer {self.token}"}
        sample_album = os.path.join(self.downloads_dir, "album1")
        os.makedirs(sample_album, exist_ok=True)
        ops_mod.set_import_roots([self.downloads_dir])

        beets_config["import"]["quiet"] = False
        beets_config["import"]["timid"] = True
        beets_config["import"]["resume"] = True

        with patch("beets.importer.ImportSession.run", side_effect=RuntimeError("Simulated failure")):
            res = self.client.post(
                "/webmanager/import",
                headers=auth,
                json={"paths": [sample_album]},
            )
            self.assertEqual(res.status_code, 500)
            self.assertEqual(res.get_json()["error_code"], "IMPORT_FAILED")

        # Invariant: config values must be restored in finally
        self.assertFalse(beets_config["import"]["quiet"].get())
        self.assertTrue(beets_config["import"]["timid"].get())
        self.assertTrue(beets_config["import"]["resume"].get())

    def test_error_sanitization_no_leak(self):
        """Verify raw exception text (paths, internal secrets) is never leaked in HTTP response."""
        auth = {"Authorization": f"Bearer {self.token}"}
        sample_album = os.path.join(self.downloads_dir, "album1")
        os.makedirs(sample_album, exist_ok=True)
        ops_mod.set_import_roots([self.downloads_dir])

        secret_leak_message = "CRITICAL_SECRET_PASSWORD_999 at /etc/shadow/path"
        with patch("beets.importer.ImportSession.run", side_effect=Exception(secret_leak_message)):
            res = self.client.post(
                "/webmanager/import",
                headers=auth,
                json={"paths": [sample_album]},
            )
            self.assertEqual(res.status_code, 500)
            body = res.get_json()
            self.assertNotIn(secret_leak_message, json.dumps(body))
            self.assertEqual(body["error"], "Import failed")
            self.assertEqual(body["error_code"], "IMPORT_FAILED")

    def test_operation_registry_retention_and_public_schema(self):
        """Verify bounded retention, cleanup of expired operations, and public schema hiding _fingerprint."""
        op_id = "op-retention-test-1"
        ops_mod.register_operation("import", op_id, fingerprint="secret_fp_123")
        ops_mod.update_operation(op_id, "succeeded", result={"success": True})

        # Public schema must not expose _fingerprint
        public_record = ops_mod.get_operation(op_id)
        self.assertIsNotNone(public_record)
        self.assertEqual(public_record["operation_id"], op_id)
        self.assertNotIn("_fingerprint", public_record)
        self.assertNotIn("fingerprint", public_record)
        self.assertEqual(public_record["status"], "succeeded")

        # Simulate expired operation and trigger pruning
        with ops_mod._operations_lock:
            ops_mod._operations[op_id]["updated_at"] = time.time() - (ops_mod.DEFAULT_RETENTION_SECONDS + 10)
            ops_mod._prune_operations_locked()

        self.assertIsNone(ops_mod.get_operation(op_id))

    def test_modify_item(self):
        # Add item to library
        item = Item(
            title="Old Title",
            artist="Old Artist",
            album="Old Album",
            path=os.path.join(self.td, "track1.mp3").encode("utf-8"),
        )
        self.lib.add(item)
        self.lib._connection().commit()

        # Modify item
        res = self.client.post(
            "/webmanager/modify",
            headers={"Authorization": f"Bearer {self.token}"},
            json={
                "item_ids": [item.id],
                "fields": {"title": "New Title", "genre": "Electronic"},
                "write": False,
                "move": False,
            },
        )
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["success"])
        self.assertEqual(data["modified_items"], 1)

        # Verify changes
        refreshed = self.lib.get_item(item.id)
        self.assertEqual(refreshed.title, "New Title")
        self.assertEqual(refreshed.genre, "Electronic")

    def test_idempotency_collision_safety(self):
        """Verify duplicate idempotency keys with matching payloads succeed, and differing payloads return 409 Conflict."""
        auth = {"Authorization": f"Bearer {self.token}", "Idempotency-Key": "idemp-unique-12345"}
        sample_album = os.path.join(self.downloads_dir, "album1")
        os.makedirs(sample_album, exist_ok=True)
        ops_mod.set_import_roots([self.downloads_dir])

        # First request
        payload_a = {"paths": [sample_album], "autotag": False, "duplicate_action": "skip"}
        res_a = self.client.post("/webmanager/import", headers=auth, json=payload_a)
        self.assertIn(res_a.status_code, (200, 202))

        # Replayed identical request (same key + same payload)
        res_replay = self.client.post("/webmanager/import", headers=auth, json=payload_a)
        self.assertIn(res_replay.status_code, (200, 202))
        self.assertEqual(res_replay.get_json()["operation_id"], "idemp-unique-12345")

        # Conflicting request (same key + DIFFERENT payload)
        payload_b = {"paths": [sample_album], "autotag": False, "duplicate_action": "remove"}
        res_b = self.client.post("/webmanager/import", headers=auth, json=payload_b)
        self.assertEqual(res_b.status_code, 409)
        self.assertEqual(res_b.get_json()["error_code"], "OPERATION_COLLISION")

    def test_compat_failsafe_divergence(self):
        """Verify that if beetsplug.web or Flask app is incompatible, registration fails cleanly without raising."""
        class MockPlugin:
            pass

        # Should safely return boolean without raising
        res = register_webmanager_blueprint(MockPlugin())
        self.assertIsInstance(res, bool)


class WebManagerPhase3MutationTests(unittest.TestCase):
    """Phase 3: remove/move (core, always available) and mbsync/fetchart/
    embedart/lastgenre (plugin-gated, dynamic capability handshake)."""

    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.music_dir = os.path.join(self.td, "music")
        os.makedirs(self.music_dir, exist_ok=True)

        self.dbpath = os.path.join(self.td, "test_library.blb")
        self.lib = Library(self.dbpath, directory=self.music_dir)

        self.key_file = os.path.join(self.td, ".webmanager_api_key")
        self.token = TEST_64_HEX_TOKEN
        with open(self.key_file, "w", encoding="utf-8") as f:
            f.write(self.token + "\n")

        self.plugin = WebManagerPlugin()
        set_api_key_file(self.key_file)

        beets_web_app.config["lib"] = self.lib
        beets_web_app.config["TESTING"] = True
        self.client = beets_web_app.test_client()
        self.auth = {"Authorization": f"Bearer {self.token}"}

        import beets.plugins as beets_plugins_mod
        self._plugins_mod = beets_plugins_mod
        self._saved_instances = list(beets_plugins_mod._instances)
        beets_plugins_mod._instances.clear()

    def tearDown(self):
        self._plugins_mod._instances[:] = self._saved_instances
        set_api_key_file(None)
        try:
            self.lib._connection().close()
        except Exception:
            pass
        shutil.rmtree(self.td, ignore_errors=True)

    def _add_item(self, filename="track1.mp3", with_album=False, **overrides):
        path = os.path.join(self.music_dir, filename)
        with open(path, "wb") as f:
            f.write(b"FAKE_AUDIO_BYTES")
        fields = dict(
            title="Test Track",
            artist="Test Artist",
            album="Test Album",
            albumartist="Test Artist",
            path=path.encode("utf-8"),
        )
        fields.update(overrides)
        item = Item(**fields)
        if with_album:
            self.lib.add_album([item])
        else:
            self.lib.add(item)
        self.lib._connection().commit()
        return item, path

    # ---- remove ----

    def test_remove_requires_explicit_ids(self):
        res = self.client.post("/webmanager/remove", headers=self.auth, json={})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.get_json()["error_code"], "MISSING_TARGET")

    def test_remove_defaults_to_no_file_deletion(self):
        item, path = self._add_item()
        self.assertTrue(os.path.exists(path))
        res = self.client.post("/webmanager/remove", headers=self.auth, json={"item_ids": [item.id]})
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertTrue(body["success"])
        self.assertEqual(body["removed_items"], 1)
        self.assertFalse(body["delete_files"])
        self.assertIsNone(self.lib.get_item(item.id))
        # File must survive -- delete_files defaulted to False.
        self.assertTrue(os.path.exists(path))

    def test_remove_with_delete_files_true_deletes_physical_file(self):
        item, path = self._add_item()
        res = self.client.post(
            "/webmanager/remove",
            headers=self.auth,
            json={"item_ids": [item.id], "delete_files": True},
        )
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json()["delete_files"])
        self.assertFalse(os.path.exists(path))

    def test_remove_missing_id_reported_not_silently_ignored(self):
        res = self.client.post("/webmanager/remove", headers=self.auth, json={"item_ids": [999999]})
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertEqual(body["removed_items"], 0)
        self.assertIn(999999, body["missing_item_ids"])

    def test_remove_idempotency_collision(self):
        item, _ = self._add_item()
        headers = {**self.auth, "Idempotency-Key": "remove-key-1"}
        payload_a = {"item_ids": [item.id]}
        res_a = self.client.post("/webmanager/remove", headers=headers, json=payload_a)
        self.assertEqual(res_a.status_code, 200)

        # Same idempotency key, genuinely different payload (differing
        # delete_files) -- must collide regardless of item.id, which SQLite
        # can reuse after a delete, so the payload difference must not
        # depend on a second freshly-inserted row's id.
        payload_b = {"item_ids": [item.id], "delete_files": True}
        res_b = self.client.post("/webmanager/remove", headers=headers, json=payload_b)
        self.assertEqual(res_b.status_code, 409)
        self.assertEqual(res_b.get_json()["error_code"], "OPERATION_COLLISION")

    # ---- move ----

    def test_move_requires_explicit_ids(self):
        res = self.client.post("/webmanager/move", headers=self.auth, json={})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.get_json()["error_code"], "MISSING_TARGET")

    def test_move_relocates_file_under_configured_directory(self):
        item, old_path = self._add_item(filename="messy name.mp3")
        res = self.client.post("/webmanager/move", headers=self.auth, json={"item_ids": [item.id]})
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertTrue(body["success"])
        self.assertEqual(body["moved_items"], 1)
        refreshed = self.lib.get_item(item.id)
        new_path = refreshed.path.decode("utf-8") if isinstance(refreshed.path, bytes) else refreshed.path
        self.assertTrue(new_path.startswith(self.music_dir))

    # ---- capability handshake ----

    def test_status_reports_plugin_gated_capabilities_as_unavailable_by_default(self):
        res = self.client.get("/webmanager/status", headers=self.auth)
        caps = res.get_json()["capabilities"]
        self.assertIn("remove", caps)
        self.assertIn("move", caps)
        for gated in ("mbsync", "fetchart", "embedart", "lastgenre"):
            self.assertNotIn(gated, caps)

    def test_status_reports_plugin_gated_capability_available_when_loaded(self):
        from beetsplug.mbsync import MBSyncPlugin

        self._plugins_mod._instances.append(MBSyncPlugin())
        res = self.client.get("/webmanager/status", headers=self.auth)
        self.assertIn("mbsync", res.get_json()["capabilities"])

    def test_mbsync_endpoint_returns_stable_error_when_capability_unavailable(self):
        res = self.client.post("/webmanager/mbsync", headers=self.auth, json={"item_ids": [1]})
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.get_json()["error_code"], "CAPABILITY_UNAVAILABLE")

    def test_fetchart_endpoint_returns_stable_error_when_capability_unavailable(self):
        res = self.client.post("/webmanager/fetchart", headers=self.auth, json={"album_ids": [1]})
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.get_json()["error_code"], "CAPABILITY_UNAVAILABLE")

    def test_embedart_endpoint_returns_stable_error_when_capability_unavailable(self):
        res = self.client.post("/webmanager/embedart", headers=self.auth, json={"album_ids": [1]})
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.get_json()["error_code"], "CAPABILITY_UNAVAILABLE")

    def test_lastgenre_endpoint_returns_stable_error_when_capability_unavailable(self):
        res = self.client.post("/webmanager/lastgenre", headers=self.auth, json={"album_ids": [1]})
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.get_json()["error_code"], "CAPABILITY_UNAVAILABLE")

    def test_mbsync_missing_ids_rejected_before_any_operation_registered(self):
        from beetsplug.mbsync import MBSyncPlugin

        self._plugins_mod._instances.append(MBSyncPlugin())
        res = self.client.post("/webmanager/mbsync", headers=self.auth, json={})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.get_json()["error_code"], "INVALID_REQUEST")

    def test_mbsync_runs_against_real_plugin_and_skips_items_without_mbid(self):
        from beetsplug.mbsync import MBSyncPlugin

        self._plugins_mod._instances.append(MBSyncPlugin())
        item, _ = self._add_item(mb_trackid="")
        res = self.client.post(
            "/webmanager/mbsync", headers=self.auth, json={"item_ids": [item.id]}
        )
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertEqual(body["status"], "succeeded")
        self.assertEqual(body["result"]["requested_items"], 1)
        self.assertEqual(body["result"]["skipped_items"], 1)
        self.assertEqual(body["result"]["processed_items"], 0)
        self.assertEqual(body["result"]["changed_items"], 0)
        self.assertEqual(body["result"]["synced_items"], 0)

    def test_mbsync_album_and_singleton_target_queries(self):
        """Prove that mbsync targets exact item id and album id (not album_id)."""
        from beetsplug.mbsync import MBSyncPlugin

        self._plugins_mod._instances.append(MBSyncPlugin())
        singleton, _ = self._add_item(mb_trackid="track-mbid-1234")
        singleton.singleton = True
        singleton.store()

        album_item, path = self._add_item(with_album=True, mb_trackid="track-mbid-5678")
        album = self.lib.get_album(album_item.album_id)
        album.mb_albumid = "album-mbid-9999"
        album.store()

        res = self.client.post(
            "/webmanager/mbsync",
            headers=self.auth,
            json={"item_ids": [singleton.id], "album_ids": [album.id]},
        )
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertEqual(body["status"], "succeeded")
        self.assertEqual(body["result"]["requested_items"], 1)
        self.assertEqual(body["result"]["requested_albums"], 1)
        self.assertEqual(body["result"]["skipped_items"], 0)
        self.assertEqual(body["result"]["skipped_albums"], 0)

    def test_fetchart_runs_against_real_plugin_with_filesystem_source(self):
        """Real, deterministic, network-free acceptance: fetchart's default
        `filesystem` source picks up a local cover file already present in
        the album directory -- no network call involved."""
        from beetsplug.fetchart import FetchArtPlugin

        item, path = self._add_item(with_album=True)
        album_dir = os.path.dirname(path)
        with open(os.path.join(album_dir, "cover.jpg"), "wb") as f:
            f.write(b"\xff\xd8\xff\xe0FAKEJPEGDATA")

        beets_config["fetchart"]["sources"] = ["filesystem"]
        self._plugins_mod._instances.append(FetchArtPlugin())

        album = self.lib.get_album(item.album_id) if item.album_id else None
        self.assertIsNotNone(album, "item must have an album for fetchart to target")

        res = self.client.post(
            "/webmanager/fetchart", headers=self.auth, json={"album_ids": [album.id]}
        )
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertEqual(body["status"], "succeeded")
        self.assertEqual(body["result"]["albums_with_art"], 1)
        refreshed_album = self.lib.get_album(album.id)
        self.assertTrue(refreshed_album.artpath)

    def test_lastgenre_missing_album_reported_not_silently_ignored(self):
        try:
            from beetsplug.lastgenre import LastGenrePlugin
        except ImportError as ex:
            self.skipTest(f"lastgenre plugin dependency not installed in this environment: {ex}")

        self._plugins_mod._instances.append(LastGenrePlugin())
        res = self.client.post(
            "/webmanager/lastgenre", headers=self.auth, json={"album_ids": [999999]}
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["result"]["processed_albums"], 0)


if __name__ == "__main__":
    unittest.main()

