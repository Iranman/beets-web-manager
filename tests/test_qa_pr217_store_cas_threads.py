"""QA (PR #217): two TransactionStore instances on one directory racing a
status compare-and-set from real threads -- exactly one may win, and no
write may fail (the route's store and the engine's store are distinct
objects in the app)."""

import tempfile
import threading
import unittest

from backend.transaction_engine import TransactionStore


class CrossStoreCasThreadTests(unittest.TestCase):
    def test_claim_and_cancel_from_two_stores_never_both_win(self):
        with tempfile.TemporaryDirectory() as d:
            engine, route = TransactionStore(d), TransactionStore(d)
            for _ in range(100):
                tid = engine.create(operation_type="Delete", status="Approved")["id"]
                barrier = threading.Barrier(2)
                won, errors = {}, []

                def race(store, new_status):
                    barrier.wait()
                    try:
                        won[new_status] = store.transition(tid, "Approved", new_status) is not None
                    except Exception as ex:  # a torn write is a failure too
                        errors.append(ex)

                threads = [threading.Thread(target=race, args=(engine, "Running")),
                           threading.Thread(target=race, args=(route, "Cancelled"))]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
                self.assertEqual(errors, [])
                self.assertEqual(sum(won.values()), 1, won)
                winner = next(s for s, ok in won.items() if ok)
                self.assertEqual(engine.get(tid)["status"], winner)


if __name__ == "__main__":
    unittest.main()
