"""Beets API Client — used by beets-web-manager to communicate with the Beets Control Agent.

Replaces local subprocess execution and direct SQLite access with authenticated API calls.
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional


class BeetsError(Exception):
    """Base exception for Beets API client errors."""
    pass


class BeetsUnavailableError(BeetsError):
    """Raised when the Beets Control Agent is unreachable or returning 50x errors."""
    pass


class BeetsAuthError(BeetsError):
    """Raised when authentication with the Beets Control Agent fails (401)."""
    pass


class BeetsCommandError(BeetsError):
    """Raised when a Beets command fails or returns a non-zero exit code."""
    def __init__(self, message: str, returncode: int = 1, stdout: str = "", stderr: str = ""):
        super().__init__(message)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class BeetsClient:
    def __init__(self, base_url: Optional[str] = None, token: Optional[str] = None, timeout: float = 30.0):
        self.base_url = (base_url or os.environ.get("BEETS_API_URL", "http://beets:8338")).rstrip("/")
        self.token = token or os.environ.get("BEETS_API_TOKEN", "")
        self.timeout = timeout

    def _request(self, method: str, endpoint: str, data: Optional[Dict[str, Any]] = None, timeout: Optional[float] = None) -> Dict[str, Any]:
        url = f"{self.base_url}{endpoint}"
        req_timeout = timeout or self.timeout
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "beets-web-manager/1.0",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        body = json.dumps(data).encode("utf-8") if data is not None else None
        req = urllib.request.Request(url, data=body, headers=headers, method=method)

        try:
            with urllib.request.urlopen(req, timeout=req_timeout) as resp:
                resp_bytes = resp.read()
                if not resp_bytes:
                    return {}
                return json.loads(resp_bytes.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise BeetsAuthError("Authentication with Beets Control Agent failed: 401 Unauthorized") from exc
            err_body = ""
            try:
                err_body = exc.read().decode("utf-8")
                err_json = json.loads(err_body)
                msg = err_json.get("error", f"HTTP {exc.code}")
            except Exception:
                msg = f"HTTP {exc.code}: {err_body[:200]}"
            if exc.code >= 500:
                raise BeetsUnavailableError(f"Beets Control Agent error: {msg}") from exc
            raise BeetsError(f"Beets API request error: {msg}") from exc
        except urllib.error.URLError as exc:
            raise BeetsUnavailableError(f"Beets Control Agent is unavailable at {self.base_url}: {exc.reason}") from exc
        except Exception as exc:
            raise BeetsUnavailableError(f"Failed to communicate with Beets Control Agent: {exc}") from exc

    def health(self) -> Dict[str, Any]:
        """Check Beets agent health status."""
        return self._request("GET", "/health", timeout=5.0)

    def version(self) -> Dict[str, Any]:
        """Fetch Beets version and agent version."""
        return self._request("GET", "/version", timeout=5.0)

    def capabilities(self) -> Dict[str, Any]:
        """Fetch agent capabilities and allowlisted commands."""
        return self._request("GET", "/capabilities", timeout=5.0)

    def config_status(self) -> Dict[str, Any]:
        """Fetch configuration and database existence status."""
        return self._request("GET", "/config/status", timeout=5.0)

    def raw_sqlite_query(self, sql: str, params: tuple = (), offset: int = 0, limit: int = 1000) -> List[Dict[str, Any]]:
        """Run a strictly read-only SELECT query against the Beets database via the control agent."""
        res = self._request("POST", "/library/raw_query", {"query": sql, "params": list(params), "offset": offset, "limit": limit})
        return res.get("rows", [])

    def run_command(self, command: str, args: Optional[List[str]] = None, timeout: float = 120.0, config_override: str = "", source_path: str = "", target_path: str = "") -> Dict[str, Any]:
        """Execute a beet command synchronously on the Beets control agent."""
        payload = {
            "command": command,
            "args": args or [],
            "timeout": timeout,
            "config_override": config_override,
        }
        if source_path:
            payload["source_path"] = source_path
        if target_path:
            payload["target_path"] = target_path

        return self._request("POST", "/commands/execute", payload, timeout=timeout + 5.0)

    def start_job(self, command: str, args: Optional[List[str]] = None, label: str = "", config_override: str = "", source_path: str = "") -> str:
        """Start a background job inside the Beets control agent."""
        payload = {
            "command": command,
            "args": args or [],
            "label": label,
            "config_override": config_override,
        }
        if source_path:
            payload["source_path"] = source_path

        res = self._request("POST", "/jobs/create", payload)
        return res.get("job_id", "")

    def get_job(self, job_id: str) -> Dict[str, Any]:
        """Fetch job details and logs."""
        res = self._request("GET", f"/jobs/{job_id}")
        return res.get("job", {})

    def cancel_job(self, job_id: str) -> Dict[str, Any]:
        """Cancel a running job."""
        return self._request("POST", f"/jobs/{job_id}/cancel")

    def read_tags(self, file_path: str) -> Dict[str, Any]:
        """Read media tags from an audio file via the Beets agent."""
        res = self._request("POST", "/tags/read", {"file_path": file_path})
        return res.get("tags", {})

    def write_tags(self, file_path: str, tags: Dict[str, Any]) -> Dict[str, Any]:
        """Write media tags to an audio file via the Beets agent under OS file lock."""
        return self._request("POST", "/tags/write", {"file_path": file_path, "tags": tags})

    def move_file(self, source_path: str, target_path: str) -> Dict[str, Any]:
        """Move or rename a file via the Beets agent under OS file lock."""
        return self._request("POST", "/files/move", {"source_path": source_path, "target_path": target_path})

    def delete_file(self, path: str) -> Dict[str, Any]:
        """Delete a file or directory via the Beets agent under OS file lock."""
        return self._request("POST", "/files/delete", {"path": path})

    def mkdir(self, path: str) -> Dict[str, Any]:
        """Create a directory via the Beets agent under OS file lock."""
        return self._request("POST", "/files/mkdir", {"path": path})

    # Purpose-built structured library methods
    def get_item(self, item_id: int) -> Optional[Dict[str, Any]]:
        """Fetch single item dict by ID."""
        res = self._request("GET", f"/items/{item_id}")
        return res.get("item")

    def get_album(self, album_id: int) -> Optional[Dict[str, Any]]:
        """Fetch single album dict by ID (includes items list)."""
        res = self._request("GET", f"/albums/{album_id}")
        return res.get("album")

    def find_items_by_album_id(self, album_id: int) -> List[Dict[str, Any]]:
        """Fetch all items belonging to album_id."""
        res = self._request("GET", f"/items?album_id={album_id}")
        return res.get("items", [])

    def find_items_by_path(self, path: str) -> List[Dict[str, Any]]:
        """Fetch items by audio file path."""
        res = self._request("GET", f"/items?path={urllib.parse.quote(path)}")
        return res.get("items", [])

    def find_items_by_mbid(self, mbid: str) -> List[Dict[str, Any]]:
        """Fetch items by MusicBrainz track ID."""
        res = self._request("GET", f"/items?mbid={urllib.parse.quote(mbid)}")
        return res.get("items", [])

    def find_items_by_query(self, query: str, limit: int = 500) -> List[Dict[str, Any]]:
        """Find items matching query string."""
        res = self._request("GET", f"/items?query={urllib.parse.quote(query)}&limit={limit}")
        return res.get("items", [])

    def find_albums_by_query(self, query: str, limit: int = 500) -> List[Dict[str, Any]]:
        """Find albums matching query string."""
        res = self._request("GET", f"/albums?query={urllib.parse.quote(query)}&limit={limit}")
        return res.get("albums", [])

    def search_items_text(self, text: str, limit: int = 500) -> List[Dict[str, Any]]:
        """Perform a parameterized bare-word text search across item fields (title, artist, album, albumartist, path)."""
        if not text or not text.strip():
            return []
        res = self._request("GET", f"/items?query={urllib.parse.quote(text.strip())}&limit={limit}")
        return res.get("items", [])

    def search_albums_text(self, text: str, limit: int = 500) -> List[Dict[str, Any]]:
        """Perform a parameterized bare-word text search across album fields (album, albumartist, artist)."""
        if not text or not text.strip():
            return []
        res = self._request("GET", f"/albums?query={urllib.parse.quote(text.strip())}&limit={limit}")
        return res.get("albums", [])

    def list_all_albums(self, page_size: int = 500, safety_ceiling: int = 20000) -> List[Dict[str, Any]]:
        """Fetch all albums by paginating through all available pages up to safety ceiling."""
        all_albums = []
        offset = 0
        while True:
            res = self._request("GET", f"/albums?offset={offset}&limit={page_size}")
            albums_page = res.get("albums", [])
            all_albums.extend(albums_page)

            has_more = res.get("has_more", False)
            if not has_more or not albums_page:
                break

            next_offset = res.get("next_offset")
            if next_offset is None or next_offset <= offset:
                raise BeetsError(f"Inconsistent pagination state received from server: next_offset={next_offset}, current_offset={offset}")

            offset = next_offset
            if len(all_albums) >= safety_ceiling:
                raise BeetsError(f"Safety ceiling reached while paginating albums ({len(all_albums)} >= {safety_ceiling})")

        return all_albums

    def list_all_items(self, page_size: int = 1000, safety_ceiling: int = 100000) -> List[Dict[str, Any]]:
        """Fetch all items by paginating through all available pages up to safety ceiling."""
        all_items = []
        offset = 0
        while True:
            res = self._request("GET", f"/items?offset={offset}&limit={page_size}")
            items_page = res.get("items", [])
            all_items.extend(items_page)

            has_more = res.get("has_more", False)
            if not has_more or not items_page:
                break

            next_offset = res.get("next_offset")
            if next_offset is None or next_offset <= offset:
                raise BeetsError(f"Inconsistent pagination state received from server: next_offset={next_offset}, current_offset={offset}")

            offset = next_offset
            if len(all_items) >= safety_ceiling:
                raise BeetsError(f"Safety ceiling reached while paginating items ({len(all_items)} >= {safety_ceiling})")

        return all_items

    def update_item_fields(self, item_id: int, fields: Dict[str, Any]) -> Dict[str, Any]:
        """Update fields on item row in SQLite under lock."""
        return self._request("PATCH", f"/items/{item_id}", {"fields": fields})

    def update_album_fields(self, album_id: int, fields: Dict[str, Any]) -> Dict[str, Any]:
        """Update fields on album row in SQLite under lock."""
        return self._request("PATCH", f"/albums/{album_id}", {"fields": fields})

    def set_album_artpath(self, album_id: int, artpath: str) -> Dict[str, Any]:
        """Set artpath field on album in SQLite under lock."""
        return self._request("POST", f"/albums/{album_id}/artpath", {"artpath": artpath})

    def clear_album_artpath(self, album_id: int) -> Dict[str, Any]:
        """Clear artpath field on album in SQLite under lock."""
        return self._request("DELETE", f"/albums/{album_id}/artpath")

    def delete_album(self, album_id: int, delete_files: bool = True) -> Dict[str, Any]:
        """Perform transactional single-writer album deletion under lock."""
        return self._request("DELETE", f"/albums/{album_id}", {"delete_files": delete_files})


class RemoteSQLiteCursor:
    def __init__(self, client: BeetsClient):
        self.client = client
        self._rows = []
        self._idx = 0
        self.row_factory = None

    def execute(self, sql: str, params: tuple = ()):
        res = self.client.raw_sqlite_query(sql, params)
        if isinstance(res, list):
            self._rows = res
        else:
            self._rows = []
        self._idx = 0
        return self

    def fetchall(self):
        rows = self._rows
        self._rows = []
        return rows

    def fetchone(self):
        if self._idx < len(self._rows):
            row = self._rows[self._idx]
            self._idx += 1
            return row
        return None


class RemoteSQLiteConnection:
    def __init__(self, client: BeetsClient):
        self.client = client
        self.row_factory = None

    def cursor(self):
        c = RemoteSQLiteCursor(self.client)
        c.row_factory = self.row_factory
        return c

    def execute(self, sql: str, params: tuple = ()):
        c = self.cursor()
        c.execute(sql, params)
        return c

    def close(self):
        pass

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass


def get_db_connection(db_path: Optional[str] = None):
    """Return a database connection — ALWAYS connects via RemoteSQLiteConnection.
    Never opens local SQLite files in the web manager.
    """
    return RemoteSQLiteConnection(beets_client)


class DictAttr:
    """Dictionary wrapper providing attribute and item access."""
    def __init__(self, data: Dict[str, Any]):
        object.__setattr__(self, "_data", data or {})

    def __getattr__(self, name: str) -> Any:
        data = object.__getattribute__(self, "_data")
        if name in data:
            val = data[name]
            if name == "path" and isinstance(val, (bytes, bytearray)):
                return val.decode("utf-8", errors="replace")
            return val
        return ""

    def __setattr__(self, name: str, value: Any) -> None:
        data = object.__getattribute__(self, "_data")
        data[name] = value

    def __getitem__(self, key: str) -> Any:
        return self.__getattr__(key)

    def __setitem__(self, key: str, value: Any) -> None:
        self.__setattr__(key, value)

    def __contains__(self, key: str) -> bool:
        return key in object.__getattribute__(self, "_data")

    def get(self, key: str, default: Any = "") -> Any:
        data = object.__getattribute__(self, "_data")
        val = data.get(key, default)
        return val if val is not None else default

    def keys(self):
        return object.__getattribute__(self, "_data").keys()

    def values(self):
        return object.__getattribute__(self, "_data").values()

    def items(self):
        return object.__getattribute__(self, "_data").items()

    def to_dict(self) -> Dict[str, Any]:
        return dict(object.__getattribute__(self, "_data"))

    def store(self):
        raise NotImplementedError(
            "Direct ORM .store() is not supported. Use beets_client.update_item_fields() "
            "or beets_client.update_album_fields() to issue explicit remote updates."
        )

    def save(self):
        raise NotImplementedError(
            "Direct ORM .save() is not supported. Use beets_client.update_item_fields() "
            "or beets_client.update_album_fields() to issue explicit remote updates."
        )

    def remove(self):
        raise NotImplementedError(
            "Direct ORM .remove() is not supported. Use beets_client.delete_album() "
            "or beets_client.delete_file() to issue explicit remote deletions."
        )


class RemoteItem(DictAttr):
    pass


class RemoteAlbum(DictAttr):
    def items(self) -> List[RemoteItem]:
        aid = self.id
        if not aid:
            return []
        items_data = beets_client.find_items_by_album_id(int(aid))
        return [RemoteItem(r) for r in items_data]


class RemoteLibrary:
    """Strict wrapper for legacy library call compatibility."""
    def __init__(self, client: BeetsClient):
        self.client = client

    def get_item(self, iid: int) -> Optional[RemoteItem]:
        if not iid:
            return None
        data = self.client.get_item(int(iid))
        return RemoteItem(data) if data else None

    def get_album(self, aid: int) -> Optional[RemoteAlbum]:
        if not aid:
            return None
        data = self.client.get_album(int(aid))
        return RemoteAlbum(data) if data else None

    def _validate_item_term(self, term: str) -> None:
        if not isinstance(term, str):
            raise BeetsError(f"Query term must be a string, got {type(term).__name__}")
        q_str = term.strip()
        if not q_str:
            raise BeetsError("Query term cannot be empty or whitespace")
        if ":" in q_str:
            field, val = q_str.split(":", 1)
            field = field.strip()
            if not field:
                raise BeetsError(f"Query field prefix cannot be empty in '{term}'")
            allowed_fields = {"album_id", "album", "artist", "title", "path", "mb_trackid", "mbid", "singleton"}
            if field not in allowed_fields:
                raise BeetsError(f"Unsupported query field '{field}' in '{term}'")
            if not val.strip() and field not in {"singleton"}:
                raise BeetsError(f"Query field '{field}' requires a non-empty value in '{term}'")
            if field == "album_id":
                if not val.strip().isdigit():
                    raise BeetsError(f"album_id must be an integer: {val!r}")
            elif field == "singleton":
                if val.strip().lower() not in {"true", "false"}:
                    raise BeetsError(f"singleton value must be 'true' or 'false': {val!r}")

    def _validate_album_term(self, term: str) -> None:
        if not isinstance(term, str):
            raise BeetsError(f"Query term must be a string, got {type(term).__name__}")
        q_str = term.strip()
        if not q_str:
            raise BeetsError("Query term cannot be empty or whitespace")
        if ":" in q_str:
            field, val = q_str.split(":", 1)
            field = field.strip()
            if not field:
                raise BeetsError(f"Query field prefix cannot be empty in '{term}'")
            allowed_fields = {"mb_albumid", "mb_releasegroupid", "album", "artist", "albumartist"}
            if field not in allowed_fields:
                raise BeetsError(f"Unsupported query field '{field}' in '{term}'")
            if not val.strip():
                raise BeetsError(f"Query field '{field}' requires a non-empty value in '{term}'")

    def items(self, query: Any = None) -> List[RemoteItem]:
        if query is None or query == [] or query == ():
            items_data = self.client.list_all_items()
            return [RemoteItem(r) for r in items_data]

        if isinstance(query, str) and query == "":
            items_data = self.client.list_all_items()
            return [RemoteItem(r) for r in items_data]

        if isinstance(query, list):
            if not query:
                items_data = self.client.list_all_items()
                return [RemoteItem(r) for r in items_data]

            for term in query:
                self._validate_item_term(term)

            first_term = query[0]
            first_results = self.items(first_term)
            if not first_results or len(query) == 1:
                return first_results

            matching_ids = {item.id for item in first_results}
            for term in query[1:]:
                if not matching_ids:
                    break
                term_results = self.items(term)
                term_ids = {item.id for item in term_results}
                matching_ids = matching_ids & term_ids

            seen = set()
            final_items = []
            for item in first_results:
                if item.id in matching_ids and item.id not in seen:
                    seen.add(item.id)
                    final_items.append(item)
            return final_items

        if isinstance(query, str):
            self._validate_item_term(query)
            q_str = query.strip()
            if ":" in q_str:
                field, val = q_str.split(":", 1)
                field = field.strip()
                val = val.strip()
                if field == "album_id":
                    items_data = self.client.find_items_by_album_id(int(val))
                elif field in {"album", "artist", "title"}:
                    items_data = self.client.find_items_by_query(f"{field}:{val}")
                elif field == "path":
                    items_data = self.client.find_items_by_path(val)
                elif field in {"mb_trackid", "mbid"}:
                    items_data = self.client.find_items_by_mbid(val)
                elif field == "singleton":
                    items_data = self.client.find_items_by_query(f"singleton:{val.lower()}")
            else:
                items_data = self.client.search_items_text(q_str)
            return [RemoteItem(r) for r in items_data]

        raise BeetsError(f"Unsupported RemoteLibrary items() query shape: {query!r}")

    def albums(self, query: Any = None) -> List[RemoteAlbum]:
        if query is None or query == [] or query == ():
            albums_data = self.client.list_all_albums()
            return [RemoteAlbum(r) for r in albums_data]

        if isinstance(query, str) and query == "":
            albums_data = self.client.list_all_albums()
            return [RemoteAlbum(r) for r in albums_data]

        if isinstance(query, list):
            if not query:
                albums_data = self.client.list_all_albums()
                return [RemoteAlbum(r) for r in albums_data]

            for term in query:
                self._validate_album_term(term)

            first_term = query[0]
            first_results = self.albums(first_term)
            if not first_results or len(query) == 1:
                return first_results

            matching_ids = {album.id for album in first_results}
            for term in query[1:]:
                if not matching_ids:
                    break
                term_results = self.albums(term)
                term_ids = {album.id for album in term_results}
                matching_ids = matching_ids & term_ids

            seen = set()
            final_albums = []
            for album in first_results:
                if album.id in matching_ids and album.id not in seen:
                    seen.add(album.id)
                    final_albums.append(album)
            return final_albums

        if isinstance(query, str):
            self._validate_album_term(query)
            q_str = query.strip()
            if ":" in q_str:
                field, val = q_str.split(":", 1)
                field = field.strip()
                val = val.strip()
                if field == "mb_albumid":
                    res = self.client._request("GET", f"/albums?mb_albumid={urllib.parse.quote(val)}")
                    return [RemoteAlbum(r) for r in res.get("albums", [])]
                elif field == "mb_releasegroupid":
                    res = self.client._request("GET", f"/albums?mb_releasegroupid={urllib.parse.quote(val)}")
                    return [RemoteAlbum(r) for r in res.get("albums", [])]
                elif field in {"album", "artist", "albumartist"}:
                    res = self.client._request("GET", f"/albums?query={urllib.parse.quote(f'{field}:{val}')}")
                    return [RemoteAlbum(r) for r in res.get("albums", [])]
            else:
                albums_data = self.client.search_albums_text(q_str)
                return [RemoteAlbum(r) for r in albums_data]

        raise BeetsError(f"Unsupported RemoteLibrary albums() query shape: {query!r}")


# Module singleton instances
beets_client = BeetsClient()
lib = RemoteLibrary(beets_client)
