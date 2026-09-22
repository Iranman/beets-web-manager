"""Unit tests for BeetsAdapter."""

import os
import tempfile
import pytest
from beets.library import Library, Item, Album
from beetsplug.web import app as beets_web_app
from beetsplug.webmanager import WebManagerPlugin
from beetsplug.webmanager.auth import set_api_key_file
from backend.beets_adapter import (
    BeetsAdapter,
    BeetsAdapterAuthError,
    BeetsAdapterNotFoundError,
    BeetsAdapterConnectionError,
)


@pytest.fixture
def mock_beets_server():
    with tempfile.TemporaryDirectory() as td:
        dbpath = os.path.join(td, "musiclibrary.blb")
        lib = Library(dbpath, directory=td)

        key_file = os.path.join(td, ".webmanager_api_key")
        token = "test_adapter_secret_token"
        with open(key_file, "w", encoding="utf-8") as f:
            f.write(token + "\n")
        set_api_key_file(key_file)

        # Setup Beets web plugin
        plugin = WebManagerPlugin()
        beets_web_app.config["lib"] = lib
        beets_web_app.config["INCLUDE_PATHS"] = True
        beets_web_app.config["TESTING"] = True

        album = Album(album="Discovery", albumartist="Daft Punk", year=2001)
        lib.add(album)
        item = Item(
            title="One More Time",
            artist="Daft Punk",
            album="Discovery",
            albumartist="Daft Punk",
            album_id=album.id,
            track=1,
            year=2001,
            path=os.path.join(td, "track1.mp3").encode("utf-8"),
        )
        lib.add(item)
        lib._connection().commit()

        client = beets_web_app.test_client()

        # Create BeetsAdapter wired to test client using mock opener
        adapter = BeetsAdapter(base_url="http://mock-beets:8337", api_key=token)

        # Patch adapter._request to delegate to Flask test client
        def mock_request(method, path, params=None, json_data=None, headers=None, timeout=None):
            h = headers or {}
            if path.startswith("/webmanager") or "/webmanager/" in path:
                if "Authorization" not in h and adapter.api_key:
                    h["Authorization"] = f"Bearer {adapter.api_key}"

            query_str = ""
            if params:
                import urllib.parse
                query_str = "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})

            full_path = f"{path}{query_str}"
            resp = client.open(
                full_path,
                method=method,
                json=json_data,
                headers=h,
            )
            if resp.status_code == 401:
                raise BeetsAdapterAuthError("Auth failed", status_code=401, response_data=resp.get_json())
            if resp.status_code == 404:
                raise BeetsAdapterNotFoundError("Not found", status_code=404, response_data=resp.get_json())
            if resp.status_code >= 400:
                raise Exception(f"HTTP {resp.status_code}: {resp.data}")
            return resp.get_json()

        adapter._request = mock_request

        yield {
            "adapter": adapter,
            "lib": lib,
            "album": album,
            "item": item,
            "token": token,
            "temp_dir": td,
        }

        try:
            lib._connection().close()
        except Exception:
            pass


def test_adapter_get_stats(mock_beets_server):
    adapter = mock_beets_server["adapter"]
    stats = adapter.get_stats()
    assert stats["items"] == 1
    assert stats["albums"] == 1


def test_adapter_get_artists(mock_beets_server):
    adapter = mock_beets_server["adapter"]
    artists = adapter.get_artists()
    assert "Daft Punk" in artists


def test_adapter_get_items_and_album(mock_beets_server):
    adapter = mock_beets_server["adapter"]
    item = mock_beets_server["item"]
    album = mock_beets_server["album"]

    # Items
    items = adapter.get_items()
    assert len(items) == 1
    assert items[0]["title"] == "One More Time"

    # Single item
    single_item = adapter.get_item(item.id)
    assert single_item is not None
    assert single_item["title"] == "One More Time"

    # Albums
    albums = adapter.get_albums()
    assert len(albums) == 1
    assert albums[0]["album"] == "Discovery"

    # Album with expand
    expanded_album = adapter.get_album(album.id, expand=True)
    assert expanded_album is not None
    assert expanded_album["album"] == "Discovery"
    assert "items" in expanded_album
    assert len(expanded_album["items"]) == 1

    # find_all_items_by_album_id helper
    album_items = adapter.find_all_items_by_album_id(album.id)
    assert len(album_items) == 1
    assert album_items[0]["title"] == "One More Time"


def test_adapter_mutations(mock_beets_server):
    adapter = mock_beets_server["adapter"]
    item = mock_beets_server["item"]
    lib = mock_beets_server["lib"]

    # Modify item
    res = adapter.modify(
        fields={"title": "Aerodynamic", "genre": "House"},
        item_ids=[item.id],
        write=False,
        move=False,
    )
    assert res["success"] is True
    assert res["modified_items"] == 1

    # Verify
    refreshed = lib.get_item(item.id)
    assert refreshed.title == "Aerodynamic"


def test_adapter_auth_failure(mock_beets_server):
    adapter = mock_beets_server["adapter"]
    adapter._api_key = "invalid_bad_token"

    with pytest.raises(BeetsAdapterAuthError):
        adapter.get_plugin_status()


def test_adapter_not_found(mock_beets_server):
    adapter = mock_beets_server["adapter"]
    assert adapter.get_item(999999) is None
    assert adapter.get_album(999999) is None
