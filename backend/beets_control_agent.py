"""Beets Control Agent — runs inside the beets container.

Provides a secure, restricted internal HTTP control interface to the
authoritative Beets installation and database.
"""

try:
    import fcntl
except ImportError:
    fcntl = None
import hmac
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import tempfile
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

# Configuration
PORT = int(os.environ.get("BEETS_AGENT_PORT", "8338"))
BEETS_API_TOKEN = os.environ.get("BEETS_API_TOKEN", "")
BEETSDIR = os.environ.get("BEETSDIR", "/config")
MUSIC_LIBRARY_PATH = os.environ.get("MUSIC_LIBRARY_PATH", "/data/media/music")
DOWNLOAD_PATH = os.environ.get("DOWNLOAD_PATH", "/data/torrents")
LOCK_PATH = os.environ.get("BEETS_LOCK_PATH", os.path.join(BEETSDIR, ".beet_db.lock"))
LIB_PATH = os.path.join(BEETSDIR, "musiclibrary.blb")
BEET_BIN = os.environ.get("BEET_BIN", "beet")

ALLOWED_COMMANDS = {
    "import", "update", "write", "move", "modify", "ls", "stats", "fields",
    "mbsync", "fetchart", "embedart", "lastgenre", "lastimport", "alt",
    "version", "config", "check", "remove", "rm"
}

JOBS = {}
JOBS_LOCK = threading.Lock()


def is_safe_path(path: str, allowed_types: list = None) -> bool:
    """Verify that path stays strictly within allowed root directories without traversal or symlink escape."""
    if not path or not isinstance(path, str):
        return False
    if "\x00" in path:
        return False

    # Decode URL encoding if present
    try:
        decoded = urllib.parse.unquote(path)
    except Exception:
        decoded = path

    if "\x00" in decoded or "\\" in decoded:
        return False

    # Path must be absolute
    if not decoded.startswith("/"):
        return False

    parts = decoded.split("/")
    if ".." in parts or "." in parts:
        return False

    all_roots = {
        "config": [os.path.abspath(BEETSDIR), "/config"],
        "music": [os.path.abspath(MUSIC_LIBRARY_PATH), "/data/media/music"],
        "staging": [os.path.abspath(DOWNLOAD_PATH), "/data/torrents"],
        "tmp": ["/tmp", tempfile.gettempdir()],
    }

    roots_to_check = []
    if allowed_types:
        for t in allowed_types:
            if t in all_roots:
                roots_to_check.extend(all_roots[t])
    else:
        for r_list in all_roots.values():
            roots_to_check.extend(r_list)

    try:
        abs_path = os.path.abspath(decoded)
        real_path = os.path.realpath(abs_path)

        for root in roots_to_check:
            real_root = os.path.realpath(root)
            if real_path == real_root or real_path.startswith(real_root + os.sep) or real_path.startswith(real_root + "/"):
                return True
        return False
    except Exception:
        return False


def acquire_os_lock(read_only: bool = False):
    """Acquire an OS file lock on LOCK_PATH for Beets concurrency protection."""
    os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
    lock_file = open(LOCK_PATH, "a+")
    if fcntl is not None:
        mode = fcntl.LOCK_SH if read_only else fcntl.LOCK_EX
        fcntl.flock(lock_file.fileno(), mode)
    return lock_file


def release_os_lock(lock_file):
    """Release the OS file lock."""
    if lock_file:
        try:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()
        except Exception:
            pass


class AgentJob:
    def __init__(self, job_id: str, command: list, label: str = "", config_override: str = ""):
        self.job_id = job_id
        self.command = command
        self.label = label or " ".join(command)
        self.config_override = config_override
        self.created_at = time.time()
        self.started_at = None
        self.finished_at = None
        self.returncode = None
        self.stdout = []
        self.stderr = []
        self.proc = None
        self._cancel = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def cancel(self):
        self._cancel.set()
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                time.sleep(0.2)
                if self.proc.poll() is None:
                    self.proc.kill()
            except Exception:
                pass

    def _run(self):
        self.started_at = time.time()
        lock_file = None
        tmp_cfg_path = None
        try:
            mutating = self.command and self.command[0] in {
                "import", "update", "write", "move", "modify", "mbsync",
                "fetchart", "embedart", "lastgenre", "alt", "remove", "rm"
            }
            lock_file = acquire_os_lock(read_only=not mutating)

            full_cmd = [BEET_BIN]
            if self.config_override:
                tmp_cfg_path = f"/tmp/beets_job_cfg_{self.job_id}.yaml"
                with open(tmp_cfg_path, "w", encoding="utf-8") as f:
                    f.write(self.config_override)
                os.chmod(tmp_cfg_path, 0o600)
                full_cmd.extend(["-c", tmp_cfg_path])

            full_cmd.extend(self.command)
            env = os.environ.copy()
            env["BEETSDIR"] = BEETSDIR

            self.proc = subprocess.Popen(
                full_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env
            )
            out, err = self.proc.communicate(timeout=600)
            self.returncode = self.proc.returncode
            if out:
                self.stdout = out.splitlines()[-5000:]
            if err:
                self.stderr = err.splitlines()[-5000:]
        except subprocess.TimeoutExpired:
            if self.proc:
                self.proc.kill()
            self.returncode = 124
            self.stderr.append("Command timed out after 600 seconds")
        except Exception as exc:
            self.returncode = 1
            self.stderr.append(f"Execution error: {exc}")
        finally:
            if tmp_cfg_path and os.path.exists(tmp_cfg_path):
                try:
                    os.unlink(tmp_cfg_path)
                except Exception:
                    pass
            release_os_lock(lock_file)
            self.finished_at = time.time()

    def to_dict(self, include_logs=True):
        status = "running"
        if self.finished_at is not None:
            if self._cancel.is_set():
                status = "cancelled"
            else:
                status = "success" if self.returncode == 0 else "failed"

        d = {
            "job_id": self.job_id,
            "label": self.label,
            "status": status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "returncode": self.returncode,
        }
        if include_logs:
            d["stdout"] = self.stdout
            d["stderr"] = self.stderr
        return d


def _handle_delete_album(album_id: int, delete_files: bool = True) -> tuple:
    lock = acquire_os_lock(read_only=False)
    try:
        if not os.path.exists(LIB_PATH):
            return 404, {"error": "Database musiclibrary.blb not found", "status": "failed", "database_deleted": False}

        con = sqlite3.connect(LIB_PATH, timeout=30)
        con.row_factory = sqlite3.Row
        try:
            cur = con.cursor()
            cur.execute("SELECT * FROM albums WHERE id = ?", (album_id,))
            album_row = cur.fetchone()
            if not album_row:
                return 404, {"error": f"Album {album_id} not found", "status": "failed", "database_deleted": False}

            album_dict = dict(album_row)
            album_path = album_dict.get("path")
            if isinstance(album_path, bytes):
                try:
                    album_path = album_path.decode("utf-8")
                except Exception:
                    album_path = str(album_path)

            cur.execute("SELECT * FROM items WHERE album_id = ?", (album_id,))
            item_rows = cur.fetchall()
            item_files = []
            for item in item_rows:
                ipath = item["path"]
                if isinstance(ipath, bytes):
                    try:
                        ipath = ipath.decode("utf-8")
                    except Exception:
                        ipath = str(ipath)
                if ipath:
                    item_files.append(ipath)

            cur.execute("BEGIN TRANSACTION")
            cur.execute("DELETE FROM items WHERE album_id = ?", (album_id,))
            items_deleted = cur.rowcount
            cur.execute("DELETE FROM albums WHERE id = ?", (album_id,))
            albums_deleted = cur.rowcount
            con.commit()

            files_deleted = 0
            file_errors = []
            if delete_files:
                for fpath in item_files:
                    if fpath and is_safe_path(fpath, ["music", "staging"]) and os.path.exists(fpath):
                        try:
                            os.unlink(fpath)
                            files_deleted += 1
                        except Exception as exc:
                            file_errors.append(f"Failed to delete {fpath}: {exc}")

                if album_path and is_safe_path(album_path, ["music"]) and os.path.exists(album_path):
                    try:
                        if os.path.isdir(album_path) and not os.listdir(album_path):
                            os.rmdir(album_path)
                    except Exception:
                        pass

            files_failed = len(file_errors)
            if files_failed > 0:
                status_str = "partial_failure"
                success_flag = False
            else:
                status_str = "success"
                success_flag = True

            return 200, {
                "success": success_flag,
                "status": status_str,
                "database_deleted": True,
                "album_id": album_id,
                "items_deleted": items_deleted,
                "albums_deleted": albums_deleted,
                "files_deleted": files_deleted,
                "files_failed": files_failed,
                "file_errors": file_errors,
            }
        except Exception as exc:
            con.rollback()
            return 500, {"error": f"Database transaction failed: {exc}", "status": "failed", "database_deleted": False}
        finally:
            con.close()
    finally:
        release_os_lock(lock)


class ControlAgentHandler(BaseHTTPRequestHandler):
    def _send_json(self, code: int, data: dict):
        body = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authenticate(self) -> bool:
        if not BEETS_API_TOKEN:
            self._send_json(500, {"error": "Control Agent misconfigured: BEETS_API_TOKEN is missing"})
            return False

        header_token = self.headers.get("X-Beets-API-Token", "")
        if not header_token:
            auth_header = self.headers.get("Authorization", "")
            if auth_header.startswith("Bearer "):
                header_token = auth_header[7:]

        if not hmac.compare_digest(header_token.strip(), BEETS_API_TOKEN.strip()):
            self._send_json(401, {"error": "Unauthorized: invalid API token"})
            return False
        return True

    def log_message(self, format, *args):
        pass  # Quiet HTTP handler logging

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        params = parse_qs(parsed.query)

        if path == "/health":
            beets_ver = "unknown"
            try:
                res = subprocess.run([BEET_BIN, "version"], capture_output=True, text=True, timeout=5)
                beets_ver = res.stdout.splitlines()[0] if res.stdout else "available"
            except Exception:
                pass

            db_healthy = os.path.exists(LIB_PATH) and os.access(LIB_PATH, os.R_OK)
            config_healthy = os.path.exists(os.path.join(BEETSDIR, "config.yaml"))
            discpath_found = os.path.exists("/opt/beets-web-manager-agent/beetsplug/discpath.py") or os.path.exists(os.path.join(BEETSDIR, "beetsplug", "discpath.py"))

            self._send_json(200, {
                "status": "ok",
                "service": "beets-control-agent",
                "agent_version": "1.0.0",
                "beets_version": beets_ver,
                "db_healthy": db_healthy,
                "config_healthy": config_healthy,
                "plugins": {
                    "discpath": discpath_found
                },
                "beetsdir": BEETSDIR,
            })
            return

        if not self._authenticate():
            return

        if path == "/version":
            beets_ver = "unknown"
            try:
                res = subprocess.run([BEET_BIN, "version"], capture_output=True, text=True, timeout=5)
                beets_ver = res.stdout.splitlines()[0] if res.stdout else "available"
            except Exception:
                pass
            self._send_json(200, {"agent_version": "1.0.0", "beets_version": beets_ver})
            return

        if path == "/capabilities":
            self._send_json(200, {
                "allowed_commands": list(ALLOWED_COMMANDS),
                "os_locking": True,
                "strict_path_validation": True,
                "read_only_raw_query": True,
            })
            return

        if path == "/items":
            album_id = params.get("album_id", [None])[0]
            path_val = params.get("path", [None])[0]
            mbid = params.get("mbid", [None])[0] or params.get("mb_trackid", [None])[0]
            query = params.get("query", [None])[0]
            offset = max(int(params.get("offset", [0])[0]), 0)
            limit = min(max(int(params.get("limit", [500])[0]), 1), 2000)

            if not os.path.exists(LIB_PATH):
                self._send_json(404, {"error": "Database file musiclibrary.blb not found"})
                return

            lock = acquire_os_lock(read_only=True)
            try:
                con = sqlite3.connect(LIB_PATH, timeout=10)
                con.row_factory = sqlite3.Row
                cur = con.cursor()

                where_clause = ""
                sql_params = []
                if album_id:
                    where_clause = "WHERE album_id = ?"
                    sql_params.append(int(album_id))
                elif path_val:
                    where_clause = "WHERE path = ?"
                    sql_params.append(path_val)
                elif mbid:
                    where_clause = "WHERE mb_trackid = ?"
                    sql_params.append(mbid)
                elif query is not None:
                    q_str = str(query).strip()
                    if not q_str:
                        where_clause = "WHERE 1 = 0"
                    elif q_str.startswith("album:"):
                        where_clause = "WHERE album LIKE ? ESCAPE '\\'"
                        escaped = q_str[6:].strip().replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
                        sql_params.append(f"%{escaped}%")
                    elif q_str.startswith("artist:"):
                        where_clause = "WHERE artist LIKE ? ESCAPE '\\'"
                        escaped = q_str[7:].strip().replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
                        sql_params.append(f"%{escaped}%")
                    elif q_str.startswith("title:"):
                        where_clause = "WHERE title LIKE ? ESCAPE '\\'"
                        escaped = q_str[6:].strip().replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
                        sql_params.append(f"%{escaped}%")
                    elif q_str.startswith("path:"):
                        where_clause = "WHERE path = ?"
                        sql_params.append(q_str[5:].strip())
                    elif q_str.startswith("mb_trackid:") or q_str.startswith("mbid:"):
                        where_clause = "WHERE mb_trackid = ?"
                        sql_params.append(q_str.split(":", 1)[1].strip())
                    elif q_str == "singleton:true":
                        where_clause = "WHERE album_id IS NULL OR album_id = 0"
                    elif q_str == "singleton:false":
                        where_clause = "WHERE album_id IS NOT NULL AND album_id != 0"
                    else:
                        escaped = q_str.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
                        pattern = f"%{escaped}%"
                        where_clause = "WHERE (title LIKE ? ESCAPE '\\' OR artist LIKE ? ESCAPE '\\' OR album LIKE ? ESCAPE '\\' OR albumartist LIKE ? ESCAPE '\\' OR path LIKE ? ESCAPE '\\')"
                        sql_params.extend([pattern, pattern, pattern, pattern, pattern])

                cur.execute(f"SELECT COUNT(*) FROM items {where_clause}", sql_params)
                total_count = cur.fetchone()[0]

                cur.execute(f"SELECT * FROM items {where_clause} ORDER BY id LIMIT ? OFFSET ?", sql_params + [limit, offset])
                rows = [dict(r) for r in cur.fetchall()]
                con.close()

                has_more = (offset + len(rows)) < total_count
                next_offset = (offset + len(rows)) if has_more else None

                self._send_json(200, {
                    "items": rows,
                    "count": len(rows),
                    "total": total_count,
                    "offset": offset,
                    "limit": limit,
                    "has_more": has_more,
                    "next_offset": next_offset
                })
            except Exception as exc:
                self._send_json(500, {"error": f"Database error: {exc}"})
            finally:
                release_os_lock(lock)
            return

        if path.startswith("/items/"):
            try:
                item_id = int(path.split("/")[2])
            except ValueError:
                self._send_json(400, {"error": "Invalid item ID"})
                return

            if not os.path.exists(LIB_PATH):
                self._send_json(404, {"error": "Database file musiclibrary.blb not found"})
                return

            lock = acquire_os_lock(read_only=True)
            try:
                con = sqlite3.connect(LIB_PATH, timeout=10)
                con.row_factory = sqlite3.Row
                cur = con.cursor()
                cur.execute("SELECT * FROM items WHERE id = ?", (item_id,))
                row = cur.fetchone()
                if not row:
                    self._send_json(404, {"error": f"Item {item_id} not found"})
                else:
                    self._send_json(200, {"item": dict(row)})
            except Exception as exc:
                self._send_json(500, {"error": f"Database error: {exc}"})
            finally:
                release_os_lock(lock)
            return

        if path == "/albums":
            query = params.get("query", [None])[0]
            mb_albumid = params.get("mb_albumid", [None])[0]
            mb_releasegroupid = params.get("mb_releasegroupid", [None])[0]
            offset = max(int(params.get("offset", [0])[0]), 0)
            limit = min(max(int(params.get("limit", [500])[0]), 1), 2000)

            if not os.path.exists(LIB_PATH):
                self._send_json(404, {"error": "Database file musiclibrary.blb not found"})
                return

            lock = acquire_os_lock(read_only=True)
            try:
                con = sqlite3.connect(LIB_PATH, timeout=10)
                con.row_factory = sqlite3.Row
                cur = con.cursor()

                where_clause = ""
                sql_params = []
                if mb_albumid:
                    where_clause = "WHERE mb_albumid = ?"
                    sql_params.append(mb_albumid)
                elif mb_releasegroupid:
                    where_clause = "WHERE mb_releasegroupid = ?"
                    sql_params.append(mb_releasegroupid)
                elif query is not None:
                    q_str = str(query).strip()
                    if not q_str:
                        where_clause = "WHERE 1 = 0"
                    elif q_str.startswith("album:"):
                        where_clause = "WHERE album LIKE ? ESCAPE '\\'"
                        escaped = q_str[6:].strip().replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
                        sql_params.append(f"%{escaped}%")
                    elif q_str.startswith("artist:"):
                        where_clause = "WHERE albumartist LIKE ? ESCAPE '\\' OR artist LIKE ? ESCAPE '\\'"
                        escaped = q_str[7:].strip().replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
                        pattern = f"%{escaped}%"
                        sql_params.extend([pattern, pattern])
                    else:
                        escaped = q_str.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
                        pattern = f"%{escaped}%"
                        where_clause = "WHERE (album LIKE ? ESCAPE '\\' OR albumartist LIKE ? ESCAPE '\\' OR artist LIKE ? ESCAPE '\\')"
                        sql_params.extend([pattern, pattern, pattern])

                cur.execute(f"SELECT COUNT(*) FROM albums {where_clause}", sql_params)
                total_count = cur.fetchone()[0]

                cur.execute(f"SELECT * FROM albums {where_clause} ORDER BY id LIMIT ? OFFSET ?", sql_params + [limit, offset])
                rows = [dict(r) for r in cur.fetchall()]
                con.close()

                has_more = (offset + len(rows)) < total_count
                next_offset = (offset + len(rows)) if has_more else None

                self._send_json(200, {
                    "albums": rows,
                    "count": len(rows),
                    "total": total_count,
                    "offset": offset,
                    "limit": limit,
                    "has_more": has_more,
                    "next_offset": next_offset
                })
            except Exception as exc:
                self._send_json(500, {"error": f"Database error: {exc}"})
            finally:
                release_os_lock(lock)
            return

        if path.startswith("/albums/"):
            parts = path.split("/")
            if len(parts) == 3:
                try:
                    album_id = int(parts[2])
                except ValueError:
                    self._send_json(400, {"error": "Invalid album ID"})
                    return

                if not os.path.exists(LIB_PATH):
                    self._send_json(404, {"error": "Database file musiclibrary.blb not found"})
                    return

                lock = acquire_os_lock(read_only=True)
                try:
                    con = sqlite3.connect(LIB_PATH, timeout=10)
                    con.row_factory = sqlite3.Row
                    cur = con.cursor()
                    cur.execute("SELECT * FROM albums WHERE id = ?", (album_id,))
                    arow = cur.fetchone()
                    if not arow:
                        self._send_json(404, {"error": f"Album {album_id} not found"})
                    else:
                        adict = dict(arow)
                        cur.execute("SELECT * FROM items WHERE album_id = ? ORDER BY disc, track", (album_id,))
                        adict["items"] = [dict(r) for r in cur.fetchall()]
                        self._send_json(200, {"album": adict})
                except Exception as exc:
                    self._send_json(500, {"error": f"Database error: {exc}"})
                finally:
                    release_os_lock(lock)
                return

        if path == "/jobs":
            with JOBS_LOCK:
                job_list = [j.to_dict(include_logs=False) for j in JOBS.values()]
            self._send_json(200, {"jobs": job_list})
            return

        if path.startswith("/jobs/"):
            job_id = path[6:]
            with JOBS_LOCK:
                job = JOBS.get(job_id)
            if not job:
                self._send_json(404, {"error": "Job not found"})
                return
            self._send_json(200, {"job": job.to_dict(include_logs=True)})
            return

        self._send_json(404, {"error": f"Endpoint not found: {path}"})

    def do_POST(self):
        if not self._authenticate():
            return

        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        content_len = int(self.headers.get("Content-Length", 0))
        post_data = self.rfile.read(content_len) if content_len > 0 else b"{}"

        try:
            body = json.loads(post_data.decode("utf-8")) if post_data else {}
        except Exception:
            self._send_json(400, {"error": "Invalid JSON body"})
            return

        if path == "/library/raw_query":
            query = body.get("query", body.get("sql", "")).strip()
            params_list = body.get("params", [])
            offset = max(int(body.get("offset", 0)), 0)
            limit = min(max(int(body.get("limit", 1000)), 1), 5000)

            if not query:
                self._send_json(400, {"error": "Query string is required"})
                return

            clean_q = re.sub(r'/\*.*?\*/', '', query, flags=re.DOTALL)
            clean_q = re.sub(r'--.*$', '', clean_q, flags=re.MULTILINE).strip()

            if ";" in clean_q.rstrip(";"):
                self._send_json(400, {"error": "Multiple SQL statements are not permitted"})
                return
            clean_q = clean_q.rstrip(";")

            forbidden = [
                r"\bUPDATE\b", r"\bDELETE\b", r"\bINSERT\b", r"\bDROP\b",
                r"\bCREATE\b", r"\bALTER\b", r"\bREPLACE\b", r"\bATTACH\b",
                r"\bDETACH\b", r"\bVACUUM\b", r"\bPRAGMA\b", r"\bEXEC\b", r"\bEXECUTE\b"
            ]
            for pattern in forbidden:
                if re.search(pattern, clean_q, re.IGNORECASE):
                    self._send_json(400, {"error": f"Forbidden statement type: raw_query endpoint is strictly read-only SELECT queries"})
                    return

            if not re.match(r"^\s*(WITH\b|SELECT\b)", clean_q, re.IGNORECASE):
                self._send_json(400, {"error": "Raw query must begin with SELECT or WITH ... SELECT"})
                return

            if not os.path.exists(LIB_PATH):
                self._send_json(404, {"error": "Database file musiclibrary.blb not found"})
                return

            lock_file = acquire_os_lock(read_only=True)
            try:
                con = sqlite3.connect(LIB_PATH, timeout=10)
                con.row_factory = sqlite3.Row
                cur = con.cursor()

                count_sql = f"SELECT COUNT(*) FROM ({clean_q}) AS _total_subquery"
                cur.execute(count_sql, params_list)
                total_count = cur.fetchone()[0]

                page_sql = f"SELECT * FROM ({clean_q}) AS _page_subquery LIMIT ? OFFSET ?"
                cur.execute(page_sql, list(params_list) + [limit, offset])
                rows = [dict(r) for r in cur.fetchall()]
                con.close()

                has_more = (offset + len(rows)) < total_count
                next_offset = (offset + len(rows)) if has_more else None

                self._send_json(200, {
                    "rows": rows,
                    "count": len(rows),
                    "total": total_count,
                    "offset": offset,
                    "limit": limit,
                    "has_more": has_more,
                    "next_offset": next_offset,
                    "truncated": has_more
                })
            except Exception as exc:
                self._send_json(500, {"error": f"SQLite error: {exc}"})
            finally:
                release_os_lock(lock_file)
            return

        if path.startswith("/albums/") and path.endswith("/artpath"):
            parts = path.split("/")
            try:
                album_id = int(parts[2])
            except ValueError:
                self._send_json(400, {"error": "Invalid album ID"})
                return

            artpath = body.get("artpath", "")
            if not artpath or not is_safe_path(artpath, ["music"]):
                self._send_json(403, {"error": f"Access denied for artpath: {artpath}"})
                return

            lock_file = acquire_os_lock(read_only=False)
            try:
                con = sqlite3.connect(LIB_PATH, timeout=10)
                con.row_factory = sqlite3.Row
                cur = con.cursor()
                cur.execute("SELECT * FROM albums WHERE id = ?", (album_id,))
                if not cur.fetchone():
                    self._send_json(404, {"error": f"Album {album_id} not found"})
                    return
                cur.execute("UPDATE albums SET artpath = ? WHERE id = ?", (artpath, album_id))
                con.commit()
                con.close()
                self._send_json(200, {"ok": True, "album_id": album_id, "artpath": artpath})
            except Exception as exc:
                self._send_json(500, {"error": f"Failed to set artpath: {exc}"})
            finally:
                release_os_lock(lock_file)
            return

        if path == "/commands/execute":
            command = body.get("command")
            args = body.get("args", [])
            timeout = body.get("timeout", 120)
            config_override = body.get("config_override", "")
            source_path = body.get("source_path", "")
            target_path = body.get("target_path", "")

            if not command or command not in ALLOWED_COMMANDS:
                self._send_json(400, {"error": f"Command '{command}' is not in the allowlist"})
                return

            if source_path:
                allowed_roots = ["staging"] if command == "import" else ["music", "staging"]
                if not is_safe_path(source_path, allowed_roots):
                    self._send_json(403, {"error": f"Access denied for source_path: {source_path}"})
                    return

            if target_path:
                if not is_safe_path(target_path, ["music", "staging"]):
                    self._send_json(403, {"error": f"Access denied for target_path: {target_path}"})
                    return

            for arg in args:
                s_arg = str(arg)
                if s_arg.startswith(".") or ".." in s_arg or "\\" in s_arg or "\x00" in s_arg:
                    self._send_json(403, {"error": f"Invalid path parameter in command args: {arg}"})
                    return
                if s_arg.startswith("/") and not is_safe_path(s_arg, ["music", "staging", "config", "tmp"]):
                    self._send_json(403, {"error": f"Access denied for path in command args: {arg}"})
                    return

            cmd_list = [command]
            if source_path:
                cmd_list.append(source_path)
            cmd_list.extend([str(a) for a in args])

            mutating = command in {
                "import", "update", "write", "move", "modify", "mbsync",
                "fetchart", "embedart", "lastgenre", "alt", "remove", "rm"
            }

            lock_file = acquire_os_lock(read_only=not mutating)
            tmp_cfg_path = None
            try:
                full_cmd = [BEET_BIN]
                if config_override:
                    tmp_cfg_path = f"/tmp/beets_exec_cfg_{uuid.uuid4().hex}.yaml"
                    with open(tmp_cfg_path, "w", encoding="utf-8") as f:
                        f.write(config_override)
                    os.chmod(tmp_cfg_path, 0o600)
                    full_cmd.extend(["-c", tmp_cfg_path])

                full_cmd.extend(cmd_list)
                env = os.environ.copy()
                env["BEETSDIR"] = BEETSDIR
                res = subprocess.run(
                    full_cmd,
                    capture_output=True,
                    text=True,
                    timeout=float(timeout),
                    env=env
                )
                self._send_json(200, {
                    "returncode": res.returncode,
                    "stdout": res.stdout,
                    "stderr": res.stderr,
                })
            except subprocess.TimeoutExpired:
                self._send_json(408, {"error": f"Command '{command}' timed out after {timeout}s", "returncode": 124})
            except Exception as exc:
                self._send_json(500, {"error": f"Command execution error: {exc}"})
            finally:
                if tmp_cfg_path and os.path.exists(tmp_cfg_path):
                    try:
                        os.unlink(tmp_cfg_path)
                    except Exception:
                        pass
                release_os_lock(lock_file)
            return

        if path == "/jobs/create":
            command = body.get("command")
            args = body.get("args", [])
            label = body.get("label", "")
            config_override = body.get("config_override", "")
            source_path = body.get("source_path", "")

            if not command or command not in ALLOWED_COMMANDS:
                self._send_json(400, {"error": f"Command '{command}' is not in the allowlist"})
                return

            if source_path:
                allowed_roots = ["staging"] if command == "import" else ["music", "staging"]
                if not is_safe_path(source_path, allowed_roots):
                    self._send_json(403, {"error": f"Access denied for source_path: {source_path}"})
                    return

            for arg in args:
                s_arg = str(arg)
                if s_arg.startswith(".") or ".." in s_arg or "\\" in s_arg or "\x00" in s_arg:
                    self._send_json(403, {"error": f"Invalid path parameter in job args: {arg}"})
                    return
                if s_arg.startswith("/") and not is_safe_path(s_arg, ["music", "staging", "config", "tmp"]):
                    self._send_json(403, {"error": f"Access denied for path in job args: {arg}"})
                    return

            job_id = uuid.uuid4().hex
            cmd_list = [command]
            if source_path:
                cmd_list.append(source_path)
            cmd_list.extend([str(a) for a in args])

            job = AgentJob(job_id, cmd_list, label=label, config_override=config_override)
            with JOBS_LOCK:
                JOBS[job_id] = job

            self._send_json(200, {"job_id": job_id, "status": "started"})
            return

        if path.startswith("/jobs/") and path.endswith("/cancel"):
            parts = path.split("/")
            job_id = parts[2]
            with JOBS_LOCK:
                job = JOBS.get(job_id)
            if not job:
                self._send_json(404, {"error": "Job not found"})
                return
            job.cancel()
            self._send_json(200, {"job_id": job_id, "status": "cancel_requested"})
            return

        if path == "/tags/read":
            file_path = body.get("file_path", "")
            if not is_safe_path(file_path, ["music", "staging"]):
                self._send_json(403, {"error": f"Access denied for path: {file_path}"})
                return
            if not os.path.exists(file_path):
                self._send_json(404, {"error": f"File not found: {file_path}"})
                return

            try:
                tags = {}
                try:
                    from beets.mediafile import MediaFile
                    mf = MediaFile(file_path)
                    tags = {
                        "title": mf.title,
                        "artist": mf.artist,
                        "album": mf.album,
                        "albumartist": mf.albumartist,
                        "year": mf.year,
                        "track": mf.track,
                        "tracktotal": mf.tracktotal,
                        "disc": mf.disc,
                        "disctotal": mf.disctotal,
                        "genre": mf.genre,
                        "mb_trackid": mf.mb_trackid,
                        "mb_albumid": mf.mb_albumid,
                        "mb_artistid": mf.mb_artistid,
                        "mb_albumartistid": mf.mb_albumartistid,
                        "mb_releasegroupid": getattr(mf, "mb_releasegroupid", None),
                    }
                except Exception:
                    import mutagen
                    f = mutagen.File(file_path, easy=True)
                    if f is not None:
                        tags = {
                            "title": (f.get("title") or [""])[0],
                            "artist": (f.get("artist") or [""])[0],
                            "album": (f.get("album") or [""])[0],
                            "albumartist": (f.get("albumartist") or [""])[0],
                            "year": (f.get("date") or [0])[0],
                            "track": (f.get("tracknumber") or [0])[0],
                            "genre": (f.get("genre") or [""])[0],
                        }
                self._send_json(200, {"tags": tags})
            except Exception as exc:
                self._send_json(500, {"error": f"Failed to read tags: {exc}"})
            return

        if path == "/tags/write":
            file_path = body.get("file_path", "")
            tags = body.get("tags", {})
            if not is_safe_path(file_path, ["music", "staging"]):
                self._send_json(403, {"error": f"Access denied for path: {file_path}"})
                return
            if not os.path.exists(file_path):
                self._send_json(404, {"error": f"File not found: {file_path}"})
                return

            lock_file = acquire_os_lock(read_only=False)
            try:
                written = False
                try:
                    from beets.mediafile import MediaFile
                    mf = MediaFile(file_path)
                    for k, v in tags.items():
                        if hasattr(mf, k):
                            setattr(mf, k, v)
                    mf.save()
                    written = True
                except Exception:
                    import mutagen
                    f = mutagen.File(file_path, easy=True)
                    if f is not None:
                        for k, v in tags.items():
                            f[k] = v
                        f.save()
                        written = True
                if written:
                    self._send_json(200, {"ok": True, "file_path": file_path})
                else:
                    self._send_json(500, {"error": "Failed to write tags to file"})
            except Exception as exc:
                self._send_json(500, {"error": f"Failed to write tags: {exc}"})
            finally:
                release_os_lock(lock_file)
            return

        if path == "/files/move":
            src = body.get("source_path", "")
            dst = body.get("target_path", "")
            if not is_safe_path(src, ["music", "staging"]) or not is_safe_path(dst, ["music", "staging"]):
                self._send_json(403, {"error": "Access denied for path outside allowed roots"})
                return
            if not os.path.exists(src):
                self._send_json(404, {"error": f"Source path not found: {src}"})
                return

            lock_file = acquire_os_lock(read_only=False)
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.move(src, dst)
                self._send_json(200, {"ok": True, "source_path": src, "target_path": dst})
            except Exception as exc:
                self._send_json(500, {"error": f"Failed to move file: {exc}"})
            finally:
                release_os_lock(lock_file)
            return

        if path == "/files/delete":
            target = body.get("path", "")
            if not is_safe_path(target, ["music", "staging"]):
                self._send_json(403, {"error": f"Access denied for path: {target}"})
                return
            if not os.path.exists(target):
                self._send_json(200, {"ok": True, "path": target, "existed": False})
                return

            lock_file = acquire_os_lock(read_only=False)
            try:
                if os.path.isdir(target):
                    shutil.rmtree(target)
                else:
                    os.unlink(target)
                self._send_json(200, {"ok": True, "path": target, "existed": True})
            except Exception as exc:
                self._send_json(500, {"error": f"Failed to delete path: {exc}"})
            finally:
                release_os_lock(lock_file)
            return

        if path == "/files/mkdir":
            target = body.get("path", "")
            if not is_safe_path(target, ["music", "staging"]):
                self._send_json(403, {"error": f"Access denied for path: {target}"})
                return

            lock_file = acquire_os_lock(read_only=False)
            try:
                os.makedirs(target, exist_ok=True)
                self._send_json(200, {"ok": True, "path": target})
            except Exception as exc:
                self._send_json(500, {"error": f"Failed to create directory: {exc}"})
            finally:
                release_os_lock(lock_file)
            return

        self._send_json(404, {"error": f"Endpoint not found: {path}"})

    def do_PATCH(self):
        if not self._authenticate():
            return

        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        content_len = int(self.headers.get("Content-Length", 0))
        post_data = self.rfile.read(content_len) if content_len > 0 else b"{}"

        try:
            body = json.loads(post_data.decode("utf-8")) if post_data else {}
        except Exception:
            self._send_json(400, {"error": "Invalid JSON body"})
            return

        fields = body.get("fields", {})

        if path.startswith("/items/"):
            try:
                item_id = int(path.split("/")[2])
            except ValueError:
                self._send_json(400, {"error": "Invalid item ID"})
                return

            if not fields:
                self._send_json(400, {"error": "No fields provided"})
                return

            lock_file = acquire_os_lock(read_only=False)
            try:
                con = sqlite3.connect(LIB_PATH, timeout=10)
                con.row_factory = sqlite3.Row
                cur = con.cursor()
                cur.execute("SELECT * FROM items WHERE id = ?", (item_id,))
                if not cur.fetchone():
                    self._send_json(404, {"error": f"Item {item_id} not found"})
                    return

                set_clause = ", ".join(f"{k} = ?" for k in fields.keys())
                params = list(fields.values()) + [item_id]
                cur.execute(f"UPDATE items SET {set_clause} WHERE id = ?", params)
                con.commit()
                cur.execute("SELECT * FROM items WHERE id = ?", (item_id,))
                updated = dict(cur.fetchone())
                con.close()
                self._send_json(200, {"success": True, "item": updated})
            except Exception as exc:
                self._send_json(500, {"error": f"Failed to update item: {exc}"})
            finally:
                release_os_lock(lock_file)
            return

        if path.startswith("/albums/"):
            try:
                album_id = int(path.split("/")[2])
            except ValueError:
                self._send_json(400, {"error": "Invalid album ID"})
                return

            if not fields:
                self._send_json(400, {"error": "No fields provided"})
                return

            lock_file = acquire_os_lock(read_only=False)
            try:
                con = sqlite3.connect(LIB_PATH, timeout=10)
                con.row_factory = sqlite3.Row
                cur = con.cursor()
                cur.execute("SELECT * FROM albums WHERE id = ?", (album_id,))
                if not cur.fetchone():
                    self._send_json(404, {"error": f"Album {album_id} not found"})
                    return

                set_clause = ", ".join(f"{k} = ?" for k in fields.keys())
                params = list(fields.values()) + [album_id]
                cur.execute(f"UPDATE albums SET {set_clause} WHERE id = ?", params)
                con.commit()
                cur.execute("SELECT * FROM albums WHERE id = ?", (album_id,))
                updated = dict(cur.fetchone())
                con.close()
                self._send_json(200, {"success": True, "album": updated})
            except Exception as exc:
                self._send_json(500, {"error": f"Failed to update album: {exc}"})
            finally:
                release_os_lock(lock_file)
            return

        self._send_json(404, {"error": f"Endpoint not found: {path}"})

    def do_DELETE(self):
        if not self._authenticate():
            return

        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        content_len = int(self.headers.get("Content-Length", 0))
        post_data = self.rfile.read(content_len) if content_len > 0 else b"{}"
        try:
            body = json.loads(post_data.decode("utf-8")) if post_data else {}
        except Exception:
            body = {}

        if path.startswith("/albums/") and path.endswith("/artpath"):
            parts = path.split("/")
            try:
                album_id = int(parts[2])
            except ValueError:
                self._send_json(400, {"error": "Invalid album ID"})
                return

            lock_file = acquire_os_lock(read_only=False)
            try:
                con = sqlite3.connect(LIB_PATH, timeout=10)
                con.row_factory = sqlite3.Row
                cur = con.cursor()
                cur.execute("SELECT * FROM albums WHERE id = ?", (album_id,))
                if not cur.fetchone():
                    self._send_json(404, {"error": f"Album {album_id} not found"})
                    return
                cur.execute("UPDATE albums SET artpath = '' WHERE id = ?", (album_id,))
                con.commit()
                con.close()
                self._send_json(200, {"ok": True, "album_id": album_id, "artpath": ""})
            except Exception as exc:
                self._send_json(500, {"error": f"Failed to clear artpath: {exc}"})
            finally:
                release_os_lock(lock_file)
            return

        if path.startswith("/albums/"):
            parts = path.split("/")
            if len(parts) == 3:
                try:
                    album_id = int(parts[2])
                except ValueError:
                    self._send_json(400, {"error": "Invalid album ID"})
                    return

                delete_files = body.get("delete_files", True)
                code, res = _handle_delete_album(album_id, delete_files=delete_files)
                self._send_json(code, res)
                return

        self._send_json(404, {"error": f"Endpoint not found: {path}"})


def run_agent():
    if not BEETS_API_TOKEN:
        raise RuntimeError("BEETS_API_TOKEN environment variable is required and cannot be empty")
    server_address = ("0.0.0.0", PORT)
    httpd = HTTPServer(server_address, ControlAgentHandler)
    print(f"[BeetsControlAgent] Listening on 0.0.0.0:{PORT} (LOCK={LOCK_PATH})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    run_agent()
