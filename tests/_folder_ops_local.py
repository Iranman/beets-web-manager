"""Run folder_cleanup_v1 steps through the real webmanager plugin step code.

In production ``BeetsAdapter.folder_op`` POSTs each step to stock Beets
(``beetsplug/webmanager/folder_ops.py``) because Web Manager mounts the
library read-only. Tests on a temp library call the same plugin function
in-process instead, with the same containment rules and error codes.
"""

from __future__ import annotations

import os
from unittest import mock

from backend.beets_adapter import BeetsAdapterError
from beetsplug.webmanager import folder_ops


class _NoTrackedItems:
    def items(self, _query):
        return []


class LocalFolderOps:
    def __init__(self, library_root):
        self.root = os.path.abspath(str(library_root))
        self.calls = []

    def folder_op(self, op, idempotency_key, **paths):
        self.calls.append((op, idempotency_key, paths))
        try:
            return {"success": True, **folder_ops._step(_NoTrackedItems(), self.root, {"op": op, **paths})}
        except folder_ops._Refused as exc:
            raise BeetsAdapterError(exc.message, status_code=exc.status, error_code=exc.code) from None


def patch_local_folder_ops(testcase, library_root) -> LocalFolderOps:
    """Route the default adapter's folder_op to ``LocalFolderOps`` for one test."""
    local = LocalFolderOps(library_root)
    patcher = mock.patch("backend.beets_adapter.beets_adapter.folder_op", local.folder_op)
    patcher.start()
    testcase.addCleanup(patcher.stop)
    return local
