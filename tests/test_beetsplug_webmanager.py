"""Unit and integration tests for beetsplug.webmanager plugin."""

import os
import time
import tempfile
import json
import pytest
from unittest.mock import patch
import beets
from beets.library import Library, Item, Album
from beets import config as beets_config
from beetsplug.web import app as beets_web_app
from beetsplug.webmanager import WebManagerPlugin
from beetsplug.webmanager.schemas import (
    is_path_safe_and_allowed,
    is_strict_descendant,
    validate_fields,
    ALLOWED_ITEM_FIELDS,
    ALLOWED_ALBUM_FIELDS,
)
from beetsplug.webmanager.auth import set_api_key_file, get_expected_api_key, verify_token
from beetsplug.webmanager.compat import register_webmanager_blueprint
import beetsplug.webmanager.operations as ops_mod

TEST_64_HEX_TOKEN = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"


@pytest.fixture
def temp_dir():
    with tempfile.TemporaryDirectory() as td:
        yield td


@pytest.fixture
def dummy_lib(temp_dir):
    dbpath = os.path.join(temp_dir, "test_library.blb")
    lib = Library(dbpath, directory=temp_dir)
    yield lib
    try:
        lib._connection().close()
    except Exception:
        pass


@pytest.fixture
def test_client(dummy_lib, temp_dir):
    # Set up test 64-hex API key
    key_file = os.path.join(temp_dir, ".webmanager_api_key")
    with open(key_file, "w", encoding="utf-8") as f:
        f.write(TEST_64_HEX_TOKEN + "\n")

    # Initialize plugin
    plugin = WebManagerPlugin()
    set_api_key_file(key_file)
    ops_mod.set_allowed_roots([temp_dir, "/music", "/downloads"])

    beets_web_app.config["lib"] = dummy_lib
    beets_web_app.config["TESTING"] = True

    with beets_web_app.test_client() as client:
        yield client, TEST_64_HEX_TOKEN, dummy_lib, temp_dir

    ops_mod.set_allowed_roots(None)
    set_api_key_file(None)


def test_path_safe_and_allowed(temp_dir):
    allowed_roots = [temp_dir, "/music", "/downloads"]
    safe_path = os.path.join(temp_dir, "album", "track1.mp3")
    unsafe_path = os.path.join(temp_dir, "..", "etc", "shadow")
    nonexistent_root = "/var/secret/file.mp3"

    assert is_path_safe_and_allowed(safe_path, allowed_roots) is True
    assert is_path_safe_and_allowed(unsafe_path, allowed_roots) is False
    assert is_path_safe_and_allowed(nonexistent_root, allowed_roots) is False


def test_is_strict_descendant(temp_dir):
    music_dir = os.path.join(temp_dir, "music")
    downloads_dir = os.path.join(temp_dir, "downloads")
    os.makedirs(music_dir, exist_ok=True)
    os.makedirs(downloads_dir, exist_ok=True)

    allowed = [music_dir, downloads_dir]

    # Root itself must be rejected
    assert is_strict_descendant(downloads_dir, allowed) is False
    assert is_strict_descendant(music_dir, allowed) is False

    # Strict child is accepted
    child_album = os.path.join(downloads_dir, "album")
    assert is_strict_descendant(child_album, allowed) is True

    # Traversal up
    assert is_strict_descendant(os.path.join(downloads_dir, "..", "config"), allowed) is False
    assert is_strict_descendant(os.path.join(music_dir, "..", "..", "etc"), allowed) is False

    # Prefix confusion
    assert is_strict_descendant(os.path.join(temp_dir, "downloads2"), allowed) is False
    assert is_strict_descendant(os.path.join(temp_dir, "music-old"), allowed) is False

    # Null bytes & invalid
    assert is_strict_descendant(os.path.join(downloads_dir, "valid\x00bad"), allowed) is False
    assert is_strict_descendant("", allowed) is False
    assert is_strict_descendant(None, allowed) is False


def test_key_security_and_format_validation(temp_dir):
    """Verify 64-hex validation, symlink rejection, short/malformed keys fail closed."""
    valid_key = "a" * 64
    real_key_file = os.path.join(temp_dir, "valid.key")
    with open(real_key_file, "w", encoding="utf-8") as f:
        f.write(valid_key + "\n")

    try:
        set_api_key_file(real_key_file)
        assert get_expected_api_key() == valid_key
        assert verify_token(valid_key) is True

        # Short key (< 64 chars)
        short_file = os.path.join(temp_dir, "short.key")
        with open(short_file, "w", encoding="utf-8") as f:
            f.write("a" * 32 + "\n")
        set_api_key_file(short_file)
        assert get_expected_api_key() == ""

        # Non-hex characters (64 chars but contains 'z')
        non_hex_file = os.path.join(temp_dir, "non_hex.key")
        with open(non_hex_file, "w", encoding="utf-8") as f:
            f.write("z" * 64 + "\n")
        set_api_key_file(non_hex_file)
        assert get_expected_api_key() == ""

        # Symlink rejection
        try:
            symlink_file = os.path.join(temp_dir, "symlink.key")
            os.symlink(real_key_file, symlink_file)
            set_api_key_file(symlink_file)
            assert get_expected_api_key() == ""
        except (OSError, NotImplementedError):
            pass
    finally:
        set_api_key_file(None)


def test_field_validation():
    raw_item_fields = {
        "title": "Good Title",
        "artist": "Good Artist",
        "year": 2024,
        "malicious_sql_field": "DROP TABLE",
        "__class__": "invalid",
    }
    validated = validate_fields(raw_item_fields, is_album=False)
    assert "title" in validated
    assert "artist" in validated
    assert "year" in validated
    assert "malicious_sql_field" not in validated
    assert "__class__" not in validated


def test_auth_rejection(test_client):
    client, token, lib, temp_dir = test_client

    # No auth header
    res = client.get("/webmanager/status")
    assert res.status_code == 401
    assert res.get_json()["error_code"] == "UNAUTHORIZED"

    # Invalid token format
    res = client.get(
        "/webmanager/status",
        headers={"Authorization": "Bearer wrong_short_token"},
    )
    assert res.status_code == 401
    assert res.get_json()["error_code"] == "UNAUTHORIZED"


def test_status_endpoint(test_client):
    client, token, lib, temp_dir = test_client

    res = client.get(
        "/webmanager/status",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200
    data = res.get_json()
    assert data["protocol_version"] == "1.0"
    assert data["plugin_version"] == "0.1.0"
    assert data["upstream_web_readonly"] is True
    assert data["plugin_mutations_enabled"] is True
    assert "allowed_roots" not in data


def test_import_policy_validations(test_client):
    """Verify autotag=true rejected, root-self import rejected, invalid duplicate_action rejected."""
    client, token, lib, temp_dir = test_client
    auth = {"Authorization": f"Bearer {token}"}

    sample_album = os.path.join(temp_dir, "downloads", "album1")
    os.makedirs(sample_album, exist_ok=True)
    ops_mod.set_allowed_roots([os.path.join(temp_dir, "downloads")])

    # 1. autotag: True must be rejected with 400
    res = client.post(
        "/webmanager/import",
        headers=auth,
        json={"paths": [sample_album], "autotag": True},
    )
    assert res.status_code == 400
    assert res.get_json()["error_code"] == "AUTOTAG_NOT_ALLOWED"

    # 2. Root-self import must be rejected with 400
    res = client.post(
        "/webmanager/import",
        headers=auth,
        json={"paths": [os.path.join(temp_dir, "downloads")]},
    )
    assert res.status_code == 400
    assert res.get_json()["error_code"] == "PATH_NOT_ALLOWED"

    # 3. Invalid duplicate_action must be rejected with 400
    res = client.post(
        "/webmanager/import",
        headers=auth,
        json={"paths": [sample_album], "duplicate_action": "invalid_action"},
    )
    assert res.status_code == 400
    assert res.get_json()["error_code"] == "INVALID_DUPLICATE_ACTION"

    # 4. Non-existent path must be rejected with 400
    res = client.post(
        "/webmanager/import",
        headers=auth,
        json={"paths": [os.path.join(temp_dir, "downloads", "nonexistent")]},
    )
    assert res.status_code == 400
    assert res.get_json()["error_code"] == "SOURCE_NOT_FOUND"


def test_importer_config_snapshot_restoration(test_client):
    """Verify beets config is snapshot and restored even when an error occurs."""
    client, token, lib, temp_dir = test_client
    auth = {"Authorization": f"Bearer {token}"}

    sample_album = os.path.join(temp_dir, "downloads", "album1")
    os.makedirs(sample_album, exist_ok=True)
    ops_mod.set_allowed_roots([os.path.join(temp_dir, "downloads")])

    beets_config["import"]["quiet"] = False
    beets_config["import"]["timid"] = True
    beets_config["import"]["resume"] = True

    with patch("beets.importer.ImportSession.run", side_effect=RuntimeError("Simulated failure")):
        res = client.post(
            "/webmanager/import",
            headers=auth,
            json={"paths": [sample_album]},
        )
        assert res.status_code == 500
        assert res.get_json()["error_code"] == "IMPORT_FAILED"

    # Invariant: config values must be restored in finally
    assert beets_config["import"]["quiet"].get() is False
    assert beets_config["import"]["timid"].get() is True
    assert beets_config["import"]["resume"].get() is True


def test_error_sanitization_no_leak(test_client):
    """Verify raw exception text (paths, internal secrets) is never leaked in HTTP response."""
    client, token, lib, temp_dir = test_client
    auth = {"Authorization": f"Bearer {token}"}

    sample_album = os.path.join(temp_dir, "downloads", "album1")
    os.makedirs(sample_album, exist_ok=True)
    ops_mod.set_allowed_roots([os.path.join(temp_dir, "downloads")])

    secret_leak_message = "CRITICAL_SECRET_PASSWORD_999 at /etc/shadow/path"
    with patch("beets.importer.ImportSession.run", side_effect=Exception(secret_leak_message)):
        res = client.post(
            "/webmanager/import",
            headers=auth,
            json={"paths": [sample_album]},
        )
        assert res.status_code == 500
        body = res.get_json()
        assert secret_leak_message not in json.dumps(body)
        assert body["error"] == "Import failed"
        assert body["error_code"] == "IMPORT_FAILED"


def test_operation_registry_retention_and_public_schema():
    """Verify bounded retention, cleanup of expired operations, and public schema hiding _fingerprint."""
    op_id = "op-retention-test-1"
    ops_mod.register_operation("import", op_id, fingerprint="secret_fp_123")
    ops_mod.update_operation(op_id, "succeeded", result={"success": True})

    # Public schema must not expose _fingerprint
    public_record = ops_mod.get_operation(op_id)
    assert public_record is not None
    assert public_record["operation_id"] == op_id
    assert "_fingerprint" not in public_record
    assert "fingerprint" not in public_record
    assert public_record["status"] == "succeeded"

    # Simulate expired operation and trigger pruning
    with ops_mod._operations_lock:
        ops_mod._operations[op_id]["updated_at"] = time.time() - (ops_mod.DEFAULT_RETENTION_SECONDS + 10)
        ops_mod._prune_operations_locked()

    assert ops_mod.get_operation(op_id) is None


def test_modify_item(test_client):
    client, token, lib, temp_dir = test_client

    # Add item to library
    item = Item(
        title="Old Title",
        artist="Old Artist",
        album="Old Album",
        path=os.path.join(temp_dir, "track1.mp3").encode("utf-8"),
    )
    lib.add(item)
    lib._connection().commit()

    # Modify item
    res = client.post(
        "/webmanager/modify",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "item_ids": [item.id],
            "fields": {"title": "New Title", "genre": "Electronic"},
            "write": False,
            "move": False,
        },
    )
    assert res.status_code == 200
    data = res.get_json()
    assert data["success"] is True
    assert data["modified_items"] == 1

    # Verify changes
    refreshed = lib.get_item(item.id)
    assert refreshed.title == "New Title"
    assert refreshed.genre == "Electronic"


def test_idempotency_collision_safety(test_client):
    """Verify duplicate idempotency keys with matching payloads succeed, and differing payloads return 409 Conflict."""
    client, token, lib, temp_dir = test_client
    auth = {"Authorization": f"Bearer {token}", "Idempotency-Key": "idemp-unique-12345"}

    sample_album = os.path.join(temp_dir, "downloads", "album1")
    os.makedirs(sample_album, exist_ok=True)
    ops_mod.set_allowed_roots([os.path.join(temp_dir, "downloads")])

    # First request
    payload_a = {"paths": [sample_album], "autotag": False, "duplicate_action": "skip"}
    res_a = client.post("/webmanager/import", headers=auth, json=payload_a)
    assert res_a.status_code in (200, 202)

    # Replayed identical request (same key + same payload)
    res_replay = client.post("/webmanager/import", headers=auth, json=payload_a)
    assert res_replay.status_code in (200, 202)
    assert res_replay.get_json()["operation_id"] == "idemp-unique-12345"

    # Conflicting request (same key + DIFFERENT payload)
    payload_b = {"paths": [sample_album], "autotag": False, "duplicate_action": "remove"}
    res_b = client.post("/webmanager/import", headers=auth, json=payload_b)
    assert res_b.status_code == 409
    assert res_b.get_json()["error_code"] == "OPERATION_COLLISION"


def test_compat_failsafe_divergence():
    """Verify that if beetsplug.web or Flask app is incompatible, registration fails cleanly without raising."""
    class MockPlugin:
        pass

    # Should safely return boolean without raising
    res = register_webmanager_blueprint(MockPlugin())
    assert isinstance(res, bool)

