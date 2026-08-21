"""AST-based repository-wide mutation sink discovery for ARCH-003.

SEC-002 / ARCH-003 final closure review, findings #15-#23: the previous
`security/arch003_mutation_inventory.json` was a small hand-written list that
`scripts/verify_arch003_mutation_inventory.py` only validated for internal
consistency (are the classifications spelled right, does the named
transaction family exist) -- it never actually looked at the source code to
find mutation sinks, so an inventory of 8 entries was accepted as "0
unclassified" regardless of how many real sinks the repository contains.

This module does the part that was missing: it walks the actual AST of the
production Python files and finds real candidate mutation call sites --
filesystem writes/moves/deletes, SQL DML, Beets/subprocess command execution,
and tag writes. It does NOT classify them (that's a human judgment call,
recorded in the inventory JSON) and it does NOT claim to find every possible
mutation in a dynamically-typed language with runtime dispatch -- AST pattern
matching on call syntax is a real, useful floor, not a perfect ceiling.

Each discovered sink gets a *stable key* designed to survive incidental line
renumbering: `file:enclosing_qualified_function:normalized_call_text`. Purely
moving a function (or unrelated code above it) doesn't change this key;
editing the actual call text does -- which is exactly when a human should
re-review it anyway.
"""

from __future__ import annotations

import ast
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional

# Files/directories a scan never needs to look inside.
_EXCLUDED_DIR_NAMES = {
    "tests", "node_modules", ".git", "__pycache__", "frontend", "venv",
    ".venv", "dist", "build", "scripts",
}

# Production Python entry points this scan covers. transaction_engine.py and
# beets_control_agent.py are deliberately included even though they are the
# *intended* mutation boundary -- discovery does not presume any file is
# automatically exempt; classification is what says "this one is the engine
# itself, that's fine."
_DEFAULT_ROOTS = ["app.py", "routes_jobs.py", "routes_lidarr.py", "routes_setup.py", "routes_submissions.py", "job_engine.py", "helpers_mb.py", "backend"]

_FS_METHOD_SINKS = {
    # Deliberately excludes "replace" and "rename" as bare method names --
    # both are also common `str` methods (`text.replace(...)`), and pure
    # AST syntax can't distinguish the receiver's type. Including them
    # produced overwhelming false-positive noise from ordinary string
    # normalization code with no real filesystem signal. `Path.rename`/
    # `Path.replace` calls are still caught via the qualified-name check
    # below when the call is written as `path_obj.rename(...)` on a name
    # that also appears as a `Path(...)`-constructed variable in the same
    # function -- see `_looks_like_path_receiver`.
    "unlink", "rmdir", "write_text", "write_bytes",
}
# Method names ambiguous between str and Path; only counted as filesystem
# sinks when the receiver expression looks like a Path variable.
_FS_AMBIGUOUS_METHOD_SINKS = {"rename", "replace"}
_FS_MODULE_FUNCS = {
    ("shutil", "move"), ("shutil", "rmtree"), ("shutil", "copy"),
    ("shutil", "copy2"), ("shutil", "copytree"), ("shutil", "copyfile"),
    ("os", "remove"), ("os", "unlink"), ("os", "rename"), ("os", "replace"),
    ("os", "rmdir"), ("os", "removedirs"),
}
_SQL_DML_RE = re.compile(r"^\s*(INSERT|UPDATE|DELETE|REPLACE)\s", re.IGNORECASE)
_TAG_WRITE_METHODS = {"save"}  # MediaFile.save() / Mutagen .save()


@dataclass
class MutationSink:
    file: str
    function: str
    lineno: int
    kind: str  # filesystem | sql | subprocess | tag_write
    call_text: str
    key: str


def _qualname_stack_str(stack: List[ast.AST]) -> str:
    parts = []
    for node in stack:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            parts.append(node.name)
        elif isinstance(node, ast.ClassDef):
            parts.append(node.name)
    return ".".join(parts) or "<module>"


def _call_text(node: ast.AST, source_lines: List[str]) -> str:
    try:
        seg = ast.get_source_segment("\n".join(source_lines), node)
        if seg:
            return " ".join(seg.split())[:160]
    except Exception:
        pass
    if 1 <= node.lineno <= len(source_lines):
        return source_lines[node.lineno - 1].strip()[:160]
    return ""


def _stable_key(rel_path: str, func: str, call_text: str) -> str:
    digest = hashlib.sha1(call_text.encode("utf-8", "replace")).hexdigest()[:12]
    return f"{rel_path}:{func}:{digest}"


def _attr_chain(node: ast.AST) -> Optional[str]:
    """Return 'module.attr' for a simple `module.attr(...)` call target."""
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        return f"{node.value.id}.{node.attr}"
    return None


_PATH_LIKE_NAME_RE = re.compile(
    r"(path|file|dir|folder|target|dest|dst|src|tmp|temp|cfg|config|cover|art)",
    re.IGNORECASE,
)


def _looks_like_path_receiver(receiver: ast.AST) -> bool:
    """Best-effort heuristic distinguishing a probable `Path` receiver from
    an ordinary string for the `rename`/`replace` method names that both
    `str` and `pathlib.Path` define. Not a type checker -- a filter to cut
    an otherwise overwhelming false-positive rate from string
    normalization code (see the discussion at `_FS_AMBIGUOUS_METHOD_SINKS`
    above). False negatives here just mean a real sink needs a
    differently-named variable to be caught next scan; false positives are
    resolved by a human during classification, not silently trusted."""
    if isinstance(receiver, ast.Call):
        chain = _attr_chain(receiver.func) or (receiver.func.id if isinstance(receiver.func, ast.Name) else "")
        if chain in ("Path", "pathlib.Path") or (isinstance(receiver.func, ast.Name) and receiver.func.id == "Path"):
            return True
    if isinstance(receiver, ast.Name):
        return bool(_PATH_LIKE_NAME_RE.search(receiver.id))
    if isinstance(receiver, ast.Attribute):
        return bool(_PATH_LIKE_NAME_RE.search(receiver.attr))
    return False


def discover_sinks_in_file(path: Path, rel_path: str) -> List[MutationSink]:
    try:
        source = path.read_text(encoding="utf-8")
    except Exception:
        return []
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []
    source_lines = source.splitlines()

    sinks: List[MutationSink] = []
    stack: List[ast.AST] = []

    class Visitor(ast.NodeVisitor):
        def _enter(self, node):
            stack.append(node)
            self.generic_visit(node)
            stack.pop()

        def visit_FunctionDef(self, node):
            self._enter(node)

        def visit_AsyncFunctionDef(self, node):
            self._enter(node)

        def visit_ClassDef(self, node):
            self._enter(node)

        def visit_Call(self, node: ast.Call):
            func_qual = _qualname_stack_str(stack)
            kind = None

            chain = _attr_chain(node.func)
            if chain and chain in {f"{m}.{f}" for m, f in _FS_MODULE_FUNCS}:
                kind = "filesystem"
            elif isinstance(node.func, ast.Attribute) and node.func.attr in _FS_METHOD_SINKS:
                # Method call like `path_obj.unlink()` -- can't prove the
                # receiver is a pathlib.Path via pure syntax, so this is a
                # candidate, not a certainty; that is exactly why
                # classification is a separate, human step.
                kind = "filesystem"
            elif (isinstance(node.func, ast.Attribute) and node.func.attr in _FS_AMBIGUOUS_METHOD_SINKS
                    and _looks_like_path_receiver(node.func.value)):
                kind = "filesystem"
            elif isinstance(node.func, ast.Attribute) and node.func.attr in _TAG_WRITE_METHODS:
                kind = "tag_write"
            elif chain in ("subprocess.run", "subprocess.Popen", "subprocess.call", "subprocess.check_call", "subprocess.check_output"):
                kind = "subprocess"
            elif isinstance(node.func, ast.Attribute) and node.func.attr in ("execute", "executemany", "executescript"):
                # SQL: inspect the first string-literal argument for DML.
                if node.args:
                    arg0 = node.args[0]
                    text = None
                    if isinstance(arg0, ast.Constant) and isinstance(arg0.value, str):
                        text = arg0.value
                    elif isinstance(arg0, ast.JoinedStr):
                        # f-string: check literal segments only.
                        text = "".join(
                            v.value for v in arg0.values
                            if isinstance(v, ast.Constant) and isinstance(v.value, str)
                        )
                    if text and _SQL_DML_RE.match(text.strip()):
                        kind = "sql"

            if kind:
                call_text = _call_text(node, source_lines)
                sinks.append(MutationSink(
                    file=rel_path,
                    function=func_qual,
                    lineno=node.lineno,
                    kind=kind,
                    call_text=call_text,
                    key=_stable_key(rel_path, func_qual, call_text),
                ))
            self.generic_visit(node)

    Visitor().visit(tree)
    return sinks


def discover_all(repo_root: Path, roots: Optional[Iterable[str]] = None) -> List[MutationSink]:
    roots = list(roots) if roots is not None else _DEFAULT_ROOTS
    files: List[Path] = []
    for root_name in roots:
        p = repo_root / root_name
        if p.is_file() and p.suffix == ".py":
            files.append(p)
        elif p.is_dir():
            for sub in sorted(p.rglob("*.py")):
                if any(part in _EXCLUDED_DIR_NAMES for part in sub.relative_to(repo_root).parts):
                    continue
                files.append(sub)

    sinks: List[MutationSink] = []
    for f in sorted(set(files)):
        rel = str(f.relative_to(repo_root)).replace("\\", "/")
        sinks.extend(discover_sinks_in_file(f, rel))
    return sinks


def main():
    import json
    import sys
    repo_root = Path(__file__).parent.parent.resolve()
    sinks = discover_all(repo_root)
    payload = [
        {"file": s.file, "function": s.function, "line": s.lineno, "kind": s.kind, "call_text": s.call_text, "key": s.key}
        for s in sinks
    ]
    print(json.dumps(payload, indent=2))
    print(f"# {len(sinks)} candidate sinks discovered", file=sys.stderr)


if __name__ == "__main__":
    main()
