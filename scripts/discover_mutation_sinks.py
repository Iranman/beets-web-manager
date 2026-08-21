"""AST-based repository-wide mutation sink discovery for ARCH-003.

SEC-002 / ARCH-003 Wave 23: Enhanced discovery scanner identifying all candidate
mutation call sites -- filesystem writes/moves/deletes/mkdir/touch, open(..., "w"),
file handle writes, SQL DML (including variable references), Beets/subprocess execution,
and audio tag writes.
"""

from __future__ import annotations

import ast
import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set

# Files/directories a scan never needs to look inside.
_EXCLUDED_DIR_NAMES = {
    "tests", "node_modules", ".git", "__pycache__", "frontend", "venv",
    ".venv", "dist", "build", "scripts",
}

# Production Python entry points this scan covers.
_DEFAULT_ROOTS = [
    "app.py", "routes_jobs.py", "routes_lidarr.py", "routes_setup.py",
    "routes_submissions.py", "job_engine.py", "helpers_mb.py", "backend",
]

_FS_UNAMBIGUOUS_METHOD_SINKS = {
    "unlink", "rmdir", "mkdir", "touch", "write_text", "write_bytes",
}
_FS_AMBIGUOUS_METHOD_SINKS = {"rename", "replace"}

_FS_MODULE_FUNCS = {
    ("shutil", "move"), ("shutil", "rmtree"), ("shutil", "copy"),
    ("shutil", "copy2"), ("shutil", "copytree"), ("shutil", "copyfile"),
    ("os", "remove"), ("os", "unlink"), ("os", "rename"), ("os", "replace"),
    ("os", "rmdir"), ("os", "removedirs"), ("os", "mkdir"), ("os", "makedirs"),
    ("tempfile", "NamedTemporaryFile"), ("tempfile", "mkstemp"), ("tempfile", "mkdtemp"),
}

_SQL_DML_RE = re.compile(r"^\s*(INSERT|UPDATE|DELETE|REPLACE)\s", re.IGNORECASE)
# Bare-name (ast.Name) wrapper calls -- `_beet_run(...)`, not
# `module._beet_run(...)`. SEC-002 / ARCH-003 Wave 23 final review,
# finding #9: `_attr_chain()` only resolves `ast.Attribute` call targets
# (`module.attr(...)`), so a bare-name call's `.func` is an `ast.Name` and
# `_attr_chain()` returns None for it -- `_beet_run` was listed in the
# subprocess-detection tuple but could never actually match. Handled via a
# dedicated bare-name set (finding #10: audited for other project wrapper
# functions called the same way, not just `_beet_run`).
_BARE_SUBPROCESS_WRAPPER_NAMES = {"_beet_run"}
_TAG_WRITE_METHODS = {"save", "write_tags"}
_PATH_LIKE_NAME_RE = re.compile(
    r"(path|file|dir|folder|target|dest|dst|src|tmp|temp|cfg|config|cover|art|canonical|trash|source|root)",
    re.IGNORECASE,
)
# SEC-002 / ARCH-003 Wave 23 final review, finding #11: the descriptive
# pattern above previously also carried bare `p`/`f`/`d` as ordinary
# regex alternatives -- since it's matched with `search()` (substring,
# not full-match), those matched almost any identifier containing that
# letter anywhere ("data", "config", "id", "added", ...), destroying any
# real signal in rename/replace classification. Single-letter
# conventional path variable names are real (`p = Path(...)`) but must be
# matched as the *whole* identifier, not a substring -- checked
# separately.
_PATH_LIKE_SHORT_NAME_RE = re.compile(r"^(?:p|f|d)$")
_FILE_HANDLE_NAME_RE = re.compile(
    r"(fh|file|handle|f_out|out|writer|fp|stream)",
    re.IGNORECASE,
)


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


def _extract_string_const(node: ast.AST) -> Optional[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            v.value for v in node.values
            if isinstance(v, ast.Constant) and isinstance(v.value, str)
        )
    return None


def _is_writable_open_mode(mode_str: str) -> bool:
    s = mode_str.lower()
    return any(c in s for c in ("w", "a", "x", "+"))


def _is_path_expr(node: ast.AST) -> bool:
    if isinstance(node, ast.Call):
        chain = _attr_chain(node.func) or (node.func.id if isinstance(node.func, ast.Name) else "")
        if chain in ("Path", "pathlib.Path") or (isinstance(node.func, ast.Attribute) and node.func.attr in ("resolve", "joinpath", "with_name", "with_suffix", "parent")):
            return True
    elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return True
    return False


def _looks_like_path_receiver(receiver: ast.AST, path_vars: Set[str]) -> bool:
    if isinstance(receiver, ast.Name):
        if (receiver.id in path_vars or _PATH_LIKE_NAME_RE.search(receiver.id)
                or _PATH_LIKE_SHORT_NAME_RE.match(receiver.id)):
            return True
    if isinstance(receiver, ast.Attribute):
        if bool(_PATH_LIKE_NAME_RE.search(receiver.attr)):
            return True
    if isinstance(receiver, ast.Call):
        chain = _attr_chain(receiver.func) or (receiver.func.id if isinstance(receiver.func, ast.Name) else "")
        if chain in ("Path", "pathlib.Path") or (isinstance(receiver.func, ast.Name) and receiver.func.id == "Path"):
            return True
    return False


def _walk_own_scope_only(node: ast.AST):
    """Like `ast.walk`, but does not descend into nested function/class
    scopes. SEC-002 / ARCH-003 Wave 23 final review, finding #13: the
    pre-scan that collects a function's local `path_vars`/`sql_vars`/
    `file_handle_vars` used plain `ast.walk(node)`, which visits every
    descendant including nested `def`/`class`/lambda bodies -- an inner
    function's own local `Path(...)` assignment would pollute the outer
    function's scope for the rest of its body, and (for a name reused in
    both scopes) can silently persist into places it does not belong.
    Yields `node` itself and everything in its immediate scope; stops at
    (does not yield into) any nested `FunctionDef`/`AsyncFunctionDef`/
    `ClassDef`/`Lambda`."""
    stack = [node]
    seen_root = True
    while stack:
        current = stack.pop()
        yield current
        if not seen_root and isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue  # do not descend into a nested scope
        seen_root = False
        stack.extend(ast.iter_child_nodes(current))


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
        def __init__(self):
            self.path_vars: Set[str] = set()
            self.sql_vars: Dict[str, str] = {}
            self.file_handle_vars: Set[str] = set()

        def _enter_scope(self, node: ast.AST):
            prev_path_vars = set(self.path_vars)
            prev_sql_vars = dict(self.sql_vars)
            prev_fh_vars = set(self.file_handle_vars)

            self.path_vars = set()
            self.sql_vars = {}
            self.file_handle_vars = set()

            # Pre-scan function body to gather local assignments
            for child in _walk_own_scope_only(node):
                if isinstance(child, ast.Assign):
                    target_names = [t.id for t in child.targets if isinstance(t, ast.Name)]
                    val = child.value

                    if _is_path_expr(val):
                        for name in target_names:
                            self.path_vars.add(name)

                    sql_str = _extract_string_const(val)
                    if sql_str and _SQL_DML_RE.match(sql_str.strip()):
                        for name in target_names:
                            self.sql_vars[name] = sql_str

                    if isinstance(val, ast.Call) and isinstance(val.func, ast.Name) and val.func.id == "open":
                        mode = "r"
                        if len(val.args) > 1:
                            m_val = _extract_string_const(val.args[1])
                            if m_val:
                                mode = m_val
                        for kw in val.keywords:
                            if kw.arg == "mode":
                                m_val = _extract_string_const(kw.value)
                                if m_val:
                                    mode = m_val
                        if _is_writable_open_mode(mode):
                            for name in target_names:
                                self.file_handle_vars.add(name)

                elif isinstance(child, ast.With):
                    for item in child.items:
                        ctx = item.context_expr
                        if isinstance(ctx, ast.Call) and isinstance(ctx.func, ast.Name) and ctx.func.id == "open":
                            mode = "r"
                            if len(ctx.args) > 1:
                                m_val = _extract_string_const(ctx.args[1])
                                if m_val:
                                    mode = m_val
                            for kw in ctx.keywords:
                                if kw.arg == "mode":
                                    m_val = _extract_string_const(kw.value)
                                    if m_val:
                                        mode = m_val
                            if _is_writable_open_mode(mode):
                                if isinstance(item.optional_vars, ast.Name):
                                    self.file_handle_vars.add(item.optional_vars.id)

            stack.append(node)
            self.generic_visit(node)
            stack.pop()

            self.path_vars = prev_path_vars
            self.sql_vars = prev_sql_vars
            self.file_handle_vars = prev_fh_vars

        def visit_FunctionDef(self, node):
            self._enter_scope(node)

        def visit_AsyncFunctionDef(self, node):
            self._enter_scope(node)

        def visit_ClassDef(self, node):
            self._enter_scope(node)

        def visit_Call(self, node: ast.Call):
            func_qual = _qualname_stack_str(stack)
            kind = None

            chain = _attr_chain(node.func)

            # 1. Module function calls (shutil, os, tempfile)
            if chain and chain in {f"{m}.{f}" for m, f in _FS_MODULE_FUNCS}:
                kind = "filesystem"

            # 2. Path method calls (unlink, rmdir, mkdir, touch, write_text, write_bytes)
            elif isinstance(node.func, ast.Attribute) and node.func.attr in _FS_UNAMBIGUOUS_METHOD_SINKS:
                kind = "filesystem"

            # 3. Path ambiguous methods (rename, replace) with path receiver check
            elif (isinstance(node.func, ast.Attribute) and node.func.attr in _FS_AMBIGUOUS_METHOD_SINKS
                  and _looks_like_path_receiver(node.func.value, self.path_vars)):
                kind = "filesystem"

            # 4. open(...) or Path.open(...) with writable mode. The mode
            # argument's position differs between the two forms: builtin
            # `open(path, mode)` takes mode as args[1]; `Path(...).open
            # (mode)` takes it as args[0] (no separate path argument --
            # the receiver already is the path). Using args[1] for both
            # meant `Path(...).open("wb")` (a single-arg call) always fell
            # back to the "r" default and was silently never flagged.
            elif isinstance(node.func, ast.Name) and node.func.id == "open":
                mode = "r"
                if len(node.args) > 1:
                    m_val = _extract_string_const(node.args[1])
                    if m_val:
                        mode = m_val
                for kw in node.keywords:
                    if kw.arg == "mode":
                        m_val = _extract_string_const(kw.value)
                        if m_val:
                            mode = m_val
                if _is_writable_open_mode(mode):
                    kind = "filesystem"
            elif isinstance(node.func, ast.Attribute) and node.func.attr == "open":
                mode = "r"
                if node.args:
                    m_val = _extract_string_const(node.args[0])
                    if m_val:
                        mode = m_val
                for kw in node.keywords:
                    if kw.arg == "mode":
                        m_val = _extract_string_const(kw.value)
                        if m_val:
                            mode = m_val
                if _is_writable_open_mode(mode):
                    kind = "filesystem"

            # 5. File handle writes (fh.write, fh.writelines, fh.truncate)
            elif isinstance(node.func, ast.Attribute) and node.func.attr in ("write", "writelines", "truncate"):
                if isinstance(node.func.value, ast.Name):
                    rec_name = node.func.value.id
                    if rec_name in self.file_handle_vars or _FILE_HANDLE_NAME_RE.search(rec_name):
                        kind = "filesystem"

            # 6. Audio tag writes
            elif isinstance(node.func, ast.Attribute) and node.func.attr in _TAG_WRITE_METHODS:
                kind = "tag_write"

            # 7. Subprocess and Beets command execution (module.attr form)
            elif chain in (
                "subprocess.run", "subprocess.Popen", "subprocess.call",
                "subprocess.check_call", "subprocess.check_output",
                "beets_client.run_command", "beets_client.reimport_source",
                "beets_client.reimport_disk", "beets_client.modify", "beets_client.retag",
                "beets_client.write_tags",
            ):
                kind = "subprocess"

            # 7b. Bare-name wrapper calls -- `_beet_run(...)`, not
            # `module._beet_run(...)`. `chain` (from `_attr_chain`) is only
            # ever set for `ast.Attribute` call targets, so it is always
            # None here and can never match a name in this tuple; that was
            # the actual bug (finding #9).
            elif isinstance(node.func, ast.Name) and node.func.id in _BARE_SUBPROCESS_WRAPPER_NAMES:
                kind = "subprocess"

            # 8. SQL DML execution (direct string literal or local variable reference)
            elif isinstance(node.func, ast.Attribute) and node.func.attr in ("execute", "executemany", "executescript"):
                if node.args:
                    arg0 = node.args[0]
                    text = _extract_string_const(arg0)
                    if text is None and isinstance(arg0, ast.Name) and arg0.id in self.sql_vars:
                        text = self.sql_vars[arg0.id]
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

    v = Visitor()
    v.visit(tree)
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
