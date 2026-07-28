"""Fail-closed preflight guard against silent Beets library replacement.

Root cause this exists for (TrueNAS production incident, 2026-07-28):
Beets creates a brand-new, valid, completely empty SQLite library the
moment it opens a configured `library:` path that doesn't exist -- this
is normal, documented, and correct behavior for a genuine first-run
setup, but it is indistinguishable, from Beets' own point of view, from
a production deployment whose real database went missing mid-migration.
On the affected deployment, the real database (and most of config.yaml)
was removed from the live path as an incomplete migration/decommission
step; the next time anything invoked `beet` against that path, a fresh
empty library silently became "production" and stayed that way for
~29 hours before anyone noticed.

This module adds the missing distinction: an operator can declare
"this deployment expects an existing, populated library" via
BEETS_EXPECT_EXISTING_LIBRARY=1, and the guard then refuses to let the
caller proceed (non-zero exit, no side effects) unless the configured
database is a real file, passes PRAGMA integrity_check, has the
required Beets tables, and meets configured minimum item/album counts.
It never creates, repairs, or overwrites anything -- it only decides
whether it is safe to let Beets' own process continue.

Deliberately dependency-free (stdlib only): this same file is meant to
be droppable, unmodified, onto a bare LinuxServer.io Beets image as a
custom-cont-init.d hook, where nothing from this application's own
Python environment is guaranteed to be importable.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
from dataclasses import dataclass, field
from typing import List, Optional

REQUIRED_TABLES = ("items", "albums")

# Environment contract (see module docstring). Reusable code must not
# hard-code any single deployment's current row counts here -- a
# specific production deployment supplies stronger values via its own
# environment (compose file / .env), not by editing this module.
ENV_EXPECT_EXISTING_LIBRARY = "BEETS_EXPECT_EXISTING_LIBRARY"
ENV_MIN_EXPECTED_ITEMS = "BEETS_MIN_EXPECTED_ITEMS"
ENV_MIN_EXPECTED_ALBUMS = "BEETS_MIN_EXPECTED_ALBUMS"
ENV_LIBRARY_PATH = "BEETS_LIBRARY"
ENV_APPROVED_ROOT = "BEETS_APPROVED_CONFIG_ROOT"

DEFAULT_MIN_ITEMS = 1
DEFAULT_MIN_ALBUMS = 1
DEFAULT_APPROVED_ROOT = "/config"


@dataclass
class GuardResult:
    ok: bool
    reason: str
    path: str = ""
    size: int = 0
    sha256_prefix: str = ""
    integrity: str = ""
    items: Optional[int] = None
    albums: Optional[int] = None
    first_run: bool = False
    details: List[str] = field(default_factory=list)

    def log_lines(self) -> List[str]:
        """Lines safe to print/log: identity and status, never secrets."""
        lines = [
            f"path={self.path or '(unset)'}",
            f"size={self.size}",
            f"sha256_prefix={self.sha256_prefix}",
            f"integrity={self.integrity or 'unknown'}",
            f"items={self.items if self.items is not None else 'n/a'}",
            f"albums={self.albums if self.albums is not None else 'n/a'}",
            f"first_run={self.first_run}",
            f"ok={self.ok}",
            f"reason={self.reason}",
        ]
        lines.extend(self.details)
        return lines


def _sha256_prefix(path: str, length: int = 12) -> str:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()[:length]
    except OSError:
        return ""


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def check_library(
    library_path: str,
    *,
    expect_existing: Optional[bool] = None,
    min_items: Optional[int] = None,
    min_albums: Optional[int] = None,
    approved_root: Optional[str] = None,
) -> GuardResult:
    """Read-only decision: is it safe to let Beets open this library?

    Never creates, writes, or deletes anything. Returns a GuardResult
    whose `.ok` is False for every unsafe condition; callers (a CLI
    wrapper, a custom-cont-init.d script, or an engine's own startup
    path) are expected to refuse to start Beets when `.ok` is False.
    """
    expect_existing = (
        _env_flag(ENV_EXPECT_EXISTING_LIBRARY) if expect_existing is None else expect_existing
    )
    min_items = _env_int(ENV_MIN_EXPECTED_ITEMS, DEFAULT_MIN_ITEMS) if min_items is None else min_items
    min_albums = _env_int(ENV_MIN_EXPECTED_ALBUMS, DEFAULT_MIN_ALBUMS) if min_albums is None else min_albums
    approved_root = (
        os.environ.get(ENV_APPROVED_ROOT, DEFAULT_APPROVED_ROOT) if approved_root is None else approved_root
    )

    if not library_path:
        return GuardResult(ok=False, reason="no_library_path_configured")

    # Symlink-escape check: a symlink is fine as long as it still
    # resolves inside the approved config root.
    if os.path.islink(library_path):
        real = os.path.realpath(library_path)
        approved_real = os.path.realpath(approved_root)
        if os.path.commonpath([real, approved_real]) != approved_real:
            return GuardResult(
                ok=False,
                reason="symlink_escapes_approved_root",
                path=library_path,
                details=[f"resolved={real}", f"approved_root={approved_real}"],
            )

    exists = os.path.exists(library_path)

    if not exists:
        if expect_existing:
            return GuardResult(
                ok=False,
                reason="library_missing_but_existing_library_expected",
                path=library_path,
            )
        return GuardResult(ok=True, reason="first_run_no_existing_library", path=library_path, first_run=True)

    if not os.path.isfile(library_path):
        return GuardResult(ok=False, reason="library_path_is_not_a_regular_file", path=library_path)

    size = os.path.getsize(library_path)
    sha_prefix = _sha256_prefix(library_path)

    if size == 0:
        if expect_existing:
            return GuardResult(
                ok=False,
                reason="library_file_is_zero_bytes",
                path=library_path,
                size=size,
                sha256_prefix=sha_prefix,
            )
        return GuardResult(
            ok=True,
            reason="first_run_zero_byte_placeholder",
            path=library_path,
            size=size,
            sha256_prefix=sha_prefix,
            first_run=True,
        )

    try:
        con = sqlite3.connect(f"file:{library_path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return GuardResult(
            ok=False,
            reason=f"cannot_open_database: {exc}",
            path=library_path,
            size=size,
            sha256_prefix=sha_prefix,
        )

    try:
        cur = con.cursor()
        try:
            cur.execute("PRAGMA integrity_check;")
            integrity_rows = cur.fetchall()
            integrity = integrity_rows[0][0] if integrity_rows else "unknown"
        except sqlite3.DatabaseError as exc:
            return GuardResult(
                ok=False,
                reason=f"integrity_check_failed: {exc}",
                path=library_path,
                size=size,
                sha256_prefix=sha_prefix,
                integrity="error",
            )

        if integrity != "ok":
            return GuardResult(
                ok=False,
                reason="integrity_check_not_ok",
                path=library_path,
                size=size,
                sha256_prefix=sha_prefix,
                integrity=integrity,
            )

        try:
            cur.execute("SELECT name FROM sqlite_master WHERE type='table';")
            tables = {row[0] for row in cur.fetchall()}
        except sqlite3.DatabaseError as exc:
            return GuardResult(
                ok=False,
                reason=f"cannot_list_tables: {exc}",
                path=library_path,
                size=size,
                sha256_prefix=sha_prefix,
                integrity=integrity,
            )

        missing_tables = [t for t in REQUIRED_TABLES if t not in tables]
        if missing_tables:
            return GuardResult(
                ok=False,
                reason="missing_required_tables",
                path=library_path,
                size=size,
                sha256_prefix=sha_prefix,
                integrity=integrity,
                details=[f"missing={','.join(missing_tables)}"],
            )

        try:
            cur.execute("SELECT COUNT(*) FROM items;")
            items = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM albums;")
            albums = cur.fetchone()[0]
        except sqlite3.DatabaseError as exc:
            return GuardResult(
                ok=False,
                reason=f"cannot_read_counts: {exc}",
                path=library_path,
                size=size,
                sha256_prefix=sha_prefix,
                integrity=integrity,
            )

    finally:
        con.close()

    if expect_existing:
        if items == 0 and albums == 0:
            return GuardResult(
                ok=False,
                reason="valid_schema_but_zero_items_and_albums_in_existing_library_mode",
                path=library_path,
                size=size,
                sha256_prefix=sha_prefix,
                integrity=integrity,
                items=items,
                albums=albums,
            )
        if items < min_items:
            return GuardResult(
                ok=False,
                reason=f"items_below_minimum_threshold ({items} < {min_items})",
                path=library_path,
                size=size,
                sha256_prefix=sha_prefix,
                integrity=integrity,
                items=items,
                albums=albums,
            )
        if albums < min_albums:
            return GuardResult(
                ok=False,
                reason=f"albums_below_minimum_threshold ({albums} < {min_albums})",
                path=library_path,
                size=size,
                sha256_prefix=sha_prefix,
                integrity=integrity,
                items=items,
                albums=albums,
            )

    return GuardResult(
        ok=True,
        reason="existing_library_verified" if items or albums else "empty_but_permitted",
        path=library_path,
        size=size,
        sha256_prefix=sha_prefix,
        integrity=integrity,
        items=items,
        albums=albums,
    )


def main(argv: Optional[List[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    library_path = argv[0] if argv else os.environ.get(ENV_LIBRARY_PATH, "/config/musiclibrary.blb")

    result = check_library(library_path)
    for line in result.log_lines():
        print(f"[beets-startup-guard] {line}", file=sys.stderr)

    if not result.ok:
        print(
            f"[beets-startup-guard] REFUSING TO START: {result.reason}. "
            "No files were created, modified, or deleted.",
            file=sys.stderr,
        )
        return 1

    print(f"[beets-startup-guard] OK: {result.reason}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
