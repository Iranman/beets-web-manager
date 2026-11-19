"""#794 Part 2: the restart sweep must resolve EVERY Running transaction,
regardless of how many there are.

``transaction_recovery.sweep`` asked ``TransactionStore.list`` for
``limit=1000``, but ``list`` caps a page at 500 with no pagination. With a
backlog of 500+ simultaneously-Running transactions the oldest (the 501st+)
were silently missed by a sweep and left Running forever. The fix pages the
sweep until the reported total is covered.

This test fails before the fix (records past the 500 cap stay Running) and
passes after. It touches only coverage, not the resolution disposition:
every resolved record lands in Recovery Required (never Completed, never
auto-rolled-back).
"""
import os
import tempfile
import time
import unittest
from unittest import mock

import backend.transaction_recovery as recovery
from backend.transaction_engine import TransactionStore


class SweepPaginationTests(unittest.TestCase):
    def test_sweep_resolves_backlog_larger_than_the_500_page_cap(self):
        count = 620  # > 500, so a single un-paged list() misses the overflow
        with tempfile.TemporaryDirectory() as tmp:
            tx_store = TransactionStore(root=os.path.join(tmp, "txstore"))
            ids = []
            for i in range(count):
                tx = tx_store.create(
                    operation_type="Import", status="Running",
                    source="recover_upload_singleton",
                    summary=f"backlog {i}",
                    metadata={"workflow": "recover_upload_singleton"},  # legacy, no family
                )
                ids.append(tx["id"])

            adapter = mock.Mock()
            recovery.sweep(adapter=adapter, store=tx_store, before=time.time() + 5)

            statuses = [tx_store.get(i)["status"] for i in ids]
            stuck = [i for i, s in zip(ids, statuses) if s != "Recovery Required"]
            self.assertEqual(stuck, [], f"{len(stuck)} of {count} left unresolved")
            # Coverage only: no record resolved to Completed, no adapter mutation.
            self.assertNotIn("Completed", statuses)
            self.assertEqual(adapter.method_calls, [], adapter.method_calls)


if __name__ == "__main__":
    unittest.main()
