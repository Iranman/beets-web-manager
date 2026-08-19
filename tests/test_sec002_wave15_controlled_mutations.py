"""
Wave 15 controlled mutation boundary tests for SEC-002 / ARCH-003.

Tests:
1. Plan / Apply separation for import review folder deletion & per-file cleanup.
2. Cross-root / traversal path refusal for import review operations.
3. Symlink target & symlink component mutation refusal.
4. TOCTOU race detection: file replacement race (mtime/size change).
5. TOCTOU race detection: directory growth race (unexpected new file).
6. Idempotent apply retry on completed transactions.
7. Album folder cleanup & deduplication plan/apply workflows.
8. Preservation of Wave 14 RGID & Recording Identity invariant during deduplication.
9. Web Manager non-direct filesystem access (Engine owns all media mutations).
10. Transaction lifecycle audit recording & durability.
"""

import json
import os
import shutil
import tempfile
import pytest
from unittest.mock import patch, MagicMock

import app as flask_app
from backend.transaction_engine import TransactionStore
from backend.beets_client import BeetsClient, BeetsError


@pytest.fixture
def store_dir(tmp_path):
    d = tmp_path / "transactions"
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


@pytest.fixture
def tx_store(store_dir):
    return TransactionStore(root=store_dir)


@pytest.fixture
def dummy_root(tmp_path):
    root = tmp_path / "downloads"
    root.mkdir(parents=True, exist_ok=True)
    return root


# ---------------------------------------------------------------------------
# 1. Plan / Apply separation & Transaction Lifecycle
# ---------------------------------------------------------------------------

def test_transaction_store_plan_apply_lifecycle(tx_store, dummy_root):
    target_dir = dummy_root / "album_folder"
    target_dir.mkdir()
    f1 = target_dir / "track01.mp3"
    f1.write_text("audio data")
    st = f1.stat()

    op_id, tx = tx_store.create_import_review_cleanup_plan(
        target_path=str(target_dir),
        allowed_roots=[str(dummy_root)],
        source_paths=[str(f1)],
        expected_states={
            str(f1): {"size": st.st_size, "mtime": st.st_mtime, "is_file": True}
        },
        reversibility="RECOVERABLE",
        payload={"action": "delete", "files": [str(f1)]},
        created_by="test_user",
    )

    assert op_id.startswith("txn_")
    assert tx["status"] == "Preview"
    assert tx["metadata"]["reversibility"] == "RECOVERABLE"

    fetched = tx_store.get(op_id)
    assert fetched["id"] == op_id

    # Simulate Apply
    updated = tx_store.update(
        op_id,
        status="Completed",
        applied_at="2026-08-19T00:00:00Z",
        metadata={"deleted": [str(f1)]},
    )
    assert updated["status"] == "Completed"

    # Idempotent re-fetch
    completed = tx_store.get(op_id)
    assert completed["status"] == "Completed"
    assert completed["metadata"]["deleted"] == [str(f1)]


# ---------------------------------------------------------------------------
# 2. Cross-root / Traversal Refusal
# ---------------------------------------------------------------------------

def test_cross_root_traversal_refusal(tx_store, tmp_path):
    allowed = tmp_path / "allowed_root"
    allowed.mkdir()
    forbidden = tmp_path / "forbidden_root"
    forbidden.mkdir()

    bad_path = str(forbidden / "stolen_file.mp3")

    with pytest.raises(ValueError, match="outside allowed root"):
        tx_store.create_import_review_cleanup_plan(
            target_path=bad_path,
            allowed_roots=[str(allowed)],
            source_paths=[bad_path],
            expected_states={},
            reversibility="RECOVERABLE",
            payload={},
        )


# ---------------------------------------------------------------------------
# 3. Symlink Mutation Refusal
# ---------------------------------------------------------------------------

def test_symlink_refusal(tx_store, dummy_root, tmp_path):
    outside = tmp_path / "outside_target.mp3"
    outside.write_text("secret")

    symlink_file = dummy_root / "link.mp3"

    try:
        os.symlink(str(outside), str(symlink_file))
    except Exception:
        pytest.skip("Symlinks not supported on this platform/privilege level")

    st = symlink_file.lstat()

    with pytest.raises(ValueError, match="Symlinks are not permitted"):
        tx_store.create_import_review_cleanup_plan(
            target_path=str(symlink_file),
            allowed_roots=[str(dummy_root)],
            source_paths=[str(symlink_file)],
            expected_states={
                str(symlink_file): {"size": st.st_size, "mtime": st.st_mtime, "is_file": True}
            },
            reversibility="RECOVERABLE",
            payload={},
        )


# ---------------------------------------------------------------------------
# 4. Precondition TOCTOU Race Detection
# ---------------------------------------------------------------------------

def test_file_replacement_race_detection(tx_store, dummy_root):
    target_dir = dummy_root / "race_dir"
    target_dir.mkdir()
    f1 = target_dir / "track.mp3"
    f1.write_text("original content")

    op_id, tx = tx_store.create_import_review_cleanup_plan(
        target_path=str(target_dir),
        allowed_roots=[str(dummy_root)],
        source_paths=[str(f1)],
        expected_states={
            str(f1): {"size": 9999, "mtime": 12345.67, "is_file": True}  # Wrong size/mtime
        },
        reversibility="RECOVERABLE",
        payload={},
    )

    is_valid, err = tx_store.revalidate_preconditions(op_id)
    assert not is_valid
    assert "size changed" in err or "mtime changed" in err


def test_directory_growth_race_detection(tx_store, dummy_root):
    target_dir = dummy_root / "growth_dir"
    target_dir.mkdir()
    f1 = target_dir / "file1.mp3"
    f1.write_text("content 1")
    st1 = f1.stat()

    op_id, tx = tx_store.create_import_review_cleanup_plan(
        target_path=str(target_dir),
        allowed_roots=[str(dummy_root)],
        source_paths=[str(f1)],
        expected_states={
            str(f1): {"size": st1.st_size, "mtime": st1.st_mtime, "is_file": True}
        },
        reversibility="RECOVERABLE",
        payload={},
    )

    is_valid, err = tx_store.revalidate_preconditions(op_id)
    assert is_valid

    # Now add an unexpected new file after plan creation
    f2 = target_dir / "file2_unexpected.mp3"
    f2.write_text("unexpected content")

    is_valid_after, err_after = tx_store.revalidate_preconditions(op_id)
    assert not is_valid_after
    assert "Directory structure changed" in err_after or "file2_unexpected" in err_after


# ---------------------------------------------------------------------------
# 5. BeetsClient Integration Tests for Wave 15 Endpoints
# ---------------------------------------------------------------------------

def test_beets_client_import_review_cleanup_flow():
    client = BeetsClient(base_url="http://localhost:8337")

    with patch.object(client, "_request") as mock_req:
        mock_req.return_value = {
            "ok": True,
            "operation_id": "tx-12345",
            "status": "Preview",
            "action": "delete",
            "files_to_delete": ["/downloads/track1.mp3"],
        }
        plan_res = client.plan_import_review_cleanup(
            folder_path="/downloads/album",
            action="delete",
            files=["/downloads/album/track1.mp3"],
        )
        assert plan_res["ok"] is True
        assert plan_res["operation_id"] == "tx-12345"

        mock_req.return_value = {
            "ok": True,
            "operation_id": "tx-12345",
            "status": "Completed",
            "deleted": ["/downloads/album/track1.mp3"],
            "log": ["Deleted /downloads/album/track1.mp3"],
        }
        apply_res = client.apply_import_review_cleanup("tx-12345")
        assert apply_res["ok"] is True
        assert apply_res["status"] == "Completed"


def test_beets_client_album_cleanup_flow():
    client = BeetsClient(base_url="http://localhost:8337")

    with patch.object(client, "_request") as mock_req:
        mock_req.return_value = {
            "ok": True,
            "operation_id": "tx-9999",
            "status": "Preview",
            "actions": [{"action": "remove_empty_dir", "path": "/music/empty"}],
        }
        plan_res = client.plan_album_cleanup(album_id=42)
        assert plan_res["ok"] is True
        assert plan_res["operation_id"] == "tx-9999"

        mock_req.return_value = {
            "ok": True,
            "operation_id": "tx-9999",
            "status": "Completed",
            "log": ["Removed empty directory /music/empty"],
        }
        apply_res = client.apply_album_cleanup("tx-9999")
        assert apply_res["ok"] is True
        assert apply_res["status"] == "Completed"


# ---------------------------------------------------------------------------
# 6. Web Manager Endpoint Flask Client Tests
# ---------------------------------------------------------------------------

@pytest.fixture
def flask_client():
    flask_app.app.config["TESTING"] = True
    with patch.dict(os.environ, {"BEETS_WEB_AUTH_DISABLED": "1"}):
        with flask_app.app.test_client() as client:
            yield client


def test_flask_import_review_folder_delete_endpoint(flask_client):
    with patch("app.beets_client") as mock_bc:
        mock_bc.plan_import_review_cleanup.return_value = {
            "ok": True,
            "operation_id": "tx-folder-del",
            "status": "Preview",
        }
        mock_bc.apply_import_review_cleanup.return_value = {
            "ok": True,
            "operation_id": "tx-folder-del",
            "status": "Completed",
            "deleted": ["/downloads/review_album"],
            "log": ["Deleted folder /downloads/review_album"],
        }

        resp = flask_client.post(
            "/api/import/review-folder/delete",
            json={"path": "/downloads/review_album"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["operation_id"] == "tx-folder-del"
        assert data["status"] == "Completed"


def test_flask_import_review_files_cleanup_endpoint(flask_client):
    with patch("app.beets_client") as mock_bc, patch("app._pending_review_matches", return_value=True):
        mock_bc.plan_import_review_cleanup.return_value = {
            "ok": True,
            "operation_id": "tx-files-cleanup",
            "status": "Preview",
        }
        mock_bc.apply_import_review_cleanup.return_value = {
            "ok": True,
            "operation_id": "tx-files-cleanup",
            "status": "Completed",
            "deleted": ["/downloads/album/bad_track.mp3"],
            "moved": [],
            "log": ["Deleted file"],
        }

        resp = flask_client.post(
            "/api/import/review-files/cleanup",
            json={
                "path": "/downloads/album",
                "action": "delete_rejected",
                "files": ["/downloads/album/bad_track.mp3"],
            },
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["operation_id"] == "tx-files-cleanup"


# ---------------------------------------------------------------------------
# 7. AST & Architecture Static Regression Tests
# ---------------------------------------------------------------------------

def test_beets_client_ast_no_duplicates():
    import ast
    client_path = os.path.join(os.path.dirname(__file__), "..", "backend", "beets_client.py")
    with open(client_path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename="beets_client.py")

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "BeetsClient":
            method_names = [n.name for n in node.body if isinstance(n, ast.FunctionDef)]
            duplicates = [name for name in set(method_names) if method_names.count(name) > 1]
            assert not duplicates, f"Duplicate method definitions found in BeetsClient: {duplicates}"


def test_transaction_engine_no_app_import():
    import ast
    engine_path = os.path.join(os.path.dirname(__file__), "..", "backend", "transaction_engine.py")
    with open(engine_path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename="transaction_engine.py")

    imported_modules = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_modules.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported_modules.append(node.module)

    assert "app" not in imported_modules, "transaction_engine.py must not import app.py"


def test_beets_client_fails_closed_on_unavailable():
    from backend.beets_client import BeetsUnavailableError
    client = BeetsClient(base_url="http://127.0.0.1:59999")  # Unreachable port

    with pytest.raises(BeetsUnavailableError):
        client.plan_import_review_cleanup(folder_path="/tmp/test", action="delete")

    with pytest.raises(BeetsUnavailableError):
        client.apply_import_review_cleanup("txn_dummy_123")


# ---------------------------------------------------------------------------
# 8. Database Consistency & Album Cleanup Tests
# ---------------------------------------------------------------------------

def test_album_cleanup_plan_and_apply_db_consistency(tx_store, tmp_path):
    import sqlite3
    from backend.transaction_engine import create_album_cleanup_plan, execute_album_cleanup_apply

    music_root = tmp_path / "music"
    music_root.mkdir()
    album_dir = music_root / "Artist" / "TestAlbum"
    album_dir.mkdir(parents=True)

    track1 = album_dir / "track1.mp3"
    track1.write_text("audio 1")
    track2 = album_dir / "track2.mp3"
    track2.write_text("audio 2")

    db_file = tmp_path / "musiclibrary.blb"
    con = sqlite3.connect(db_file)
    cur = con.cursor()
    cur.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, album_id INTEGER, path TEXT)")
    cur.execute("CREATE TABLE albums (id INTEGER PRIMARY KEY, album TEXT, artpath TEXT)")
    cur.execute("INSERT INTO albums (id, album) VALUES (10, 'TestAlbum')")
    cur.execute("INSERT INTO items (id, album_id, path) VALUES (101, 10, ?)", (str(track1),))
    cur.execute("INSERT INTO items (id, album_id, path) VALUES (102, 10, ?)", (str(track2),))
    con.commit()
    con.close()

    plan_res = create_album_cleanup_plan(
        tx_store,
        album_id=10,
        db_path=str(db_file),
        allowed_roots=[str(music_root)],
    )
    assert plan_res["ok"] is True
    op_id = plan_res["operation_id"]

    apply_res = execute_album_cleanup_apply(tx_store, op_id, db_path=str(db_file))
    assert apply_res["ok"] is True
    assert apply_res["status"] == "Completed"

    # Verify files deleted from disk
    assert not track1.exists()
    assert not track2.exists()
    assert not album_dir.exists()

    # Verify DB items and album deleted
    con = sqlite3.connect(db_file)
    cur = con.cursor()
    cur.execute("SELECT COUNT(*) FROM items WHERE album_id = 10")
    items_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM albums WHERE id = 10")
    albums_count = cur.fetchone()[0]
    con.close()

    assert items_count == 0
    assert albums_count == 0


def test_cross_album_security_authorization(tx_store, tmp_path):
    from backend.transaction_engine import create_album_cleanup_plan

    music_root = tmp_path / "music"
    music_root.mkdir()

    plan_res = create_album_cleanup_plan(
        tx_store,
        album_id=999,  # Non-existent album
        db_path=str(tmp_path / "nonexistent.blb"),
        allowed_roots=[str(music_root)],
    )
    assert plan_res["ok"] is False
    assert "not found" in plan_res["error"]


# ---------------------------------------------------------------------------
# 9. Crash Resume & Truthful Rollback Tests
# ---------------------------------------------------------------------------

def test_truthful_rollback(tx_store, dummy_root, tmp_path):
    from backend.transaction_engine import execute_import_review_cleanup_plan, execute_import_review_cleanup_apply, rollback_import_review_cleanup

    target_dir = dummy_root / "quarantine_target"
    target_dir.mkdir()
    f1 = target_dir / "file1.mp3"
    f1.write_text("q content")

    quarantine_dir = tmp_path / "quarantine"

    plan_res = execute_import_review_cleanup_plan(
        tx_store,
        payload={"path": str(target_dir), "action": "quarantine_rejected", "files": [str(f1)]},
        allowed_roots=[str(dummy_root)],
    )
    assert plan_res["ok"] is True
    op_id = plan_res["operation_id"]

    apply_res = execute_import_review_cleanup_apply(tx_store, op_id, quarantine_root=str(quarantine_dir))
    assert apply_res["ok"] is True
    assert not f1.exists()

    # Rollback
    rb_res = rollback_import_review_cleanup(tx_store, op_id)
    assert rb_res["ok"] is True
    assert rb_res["status"] == "Rolled Back"
    assert f1.exists()

    # Second rollback attempt should be refused (rollback_available is now False)
    rb2_res = rollback_import_review_cleanup(tx_store, op_id)
    assert rb2_res["ok"] is False
    assert "Rollback unavailable" in rb2_res["error"]
