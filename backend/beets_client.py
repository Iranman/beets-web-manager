"""Beets API Client — used by beets-web-manager to communicate with the Beets Control Agent.

Replaces local subprocess execution and direct SQLite access with authenticated API calls.
"""

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional


class BeetsError(Exception):
    """Base exception for Beets API client errors.

    error_code/status_code carry the agent's own structured error_code
    field and original HTTP status when available (e.g. "config_not_found"
    / 404), so callers can build a specific, stable response instead of
    string-matching the free-text message. Both default to "" / 0 for
    transport-level failures (BeetsUnavailableError, BeetsAuthError) that
    never got a structured agent response to read them from.
    """
    def __init__(self, message: str, error_code: str = "", status_code: int = 0):
        super().__init__(message)
        self.error_code = error_code
        self.status_code = status_code


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


class ParsedQuery:
    def __init__(self, target: str, field: Optional[str], value: str, operator: str = "equals"):
        self.target = target  # "items" or "albums"
        self.field = field    # field name e.g. "album_id", "mb_albumid", or None for bare text
        self.value = value
        self.operator = operator  # "equals", "contains", "singleton"

    def __repr__(self):
        return f"<ParsedQuery target={self.target!r} field={self.field!r} value={self.value!r} op={self.operator!r}>"


def parse_query_term(term: str, target: str) -> ParsedQuery:
    """Parse and validate a query term for target ('items' or 'albums'). Raises BeetsError on invalid syntax/field."""
    if not isinstance(term, str):
        raise BeetsError(f"Query term must be a string, got {type(term).__name__}")
    q_str = term.strip()
    if not q_str:
        raise BeetsError("Query term cannot be empty or whitespace")

    if ":" in q_str:
        field, val = q_str.split(":", 1)
        field = field.strip()
        val = val.strip()
        if not field:
            raise BeetsError(f"Query field prefix cannot be empty in '{term}'")

        if target == "items":
            allowed_fields = {"album_id", "album", "artist", "title", "path", "mb_trackid", "mbid", "singleton"}
            if field not in allowed_fields:
                raise BeetsError(f"Unsupported query field '{field}' in '{term}'")
            if not val and field != "singleton":
                raise BeetsError(f"Query field '{field}' requires a non-empty value in '{term}'")
            if field == "album_id":
                if not val.isdigit():
                    raise BeetsError(f"album_id must be an integer: {val!r}")
                return ParsedQuery(target="items", field="album_id", value=val, operator="equals")
            elif field == "singleton":
                if val.lower() not in {"true", "false"}:
                    raise BeetsError(f"singleton value must be 'true' or 'false': {val!r}")
                return ParsedQuery(target="items", field="singleton", value=val.lower(), operator="singleton")
            elif field in {"mb_trackid", "mbid"}:
                return ParsedQuery(target="items", field="mb_trackid", value=val, operator="equals")
            elif field == "path":
                return ParsedQuery(target="items", field="path", value=val, operator="equals")
            elif field in {"album", "artist", "title"}:
                return ParsedQuery(target="items", field=field, value=val, operator="contains")

        elif target == "albums":
            allowed_fields = {"mb_albumid", "mb_releasegroupid", "album", "artist", "albumartist"}
            if field not in allowed_fields:
                raise BeetsError(f"Unsupported query field '{field}' in '{term}'")
            if not val:
                raise BeetsError(f"Query field '{field}' requires a non-empty value in '{term}'")
            if field == "mb_albumid":
                return ParsedQuery(target="albums", field="mb_albumid", value=val, operator="equals")
            elif field == "mb_releasegroupid":
                return ParsedQuery(target="albums", field="mb_releasegroupid", value=val, operator="equals")
            elif field in {"album", "artist", "albumartist"}:
                return ParsedQuery(target="albums", field=field, value=val, operator="contains")

    # Bare-word query
    return ParsedQuery(target=target, field=None, value=q_str, operator="contains")


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
            err_code = ""
            try:
                err_body = exc.read().decode("utf-8")
                err_json = json.loads(err_body)
                msg = err_json.get("error", f"HTTP {exc.code}")
                err_code = str(err_json.get("error_code") or "")
            except Exception:
                msg = f"HTTP {exc.code}: {err_body[:200]}"
            if exc.code >= 500:
                raise BeetsUnavailableError(f"Beets Control Agent error: {msg}", error_code=err_code, status_code=exc.code) from exc
            raise BeetsError(f"Beets API request error: {msg}", error_code=err_code, status_code=exc.code) from exc
        except urllib.error.URLError as exc:
            raise BeetsUnavailableError(f"Beets Control Agent is unavailable at {self.base_url}: {exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise BeetsUnavailableError("Beets Control Agent returned malformed JSON") from exc
        except TimeoutError as exc:
            raise BeetsUnavailableError("Timed out communicating with Beets Control Agent") from exc
        except Exception as exc:
            raise BeetsUnavailableError("Failed to communicate with Beets Control Agent") from exc

    def health(self) -> Dict[str, Any]:
        """Check Beets agent health status."""
        return self._request("GET", "/health", timeout=5.0)

    def get_status(self) -> Dict[str, Any]:
        """Fetch status and plugin details from the Beets control agent."""
        return self._request("GET", "/status", timeout=5.0)

    def version(self) -> Dict[str, Any]:
        """Fetch Beets version and agent version."""
        return self._request("GET", "/version", timeout=5.0)

    def capabilities(self) -> Dict[str, Any]:
        """Fetch agent capabilities and allowlisted commands."""
        return self._request("GET", "/capabilities", timeout=5.0)

    def config_status(self) -> Dict[str, Any]:
        """Fetch configuration and database existence status."""
        return self._request("GET", "/config/status", timeout=5.0)

    def get_config(self) -> Dict[str, Any]:
        """Fetch raw config.yaml content (plus backup metadata) from the
        engine's own BEETSDIR. The web manager has no local mount of this
        file in the two-service topology; this is the only access path.
        Returns {"content": str, "has_backup": bool, "backup_ts": float|None}.
        Raises BeetsError (with a stable .args[0] message sourced from the
        agent's own error_code-bearing response) on a caller-facing
        rejection such as a missing/unreadable file, BeetsUnavailableError
        on a transport failure."""
        return self._request("GET", "/config", timeout=10.0)

    def save_config(self, content: str) -> Dict[str, Any]:
        """Write config.yaml on the engine, backing up the previous version
        first. Returns {"ok": True, "backed_up": bool}."""
        return self._request("POST", "/config", {"content": content}, timeout=10.0)

    def revert_config(self) -> Dict[str, Any]:
        """Restore config.yaml from its most recent engine-side backup."""
        return self._request("POST", "/config/revert", timeout=10.0)

    def raw_sqlite_query(self, sql: str, params: tuple = (), offset: int = 0, limit: int = 1000) -> List[Dict[str, Any]]:
        """Raw SQL is intentionally unavailable; use structured query helpers."""
        raise BeetsError("Raw SQLite queries are not permitted; use structured library query helpers")

    def inspect_import_source(self, source_path: str, operation: str, *, timeout: float = 60.0) -> Dict[str, Any]:
        """Engine-authoritative validation + bounded audio inventory for an
        import/reimport source (SEC-002 Wave 8 ARCH-003).

        The web manager has no local filesystem access to the engine's
        music/staging roots in the shipped Compose topology, so it cannot
        validate existence, reject symlinks, or read audio properties
        itself -- this asks the engine (which owns those mounts) to do it
        and return structured evidence instead. `operation` is required and
        selects a fixed, engine-defined root policy; the caller never
        supplies its own trusted-root list.

        Raises BeetsError/BeetsUnavailableError/BeetsAuthError on transport
        failure. On a caller-facing rejection (invalid path, wrong root,
        etc.) the engine returns a normal non-2xx JSON error, which
        _request() already turns into a BeetsError with the engine's own
        stable error_code string as the message -- callers should not parse
        HTML/traceback text out of it.
        """
        payload = {"source_path": source_path, "operation": operation}
        return self._request("POST", "/imports/source/inspect", payload, timeout=timeout + 5.0)

    def discover_import_sources(self, source_path: str, operation: str = "ai_batch_discovery", cursor: Optional[str] = None, limits: Optional[Dict[str, Any]] = None, *, timeout: float = 60.0) -> Dict[str, Any]:
        """Engine-side recursive import candidate discovery operation (SEC-002 Wave 8 ARCH-003)."""
        payload: Dict[str, Any] = {"source_path": source_path, "operation": operation}
        if cursor:
            payload["cursor"] = cursor
        if limits:
            payload["limits"] = limits
        return self._request("POST", "/imports/source/discover", payload, timeout=timeout + 5.0)

    def preserve_import_source(self, source_path: str, expected_source_signature: Optional[str] = None, plan_id: Optional[str] = None, *, timeout: float = 120.0) -> Dict[str, Any]:
        """Engine-side torrent preservation operation (SEC-002 Wave 8 ARCH-003)."""
        payload: Dict[str, Any] = {"source_path": source_path}
        if expected_source_signature:
            payload["expected_source_signature"] = expected_source_signature
        if plan_id:
            payload["plan_id"] = plan_id
        return self._request("POST", "/imports/source/preserve", payload, timeout=timeout + 5.0)

    def plan_import_review_cleanup(self, payload_or_folder: Any = None, *, timeout: float = 30.0, **kwargs) -> Dict[str, Any]:
        """Engine-side import review cleanup planning (SEC-002 Wave 15)."""
        if isinstance(payload_or_folder, dict):
            payload = payload_or_folder
        else:
            target = kwargs.get("folder_path") or payload_or_folder or ""
            payload = {
                "path": target,
                "folder": target,
                "action": kwargs.get("action", "delete"),
                "files": kwargs.get("files", []),
            }
            if "allow_delete" in kwargs:
                payload["allow_delete"] = kwargs["allow_delete"]
            if "album_id" in kwargs:
                payload["album_id"] = kwargs["album_id"]
            if "confirmed_wrong_library_folder" in kwargs:
                payload["confirmed_wrong_library_folder"] = kwargs["confirmed_wrong_library_folder"]
        return self._request("POST", "/imports/review/cleanup/plan", payload, timeout=timeout)

    def apply_import_review_cleanup(self, operation_id: str, *, timeout: float = 60.0) -> Dict[str, Any]:
        """Engine-side import review cleanup application (SEC-002 Wave 15)."""
        return self._request("POST", "/imports/review/cleanup/apply", {"operation_id": operation_id}, timeout=timeout)

    def plan_album_cleanup(self, album_id: int, *, timeout: float = 30.0) -> Dict[str, Any]:
        """Engine-side album cleanup planning (SEC-002 Wave 15)."""
        return self._request("POST", "/albums/cleanup/plan", {"album_id": album_id}, timeout=timeout)

    def apply_album_cleanup(self, operation_id: str, *, timeout: float = 60.0) -> Dict[str, Any]:
        """Engine-side album cleanup application (SEC-002 Wave 15)."""
        return self._request("POST", "/albums/cleanup/apply", {"operation_id": operation_id}, timeout=timeout)

    def rollback_import_review_cleanup(self, operation_id: str, *, timeout: float = 60.0) -> Dict[str, Any]:
        """Engine-side Import Review cleanup rollback (SEC-002 Wave 15).

        This executor only understands "move_quarantine" steps and only
        accepts transactions whose mutation_family is
        "import_review_cleanup_v1" (the engine rejects anything else).
        Album Cleanup transactions are irreversible by design (Wave 15/16)
        and are never routed through this method -- do not repurpose it as
        a generic rollback entry point.
        """
        return self._request("POST", "/imports/review/cleanup/rollback", {"operation_id": operation_id}, timeout=timeout)

    def plan_track_replacement(self, payload: Dict[str, Any], *, timeout: float = 30.0) -> Dict[str, Any]:
        """Engine-side track replacement planning (SEC-002 Wave 17)."""
        return self._request("POST", "/tracks/replacement/plan", payload, timeout=timeout)

    def apply_track_replacement(self, operation_id: str, *, timeout: float = 60.0) -> Dict[str, Any]:
        """Engine-side track replacement application (SEC-002 Wave 17)."""
        return self._request("POST", "/tracks/replacement/apply", {"operation_id": operation_id}, timeout=timeout)

    def rollback_track_replacement(self, operation_id: str, *, timeout: float = 60.0) -> Dict[str, Any]:
        """Engine-side track replacement rollback (SEC-002 Wave 17)."""
        return self._request("POST", "/tracks/replacement/rollback", {"operation_id": operation_id}, timeout=timeout)

    def plan_bulk_import_replacement(self, payload: Dict[str, Any], *, timeout: float = 30.0) -> Dict[str, Any]:
        """Engine-side bulk import replacement planning (SEC-002 Wave 18)."""
        return self._request("POST", "/imports/bulk-replacement/plan", payload, timeout=timeout)

    def apply_bulk_import_replacement(self, operation_id: str, *, timeout: float = 60.0) -> Dict[str, Any]:
        """Engine-side bulk import replacement application (SEC-002 Wave 18)."""
        return self._request("POST", "/imports/bulk-replacement/apply", {"operation_id": operation_id}, timeout=timeout)

    def rollback_bulk_import_replacement(self, operation_id: str, *, timeout: float = 60.0) -> Dict[str, Any]:
        """Engine-side bulk import replacement rollback (SEC-002 Wave 18)."""
        return self._request("POST", "/imports/bulk-replacement/rollback", {"operation_id": operation_id}, timeout=timeout)

    def plan_album_mb_track_repair(self, payload: Dict[str, Any], *, timeout: float = 30.0) -> Dict[str, Any]:
        """Engine-side album MB track repair planning (SEC-002 Wave 19)."""
        return self._request("POST", "/albums/mb-track-repair/plan", payload, timeout=timeout)

    def apply_album_mb_track_repair(self, operation_id: str, *, write_tags: bool = True, timeout: float = 60.0) -> Dict[str, Any]:
        """Engine-side album MB track repair application (SEC-002 Wave 19).

        `write_tags=False` performs the Beets DB update only and skips the
        on-disk audio tag write stage -- preserves the
        `_repair_album_mbid_sticking_once(write_tags=False)` contract.
        """
        return self._request(
            "POST", "/albums/mb-track-repair/apply",
            {"operation_id": operation_id, "write_tags": bool(write_tags)},
            timeout=timeout,
        )

    def rollback_album_mb_track_repair(self, operation_id: str, *, timeout: float = 60.0) -> Dict[str, Any]:
        """Engine-side album MB track repair rollback (SEC-002 Wave 19)."""
        return self._request("POST", "/albums/mb-track-repair/rollback", {"operation_id": operation_id}, timeout=timeout)

    def plan_existing_album_reconcile(self, payload: Dict[str, Any], *, timeout: float = 30.0) -> Dict[str, Any]:
        """Engine-side existing-album duplicate & move reconciliation planning (SEC-002 Wave 20)."""
        return self._request("POST", "/albums/existing-reconcile/plan", payload, timeout=timeout)

    def apply_existing_album_reconcile(self, operation_id: str, *, timeout: float = 60.0) -> Dict[str, Any]:
        """Engine-side existing-album duplicate & move reconciliation application (SEC-002 Wave 20)."""
        return self._request("POST", "/albums/existing-reconcile/apply", {"operation_id": operation_id}, timeout=timeout)

    def rollback_existing_album_reconcile(self, operation_id: str, *, timeout: float = 60.0) -> Dict[str, Any]:
        """Engine-side existing-album duplicate & move reconciliation rollback (SEC-002 Wave 20)."""
        return self._request("POST", "/albums/existing-reconcile/rollback", {"operation_id": operation_id}, timeout=timeout)

    def plan_artist_folder_reconcile(self, payload: Dict[str, Any], *, timeout: float = 30.0) -> Dict[str, Any]:
        """Engine-side artist-folder merge & MBID stamping planning (SEC-002 Wave 21)."""
        return self._request("POST", "/artists/reconcile/plan", payload, timeout=timeout)

    def apply_artist_folder_reconcile(self, operation_id: str, *, timeout: float = 60.0) -> Dict[str, Any]:
        """Engine-side artist-folder merge & MBID stamping application (SEC-002 Wave 21)."""
        return self._request("POST", "/artists/reconcile/apply", {"operation_id": operation_id}, timeout=timeout)

    def rollback_artist_folder_reconcile(self, operation_id: str, *, timeout: float = 60.0) -> Dict[str, Any]:
        """Engine-side artist-folder merge & MBID stamping rollback (SEC-002 Wave 21)."""
        return self._request("POST", "/artists/reconcile/rollback", {"operation_id": operation_id}, timeout=timeout)

    def plan_album_maintenance(self, payload: Dict[str, Any], *, timeout: float = 30.0) -> Dict[str, Any]:
        """Engine-side album maintenance planning (SEC-002 Wave 22)."""
        return self._request("POST", "/albums/maintenance/plan", payload, timeout=timeout)

    def apply_album_maintenance(self, operation_id: str, *, timeout: float = 60.0) -> Dict[str, Any]:
        """Engine-side album maintenance application (SEC-002 Wave 22)."""
        return self._request("POST", "/albums/maintenance/apply", {"operation_id": operation_id}, timeout=timeout)

    def rollback_album_maintenance(self, operation_id: str, *, timeout: float = 60.0) -> Dict[str, Any]:
        """Engine-side album maintenance rollback (SEC-002 Wave 22)."""
        return self._request("POST", "/albums/maintenance/rollback", {"operation_id": operation_id}, timeout=timeout)

    def plan_album_artwork(self, payload: Dict[str, Any], *, timeout: float = 30.0) -> Dict[str, Any]:
        """Engine-side album artwork planning (SEC-002 Wave 22)."""
        return self._request("POST", "/albums/artwork/plan", payload, timeout=timeout)

    def apply_album_artwork(self, operation_id: str, *, timeout: float = 60.0) -> Dict[str, Any]:
        """Engine-side album artwork application (SEC-002 Wave 22)."""
        return self._request("POST", "/albums/artwork/apply", {"operation_id": operation_id}, timeout=timeout)

    def rollback_album_artwork(self, operation_id: str, *, timeout: float = 60.0) -> Dict[str, Any]:
        """Engine-side album artwork rollback (SEC-002 Wave 22)."""
        return self._request("POST", "/albums/artwork/rollback", {"operation_id": operation_id}, timeout=timeout)

    def plan_import_folder(self, payload: Dict[str, Any], *, timeout: float = 30.0) -> Dict[str, Any]:
        """Engine-side import folder planning (SEC-002 Wave 22 Closure)."""
        return self._request("POST", "/import/plan", payload, timeout=timeout)

    def apply_import_folder(self, operation_id: str, *, timeout: float = 120.0) -> Dict[str, Any]:
        """Engine-side import folder application (SEC-002 Wave 22 Closure)."""
        return self._request("POST", "/import/apply", {"operation_id": operation_id}, timeout=timeout)

    def rollback_import_folder(self, operation_id: str, *, timeout: float = 60.0) -> Dict[str, Any]:
        """Engine-side import folder rollback (SEC-002 Wave 22 Closure)."""
        return self._request("POST", "/import/rollback", {"operation_id": operation_id}, timeout=timeout)

    def plan_folder_cleanup(self, payload: Dict[str, Any], *, timeout: float = 30.0) -> Dict[str, Any]:
        """Engine-side folder cleanup planning (SEC-002 Wave 22 Closure)."""
        return self._request("POST", "/folders/cleanup/plan", payload, timeout=timeout)

    def apply_folder_cleanup(self, operation_id: str, *, timeout: float = 60.0) -> Dict[str, Any]:
        """Engine-side folder cleanup application (SEC-002 Wave 22 Closure)."""
        return self._request("POST", "/folders/cleanup/apply", {"operation_id": operation_id}, timeout=timeout)

    def rollback_folder_cleanup(self, operation_id: str, *, timeout: float = 60.0) -> Dict[str, Any]:
        """Engine-side folder cleanup rollback (SEC-002 Wave 22 Closure)."""
        return self._request("POST", "/folders/cleanup/rollback", {"operation_id": operation_id}, timeout=timeout)

    def plan_playlist_media_cleanup(self, payload: Dict[str, Any], *, timeout: float = 30.0) -> Dict[str, Any]:
        """Engine-side playlist media cleanup planning (SEC-002 Wave 22 Closure)."""
        return self._request("POST", "/playlists/media-cleanup/plan", payload, timeout=timeout)

    def apply_playlist_media_cleanup(self, operation_id: str, *, timeout: float = 60.0) -> Dict[str, Any]:
        """Engine-side playlist media cleanup application (SEC-002 Wave 22 Closure)."""
        return self._request("POST", "/playlists/media-cleanup/apply", {"operation_id": operation_id}, timeout=timeout)

    def rollback_playlist_media_cleanup(self, operation_id: str, *, timeout: float = 60.0) -> Dict[str, Any]:
        """Engine-side playlist media cleanup rollback (SEC-002 Wave 22 Closure)."""
        return self._request("POST", "/playlists/media-cleanup/rollback", {"operation_id": operation_id}, timeout=timeout)





    def get_transaction(self, transaction_id: str, *, format: str = "json", timeout: float = 15.0) -> Dict[str, Any]:
        """Fetch transaction record from engine TransactionStore (SEC-002 Wave 15)."""
        return self._request("GET", f"/transactions/{transaction_id}?format={format}", timeout=timeout)

    def list_transactions(self, *, offset: int = 0, limit: int = 50, status: str = "", operation: str = "", query: str = "", timeout: float = 15.0) -> Dict[str, Any]:
        """List transaction records from engine TransactionStore (SEC-002 Wave 15)."""
        params = f"?offset={offset}&limit={limit}"
        if status:
            params += f"&status={urllib.parse.quote(status)}"
        if operation:
            params += f"&operation={urllib.parse.quote(operation)}"
        if query:
            params += f"&query={urllib.parse.quote(query)}"
        return self._request("GET", f"/transactions{params}", timeout=timeout)

    def reimport_source(self, source_path: str, expected_source_signature: Optional[str] = None, expected_deterministic_identity: Optional[Dict[str, Any]] = None, beets_options: Optional[Dict[str, Any]] = None, *, timeout: float = 180.0) -> Dict[str, Any]:
        """Engine-side atomic reimport operation (SEC-002 Wave 8 ARCH-003)."""
        payload: Dict[str, Any] = {"source_path": source_path}
        if expected_source_signature:
            payload["expected_source_signature"] = expected_source_signature
        if expected_deterministic_identity:
            payload["expected_deterministic_identity"] = expected_deterministic_identity
        if beets_options:
            payload["beets_options"] = beets_options
        return self._request("POST", "/imports/reimport", payload, timeout=timeout + 5.0)

    def run_command(self, command: str, args: Optional[List[str]] = None, timeout: float = 120.0, config_override: str = "", source_path: str = "") -> Dict[str, Any]:
        """Execute a beet command synchronously on the Beets control agent."""
        payload = {
            "command": command,
            "args": args or [],
            "timeout": timeout,
            "config_override": config_override,
        }
        if source_path:
            payload["source_path"] = source_path

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

    def ensure_playlist_staging(self, playlist_key: str, playlist_id: str = "", name: str = "") -> Dict[str, Any]:
        """Ensure playlist download and import staging directories exist via engine control agent."""
        return self._request("POST", "/playlists/staging/ensure", {
            "playlist_key": playlist_key,
            "playlist_id": playlist_id,
            "name": name,
        })

    def delete_playlist_staged_track(self, playlist_key: str, track_id: str, requested_path: str = "") -> Dict[str, Any]:
        """Delete a staged track file via engine control agent under OS lock and containment checks."""
        return self._request("POST", "/playlists/staging/delete-track", {
            "playlist_key": playlist_key,
            "track_id": track_id,
            "requested_path": requested_path,
        })

    def inspect_playlist_staged_track(self, playlist_key: str, track_id: str, requested_path: str = "") -> Dict[str, Any]:
        """Inspect a staged playlist track via the engine-owned filesystem boundary."""
        return self._request("POST", "/playlists/staging/inspect-track", {
            "playlist_key": playlist_key,
            "track_id": track_id,
            "requested_path": requested_path,
        })

    def import_playlist_staged(self,
                              playlist_key: str,
                              playlist_id: str,
                              tracks: List[Dict[str, Any]],
                              *,
                              operation_id: str = "") -> Dict[str, Any]:
        """Request the Beets engine to re-verify, enrich, move, and import staged playlist tracks."""
        return self._request("POST", "/playlists/import-staged", {
            "playlist_key": playlist_key,
            "playlist_id": playlist_id,
            "operation_id": operation_id,
            "tracks": tracks,
        })

    def list_playlist_staged_files(self, playlist_key: str, playlist_id: str = "") -> Dict[str, Any]:
        """List regular non-symlink audio files in engine playlist staging."""
        return self._request("POST", "/playlists/staging/list-files", {
            "playlist_key": playlist_key,
            "playlist_id": playlist_id,
        })

    def validate_playlist_staged_track(self,
                                      playlist_key: str,
                                      requested_path: str,
                                      artist: str = "",
                                      title: str = "",
                                      expected_mb_trackid: str = "",
                                      preferences: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Validate audio format preferences and fingerprint identity engine-side."""
        return self._request("POST", "/playlists/staging/validate-track", {
            "playlist_key": playlist_key,
            "requested_path": requested_path,
            "artist": artist,
            "title": title,
            "expected_mb_trackid": expected_mb_trackid,
            "preferences": preferences or {},
        })

    def get_playlist_quality_candidates(self,
                                        filter_mode: str = "all",
                                        limit: int = 200,
                                        item_ids: Optional[List[int]] = None) -> Dict[str, Any]:
        """Query engine library for playlist quality repair/cleanup candidates."""
        return self._request("POST", "/playlists/quality-candidates", {
            "filter_mode": filter_mode,
            "limit": limit,
            "item_ids": item_ids,
        })

    def place_playlist_imported_item(self,
                                    playlist_key: str,
                                    item_id: int,
                                    placement: Optional[Dict[str, Any]] = None,
                                    action: str = "repair",
                                    playlist_id: str = "") -> Dict[str, Any]:
        """Reconcile and place an imported item in the engine library under OS lock."""
        return self._request("POST", "/playlists/place-imported", {
            "playlist_key": playlist_key,
            "playlist_id": playlist_id,
            "item_id": item_id,
            "placement": placement or {},
            "action": action,
        })

    def export_playlist_m3u(self, playlist_key: str, display_name: str, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Export an M3U playlist file engine-side under PLAYLIST_DIR."""
        return self._request("POST", "/playlists/export_m3u", {
            "playlist_key": playlist_key,
            "display_name": display_name,
            "items": items,
        })

    def read_playlist_m3u(self, playlist_key: str, fallback_name: str = "") -> Dict[str, Any]:
        """Read and parse an M3U playlist file engine-side under PLAYLIST_DIR."""
        return self._request("POST", "/playlists/m3u/read", {
            "playlist_key": playlist_key,
            "fallback_name": fallback_name,
        })

    def delete_playlist_m3u(self, playlist_key: str, fallback_name: str = "") -> Dict[str, Any]:
        """Delete an M3U playlist file engine-side under PLAYLIST_DIR."""
        return self._request("POST", "/playlists/m3u/delete", {
            "playlist_key": playlist_key,
            "fallback_name": fallback_name,
        })

    def list_playlist_m3u(self) -> Dict[str, Any]:
        """List all M3U playlist files engine-side under PLAYLIST_DIR."""
        return self._request("POST", "/playlists/m3u/list")

    # Purpose-built structured library methods
    def get_item(self, item_id: int) -> Optional[Dict[str, Any]]:
        """Fetch single item dict by ID."""
        res = self._request("GET", f"/items/{item_id}")
        return res.get("item")

    def get_album(self, album_id: int) -> Optional[Dict[str, Any]]:
        """Fetch single album dict by ID (includes items list)."""
        res = self._request("GET", f"/albums/{album_id}")
        return res.get("album")

    def get_items_page(self, offset: int = 0, limit: int = 50) -> Dict[str, Any]:
        """Fetch a single page of items directly from the Beets agent without auto-paginating all pages."""
        off = max(0, offset)
        lim = max(1, limit)
        res = self._request("GET", f"/items?offset={off}&limit={lim}")
        items = res.get("items", [])
        total = res.get("total", len(items))
        return {
            "items": items,
            "offset": off,
            "limit": lim,
            "returned": len(items),
            "total": total,
        }

    def _fetch_all_paginated(self, endpoint_base: str, key: str, page_size: int = 500, safety_ceiling: int = 100000) -> List[Dict[str, Any]]:
        """Helper to fetch all pages for endpoint_base with strict validation and error handling."""
        sep = "&" if "?" in endpoint_base else "?"
        all_results = []
        seen_ids = set()
        offset = 0

        while True:
            url = f"{endpoint_base}{sep}offset={offset}&limit={page_size}"
            res = self._request("GET", url)
            page_rows = res.get(key, [])
            count = res.get("count", len(page_rows))
            total = res.get("total")
            has_more = res.get("has_more", False)
            next_offset = res.get("next_offset")
            resp_offset = res.get("offset")

            if resp_offset is not None and resp_offset != offset:
                raise BeetsError(f"Pagination offset mismatch: requested {offset}, got {resp_offset}")

            if count != len(page_rows):
                raise BeetsError(f"Pagination count mismatch: reported count {count} != actual rows length {len(page_rows)}")

            if has_more and not page_rows:
                raise BeetsError("Server returned empty page with has_more=True")

            if has_more and next_offset is None:
                raise BeetsError("Server returned has_more=True but next_offset is None")

            if has_more and (next_offset is not None and next_offset <= offset):
                raise BeetsError(f"Inconsistent next_offset progress: current {offset}, next {next_offset}")

            if total is not None and (offset + len(page_rows) >= total) and has_more:
                raise BeetsError(f"Inconsistent total metadata: offset {offset} + len {len(page_rows)} >= total {total} but has_more=True")

            for row in page_rows:
                row_id = row.get("id")
                if row_id is None or not isinstance(row_id, int):
                    raise BeetsError(f"Record missing valid integer ID: {row}")
                if row_id in seen_ids:
                    raise BeetsError(f"Duplicate ID {row_id} encountered across pagination pages")
                seen_ids.add(row_id)

            all_results.extend(page_rows)

            if len(all_results) >= safety_ceiling:
                raise BeetsError(f"Safety ceiling reached while paginating {key} ({len(all_results)} >= {safety_ceiling})")

            if not has_more:
                break

            offset = next_offset

        return all_results

    def find_all_items_by_album_id(self, album_id: int) -> List[Dict[str, Any]]:
        """Fetch all items belonging to album_id with complete pagination."""
        return self._fetch_all_paginated(f"/items?album_id={int(album_id)}", "items", safety_ceiling=100000)

    def find_all_items_by_mbid(self, mbid: str) -> List[Dict[str, Any]]:
        """Fetch all items by MusicBrainz track ID with complete pagination."""
        if not mbid or not mbid.strip():
            raise BeetsError("MusicBrainz track ID cannot be empty")
        return self._fetch_all_paginated(f"/items?mbid={urllib.parse.quote(mbid.strip())}", "items", safety_ceiling=100000)

    def find_all_items_by_path(self, path: str) -> List[Dict[str, Any]]:
        """Fetch all items by file path with complete pagination."""
        if not path or not path.strip():
            raise BeetsError("Path cannot be empty")
        return self._fetch_all_paginated(f"/items?path={urllib.parse.quote(path.strip())}", "items", safety_ceiling=100000)

    def find_all_items_by_singleton(self, is_singleton: bool) -> List[Dict[str, Any]]:
        """Fetch all singleton items with complete pagination."""
        val = "true" if is_singleton else "false"
        return self._fetch_all_paginated(f"/items?singleton={val}", "items", safety_ceiling=100000)

    def find_all_albums_by_mb_albumid(self, mb_albumid: str) -> List[Dict[str, Any]]:
        """Fetch all albums by MusicBrainz release ID with complete pagination."""
        if not mb_albumid or not mb_albumid.strip():
            raise BeetsError("MusicBrainz album ID cannot be empty")
        return self._fetch_all_paginated(f"/albums?mb_albumid={urllib.parse.quote(mb_albumid.strip())}", "albums", safety_ceiling=20000)

    def find_all_albums_by_releasegroupid(self, rg_id: str) -> List[Dict[str, Any]]:
        """Fetch all albums by MusicBrainz Release Group ID with complete pagination."""
        if not rg_id or not rg_id.strip():
            raise BeetsError("MusicBrainz releasegroup ID cannot be empty")
        return self._fetch_all_paginated(f"/albums?mb_releasegroupid={urllib.parse.quote(rg_id.strip())}", "albums", safety_ceiling=20000)

    def find_all_items_for_term(self, query_str: str, page_size: int = 500, safety_ceiling: int = 100000) -> List[Dict[str, Any]]:
        """Fetch ALL matching items for a query term by fully paginating through all available pages with strict validation."""
        if not query_str or not query_str.strip():
            return []
        return self._fetch_all_paginated(f"/items?query={urllib.parse.quote(query_str.strip())}", "items", page_size=page_size, safety_ceiling=safety_ceiling)

    def find_all_albums_for_term(self, query_str: str, page_size: int = 500, safety_ceiling: int = 20000) -> List[Dict[str, Any]]:
        """Fetch ALL matching albums for a query term by fully paginating through all available pages with strict validation."""
        if not query_str or not query_str.strip():
            return []
        return self._fetch_all_paginated(f"/albums?query={urllib.parse.quote(query_str.strip())}", "albums", page_size=page_size, safety_ceiling=safety_ceiling)

    def find_items_by_album_id(self, album_id: int) -> List[Dict[str, Any]]:
        return self.find_all_items_by_album_id(album_id)

    def find_items_by_path(self, path: str) -> List[Dict[str, Any]]:
        return self.find_all_items_by_path(path)

    def find_items_by_mbid(self, mbid: str) -> List[Dict[str, Any]]:
        return self.find_all_items_by_mbid(mbid)

    def find_items_by_query(self, query: str, limit: int = 500) -> List[Dict[str, Any]]:
        return self.find_all_items_for_term(query, page_size=limit)

    def find_albums_by_query(self, query: str, limit: int = 500) -> List[Dict[str, Any]]:
        return self.find_all_albums_for_term(query, page_size=limit)

    def search_items_text(self, text: str, limit: int = 500) -> List[Dict[str, Any]]:
        """Perform a parameterized bare-word text search across item fields (auto-paginates all matching pages)."""
        return self.find_all_items_for_term(text, page_size=limit)

    def search_albums_text(self, text: str, limit: int = 500) -> List[Dict[str, Any]]:
        """Perform a parameterized bare-word text search across album fields (auto-paginates all matching pages)."""
        return self.find_all_albums_for_term(text, page_size=limit)

    def find_all_items_by_bare_text(self, text: str, limit: int = 500) -> List[Dict[str, Any]]:
        return self.search_items_text(text, limit=limit)

    def find_all_albums_by_bare_text(self, text: str, limit: int = 500) -> List[Dict[str, Any]]:
        return self.search_albums_text(text, limit=limit)

    def list_all_albums(self, page_size: int = 500, safety_ceiling: int = 20000) -> List[Dict[str, Any]]:
        """Fetch all albums by paginating through all available pages up to safety ceiling."""
        return self._fetch_all_paginated("/albums", "albums", page_size=page_size, safety_ceiling=safety_ceiling)

    def list_all_items(self, page_size: int = 1000, safety_ceiling: int = 100000) -> List[Dict[str, Any]]:
        """Fetch all items by paginating through all available pages up to safety ceiling."""
        return self._fetch_all_paginated("/items", "items", page_size=page_size, safety_ceiling=safety_ceiling)

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

    def replace_album_art(
        self,
        album_id: int,
        image_data_b64: str,
        *,
        source: str = "user",
        expected_mb_releasegroupid: str = "",
    ) -> Dict[str, Any]:
        """Atomically replace album artwork inside the Beets engine.

        SEC-002 / ARCH-003 Wave 24 final review: a prior revision of this
        method routed through album_artwork_v1's "move" mode by base64-
        decoding the image and writing a staging file to a local
        `STAGING_ROOT` path *on this (web-manager) container*, then
        referencing that local path as a "source" candidate in the HTTP
        request to the engine. That is broken on two independent levels:
        (1) the standard web-manager deployment (docker-compose.yml) runs
        this container with `read_only: true` and no `/config` volume
        mount at all, so the local `stg_base.mkdir()`/`write_bytes()`
        calls raise immediately in production; (2) even with a writable
        filesystem, the engine is a *separate container/process* per the
        two-service architecture -- a path written to this container's
        disk is not visible to the engine's filesystem unless explicitly
        bind-mounted identically into both, which is not part of this
        architecture. Independently, `album_artwork_v1`'s "move"-mode
        apply (`execute_album_artwork_apply`, in the engine's own
        transaction module) never writes the album's `artpath` DB column
        for mode=="move" --
        only mode=="quarantine" updates the DB -- so even a
        same-container deployment would silently leave the album's
        artwork pointer stale after a "successful" replace. Restored to
        the pure single-call HTTP proxy: the engine already implements
        replace as an atomic tempfile+fsync+os.replace with DB update and
        rollback-on-failure (`_replace_album_art_locked`), entirely
        within its own container. Migrating this onto album_artwork_v1
        for real requires first teaching that family's "move" mode to
        also update `albums.artpath` -- tracked in
        docs/TECHNICAL_DEBT.md, not attempted this pass."""
        if isinstance(image_data_b64, bytes):
            image_data_b64 = base64.b64encode(image_data_b64).decode("utf-8")
        return self._request("POST", f"/albums/{album_id}/art", {
            "image_data_b64": image_data_b64,
            "source": source,
            "expected_mb_releasegroupid": expected_mb_releasegroupid,
        })

    def delete_album_art(self, album_id: int) -> Dict[str, Any]:
        """Quarantine local album artwork and clear artpath inside the Beets engine.

        See the note on replace_album_art above: a prior revision of this
        method probed `album_dir / art_name` paths directly from this
        container via `Path.exists()`/`Path.is_file()`, which requires
        this container to have direct filesystem access to the media
        library -- it does not, by design (two-service architecture,
        engine owns the filesystem). Restored to the pure HTTP proxy."""
        return self._request("DELETE", f"/albums/{album_id}/art")

    def rewrite_library_path(self, old_path: str, new_path: str) -> Dict[str, Any]:
        """Rewrite Beets item/art paths after a validated library file move."""
        return self._request("POST", "/library/rewrite-path", {"old_path": old_path, "new_path": new_path})

    def create_hardlink(self, source_path: str, target_path: str, expected_size: Optional[int] = None) -> Dict[str, Any]:
        """Create a hardlink under control-agent validation and lock."""
        payload: Dict[str, Any] = {"source_path": source_path, "target_path": target_path}
        if expected_size is not None:
            payload["expected_size"] = expected_size
        return self._request("POST", "/files/hardlink", payload)

    def delete_album(self, album_id: int, delete_files: bool = True) -> Dict[str, Any]:
        """Perform transaction-controlled album removal through album_maintenance_v1 family."""
        album_data = self.get_album(album_id) or {}
        items = album_data.get("items") or []
        item_ids = [int(it.get("id") or 0) for it in items if int(it.get("id") or 0) > 0]
        if not item_ids:
            return {"ok": False, "error": f"Album {album_id} has no track items"}

        plan_res = self.plan_album_maintenance({
            "mode": "remove_tracks",
            "album_id": album_id,
            "item_ids": item_ids,
            "delete_files": delete_files,
            "clean_empty_folders": True,
        })
        if not plan_res.get("ok"):
            return {"ok": False, "error": plan_res.get("error") or "Album removal plan rejected", "database_deleted": False, "status": "failed"}
        op_id = plan_res.get("operation_id")
        if not op_id:
            return {"ok": True, "database_deleted": True, "items_deleted": 0, "files_deleted": 0, "status": "completed"}
        apply_res = self.apply_album_maintenance(op_id)
        if not apply_res.get("ok"):
            return {"ok": False, "error": apply_res.get("error") or "Album removal apply failed", "database_deleted": False, "status": "failed"}
        return {
            "ok": True,
            "status": "completed",
            "database_deleted": True,
            "items_deleted": len(item_ids),
            "files_deleted": len(item_ids) if delete_files else 0,
        }

    # ── album_relocation_v1 Pure HTTP Client Methods ────────────────────────────

    def plan_album_relocation(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Request preview plan for album relocation via HTTP."""
        return self._request("POST", "/albums/relocation/plan", payload)

    def apply_album_relocation(self, operation_id: str) -> Dict[str, Any]:
        """Execute album relocation apply via HTTP."""
        return self._request("POST", "/albums/relocation/apply", {"operation_id": operation_id})

    def rollback_album_relocation(self, operation_id: str) -> Dict[str, Any]:
        """Roll back album relocation via HTTP."""
        return self._request("POST", "/albums/relocation/rollback", {"operation_id": operation_id})

    def relocate_album(self, album_id: int, mode: str = "rename", **kwargs) -> Dict[str, Any]:
        """Perform relocation (rename or move-to-library) through album_relocation_v1 family via HTTP."""
        payload = {"album_id": album_id, "mode": mode, **kwargs}
        plan_res = self.plan_album_relocation(payload)
        if not plan_res.get("ok"):
            return {"ok": False, "error": plan_res.get("error") or "Relocation plan rejected"}
        op_id = plan_res.get("operation_id")
        if not op_id:
            return {"ok": True, "dest_dir": plan_res.get("dest_dir") or "", "moved_count": 0}
        apply_res = self.apply_album_relocation(op_id)
        if not apply_res.get("ok"):
            return {"ok": False, "error": apply_res.get("error") or "Relocation apply failed"}
        return {"ok": True, "dest_dir": apply_res.get("dest_dir") or plan_res.get("dest_dir") or "", "moved_count": apply_res.get("moved_count", 0)}

    def move_album_to_library(self, album_id: int) -> Dict[str, Any]:
        """Move imported album items into authoritative MUSIC_ROOT through album_relocation_v1 family via HTTP."""
        return self.relocate_album(album_id, mode="move_to_library")

    # ── album_metadata_repair_v1 Pure HTTP Client Methods ──────────────────────

    def plan_album_metadata(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Request preview plan for album metadata update via HTTP."""
        return self._request("POST", "/albums/metadata/plan", payload)

    def apply_album_metadata(self, operation_id: str) -> Dict[str, Any]:
        """Execute album metadata apply via HTTP."""
        return self._request("POST", "/albums/metadata/apply", {"operation_id": operation_id})

    def rollback_album_metadata(self, operation_id: str) -> Dict[str, Any]:
        """Roll back album metadata update via HTTP."""
        return self._request("POST", "/albums/metadata/rollback", {"operation_id": operation_id})

    def update_album_metadata(self, album_id: int, updates: Dict[str, Any], item_updates: Optional[Dict[str, Any]] = None, **kwargs) -> Dict[str, Any]:
        """Update album metadata & audio tags through album_metadata_repair_v1 family via HTTP."""
        payload = {"album_id": album_id, "updates": updates, "item_updates": item_updates or {}, **kwargs}
        plan_res = self.plan_album_metadata(payload)
        if not plan_res.get("ok"):
            return {"ok": False, "error": plan_res.get("error") or "Metadata plan rejected"}
        op_id = plan_res.get("operation_id")
        if not op_id:
            return {"ok": True, "album_fields_changed": 0, "items_changed": 0}
        apply_res = self.apply_album_metadata(op_id)
        if not apply_res.get("ok"):
            return {"ok": False, "error": apply_res.get("error") or "Metadata apply failed"}
        return {"ok": True, "album_fields_changed": plan_res.get("album_fields_changed", 0), "items_changed": plan_res.get("items_changed", 0)}


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
    """Always returns a RemoteSQLiteConnection to the Beets control agent.
    Never opens local SQLite files in the web manager.

    `db_path` is accepted (and threaded through unused) only so app.py's
    `_db(path=None, ...)` context manager keeps its existing call shape
    (`get_db_connection(path)`; see tests/test_sqlite_db_timeout.py) -- it
    is NOT a local-sqlite escape hatch. PR #64 removed the local DB
    fallback specifically so production code can never silently read/write
    a stale on-disk copy instead of the authoritative remote database;
    resurrecting a path-triggered `sqlite3.connect()` branch here would
    reintroduce exactly that regression (see
    tests/test_external_beets_architecture.py's
    test_get_db_connection_never_uses_local_sqlite).

    Tests that need a real local SQLite connection (e.g. to seed rows for
    an integration test) must patch `app._db` itself, not rely on this
    function -- see tests/test_post_retag_artwork_integration.py's
    `_mock_db` fixture for the pattern.
    """
    del db_path  # intentionally unused; see docstring
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

            parsed_list = [parse_query_term(term, "items") for term in query]

            first_results = self.items(query[0])
            if not first_results or len(query) == 1:
                return first_results

            matching_ids = {item.id for item in first_results if item.id is not None}
            for term in query[1:]:
                if not matching_ids:
                    break
                term_results = self.items(term)
                term_ids = {item.id for item in term_results if item.id is not None}
                matching_ids = matching_ids & term_ids

            seen = set()
            final_items = []
            for item in first_results:
                if item.id in matching_ids and item.id not in seen:
                    seen.add(item.id)
                    final_items.append(item)
            return final_items

        if isinstance(query, str):
            pq = parse_query_term(query, "items")
            if pq.field == "album_id":
                items_data = self.client.find_all_items_by_album_id(int(pq.value))
            elif pq.field == "mb_trackid":
                items_data = self.client.find_all_items_by_mbid(pq.value)
            elif pq.field == "path":
                items_data = self.client.find_all_items_by_path(pq.value)
            elif pq.field == "singleton":
                items_data = self.client.find_all_items_by_singleton(pq.value == "true")
            elif pq.field in {"album", "artist", "title"}:
                items_data = self.client.find_all_items_for_term(f"{pq.field}:{pq.value}")
            else:
                items_data = self.client.find_all_items_for_term(pq.value)

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

            parsed_list = [parse_query_term(term, "albums") for term in query]

            first_results = self.albums(query[0])
            if not first_results or len(query) == 1:
                return first_results

            matching_ids = {album.id for album in first_results if album.id is not None}
            for term in query[1:]:
                if not matching_ids:
                    break
                term_results = self.albums(term)
                term_ids = {album.id for album in term_results if album.id is not None}
                matching_ids = matching_ids & term_ids

            seen = set()
            final_albums = []
            for album in first_results:
                if album.id in matching_ids and album.id not in seen:
                    seen.add(album.id)
                    final_albums.append(album)
            return final_albums

        if isinstance(query, str):
            pq = parse_query_term(query, "albums")
            if pq.field == "mb_albumid":
                albums_data = self.client.find_all_albums_by_mb_albumid(pq.value)
            elif pq.field == "mb_releasegroupid":
                albums_data = self.client.find_all_albums_by_releasegroupid(pq.value)
            elif pq.field in {"album", "artist", "albumartist"}:
                albums_data = self.client.find_all_albums_for_term(f"{pq.field}:{pq.value}")
            else:
                albums_data = self.client.find_all_albums_for_term(pq.value)

            return [RemoteAlbum(r) for r in albums_data]

        raise BeetsError(f"Unsupported RemoteLibrary albums() query shape: {query!r}")


# Module singleton instances
beets_client = BeetsClient()
lib = RemoteLibrary(beets_client)
