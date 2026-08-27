import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ellm.daemon import Daemon
from ellm.router import MockAdapter
from ellm import store


class DaemonTurnTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg_path = os.path.join(self.tmp.name, "config.xml")
        with open(self.cfg_path, "w") as f:
            f.write("""<ellm>
  <backend>mock</backend>
  <trigger-tokens>800</trigger-tokens>
  <compressed-budget>40</compressed-budget>
  <cut-tokens>8</cut-tokens>
  <k>2</k>
  <chars-per-token>4</chars-per-token>
  <instances-dir>%s</instances-dir>
  <idle-timeout>0</idle-timeout>
  <turn-timeout>5</turn-timeout>
</ellm>
""" % os.path.join(self.tmp.name, "ellms"))
        os.environ["ELLM_CONFIG"] = self.cfg_path
        self.daemon = Daemon("tester")
        self.thread = threading.Thread(target=self.daemon.run, daemon=True)
        self.thread.start()
        deadline = time.time() + 3
        while time.time() < deadline:
            if os.path.exists(self.daemon.sock_path):
                break
            time.sleep(0.02)
        else:
            self.fail("daemon socket did not appear")

    def tearDown(self):
        self.daemon.running = False
        try:
            self._rpc({"cmd": "stop"})
        except OSError:
            pass
        self.thread.join(timeout=3)
        os.environ.pop("ELLM_CONFIG", None)
        self.tmp.cleanup()

    def _rpc(self, obj):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.settimeout(5)
            s.connect(self.daemon.sock_path)
            s.sendall((json.dumps(obj) + "\n").encode())
            fp = s.makefile("r")
            msgs = []
            for line in fp:
                msgs.append(json.loads(line))
                if msgs[-1].get("type") in ("done", "error", "status"):
                    break
            return msgs
        finally:
            s.close()

    def test_chat_tokens_track_the_active_session(self):
        msgs = self._rpc({"cmd": "prompt", "text": "hello"})
        done = [m for m in msgs if m["type"] == "done"][0]
        self.assertTrue(done["ok"])

        msgs = self._rpc({"cmd": "prompt", "text": "again"})
        done = [m for m in msgs if m["type"] == "done"][0]
        self.assertEqual(done["chat_tokens"], store.session_chat_tokens(
            self.daemon.conn, done["session_id"], 4))
        self.assertFalse(any(m["type"] == "leap" for m in msgs))
        self.assertFalse(any(m["type"] == "chunk" and "leaping" in m.get("text", "") for m in msgs))

    def test_chat_length_alone_triggers_a_leap(self):
        self.daemon.cfg["trigger_tokens"] = 1
        msgs = self._rpc({"cmd": "prompt", "text": "hello"})
        start = next(m for m in msgs if m["type"] == "leap" and m["phase"] == "start")
        self.assertEqual(start["reason"], "chat_context")
        self.assertGreater(start["chat_tokens"], 1)

    def test_leap_is_a_protocol_event_not_a_chunk(self):
        # Force a leap by making the local chat estimate reach the trigger.
        self.daemon.cfg["trigger_tokens"] = 1
        msgs = self._rpc({"cmd": "prompt", "text": "please leap"})
        kinds = [m["type"] for m in msgs]
        self.assertIn("leap", kinds)
        self.assertIn("done", kinds)
        leap_msgs = [m for m in msgs if m["type"] == "leap"]
        self.assertEqual(leap_msgs[0]["phase"], "start")
        self.assertEqual(leap_msgs[-1]["phase"], "done")
        self.assertFalse(any(m["type"] == "chunk" and "leaping" in m.get("text", "") for m in msgs))

    def test_stop_rejects_a_prompt_already_waiting_in_the_queue(self):
        original_send = MockAdapter.send
        started = threading.Event()
        release = threading.Event()

        def slow_send(adapter, work_dir, session_id, prompt, on_chunk=None, timeout=None):
            if prompt == "first":
                started.set()
                self.assertTrue(release.wait(3), "test did not release active turn")
            return original_send(adapter, work_dir, session_id, prompt, on_chunk, timeout)

        MockAdapter.send = slow_send
        try:
            first, queued = {}, {}
            first_thread = threading.Thread(
                target=lambda: first.setdefault("msgs", self._rpc({"cmd": "prompt", "text": "first"})))
            first_thread.start()
            self.assertTrue(started.wait(2), "first prompt did not start")

            queued_thread = threading.Thread(
                target=lambda: queued.setdefault("msgs", self._rpc({"cmd": "prompt", "text": "second"})))
            queued_thread.start()
            deadline = time.time() + 2
            while len(self.daemon.turn_lock._waiters) < 2:
                if time.time() > deadline:
                    self.fail("second prompt did not enter FIFO queue")
                time.sleep(0.01)

            stopped = self._rpc({"cmd": "stop"})
            self.assertEqual(stopped[-1]["type"], "done")
            release.set()
            first_thread.join(timeout=3)
            queued_thread.join(timeout=3)

            self.assertFalse(first_thread.is_alive())
            self.assertFalse(queued_thread.is_alive())
            self.assertEqual(queued["msgs"][-1]["type"], "error")
            self.assertEqual(queued["msgs"][-1]["message"], "daemon is stopping")
        finally:
            release.set()
            MockAdapter.send = original_send


if __name__ == "__main__":
    unittest.main()
