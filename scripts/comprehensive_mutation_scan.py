"""Filtered and Classified Mutation Inventory Script for app.py."""

import ast
import json
from pathlib import Path

ROOT = Path(__file__).parent.parent
APP_PY = ROOT / "app.py"


def scan_mutations():
    with open(APP_PY, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename="app.py")

    functions = []

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func_name = node.name
            start_line = node.lineno
            end_line = getattr(node, "end_lineno", start_line)

            fs_sinks = []
            db_sinks = []
            beet_sinks = []

            for child in ast.walk(node):
                if isinstance(child, ast.Call):
                    func_str = ""
                    obj_str = ""

                    if isinstance(child.func, ast.Attribute):
                        func_str = child.func.attr
                        if isinstance(child.func.value, ast.Name):
                            obj_str = child.func.value.id
                        elif isinstance(child.func.value, ast.Attribute):
                            obj_str = child.func.value.attr
                    elif isinstance(child.func, ast.Name):
                        func_str = child.func.id

                    # Filesystem sinks (excluding string.replace)
                    if func_str in ("unlink", "rmdir", "rename", "remove") or (func_str == "replace" and obj_str in ("os", "tmp", "file", "p", "path", "Path")):
                        fs_sinks.append((child.lineno, f"{obj_str}.{func_str}" if obj_str else func_str))
                    elif func_str in ("move", "rmtree", "copy", "copy2", "copytree") and obj_str == "shutil":
                        fs_sinks.append((child.lineno, f"shutil.{func_str}"))

                    # Beets subprocess / execution sinks
                    if func_str in ("_beet_run", "_beet_env", "beet_run"):
                        beet_sinks.append((child.lineno, func_str))
                    elif func_str == "run" and obj_str == "subprocess":
                        beet_sinks.append((child.lineno, "subprocess.run"))

                # Database DML strings
                if isinstance(child, ast.Constant) and isinstance(child.value, str):
                    val = child.value.strip()
                    first_word = val.split()[0].upper() if val.split() else ""
                    if first_word in ("UPDATE", "DELETE", "INSERT"):
                        if any(table in val.lower() for table in ("items", "albums")):
                            db_sinks.append((child.lineno, f"{first_word} query: {val[:60]}..."))

            if fs_sinks or db_sinks or beet_sinks:
                functions.append({
                    "func": func_name,
                    "start": start_line,
                    "end": end_line,
                    "fs": fs_sinks,
                    "db": db_sinks,
                    "beet": beet_sinks,
                })

    return functions


if __name__ == "__main__":
    funcs = scan_mutations()
    print(f"Discovered {len(funcs)} functions with real filesystem/DB/subprocess mutations in app.py:\n")
    for fn in funcs:
        print(f"L{fn['start']:5d}-L{fn['end']:5d}: {fn['func']}")
        for line, s in fn["fs"]:
            print(f"   [FS L{line:5d}] {s}")
        for line, s in fn["db"]:
            print(f"   [DB L{line:5d}] {s}")
        for line, s in fn["beet"]:
            print(f"   [BEET L{line:5d}] {s}")
        print()
