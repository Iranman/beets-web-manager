"""Music metadata submission routes.

Registered after app.py initializes. Keeps submission-only Beets commands out of
the main app module while reusing the existing JobStore and Beets config helpers.
"""
import os
import uuid

from flask import jsonify

from app import (  # noqa: E402
    BEET_BIN,
    _ANSI_RE,
    _beet_env,
    _beet_run,
    _s,
    _write_job_beets_config,
    app,
    jobs,
    lib,
)


def _acoustid_key() -> str:
    return (
        os.environ.get("ACOUSTID_API_KEY", "").strip()
        or os.environ.get("ACOUSTID_KEY", "").strip()
    )


def _acoustid_submit_config_extra() -> str:
    key = _acoustid_key()
    if not key:
        return "chroma:\n  auto: no\n"
    safe_key = key.replace("\\", "\\\\").replace('"', '\\"')
    return (
        "chroma:\n"
        "  auto: no\n"
        f'  apikey: "{safe_key}"\n'
        "acoustid:\n"
        f'  apikey: "{safe_key}"\n'
    )


def _append_clean_output(log, stdout: str = "", stderr: str = "") -> str:
    output = _ANSI_RE.sub("", ((stdout or "") + (stderr or "")).strip())
    for line in output.splitlines():
        if line.strip():
            log.append(line)
    return output


def _start_acoustid_submit_job(query: str, label: str):
    def _do(log, cancel_event=None):
        cfg = _write_job_beets_config(
            f"/tmp/beets_acoustid_submit_{uuid.uuid4().hex}.yaml",
            _acoustid_submit_config_extra(),
        )
        if not _acoustid_key():
            log.append("ACOUSTID_API_KEY/ACOUSTID_KEY is not set in the environment; using Beets config if present.")
        log.append(f"Running beet submit {query}")
        result = _beet_run(
            [BEET_BIN, "-c", cfg, "submit", query],
            log,
            timeout=300,
            env=_beet_env(),
            cancel=cancel_event,
        )
        output = _append_clean_output(log, result.stdout, result.stderr)
        if result.returncode != 0:
            raise RuntimeError(f"beet submit failed with exit code {result.returncode}")
        return {"output": output, "query": query}

    job = jobs.start_python(_do, label=label)
    return jsonify({"ok": True, "job_id": job.job_id})


@app.post("/api/albums/<int:aid>/acoustid-submit")
def album_acoustid_submit(aid: int):
    album = lib.get_album(aid)
    if not album:
        return jsonify({"ok": False, "error": "Album not found"}), 404
    title = " - ".join(part for part in (_s(album.albumartist), _s(album.album)) if part)
    label = f"AcoustID submit: {title or f'album {aid}'}"
    return _start_acoustid_submit_job(f"album_id:{aid}", label)


@app.post("/api/items/<int:iid>/acoustid-submit")
def item_acoustid_submit(iid: int):
    item = lib.get_item(iid)
    if not item:
        return jsonify({"ok": False, "error": "Item not found"}), 404
    query = f"album_id:{item.album_id}" if getattr(item, "album_id", None) else f"id:{iid}"
    title = " - ".join(part for part in (_s(item.artist), _s(item.title)) if part)
    label = f"AcoustID submit: {title or f'item {iid}'}"
    return _start_acoustid_submit_job(query, label)
