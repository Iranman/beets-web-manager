"""Audit script to discover all filesystem, database, and Beets mutations in Web Manager."""

import ast
import os
import re
from pathlib import Path

ROOT = Path(__file__).parent.parent
APP_PY = ROOT / "app.py"


def audit_app_py():
    with open(APP_PY, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename="app.py")

    functions_with_mutations = []

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func_name = node.name
            lineno = node.lineno
            end_lineno = getattr(node, "end_lineno", lineno)

            fs_mutations = []
            db_mutations = []
            beet_mutations = []

            for child in ast.walk(node):
                # Filesystem call check
                if isinstance(child, ast.Call):
                    func_str = ""
                    if isinstance(child.func, ast.Attribute):
                        func_str = child.func.attr
                    elif isinstance(child.func, ast.Name):
                        func_str = child.func.id

                    if func_str in ("unlink", "rmdir", "rename", "replace", "mkdir", "write_bytes", "write_text", "remove"):
                        fs_mutations.append((child.lineno, func_str))
                    elif func_str in ("move", "rmtree", "copy", "copy2", "copytree") and isinstance(child.func, ast.Attribute) and isinstance(child.func.value, ast.Name) and child.func.value.id == "shutil":
                        fs_mutations.append((child.lineno, f"shutil.{func_str}"))
                    elif func_str in ("unlink", "remove", "rename", "replace", "rmdir") and isinstance(child.func, ast.Attribute) and isinstance(child.func.value, ast.Name) and child.func.value.id == "os":
                        fs_mutations.append((child.lineno, f"os.{func_str}"))

                    if func_str in ("_beet_run", "_beet_env") or "beet" in func_str.lower():
                        beet_mutations.append((child.lineno, func_str))

                # SQL String check
                if isinstance(child, ast.Constant) and isinstance(child.value, str):
                    val = child.value.strip().upper()
                    if val.startswith("UPDATE ") or val.startswith("DELETE ") or val.startswith("INSERT "):
                        db_mutations.append((child.lineno, val[:60]))

            if fs_mutations or db_mutations or beet_mutations:
                functions_with_mutations.append({
                    "name": func_name,
                    "line": lineno,
                    "end_line": end_lineno,
                    "fs": fs_mutations,
                    "db": db_mutations,
                    "beet": beet_mutations,
                })

    print(f"Total functions with mutations found in app.py: {len(functions_with_mutations)}\n")
    for f in sorted(functions_with_mutations, key=lambda x: x["line"]):
        print(f"Line {f['line']:5d}-{f['end_line']:5d}: def {f['name']}()")
        for line, m in f['fs']:
            print(f"    [FS L{line}] {m}")
        for line, m in f['db']:
            print(f"    [DB L{line}] {m}")
        for line, m in f['beet']:
            print(f"    [BEET L{line}] {m}")
        print()

if __name__ == "__main__":
    audit_app_py()
