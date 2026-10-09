#!/usr/bin/env python3
"""Fake `curl` for endpoint-verification tests: serves canned JSON responses
for the fixed set of paths the rollout script probes, honoring `-o FILE`,
`-w FORMAT`, and reading FAKE_CURL_STATE (a JSON file: {"item_count": N,
"fail_paths": [...], "blocking_reasons_by_image": {image_ref: [...]},
"blocking_reason_codes_by_image": {image_ref: [...]},
"setup_status_http_by_image": {image_ref: "503"},
"auth_required": true -> /api/library answers 401 without an -H header}) so
tests can control counts and simulate failures without a real HTTP server.

/health/live and /api/setup/status answer for whatever image the fake
`docker` world (FAKE_DOCKER_STATE) says the beets-web-manager service is
running, so a deploy/rollback is observable through the endpoints exactly
as on a real host.
"""
import json
import os
import re
import sys


def _running_webmgr():
    """(image_ref, version_label) of the fake beets-web-manager container."""
    path = os.environ.get("FAKE_DOCKER_STATE")
    if not path or not os.path.exists(path):
        return "", ""
    with open(path, encoding="utf-8") as f:
        docker = json.load(f)
    cid = (docker.get("service_containers") or {}).get("beets-web-manager", "")
    ref = (((docker.get("containers") or {}).get(cid) or {}).get("Config") or {}).get("Image", "")
    labels = (((docker.get("images") or {}).get(ref) or {}).get("Config") or {}).get("Labels") or {}
    return ref, labels.get("org.opencontainers.image.version", "")


def main():
    args = sys.argv[1:]
    out_file = None
    write_fmt = ""
    url = ""
    has_header = False
    i = 0
    while i < len(args):
        a = args[i]
        if a == "-o":
            out_file = args[i + 1]
            i += 2
        elif a == "-w":
            write_fmt = args[i + 1]
            i += 2
        elif a in ("-sS", "-s", "-S"):
            i += 1
        elif a == "--max-time":
            i += 2
        elif a == "-H":
            has_header = True
            i += 2
        else:
            url = a
            i += 1

    state = {}
    state_path = os.environ.get("FAKE_CURL_STATE")
    if state_path and os.path.exists(state_path):
        with open(state_path, encoding="utf-8") as f:
            state = json.load(f)

    m = re.search(r"://[^/]+(/.*)$", url)
    path = m.group(1) if m else url
    fail_paths = state.get("fail_paths", [])
    item_count = state.get("item_count", 5)

    setup_http = ""
    if path.startswith("/api/setup/status"):
        ref, _version = _running_webmgr()
        setup_http = str((state.get("setup_status_http_by_image") or {}).get(ref, ""))
    if state.get("auth_required") and path.startswith("/api/library") and not has_header:
        body = json.dumps({"error": "authentication required"})
        status = "401"
    elif any(path.startswith(p) for p in fail_paths) or setup_http not in ("", "200"):
        body = json.dumps({"error": "simulated failure"})
        status = setup_http if setup_http not in ("", "200") else "500"
    else:
        status = "200"
        if path.startswith("/api/health"):
            body = json.dumps({"status": "ok"})
        elif path.startswith("/health/live"):
            _ref, version = _running_webmgr()
            body = json.dumps({"status": "alive", "version": version})
        elif path.startswith("/api/setup/status"):
            ref, _version = _running_webmgr()
            reasons = (state.get("blocking_reasons_by_image") or {}).get(ref, [])
            payload = {"ok": True, "status": "warning" if reasons else "ready",
                       "blocking_reasons": reasons}
            codes_by_image = state.get("blocking_reason_codes_by_image") or {}
            if ref in codes_by_image:  # absent = a version without reason codes
                payload["blocking_reason_codes"] = codes_by_image[ref]
            body = json.dumps(payload)
        elif path.startswith("/api/library"):
            qs = url.split("?", 1)[1] if "?" in url else ""
            limit = 50
            m2 = re.search(r"limit=(\d+)", qs)
            if m2:
                limit = int(m2.group(1))
            returned = min(limit, item_count)
            body = json.dumps({
                "items": [{"id": n} for n in range(returned)],
                "pagination": {"limit": limit, "offset": 0, "returned": returned, "total": item_count},
            })
        else:
            body = "{}"

    if out_file:
        with open(out_file, "w", encoding="utf-8") as f:
            f.write(body)
    else:
        sys.stdout.write(body)

    if write_fmt == "%{http_code}":
        sys.stdout.write(status)
    return 0


if __name__ == "__main__":
    sys.exit(main())
