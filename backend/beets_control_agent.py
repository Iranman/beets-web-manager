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
import threading
import time
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

ALLOWED_ROOTS = [
    os.path.abspath(BEETSDIR),
    os.path.abspath(MUSIC_LIBRARY_PATH),
    os.path.abspath(DOWNLOAD_PATH),
    "/tmp",
]

JOBS = {}
JOBS_LOCK = threading.Lock()


def is_safe_path(path: str) -> bool:
    """Verify that path stays strictly within allowed root directories without traversal."""
    if not path or not isinstance(path, str):
        return False
    if "\x00" in path:
        return False
    norm = path.replace("\\", "/")
    parts = norm.split("/")
    if ".." in parts:
        return False
    for root in ("/config", "/data/media/music", "/data/torrents", "/tmp"):
        norm_root = root.rstrip("/") + "/"
        if norm == root or norm.startswith(norm_root):
            return True
    try:
        abs_path = os.path.abspath(path)
        real_path = os.path.realpath(abs_path)
        beets_dir = os.environ.get("BEETSDIR", "/config")
        allowed = (beets_dir, "/config", "/data/media/music", "/data/torrents", "/tmp")
        for root in allowed:
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


class ControlAgentHandler(BaseHTTPRequestHandler):
    def _send_json(self, status_code: int, data: dict):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authenticate(self) -> bool:
        if not BEETS_API_TOKEN:
            self._send_json(401, {"error": "Unauthorized: BEETS_API_TOKEN environment variable is missing on agent"})
            return False
        auth_header = self.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()
            if hmac.compare_digest(token, BEETS_API_TOKEN):
                return True
        self._send_json(401, {"error": "Unauthorized: Invalid or missing BEETS_API_TOKEN"})
        return False

    def log_message(self, format, *args):
        msg = format % args
        if "Authorization" in msg or (BEETS_API_TOKEN and BEETS_API_TOKEN in msg):
            msg = "[REDACTED TOKEN]"
        print(f"[BeetsControlAgent] {msg}")

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path == "/health":
            beets_ver = "unknown"
            try:
                res = subprocess.run([BEET_BIN, "version"], capture_output=True, text=True, timeout=5)
                beets_ver = res.stdout.splitlines()[0] if res.stdout else "available"
            except Exception:
                pass

            db_healthy = os.path.exists(LIB_PATH) and os.access(LIB_PATH, os.R_OK)
            config_healthy = os.path.exists(os.path.join(BEETSDIR, "config.yaml"))

            self._send_json(200, {
                "status": "ok",
                "service": "beets-control-agent",
                "agent_version": "1.0.0",
                "beets_version": beets_ver,
                "db_healthy": db_healthy,
                "config_healthy": config_healthy,
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
                "allowed_roots": ALLOWED_ROOTS,
                "os_locking": True,
            })
            return

        if path == "/config/status":
            cfg_file = os.path.join(BEETSDIR, "config.yaml")
            self._send_json(200, {
                "config_exists": os.path.exists(cfg_file),
                "db_exists": os.path.exists(LIB_PATH),
                "db_size": os.path.getsize(LIB_PATH) if os.path.exists(LIB_PATH) else 0,
                "lock_exists": os.path.exists(LOCK_PATH),
            })
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
            sql = body.get("sql", "")
            params = body.get("params", [])

            normalized_sql = sql.strip().upper()
            if not (normalized_sql.startswith("SELECT") or normalized_sql.startswith("PRAGMA")):
                self._send_json(400, {"error": "Only SELECT or PRAGMA queries are permitted via raw_query"})
                return

            if not os.path.exists(LIB_PATH):
                self._send_json(404, {"error": "Database file musiclibrary.blb not found"})
                return

            lock_file = acquire_os_lock(read_only=True)
            try:
                uri = f"file:{LIB_PATH}?mode=ro"
                conn = sqlite3.connect(uri, uri=True, timeout=10.0)
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute(sql, params)
                rows = [dict(row) for row in cursor.fetchall()]
                conn.close()
                self._send_json(200, {"rows": rows, "count": len(rows)})
            except Exception as exc:
                self._send_json(500, {"error": f"SQLite error: {exc}"})
            finally:
                release_os_lock(lock_file)
            return

        if path == "/commands/execute":
            command = body.get("command")
            args = body.get("args", [])
            timeout = body.get("timeout", 120)
            config_override = body.get("config_override", "")

            if not command or command not in ALLOWED_COMMANDS:
                self._send_json(400, {"error": f"Command '{command}' is not in the allowlist"})
                return

            for arg in args:
                if str(arg).startswith("/") and not is_safe_path(str(arg)):
                    self._send_json(403, {"error": f"Access denied for path outside allowed roots: {arg}"})
                    return

            cmd_list = [command] + [str(a) for a in args]
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

            if not command or command not in ALLOWED_COMMANDS:
                self._send_json(400, {"error": f"Command '{command}' is not in the allowlist"})
                return

            for arg in args:
                if str(arg).startswith("/") and not is_safe_path(str(arg)):
                    self._send_json(403, {"error": f"Access denied for path outside allowed roots: {arg}"})
                    return

            job_id = uuid.uuid4().hex
            cmd_list = [command] + [str(a) for a in args]
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
            if not is_safe_path(file_path):
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
            if not is_safe_path(file_path):
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
            if not is_safe_path(src) or not is_safe_path(dst):
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
            if not is_safe_path(target):
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
            if not is_safe_path(target):
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
