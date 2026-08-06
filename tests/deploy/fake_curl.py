#!/usr/bin/env python3
"""Fake `curl` for endpoint-verification tests: serves canned JSON responses
for the fixed set of paths the rollout script probes, honoring `-o FILE`,
`-w FORMAT`, and reading FAKE_CURL_STATE (a JSON file: {"item_count": N,
"fail_paths": [...]}) so tests can control counts and simulate failures
without a real HTTP server.
"""
import json
import os
import re
import sys


def main():
    args = sys.argv[1:]
    out_file = None
    write_fmt = ""
    url = ""
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

    if any(path.startswith(p) for p in fail_paths):
        body = json.dumps({"error": "simulated failure"})
        status = "500"
    else:
        status = "200"
        if path.startswith("/api/health"):
            body = json.dumps({"status": "ok"})
        elif path.startswith("/api/setup/status"):
            body = json.dumps({"ok": True, "status": "ready"})
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
