"""AcadBridge.run gives up on a call without leaving it behind to run later.

All AutoCAD work goes through one COM thread, one call at a time. When AutoCAD
sits at a dialog, the call in progress hangs, run() times out, and whatever the
model sends next (usually the same drawing again) queues behind it. Those
queued calls must not fire once the dialog is closed: their callers were
already told they failed. Needs no AutoCAD: the calls here never touch COM.

    python -m unittest discover -s tests -t .
"""

import threading
import unittest

from acad import AcadBridge, AcadError


class BridgeTimeoutTest(unittest.TestCase):
    def setUp(self):
        self.bridge = AcadBridge()
        self.release = threading.Event()
        self.started = threading.Event()

    def tearDown(self):
        self.release.set()
        self.bridge._queue.put((None, None))
        self.bridge._thread.join(5)

    def _stuck(self, _acad):
        # Stands in for a COM call blocked behind a modal dialog.
        self.started.set()
        self.release.wait(10)
        return "late"

    def _hold_worker(self):
        # The stuck call times out for its caller but keeps the COM thread busy.
        with self.assertRaises(AcadError):
            self.bridge.run(self._stuck, timeout=0.1)
        self.assertTrue(self.started.is_set())

    def test_timed_out_call_that_never_started_does_not_run_later(self):
        self._hold_worker()
        ran = threading.Event()

        with self.assertRaises(AcadError) as caught:
            self.bridge.run(lambda _acad: ran.set(), timeout=0.2)

        self.release.set()
        # Something queued after the timeout still runs, so the worker is alive
        # and has had its chance to pick up the abandoned call.
        self.assertEqual(self.bridge.run(lambda _acad: "next", timeout=5), "next")
        self.assertFalse(ran.is_set(), "the abandoned call ran after its timeout")
        self.assertIn("was not sent to AutoCAD", str(caught.exception))

    def test_timed_out_call_that_already_started_says_it_may_still_finish(self):
        with self.assertRaises(AcadError) as caught:
            self.bridge.run(self._stuck, timeout=0.2)
        self.assertIn("may still complete", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
