"""Unit and integration tests for beetsplug.webmanager plugin."""

import os
import tempfile
import json
import pytest
import beets
from beets.library import Library, Item, Album
from beetsplug.web import app as beets_web_app
from beetsplug.webmanager import WebManagerPlugin
from beetsplug.webmanager.schemas import (
    is_path_safe_and_allowed,
    validate_fields,
    ALLOWED_ITEM_FIELDS,
    ALLOWED_ALBUM_FIELDS,
)
from beetsplug.webmanager.auth import set_api_key_file, get_expected_api_key, verify_token
from beetsplug.webmanager.compat import register_webmanager_blueprint
import beetsplug.webmanager.operations as ops_mod


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
    # Set up test API key
    key_file = os.path.join(temp_dir, ".webmanager_api_key")
    with open(key_file, "w", encoding="utf-8") as f:
        f.write("test_secret_token_12345\n")
    set_api_key_file(key_file)
    ops_mod.set_allowed_roots([temp_dir, "/music", "/downloads"])

    # Initialize plugin
    plugin = WebManagerPlugin()
    beets_web_app.config["lib"] = dummy_lib
    beets_web_app.config["TESTING"] = True

    with beets_web_app.test_client() as client:
        yield client, "test_secret_token_12345", dummy_lib, temp_dir

    ops_mod.set_allowed_roots(None)


def test_path_safe_and_allowed(temp_dir):
    allowed_roots = [temp_dir, "/music", "/downloads"]
    safe_path = os.path.join(temp_dir, "album", "track1.mp3")
    unsafe_path = os.path.join(temp_dir, "..", "etc", "shadow")
    nonexistent_root = "/var/secret/file.mp3"

    assert is_path_safe_and_allowed(safe_path, allowed_roots) is True
    assert is_path_safe_and_allowed(unsafe_path, allowed_roots) is False
    assert is_path_safe_and_allowed(nonexistent_root, allowed_roots) is False


def test_adversarial_path_containment(temp_dir):
    """Specifically test adversarial directory traversal, prefix confusion, and null bytes."""
    music_dir = os.path.join(temp_dir, "music")
    downloads_dir = os.path.join(temp_dir, "downloads")
    secret_dir = os.path.join(temp_dir, "secret")
    os.makedirs(music_dir, exist_ok=True)
    os.makedirs(downloads_dir, exist_ok=True)
    os.makedirs(secret_dir, exist_ok=True)

    allowed = [music_dir, downloads_dir]

    # Traversal up
    assert is_path_safe_and_allowed(os.path.join(downloads_dir, "..", "config"), allowed) is False
    assert is_path_safe_and_allowed(os.path.join(music_dir, "..", "..", "etc"), allowed) is False

    # Prefix confusion
    music_old = os.path.join(temp_dir, "music-old")
    downloads2 = os.path.join(temp_dir, "downloads2")
    assert is_path_safe_and_allowed(music_old, allowed) is False
    assert is_path_safe_and_allowed(downloads2, allowed) is False

    # Null bytes
    assert is_path_safe_and_allowed(os.path.join(music_dir, "valid\x00malicious.mp3"), allowed) is False

    # Non-string or empty
    assert is_path_safe_and_allowed("", allowed) is False
    assert is_path_safe_and_allowed(None, allowed) is False


def test_key_security_and_symlink_rejection(test_client, temp_dir):
    """Verify symlinked, empty, or short API key files fail closed."""
    client, token, lib, _ = test_client
    real_key_file = os.path.join(temp_dir, "real_key.txt")
    with open(real_key_file, "w", encoding="utf-8") as f:
        f.write("a" * 32 + "\n")

    try:
        # Short key rejection (< 16 chars)
        short_key_file = os.path.join(temp_dir, "short.key")
        with open(short_key_file, "w", encoding="utf-8") as f:
            f.write("short\n")
        set_api_key_file(short_key_file)
        assert get_expected_api_key() == ""

        # Empty key rejection
        empty_key_file = os.path.join(temp_dir, "empty.key")
        with open(empty_key_file, "w", encoding="utf-8") as f:
            f.write("\n")
        set_api_key_file(empty_key_file)
        assert get_expected_api_key() == ""

        # Symlink rejection
        try:
            symlink_file = os.path.join(temp_dir, "symlink.key")
            os.symlink(real_key_file, symlink_file)
            set_api_key_file(symlink_file)
            assert get_expected_api_key() == ""
        except (OSError, NotImplementedError):
            pass  # Windows unprivileged symlink permissions may skip
    finally:
        # Restore valid key
        orig_key_file = os.path.join(temp_dir, ".webmanager_api_key")
        set_api_key_file(orig_key_file)


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
    assert "Unauthorized" in res.get_json()["error"]

    # Invalid token
    res = client.get(
        "/webmanager/status",
        headers={"Authorization": "Bearer wrong_token_value"},
    )
    assert res.status_code == 401


def test_status_endpoint(test_client):
    client, token, lib, temp_dir = test_client

    res = client.get(
        "/webmanager/status",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200
    data = res.get_json()
    assert data["status"] == "ok"
    assert data["version"] == "1.0.0"
    assert data["readonly"] is False


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


def test_remove_item(test_client):
    client, token, lib, temp_dir = test_client

    item = Item(
        title="To Remove",
        artist="Artist",
        path=os.path.join(temp_dir, "remove.mp3").encode("utf-8"),
    )
    lib.add(item)
    lib._connection().commit()

    res = client.post(
        "/webmanager/remove",
        headers={"Authorization": f"Bearer {token}"},
        json={"item_ids": [item.id], "delete_files": False},
    )
    assert res.status_code == 200
    assert res.get_json()["removed_items"] == 1
    assert lib.get_item(item.id) is None


def test_merge_albums(test_client):
    client, token, lib, temp_dir = test_client

    # Create target album and item
    target_album = Album(album="Target Album", albumartist="Main Artist")
    lib.add(target_album)
    item1 = Item(
        title="Track 1",
        artist="Main Artist",
        album_id=target_album.id,
        path=os.path.join(temp_dir, "t1.mp3").encode("utf-8"),
    )
    lib.add(item1)

    # Create source album and item
    source_album = Album(album="Source Album", albumartist="Main Artist")
    lib.add(source_album)
    item2 = Item(
        title="Track 2",
        artist="Main Artist",
        album_id=source_album.id,
        path=os.path.join(temp_dir, "t2.mp3").encode("utf-8"),
    )
    lib.add(item2)
    lib._connection().commit()

    # Perform merge
    res = client.post(
        "/webmanager/merge-album",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "target_album_id": target_album.id,
            "source_album_ids": [source_album.id],
            "track_reassignments": {str(item2.id): {"disc": 1, "track": 2}},
            "write": False,
            "move": False,
        },
    )
    assert res.status_code == 200
    data = res.get_json()
    assert data["success"] is True
    assert data["transferred_tracks"] == 1
    assert data["merged_source_albums"] == 1

    # Verify source album is gone and item2 now belongs to target album
    assert lib.get_album(source_album.id) is None
    refreshed_item2 = lib.get_item(item2.id)
    assert refreshed_item2.album_id == target_album.id
    assert refreshed_item2.track == 2


def test_idempotency_collision_safety(test_client):
    """Verify duplicate idempotency keys with matching payloads succeed, and differing payloads return 409 Conflict."""
    client, token, lib, temp_dir = test_client
    auth = {"Authorization": f"Bearer {token}", "Idempotency-Key": "idemp-unique-12345"}

    # First request
    payload_a = {"paths": [temp_dir], "autotag": False, "duplicate_action": "skip"}
    res_a = client.post("/webmanager/import", headers=auth, json=payload_a)
    assert res_a.status_code in (200, 202)

    # Replayed identical request (same key + same payload)
    res_replay = client.post("/webmanager/import", headers=auth, json=payload_a)
    assert res_replay.status_code in (200, 202)
    assert res_replay.get_json()["operation_id"] == "idemp-unique-12345"

    # Conflicting request (same key + DIFFERENT payload)
    payload_b = {"paths": [temp_dir], "autotag": True, "duplicate_action": "remove"}
    res_b = client.post("/webmanager/import", headers=auth, json=payload_b)
    assert res_b.status_code == 409
    assert "collision" in res_b.get_json()["error"].lower()


def test_compat_failsafe_divergence():
    """Verify that if beetsplug.web or Flask app is incompatible, registration fails cleanly without raising."""
    class MockPlugin:
        pass

    # Should safely return boolean without raising
    res = register_webmanager_blueprint(MockPlugin())
    assert isinstance(res, bool)
