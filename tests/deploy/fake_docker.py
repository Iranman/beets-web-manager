#!/usr/bin/env python3
"""Fake `docker` CLI used only by tests/test_deploy_truenas_rollout.py.

Emulates just enough of `docker inspect`, `docker image inspect`, and
`docker compose {ps,config,pull,up,stop,start,restart}`, and `docker exec`
(the engine's semantic snapshot) -- reading/writing a JSON
"world state" file (path from FAKE_DOCKER_STATE) -- to drive
scripts/deploy_truenas_web_manager.sh through its real code paths
without touching a real Docker daemon or TrueNAS host.

Supports a minimal subset of Go template syntax: {{.A.B}}, {{if .A}}X{{else}}Y{{end}},
{{json .A}}, {{index .A "key"}} -- exactly what the rollout script uses.
"""
import json
import os
import re
import sys


def _state_path():
    return os.environ["FAKE_DOCKER_STATE"]


def _load():
    with open(_state_path(), encoding="utf-8") as f:
        return json.load(f)


def _save(state):
    with open(_state_path(), "w", encoding="utf-8") as f:
        json.dump(state, f)


def _resolve(obj, path):
    cur = obj
    for part in path.strip(".").split("."):
        if not part:
            continue
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
        if cur is None:
            return None
    return cur


_IFELSE_RE = re.compile(r"\{\{if (\.[\w.]+)\}\}(.*?)\{\{else\}\}(.*?)\{\{end\}\}", re.S)
_IF_RE = re.compile(r"\{\{if (\.[\w.]+)\}\}(.*?)\{\{end\}\}", re.S)
_JSON_RE = re.compile(r"\{\{json (\.[\w.]+)\}\}")
_INDEX_RE = re.compile(r'\{\{index (\.[\w.]+) "([^"]+)"\}\}')
_FIELD_RE = re.compile(r"\{\{(\.[\w.]+)\}\}")


def render(template, obj):
    def sub_ifelse(m):
        return m.group(2) if _resolve(obj, m.group(1)) else m.group(3)

    def sub_if(m):
        return m.group(2) if _resolve(obj, m.group(1)) else ""

    template = _IFELSE_RE.sub(sub_ifelse, template)
    template = _IF_RE.sub(sub_if, template)
    template = _JSON_RE.sub(lambda m: json.dumps(_resolve(obj, m.group(1))), template)
    template = _INDEX_RE.sub(lambda m: str((_resolve(obj, m.group(1)) or {}).get(m.group(2), "")), template)
    template = _FIELD_RE.sub(lambda m: str(_resolve(obj, m.group(1)) if _resolve(obj, m.group(1)) is not None else ""), template)
    return template


def _lookup_container_or_image(target, state):
    if target in state["containers"]:
        return state["containers"][target]
    for cont in state["containers"].values():
        if cont.get("Name") in (target, "/" + target):
            return cont
    if target in state["images"]:
        return state["images"][target]
    for img in state["images"].values():
        if img.get("Id") == target:
            return img
    return None


def cmd_inspect(args, state):
    fmt = None
    targets = []
    i = 0
    while i < len(args):
        if args[i] == "--format":
            fmt = args[i + 1]
            i += 2
        else:
            targets.append(args[i])
            i += 1
    if not targets:
        print("fake_docker: inspect requires a target", file=sys.stderr)
        return 1
    obj = _lookup_container_or_image(targets[0], state)
    if obj is None:
        print(f"Error: No such object: {targets[0]}", file=sys.stderr)
        return 1
    print(render(fmt, obj) if fmt else json.dumps([obj]))
    return 0


def cmd_image_inspect(args, state):
    fmt = None
    target = None
    i = 0
    while i < len(args):
        if args[i] == "--format":
            fmt = args[i + 1]
            i += 2
        else:
            target = args[i]
            i += 1
    img = state["images"].get(target)
    if img is None:
        print(f"Error: No such image: {target}", file=sys.stderr)
        return 1
    print(render(fmt, img) if fmt else json.dumps(img))
    return 0


_VERSION_VAR_RE = re.compile(r"\$\{BEETS_WEB_MANAGER_VERSION(?::-([^}]*))?\}")


def _interpolate_version(image, compose_file):
    """Resolve ${BEETS_WEB_MANAGER_VERSION[:-default]} the way Compose does:
    process environment first, then the .env next to the Compose file, then
    the default."""
    m = _VERSION_VAR_RE.search(image)
    if not m:
        return image
    value = os.environ.get("BEETS_WEB_MANAGER_VERSION", "")
    env_file = os.path.join(os.path.dirname(compose_file), ".env")
    if not value and os.path.exists(env_file):
        for line in open(env_file, encoding="utf-8").read().splitlines():
            if line.startswith("BEETS_WEB_MANAGER_VERSION="):
                value = line.split("=", 1)[1].strip()
    if not value:
        value = m.group(1) or ""
    return image[:m.start()] + value + image[m.end():]


def _parse_service_images(content):
    """{service: image} from a Compose/override file. Handles the block form
    (`services:` / `  svc:` / `    image: x`) and the one-line flow form
    (`services: {svc: {image: x}}`). Quotes are stripped."""
    found = {}
    for m in re.finditer(r"\{([\w][\w-]*):\s*\{image:\s*\"?([^\s,}\"]+)\"?", content):
        found[m.group(1)] = m.group(2)
    current = None
    for line in content.splitlines():
        svc_m = re.match(r"^  ([\w][\w-]*):\s*$", line)
        if svc_m:
            current = svc_m.group(1)
            continue
        img_m = re.match(r"^    image:\s*\"?([^\s\"]+)\"?\s*$", line)
        if img_m and current:
            found[current] = img_m.group(1)
    return found


def _resolved_services(state, files):
    services = {}
    for svc, cid in state["service_containers"].items():
        cont = state["containers"][cid]
        services[svc] = {"image": cont["Config"]["Image"]}
    # The base Compose file only matters where it interpolates the version
    # variable (literal images keep the running container's image, which is
    # what the existing "wrong compose image" tests rely on).
    if files:
        try:
            base = open(files[0], encoding="utf-8").read()
        except OSError:
            base = ""
        for svc, image in _parse_service_images(base).items():
            if svc in services and _VERSION_VAR_RE.search(image):
                services[svc]["image"] = _interpolate_version(image, files[0])
    for f in files[1:]:
        try:
            content = open(f, encoding="utf-8").read()
        except OSError:
            continue
        for svc, image in _parse_service_images(content).items():
            if svc in services:
                services[svc]["image"] = image
    for svc, env in (state.get("compose_environment") or {}).items():
        if svc in services:
            services[svc]["environment"] = dict(env)
    return services


def cmd_compose(args, state):
    files = []
    i = 0
    while i < len(args) and args[i] == "-f":
        files.append(args[i + 1])
        i += 2
    if i >= len(args):
        print("fake_docker compose: missing subcommand", file=sys.stderr)
        return 1
    sub = args[i]
    rest = args[i + 1:]

    if sub == "ps":
        # rest == ["-q", svc]
        svc = rest[-1]
        cid = state["service_containers"].get(svc, "")
        if cid:
            print(cid)
        return 0

    if sub == "config":
        services = _resolved_services(state, files)
        print(json.dumps({"services": services}))
        return 0

    if sub == "pull":
        if state.get("pull_should_fail"):
            print("fake_docker: simulated pull failure", file=sys.stderr)
            return 1
        return 0

    if sub == "up":
        svc = rest[-1]
        if state.get("up_should_fail"):
            print("fake_docker: simulated up failure", file=sys.stderr)
            return 1
        services = _resolved_services(state, files)
        resolved_image = services.get(svc, {}).get("image", "")
        cid = state["service_containers"][svc]
        cont = state["containers"][cid]
        cont["Config"]["Image"] = resolved_image
        image_entry = state["images"].get(resolved_image)
        cont["Image"] = image_entry["Id"] if image_entry else "sha256:unknownimage"
        if state.get("never_healthy"):
            cont["State"] = {"Status": "running"}
        else:
            cont["State"] = {"Status": "running", "Health": {"Status": "healthy"}}

        other = state.get("also_recreate_other")
        if other and other in state["service_containers"]:
            old_cid = state["service_containers"][other]
            new_cid = old_cid + "-recreated"
            state["containers"][new_cid] = dict(state["containers"][old_cid])
            state["service_containers"][other] = new_cid

        _save(state)
        return 0

    if sub == "stop":
        svc = rest[-1]
        cid = state["service_containers"].get(svc)
        if cid:
            state["containers"][cid]["State"] = {"Status": "exited"}
            _save(state)
        return 0

    if sub == "start":
        svc = rest[-1]
        cid = state["service_containers"].get(svc)
        if cid:
            state["containers"][cid]["State"] = {"Status": "running", "Health": {"Status": "healthy"}}
            state.setdefault("started", []).append(svc)
            _save(state)
        return 0

    if sub == "restart":
        svc = rest[-1]
        cid = state["service_containers"].get(svc)
        state.setdefault("restarted", []).append(svc)
        # Restarting the engine re-imports the provisioned plugin files.
        if svc == "beets" and state.get("plugin_version_after_restart"):
            new_version = state["plugin_version_after_restart"]
            if state.get("semantic_snapshot"):
                state["semantic_snapshot"]["plugin_version"] = new_version
            for snap in state.get("semantic_snapshots") or []:
                snap["plugin_version"] = new_version
        if svc == "beets" and state.get("digest_after_restart"):
            if state.get("semantic_snapshot"):
                state["semantic_snapshot"]["digest"] = state["digest_after_restart"]
        if cid:
            if state.get("never_healthy"):
                state["containers"][cid]["State"] = {"Status": "running"}
            else:
                state["containers"][cid]["State"] = {"Status": "running", "Health": {"Status": "healthy"}}
            _save(state)
        return 0

    print(f"fake_docker compose: unsupported subcommand: {sub}", file=sys.stderr)
    return 1


def cmd_exec(args, state):
    """`docker exec <cid> python3 -c <snapshot>`: the engine's semantic
    snapshot. state["semantic_snapshots"] is consumed in order (the last one
    repeats); a stopped container or state["exec_should_fail"] fails."""
    target = args[0] if args else ""
    cont = _lookup_container_or_image(target, state) or {}
    if state.get("exec_should_fail") or (cont.get("State") or {}).get("Status") != "running":
        print("fake_docker: exec failed", file=sys.stderr)
        return 1
    snaps = state.get("semantic_snapshots") or [state.get("semantic_snapshot")]
    snap = snaps[0]
    if len(snaps) > 1:
        state["semantic_snapshots"] = snaps[1:]
        _save(state)
    if snap is None:
        print("fake_docker: no semantic snapshot configured", file=sys.stderr)
        return 1
    print(json.dumps(snap))
    return 0


def main():
    argv = sys.argv[1:]
    if not argv:
        print("fake_docker: missing command", file=sys.stderr)
        return 2
    state = _load()
    if argv[0] == "compose":
        return cmd_compose(argv[1:], state)
    if argv[0] == "inspect":
        return cmd_inspect(argv[1:], state)
    if argv[0] == "exec":
        return cmd_exec(argv[1:], state)
    if argv[0] == "image" and len(argv) > 1 and argv[1] == "inspect":
        return cmd_image_inspect(argv[2:], state)
    print(f"fake_docker: unsupported command: {argv}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
