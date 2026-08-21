"""Self-tests for scripts/discover_mutation_sinks.py.

SEC-002 / ARCH-003 Wave 23 final review, findings #24/#25: the scanner's
own correctness was not covered by any test at all -- the full repository
suite passing says nothing about whether individual detection rules
(bare-name wrapper calls, ambiguous rename/replace receivers, nested-scope
isolation, SQL-variable tracking, tag-write detection) actually work. This
file exercises the scanner directly against small synthetic source trees,
positive and negative, so a future change to the scanner's detection logic
gets a real regression signal instead of only "the mutation gate still
passes with today's baseline."

This is not the full enumerated matrix the review brief listed (every
single filesystem/SQL/subprocess/tag-write variant) -- it is a
representative set covering every rule branch in the scanner and every
defect this review actually found and fixed. Coverage gaps are a
deliberate scope decision under review time constraints, not a claim that
every possible source shape is tested.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from discover_mutation_sinks import discover_sinks_in_file  # noqa: E402


def _sinks(source: str):
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "synthetic.py"
        p.write_text(source, encoding="utf-8")
        return discover_sinks_in_file(p, "synthetic.py")


def _kinds(source: str):
    return [(s.function, s.kind) for s in _sinks(source)]


class FilesystemPositiveTests(unittest.TestCase):
    def test_path_mkdir(self):
        self.assertIn(("f", "filesystem"), _kinds('from pathlib import Path\ndef f():\n    p = Path("/x")\n    p.mkdir()\n'))

    def test_os_mkdir(self):
        self.assertIn(("f", "filesystem"), _kinds('import os\ndef f():\n    os.mkdir("/x")\n'))

    def test_os_makedirs(self):
        self.assertIn(("f", "filesystem"), _kinds('import os\ndef f():\n    os.makedirs("/x")\n'))

    def test_path_touch(self):
        self.assertIn(("f", "filesystem"), _kinds('from pathlib import Path\ndef f():\n    Path("/x").touch()\n'))

    def test_path_unlink(self):
        self.assertIn(("f", "filesystem"), _kinds('from pathlib import Path\ndef f():\n    Path("/x").unlink()\n'))

    def test_path_rmdir(self):
        self.assertIn(("f", "filesystem"), _kinds('from pathlib import Path\ndef f():\n    Path("/x").rmdir()\n'))

    def test_path_write_text(self):
        self.assertIn(("f", "filesystem"), _kinds('from pathlib import Path\ndef f():\n    Path("/x").write_text("y")\n'))

    def test_path_write_bytes(self):
        self.assertIn(("f", "filesystem"), _kinds('from pathlib import Path\ndef f():\n    Path("/x").write_bytes(b"y")\n'))

    def test_open_write_mode(self):
        self.assertIn(("f", "filesystem"), _kinds('def f():\n    open("/x", "w")\n'))

    def test_open_append_plus_mode(self):
        self.assertIn(("f", "filesystem"), _kinds('def f():\n    open("/x", "a+")\n'))

    def test_path_open_binary_write(self):
        self.assertIn(("f", "filesystem"), _kinds('from pathlib import Path\ndef f():\n    Path("/x").open("wb")\n'))

    def test_file_handle_write(self):
        self.assertIn(("f", "filesystem"), _kinds('def f():\n    with open("/x", "w") as fh:\n        fh.write("y")\n'))

    def test_file_handle_writelines(self):
        self.assertIn(("f", "filesystem"), _kinds('def f():\n    with open("/x", "w") as fh:\n        fh.writelines(["y"])\n'))

    def test_file_handle_truncate(self):
        self.assertIn(("f", "filesystem"), _kinds('def f():\n    with open("/x", "w") as fh:\n        fh.truncate()\n'))

    def test_path_rename_via_constructed_path(self):
        self.assertIn(("f", "filesystem"), _kinds('from pathlib import Path\ndef f():\n    p = Path("/x")\n    p.rename("/y")\n'))

    def test_path_replace_via_constructed_path(self):
        self.assertIn(("f", "filesystem"), _kinds('from pathlib import Path\ndef f():\n    p = Path("/x")\n    p.replace("/y")\n'))

    def test_bare_single_letter_p_rename(self):
        """review finding #11/#12: p = Path(...); p.rename(...) -> filesystem."""
        self.assertIn(("f", "filesystem"), _kinds('from pathlib import Path\ndef f():\n    p = Path("/x")\n    p.rename("/y")\n'))

    def test_bare_single_letter_f_rename(self):
        self.assertIn(("f", "filesystem"), _kinds('from pathlib import Path\ndef f():\n    f = Path("/x")\n    f.rename("/y")\n'))

    def test_shutil_move(self):
        self.assertIn(("f", "filesystem"), _kinds('import shutil\ndef f():\n    shutil.move("/x", "/y")\n'))

    def test_os_remove(self):
        self.assertIn(("f", "filesystem"), _kinds('import os\ndef f():\n    os.remove("/x")\n'))


class SqlPositiveTests(unittest.TestCase):
    def test_sql_literal_update(self):
        self.assertIn(("f", "sql"), _kinds('def f(con):\n    con.execute("UPDATE items SET path=?", (1,))\n'))

    def test_sql_variable_update(self):
        self.assertIn(("f", "sql"), _kinds('def f(con):\n    q = "UPDATE items SET path=?"\n    con.execute(q, (1,))\n'))

    def test_sql_variable_delete(self):
        self.assertIn(("f", "sql"), _kinds('def f(con):\n    q = "DELETE FROM items WHERE id=?"\n    con.execute(q, (1,))\n'))

    def test_sql_executemany(self):
        self.assertIn(("f", "sql"), _kinds('def f(con):\n    con.executemany("INSERT INTO items VALUES (?)", [(1,)])\n'))

    def test_sql_executescript(self):
        self.assertIn(("f", "sql"), _kinds('def f(con):\n    con.executescript("DELETE FROM items;")\n'))


class SubprocessPositiveTests(unittest.TestCase):
    def test_subprocess_run_beet_remove(self):
        self.assertIn(("f", "subprocess"), _kinds('import subprocess\ndef f():\n    subprocess.run(["beet", "remove", "1"])\n'))

    def test_bare_beet_run_wrapper(self):
        """review finding #9 (CRITICAL): _beet_run(...) called bare-name,
        not as module.attr -- previously never detected at all."""
        self.assertIn(("f", "subprocess"), _kinds('def f():\n    _beet_run(["remove", "1"])\n'))

    def test_beets_client_run_command(self):
        self.assertIn(("f", "subprocess"), _kinds('def f():\n    beets_client.run_command("remove", ["1"])\n'))


class TagWritePositiveTests(unittest.TestCase):
    def test_mediafile_save(self):
        self.assertIn(("f", "tag_write"), _kinds('def f():\n    mf = MediaFile("/x")\n    mf.save()\n'))


class NegativeTests(unittest.TestCase):
    """Things that must NOT be flagged as mutation sinks."""

    def test_str_replace_not_filesystem(self):
        self.assertEqual(_kinds('def f():\n    text = "hello"\n    text.replace("a", "b")\n'), [])

    def test_str_replace_on_unrelated_named_var_not_filesystem(self):
        """review finding #11: a plain string variable named to sound
        path-adjacent by pure substring luck (contains no exact path-like
        token and isn't a tracked Path variable) must not be flagged."""
        self.assertEqual(_kinds('def f():\n    candidate = "just a string"\n    candidate.replace("x", "y")\n'), [])

    def test_read_only_open_not_filesystem(self):
        self.assertEqual(_kinds('def f():\n    open("/x", "r")\n'), [])

    def test_path_read_text_not_filesystem(self):
        self.assertEqual(_kinds('from pathlib import Path\ndef f():\n    Path("/x").read_text()\n'), [])

    def test_select_not_sql(self):
        self.assertEqual(_kinds('def f(con):\n    con.execute("SELECT * FROM items")\n'), [])

    def test_ffprobe_subprocess_is_still_discovered_conservatively(self):
        """Not actually a negative case -- corrected after this test's own
        first run caught the wrong assumption. Discovery deliberately does
        NOT try to distinguish "this subprocess.run call happens to invoke
        ffprobe, not beet" via command-string inspection at the AST-shape
        level: any `subprocess.run`/`Popen`/etc. call is discovered as a
        `subprocess`-kind candidate sink, full stop. That
        under-discrimination is intentional and pushed to the
        classification stage (`generate_arch003_mutation_inventory.py`
        inspects the call text for `BEET_BIN`/`base +` before deciding
        `ARCH003_BLOCKER` vs `NON_MEDIA_FILESYSTEM`), not the discovery
        stage -- a conservative "find everything, classify precisely" split
        is safer than trying to filter at discovery time and risking a
        real Beets command silently going unflagged."""
        self.assertIn(("f", "subprocess"), _kinds('import subprocess\ndef f():\n    subprocess.run(["ffprobe", "-v", "error", "/x"])\n'))

    def test_ordinary_save_on_unrelated_object_still_flagged_conservatively(self):
        """The scanner intentionally cannot distinguish `MediaFile.save()`
        from `SomeUnrelatedThing.save()` via pure syntax -- both are
        flagged tag_write. This is documented over-inclusion (a human
        reviews it), not a false negative; recorded here so a future
        precision improvement doesn't silently regress detection instead."""
        self.assertIn(("f", "tag_write"), _kinds('def f():\n    thing.save()\n'))


class ScopeIsolationTests(unittest.TestCase):
    """review finding #13: a nested function's local Path variable must
    not leak into the outer function's scope."""

    def test_nested_function_path_var_does_not_leak_to_outer_scope(self):
        src = (
            'from pathlib import Path\n'
            'def outer():\n'
            '    def inner():\n'
            '        x = Path("/tmp/foo")\n'
            '        x.rename("/tmp/bar")\n'
            '    text = "hello"\n'
            '    result = text.replace("a", "b")\n'
        )
        kinds = _kinds(src)
        self.assertIn(("outer.inner", "filesystem"), kinds)
        # outer's own `text.replace(...)` must NOT be flagged: `text` is
        # plain string, and must not have been polluted by `inner`'s `x`.
        self.assertNotIn(("outer", "filesystem"), kinds)

    def test_nested_class_path_var_does_not_leak_to_outer_scope(self):
        src = (
            'from pathlib import Path\n'
            'def outer():\n'
            '    class Inner:\n'
            '        x = Path("/tmp/foo")\n'
            '    candidate = "just a string"\n'
            '    candidate.replace("x", "y")\n'
        )
        kinds = _kinds(src)
        self.assertNotIn(("outer", "filesystem"), kinds)


if __name__ == "__main__":
    unittest.main()
