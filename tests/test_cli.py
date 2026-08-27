import importlib.util
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

spec = importlib.util.spec_from_file_location("ellm_cli", os.path.join(ROOT, "ellm.py"))
ellm_cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ellm_cli)


class StopTests(unittest.TestCase):
    def test_stop_waits_until_daemon_socket_is_gone(self):
        # A default turn may run for up to ten minutes. The client must not
        # declare success merely because ten seconds elapsed.
        running_states = [True] + [True] * 101 + [False]
        with mock.patch.object(ellm_cli, "is_running", side_effect=running_states) as is_running, \
             mock.patch.object(ellm_cli, "rpc"), \
             mock.patch.object(ellm_cli.time, "sleep"):
            ellm_cli.cmd_stop({}, "demo")
        self.assertEqual(is_running.call_count, len(running_states))


if __name__ == "__main__":
    unittest.main()
