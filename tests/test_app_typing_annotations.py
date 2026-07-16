import ast
import unittest
from pathlib import Path


TYPING_NAMES = {
    "Any",
    "Callable",
    "Dict",
    "Iterable",
    "List",
    "Literal",
    "Mapping",
    "Optional",
    "Sequence",
    "Set",
    "Tuple",
    "Union",
}


def _annotation_names(annotation):
    if annotation is None:
        return set()
    return {
        node.id
        for node in ast.walk(annotation)
        if isinstance(node, ast.Name) and node.id in TYPING_NAMES
    }


class AppTypingAnnotationTests(unittest.TestCase):
    def test_typing_aliases_used_in_annotations_are_imported(self):
        app_path = Path(__file__).resolve().parents[1] / "app.py"
        tree = ast.parse(app_path.read_text(encoding="utf-8"))

        imported = set()
        used = set()

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "typing":
                imported.update(alias.asname or alias.name for alias in node.names)

            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                used.update(_annotation_names(node.returns))
                for arg in node.args.posonlyargs + node.args.args + node.args.kwonlyargs:
                    used.update(_annotation_names(arg.annotation))
                if node.args.vararg:
                    used.update(_annotation_names(node.args.vararg.annotation))
                if node.args.kwarg:
                    used.update(_annotation_names(node.args.kwarg.annotation))
            elif isinstance(node, ast.AnnAssign):
                used.update(_annotation_names(node.annotation))

        missing = sorted(used - imported)
        self.assertEqual([], missing)


if __name__ == "__main__":
    unittest.main()
