"""Patch one module's ``time`` name, never the process-wide ``time`` module (#266, #373).

``mock.patch.object(slskd.time, "sleep")`` patches ``time.sleep`` itself,
because ``slskd.time`` *is* the ``time`` module: every thread in the process
(a job thread left over from another test, a store heartbeat) then hits the
fake or uses up a fake clock's ticks. ``module_time(slskd, sleep=fake)`` swaps
only ``slskd``'s ``time`` name for a copy of ``time`` carrying the fakes.
Pass the same fake to each module whose sleeps a test must see.
"""

import time
import types
from unittest import mock


def module_time(module, **fakes):
    real = {name: getattr(time, name) for name in dir(time) if not name.startswith("_")}
    return mock.patch.object(module, "time", types.SimpleNamespace(**{**real, **fakes}))
