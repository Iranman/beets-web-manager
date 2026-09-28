#!/usr/bin/env python3
"""ARCH-001 extraction tool: move top-level units out of app.py into a module.

Usage:
    python scripts/arch001_extract.py plan   --module backend/x.py --names a,b --domains D1,D2
    python scripts/arch001_extract.py apply  --module backend/x.py --names a,b --domains D1,D2

A *unit* is one top-level statement of app.py (a function or class with its
decorators and directly preceding comment block, an assignment, ...). The
tool:

* expands the requested set with every helper function / global that is
  referenced by the moved code and used by nothing that stays in app.py
  (transitively), so private helpers travel with their owner;
* refuses (plan reports BLOCKERS) when moved code references a name that stays
  in app.py -- a backend module must never import app.py;
* refuses when a function staying in app.py rebinds (``global``) a moved name,
  or a moved function rebinds a name that stays;
* binds references to names owned by *other extracted modules* either with a
  plain ``from x import name`` (if the module is already imported earlier by
  app.py, i.e. a lower layer) or as ``_mod.name`` module-attribute access
  (cycle-safe, resolved at call time), using real scope analysis so local
  variables that shadow a global are never rewritten;
* appends the units to the target module in their original order, and
  replaces them in app.py with a re-export ``from module import names`` at
  the position of the first moved unit, so app.py's import-time order and
  every ``app.<name>`` attribute (used by other route modules) is preserved.

Other extracted modules are discovered from EXTRACTED (module -> import name).
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app.py"
INVENTORY = ROOT / "docs" / "arch001_app_ownership.json"

BUILTINS = set(dir(__builtins__)) if isinstance(__builtins__, dict) is False else set(__builtins__)


# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------

class Unit:
    __slots__ = ("idx", "node", "start", "end", "defines", "runtime_refs", "import_refs", "kind", "is_import",
                 "global_rebinds")

    def __repr__(self):
        return f"Unit({self.kind}:{sorted(self.defines)[:3]}@{self.start})"


def _target_names(t):
    if isinstance(t, ast.Name):
        yield t.id
    elif isinstance(t, (ast.Tuple, ast.List)):
        for e in t.elts:
            yield from _target_names(e)
    elif isinstance(t, ast.Starred):
        yield from _target_names(t.value)


def _unit_start(lines, node):
    """First line of the unit: decorators, plus a directly attached comment block."""
    start = node.lineno
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.decorator_list:
        start = min(d.lineno for d in node.decorator_list)
    j = start - 2
    while j >= 0 and lines[j].strip().startswith("#"):
        j -= 1
    return j + 2

class ScopeRefs(ast.NodeVisitor):
    """Collect global (module-level) name references with positions."""

    def __init__(self):
        self.refs = []  # (name, lineno, col, end_col, ctx_is_load)
        self.stack = []  # list of sets of locally-bound names
        self.globals_declared = []  # names declared global in some function
        self.rebinds = set()

    # --- scope helpers
    def _bound_in(self, body_nodes, args=None):
        bound = set()
        declared_global = set()
        if args is not None:
            for a in list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs):
                bound.add(a.arg)
            if args.vararg:
                bound.add(args.vararg.arg)
            if args.kwarg:
                bound.add(args.kwarg.arg)

        def walk(n):
            for child in ast.iter_child_nodes(n):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    bound.add(child.name)
                    for d in child.decorator_list:
                        walk_expr(d)
                    if not isinstance(child, ast.ClassDef):
                        for d in child.args.defaults + child.args.kw_defaults:
                            if d is not None:
                                walk_expr(d)
                    continue
                if isinstance(child, ast.Lambda):
                    continue
                if isinstance(child, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
                    continue
                if isinstance(child, ast.Global):
                    declared_global.update(child.names)
                    continue
                if isinstance(child, ast.Nonlocal):
                    continue
                if isinstance(child, ast.Name) and isinstance(child.ctx, (ast.Store, ast.Del)):
                    bound.add(child.id)
                elif isinstance(child, ast.alias):
                    bound.add((child.asname or child.name).split(".")[0])
                elif isinstance(child, ast.ExceptHandler) and child.name:
                    bound.add(child.name)
                elif isinstance(child, ast.MatchAs) and child.name:
                    bound.add(child.name)
                walk(child)

        def walk_expr(e):
            pass

        for b in body_nodes:
            if isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                bound.add(b.name)
                continue
            if isinstance(b, ast.Global):
                declared_global.update(b.names)
                continue
            wrapper = ast.Module(body=[b], type_ignores=[])
            walk(wrapper)
        bound -= declared_global
        return bound, declared_global

    def _resolve(self, name):
        for scope in reversed(self.stack):
            if name in scope:
                return False
        return True

    def visit_FunctionDef(self, node, is_top=False):
        for d in node.decorator_list:
            self.visit(d)
        for d in node.args.defaults + node.args.kw_defaults:
            if d is not None:
                self.visit(d)
        if node.returns:
            self.visit(node.returns)
        for a in list(node.args.posonlyargs) + list(node.args.args) + list(node.args.kwonlyargs):
            if a.annotation:
                self.visit(a.annotation)
        bound, declared = self._bound_in(node.body, node.args)
        self.globals_declared.extend(declared)
        for d in declared:
            # rebinding of a module global
            for sub in ast.walk(node):
                if isinstance(sub, ast.Name) and sub.id == d and isinstance(sub.ctx, (ast.Store, ast.Del)):
                    self.rebinds.add(d)
        self.stack.append(bound)
        for b in node.body:
            self.visit(b)
        self.stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node):
        for d in node.args.defaults + node.args.kw_defaults:
            if d is not None:
                self.visit(d)
        bound = {a.arg for a in list(node.args.posonlyargs) + list(node.args.args) + list(node.args.kwonlyargs)}
        if node.args.vararg:
            bound.add(node.args.vararg.arg)
        if node.args.kwarg:
            bound.add(node.args.kwarg.arg)
        self.stack.append(bound)
        self.visit(node.body)
        self.stack.pop()

    def _comp(self, node, elts):
        bound = set()
        for gen in node.generators:
            bound.update(_target_names(gen.target))
            for sub in ast.walk(gen.target):
                if isinstance(sub, ast.Name):
                    bound.add(sub.id)
        # first iterator evaluated in enclosing scope
        self.visit(node.generators[0].iter)
        self.stack.append(bound)
        for i, gen in enumerate(node.generators):
            if i:
                self.visit(gen.iter)
            for cond in gen.ifs:
                self.visit(cond)
        for e in elts:
            self.visit(e)
        # walrus targets inside comprehensions bind in enclosing scope: rare; ignore
        self.stack.pop()

    def visit_ListComp(self, node):
        self._comp(node, [node.elt])

    visit_SetComp = visit_ListComp
    visit_GeneratorExp = visit_ListComp

    def visit_DictComp(self, node):
        self._comp(node, [node.key, node.value])

    def visit_ClassDef(self, node):
        for d in node.decorator_list:
            self.visit(d)
        for b in node.bases:
            self.visit(b)
        for k in node.keywords:
            self.visit(k.value)
        # class body: names bound here are not visible to methods
        class_bound, _ = self._bound_in(node.body)
        for b in node.body:
            if isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                self.visit(b)
            else:
                self.stack.append(class_bound)
                self.visit(b)
                self.stack.pop()

    def visit_Name(self, node):
        if isinstance(node.ctx, ast.Load) or isinstance(node.ctx, ast.Del):
            if self._resolve(node.id):
                self.refs.append((node.id, node.lineno, node.col_offset, node.end_col_offset, True))


def build_units(src):
    tree = ast.parse(src)
    lines = src.splitlines()
    units = []
    for idx, node in enumerate(tree.body):
        u = Unit()
        u.idx = idx
        u.node = node
        u.start = _unit_start(lines, node) if idx else 1
        u.end = node.end_lineno
        u.defines = set()
        u.kind = type(node).__name__
        u.is_import = isinstance(node, (ast.Import, ast.ImportFrom))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            u.defines.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                u.defines.update(_target_names(t))
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            u.defines.update(_target_names(node.target))
        elif u.is_import:
            for a in node.names:
                u.defines.add((a.asname or a.name).split(".")[0])
        v = ScopeRefs()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            v.visit_FunctionDef(node)
            runtime = [r for r in v.refs if not _in_header(node, r)]
            header = [r for r in v.refs if _in_header(node, r)]
            u.runtime_refs, u.import_refs = runtime, header
        elif isinstance(node, ast.ClassDef):
            v.visit(node)
            u.runtime_refs, u.import_refs = [], v.refs  # conservative: class bodies run at import
        else:
            v.visit(node)
            u.runtime_refs, u.import_refs = [], v.refs
        u.global_rebinds = v.rebinds
        units.append(u)
    # fix overlapping starts (comment attribution): a unit's start can't precede previous end+1
    for a, b in zip(units, units[1:]):
        if b.start <= a.end:
            b.start = a.end + 1
    return tree, lines, units


def _in_header(fn, ref):
    _, lineno, col, _, _ = ref
    body_start = fn.body[0].lineno if fn.body else fn.end_lineno
    if lineno < body_start:
        return True
    return False


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

def load_state():
    inv = json.loads(INVENTORY.read_text(encoding="utf-8")) if INVENTORY.exists() else {}
    return inv


def extracted_modules():
    """module import path -> set of names it defines (from app's re-export lines)."""
    src = APP.read_text(encoding="utf-8")
    out = {}
    for m in re.finditer(r"^from (backend\.[a-z0-9_.]+|routes_[a-z0-9_]+) import \(  # ARCH-001 extracted\n(.*?)^\)",
                         src, re.M | re.S):
        names = re.findall(r"^\s+([A-Za-z_][A-Za-z0-9_]*),", m.group(2), re.M)
        out.setdefault(m.group(1), set()).update(names)
    return out


def plan(module_path, requested_names, requested_domains, pull=True, line_range=None, exclude_domains=(), exclude_regex=None):
    src = APP.read_text(encoding="utf-8")
    tree, lines, units = build_units(src)
    owner = {}
    for u in units:
        for n in u.defines:
            owner.setdefault(n, u)  # first definition wins for resolution
    inv = load_state()
    domain_of = {r["name"]: r["domain"] for r in inv.get("functions", [])}
    selected = set()
    for u in units:
        names = u.defines
        if not names:
            continue
        if names & set(requested_names):
            selected.add(u.idx)
        elif isinstance(u.node, (ast.FunctionDef, ast.AsyncFunctionDef)) and domain_of.get(u.node.name) in requested_domains:
            selected.add(u.idx)
    if line_range:
        lo, hi = line_range
        for u in units:
            if lo <= u.node.lineno <= hi and not u.is_import:
                selected.add(u.idx)
    excluded = set()
    for u in units:
        fn_domain = domain_of.get(u.node.name) if isinstance(u.node, (ast.FunctionDef, ast.AsyncFunctionDef)) else None
        text = lines[u.node.lineno - 1]
        if (fn_domain and fn_domain in exclude_domains) or (exclude_regex and re.search(exclude_regex, text)):
            if not (u.defines & set(requested_names)):
                selected.discard(u.idx)
                excluded.add(u.idx)
    for n in requested_names:
        if n.startswith("-"):
            for u in units:
                if n[1:] in u.defines:
                    selected.discard(u.idx)
                    excluded.add(u.idx)
    ext = extracted_modules()
    ext_owner = {n: m for m, ns in ext.items() for n in ns}

    def refs_of(u):
        return [r[0] for r in (u.runtime_refs + u.import_refs)]

    users = defaultdict(set)  # name -> unit idx using it
    for u in units:
        for n in refs_of(u):
            users[n].add(u.idx)

    changed = True
    while changed and pull:
        changed = False
        for idx in list(selected):
            for n in refs_of(units[idx]):
                v = owner.get(n)
                if v is None or v.idx in selected or v.is_import or n in ext_owner or v.idx in excluded:
                    continue
                if v.idx == idx:
                    continue
                # pull if every user of every name v defines is selected
                all_users = set()
                for dn in v.defines:
                    all_users |= users[dn]
                all_users.discard(v.idx)
                if all_users and all_users <= selected:
                    selected.add(v.idx)
                    changed = True
    moved_names = set().union(*(units[i].defines for i in selected)) if selected else set()
    blockers = defaultdict(set)
    cross = defaultdict(set)
    for idx in sorted(selected):
        u = units[idx]
        for n in refs_of(u):
            v = owner.get(n)
            if v is None:
                continue
            if v.idx in selected:
                continue
            if v.is_import:
                continue
            if n in ext_owner:
                cross[ext_owner[n]].add(n)
                continue
            blockers[n].add(sorted(u.defines)[0])
        for g in u.global_rebinds:
            if g not in moved_names:
                blockers[f"global-rebind:{g}"].add(sorted(u.defines)[0])
    for u in units:
        if u.idx in selected:
            continue
        for g in u.global_rebinds:
            if g in moved_names:
                blockers[f"app-rebinds-moved:{g}"].add(sorted(u.defines)[0] if u.defines else str(u.start))
    return {
        "src": src, "lines": lines, "units": units, "selected": sorted(selected),
        "moved_names": moved_names, "blockers": blockers, "cross": cross, "ext_owner": ext_owner,
    }


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------

def _module_name(module_path):
    return module_path.replace("\\", "/").removesuffix(".py").replace("/", ".")


def apply(module_path, p, header_doc, lower_modules):
    src, lines, units = p["src"], p["lines"], p["units"]
    selected = p["selected"]
    mod_name = _module_name(module_path)
    target = ROOT / module_path
    # imports needed: app's import units referenced by moved code
    import_units = [u for u in units if u.is_import]
    needed_imports = []
    used = set()
    for idx in selected:
        u = units[idx]
        used |= {r[0] for r in u.runtime_refs + u.import_refs}
    for iu in import_units:
        node = iu.node
        keep = []
        for a in node.names:
            name = (a.asname or a.name).split(".")[0]
            if name in used:
                keep.append(a)
        if keep:
            if isinstance(node, ast.Import):
                needed_imports.append("import " + ", ".join(
                    a.name + (f" as {a.asname}" if a.asname else "") for a in keep))
            else:
                mod = "." * node.level + (node.module or "")
                needed_imports.append(f"from {mod} import " + ", ".join(
                    a.name + (f" as {a.asname}" if a.asname else "") for a in keep))
    # cross-module bindings
    cross_lines = []
    qualify = {}
    for mod, names in sorted(p["cross"].items()):
        if mod in lower_modules:
            cross_lines.append(f"from {mod} import " + ", ".join(sorted(names)))
        else:
            alias = "_" + mod.split(".")[-1]
            cross_lines.append(f"import {mod} as {alias}")
            for n in names:
                qualify[n] = alias
    # build moved text with qualification rewrites
    out_chunks = []
    for idx in selected:
        u = units[idx]
        chunk_lines = lines[u.start - 1:u.end]
        if qualify:
            edits = [r for r in (u.runtime_refs + u.import_refs) if r[0] in qualify]
            for name, lineno, col, end_col, _ in sorted(edits, key=lambda r: (r[1], -r[2])):
                li = lineno - u.start
                line = chunk_lines[li]
                chunk_lines[li] = line[:col] + f"{qualify[name]}.{name}" + line[end_col:]
        out_chunks.append("\n".join(chunk_lines))
    body = "\n\n\n".join(out_chunks)
    if target.exists():
        existing = target.read_text(encoding="utf-8").rstrip("\n")
        # merge new imports into existing header (append if missing)
        add = [l for l in needed_imports + cross_lines if l not in existing]
        if add:
            marker = "# ── ARCH-001 extracted code ──"
            if marker in existing:
                head, tail = existing.split(marker, 1)
                existing = head.rstrip("\n") + "\n" + "\n".join(add) + "\n\n" + marker + tail
            else:
                existing = "\n".join(add) + "\n" + existing
        new_text = existing + "\n\n\n" + body + "\n"
    else:
        header = f'"""{header_doc}\n"""\n\nfrom __future__ import annotations\n\n'
        new_text = header + "\n".join(needed_imports + cross_lines) + "\n\n# ── ARCH-001 extracted code ──\n\n\n" + body + "\n"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(new_text, encoding="utf-8", newline="\n")
    # rewrite app.py
    moved_public = sorted(p["moved_names"])
    first = units[selected[0]]
    reexport = (f"from {mod_name} import (  # ARCH-001 extracted\n"
                + "".join(f"    {n},\n" for n in moved_public) + ")")
    remove = set()
    for idx in selected:
        u = units[idx]
        remove.update(range(u.start, u.end + 1))
    new_lines = []
    for i, line in enumerate(lines, 1):
        if i == first.start:
            new_lines.append(reexport)
        if i in remove:
            continue
        new_lines.append(line)
    text = "\n".join(new_lines) + "\n"
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    APP.write_text(text, encoding="utf-8", newline="\n")
    return len(selected), len(moved_public)


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["plan", "apply"])
    ap.add_argument("--module", required=True)
    ap.add_argument("--names", default="")
    ap.add_argument("--domains", default="")
    ap.add_argument("--doc", default="Extracted from app.py (ARCH-001).")
    ap.add_argument("--lower", default="", help="comma list of modules safe to from-import (lower layers)")
    ap.add_argument("--nopull", action="store_true")
    ap.add_argument("--range", default="")
    ap.add_argument("--exclude-domains", default="")
    ap.add_argument("--exclude-regex", default="")
    args = ap.parse_args(argv)
    names = [n for n in args.names.split(",") if n]
    domains = [d for d in args.domains.split(",") if d]
    rng = tuple(int(x) for x in args.range.split(":")) if args.range else None
    p = plan(args.module, names, domains, pull=not args.nopull, line_range=rng,
             exclude_domains=[d for d in args.exclude_domains.split(",") if d],
             exclude_regex=args.exclude_regex or None)
    size = sum(p["units"][i].end - p["units"][i].start + 1 for i in p["selected"])
    print(f"selected units: {len(p['selected'])}  lines: {size}  names: {len(p['moved_names'])}")
    if p["cross"]:
        print("cross-module refs:", {k: len(v) for k, v in p["cross"].items()})
    if p["blockers"]:
        counts = sorted(p["blockers"].items(), key=lambda kv: -len(kv[1]))
        print(f"BLOCKERS ({len(counts)} names stay in app.py):")
        for n, users_ in counts[:80]:
            print(f"  {n}  <- {sorted(users_)[:4]}{' ...' if len(users_) > 4 else ''}")
        if args.action == "apply":
            return 2
    if args.action == "apply":
        # Extraction order is a topological order: an earlier module can never
        # reference a later one (it would have been blocked on app.py), so every
        # already-extracted module is a lower layer and plain imports are safe.
        lower = set(m for m in args.lower.split(",") if m) | set(p["ext_owner"].values())
        n_units, n_names = apply(args.module, p, args.doc, lower)
        inv = json.loads(INVENTORY.read_text(encoding="utf-8"))
        mods = inv.setdefault("extracted_modules", [])
        rel = args.module.replace("\\", "/")
        if rel not in mods:
            mods.append(rel)
        INVENTORY.write_text(json.dumps(inv, indent=1) + "\n", encoding="utf-8", newline="\n")
        print(f"moved {n_units} units ({n_names} names) -> {args.module}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
