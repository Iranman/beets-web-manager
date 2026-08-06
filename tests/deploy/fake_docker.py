#!/usr/bin/env python3
"""Fake `docker` CLI used only by tests/test_deploy_truenas_rollout.py.

Emulates just enough of `docker inspect`, `docker image inspect`, and
`docker compose {ps,config,pull,up,stop,restart}` -- reading/writing a JSON
"world state" file (path from FAKE_DOCKER_STATE) -- to drive
scripts/deploy_truenas_web_manager_0_1_3.sh through its real code paths
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


def _resolved_services(state, override_files):
    services = {}
    for svc, cid in state["service_containers"].items():
        cont = state["containers"][cid]
        services[svc] = {"image": cont["Config"]["Image"]}
    for f in override_files:
        try:
            content = open(f, encoding="utf-8").read()
        except OSError:
            continue
        img_m = re.search(r"image:\s*([^\s,}]+)", content)
        svc_m = re.search(r"\{(\w[\w-]*):", content)
        if img_m and svc_m and svc_m.group(1) in services:
            services[svc_m.group(1)]["image"] = img_m.group(1)
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
        services = _resolved_services(state, files[1:])
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
        services = _resolved_services(state, files[1:])
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

    if sub == "restart":
        svc = rest[-1]
        cid = state["service_containers"].get(svc)
        if cid:
            if state.get("never_healthy"):
                state["containers"][cid]["State"] = {"Status": "running"}
            else:
                state["containers"][cid]["State"] = {"Status": "running", "Health": {"Status": "healthy"}}
            _save(state)
        return 0

    print(f"fake_docker compose: unsupported subcommand: {sub}", file=sys.stderr)
    return 1


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
    if argv[0] == "image" and len(argv) > 1 and argv[1] == "inspect":
        return cmd_image_inspect(argv[2:], state)
    print(f"fake_docker: unsupported command: {argv}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
