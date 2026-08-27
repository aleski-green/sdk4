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


class DaemonTurnTests(unittest.TestCase):
    def setUp(self):
        MockAdapter.next_usage_tokens = None
        MockAdapter.next_usage_is_window = False
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg_path = os.path.join(self.tmp.name, "config.xml")
        with open(self.cfg_path, "w") as f:
            f.write("""<ellm>
  <backend>mock</backend>
  <trigger-tokens>80</trigger-tokens>
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
        s.settimeout(5)
        s.connect(self.daemon.sock_path)
        s.sendall((json.dumps(obj) + "\n").encode())
        fp = s.makefile("r")
        msgs = []
        for line in fp:
            msgs.append(json.loads(line))
            if msgs[-1].get("type") in ("done", "error", "status"):
                break
        s.close()
        return msgs

    def test_window_usage_does_not_double_count(self):
        MockAdapter.next_usage_tokens = 40
        MockAdapter.next_usage_is_window = True
        msgs = self._rpc({"cmd": "prompt", "text": "hello"})
        done = [m for m in msgs if m["type"] == "done"][0]
        self.assertTrue(done["ok"])
        self.assertEqual(done["session_tokens"], 40)

        MockAdapter.next_usage_tokens = 55
        MockAdapter.next_usage_is_window = True
        msgs = self._rpc({"cmd": "prompt", "text": "again"})
        done = [m for m in msgs if m["type"] == "done"][0]
        self.assertEqual(done["session_tokens"], 55)
        self.assertFalse(any(m["type"] == "leap" for m in msgs))
        self.assertFalse(any(m["type"] == "chunk" and "leaping" in m.get("text", "") for m in msgs))

    def test_leap_is_a_protocol_event_not_a_chunk(self):
        # Force a leap by reporting a window at/over the trigger.
        MockAdapter.next_usage_tokens = 80
        MockAdapter.next_usage_is_window = True
        msgs = self._rpc({"cmd": "prompt", "text": "please leap"})
        kinds = [m["type"] for m in msgs]
        self.assertIn("leap", kinds)
        self.assertIn("done", kinds)
        leap_msgs = [m for m in msgs if m["type"] == "leap"]
        self.assertEqual(leap_msgs[0]["phase"], "start")
        self.assertEqual(leap_msgs[-1]["phase"], "done")
        self.assertFalse(any(m["type"] == "chunk" and "leaping" in m.get("text", "") for m in msgs))


if __name__ == "__main__":
    unittest.main()
