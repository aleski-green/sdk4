import importlib.util
import contextlib
import io
import os
import sys
import tempfile
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


class StatusTests(unittest.TestCase):
    def test_status_falls_back_when_daemon_predates_chat_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            inst = os.path.join(tmp, "demo")
            os.makedirs(inst)
            conn = ellm_cli.store.connect(os.path.join(inst, "ellm.db"))
            try:
                ellm_cli.store.log_event(conn, "old-session", "prompt", {"text": "abcd"})
                ellm_cli.store.log_event(conn, "old-session", "response", {"text": "efgh"})
            finally:
                conn.close()
            cfg = dict(ellm_cli.store.BUILTIN_DEFAULTS)
            cfg.update({"instances_dir": tmp, "chars_per_token": 4,
                        "trigger_tokens": 180_000})
            old_status = {"pid": 1, "backend": "mock", "session_id": "old-session",
                          "leap_count": 0}
            output = io.StringIO()
            with mock.patch.object(ellm_cli, "is_running", return_value=True), \
                 mock.patch.object(ellm_cli, "rpc", return_value=old_status), \
                 contextlib.redirect_stdout(output):
                ellm_cli.cmd_status(cfg, "demo")
            self.assertIn("chat_tokens:    ~2 / 180,000", output.getvalue())


if __name__ == "__main__":
    unittest.main()
