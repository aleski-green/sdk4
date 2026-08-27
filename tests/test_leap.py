import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ellm import leap, store
from ellm.router import MockAdapter


def _events(*texts):
    out = []
    for i, text in enumerate(texts):
        out.append({"ts": "2026-01-01T00:00:0%sZ" % i, "type": "prompt" if i % 2 == 0 else "response",
                    "text": text})
    return out


class SplitTests(unittest.TestCase):
    def test_cut_does_not_bisect_a_turn(self):
        events = _events("aaaa", "bbbb", "cccc", "dddd")
        # tiny cut budget -> only the last event
        head, cut = leap.cut_and_head(events, cut_tokens=1, chars_per_token=4)
        self.assertEqual(len(cut), 1)
        self.assertEqual(cut[0]["text"], "dddd")
        self.assertEqual([e["text"] for e in head], ["aaaa", "bbbb", "cccc"])

    def test_split_keeps_whole_turns(self):
        events = _events("one", "two", "three", "four")
        slices = leap.split_events(events, 2)
        self.assertEqual(len(slices), 2)
        joined = "\n".join(slices)
        for word in ("one", "two", "three", "four"):
            self.assertIn(word, joined)
        for sl in slices:
            self.assertTrue("User:" in sl or "Assistant:" in sl)

    def test_compressor_prompt_ignores_other_braces(self):
        filled = leap.fill_compressor_prompt(
            "Keep {foo} under {CHUNK_TOKENS} tokens / {CHUNK_CHARS} chars {bar}",
            10, 40)
        self.assertEqual(filled, "Keep {foo} under 10 tokens / 40 chars {bar}")


class LeapFlowTests(unittest.TestCase):
    def setUp(self):
        MockAdapter.compress_failures_left = 0
        self.tmp = tempfile.TemporaryDirectory()
        self.inst = os.path.join(self.tmp.name, "inst")
        os.makedirs(os.path.join(self.inst, "logs"))
        self.conn = store.connect(os.path.join(self.inst, "ellm.db"))
        store.set_state(self.conn, "session_id", "old-sess")
        for i in range(8):
            kind = "prompt" if i % 2 == 0 else "response"
            store.log_event(self.conn, "old-sess", kind, {"text": "turn-%s %s" % (i, "x" * 40)})

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()
        MockAdapter.compress_failures_left = 0

    def _manifest(self, **kw):
        m = {
            "backend": "mock",
            "compressor_backend": "mock",
            "trigger_tokens": 50,
            "compressed_budget": 40,
            "cut_tokens": 8,
            "k": 2,
            "chars_per_token": 4,
            "compressor_prompt": "Compress to {CHUNK_TOKENS} tokens. Keep {json: true}.",
            "post_leap_prompt": "You leaped.",
        }
        m.update(kw)
        return m

    def test_leap_creates_new_session_and_context(self):
        new_id = leap.leap(self.conn, self._manifest(), self.inst, log=lambda *_: None)
        self.assertTrue(new_id.startswith("mock-"))
        self.assertNotEqual(new_id, "old-sess")
        self.assertEqual(store.get_state(self.conn, "session_id"), new_id)
        self.assertEqual(store.get_state(self.conn, "leap_count"), "1")
        self.assertTrue(os.path.isfile(os.path.join(self.inst, "context.md")))
        with open(os.path.join(self.inst, "context.md")) as f:
            context = f.read()
        self.assertIn("## Compressed memory", context)
        self.assertIn("## Recent context (verbatim)", context)

    def test_compressor_retries_once(self):
        MockAdapter.compress_failures_left = 1
        new_id = leap.leap(self.conn, self._manifest(), self.inst, log=lambda *_: None)
        self.assertTrue(new_id.startswith("mock-"))
        self.assertEqual(MockAdapter.compress_failures_left, 0)

    def test_compressor_fallback_after_two_failures(self):
        MockAdapter.compress_failures_left = 5
        new_id = leap.leap(self.conn, self._manifest(), self.inst, log=lambda *_: None)
        self.assertTrue(new_id.startswith("mock-"))
        with open(os.path.join(self.inst, "context.md")) as f:
            context = f.read()
        self.assertIn("compression failed", context)


if __name__ == "__main__":
    unittest.main()
