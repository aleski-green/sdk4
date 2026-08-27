import os
import sys
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ellm.daemon import FifoLock


class FifoLockTests(unittest.TestCase):
    def test_waiters_run_in_arrival_order(self):
        lock = FifoLock()
        order = []
        lock.acquire()

        def waiter(name):
            lock.acquire()
            order.append(name)
            lock.release()

        threads = []
        for name in ("B", "C", "D"):
            before = len(lock._waiters)
            t = threading.Thread(target=waiter, args=(name,))
            t.start()
            threads.append(t)
            deadline = time.time() + 2
            while len(lock._waiters) == before:
                if time.time() > deadline:
                    self.fail("waiter %s did not queue" % name)
                time.sleep(0.005)

        lock.release()
        for t in threads:
            t.join(timeout=2)
        self.assertEqual(order, ["B", "C", "D"])


if __name__ == "__main__":
    unittest.main()
