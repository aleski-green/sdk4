import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ellm.router import reconcile_session_tokens, window_tokens_from_usage
from ellm import store


class WindowTokensTests(unittest.TestCase):
    def test_codex_does_not_count_cached(self):
        usage = {
            "input_tokens": 24763,
            "cached_input_tokens": 24448,
            "output_tokens": 122,
            "reasoning_output_tokens": 0,
        }
        self.assertEqual(window_tokens_from_usage(usage), 24763 + 122)

    def test_prefers_explicit_total(self):
        self.assertEqual(window_tokens_from_usage({
            "total_tokens": 99,
            "input_tokens": 10,
            "output_tokens": 5,
        }), 99)

    def test_fallback_skips_cached_keys(self):
        self.assertEqual(window_tokens_from_usage({
            "prompt_tokens": 100,
            "cached_tokens": 80,
            "completion_tokens": 20,
        }), 120)

    def test_empty(self):
        self.assertIsNone(window_tokens_from_usage({}))
        self.assertIsNone(window_tokens_from_usage(None))


class ReconcileTests(unittest.TestCase):
    def test_window_replaces(self):
        self.assertEqual(reconcile_session_tokens(50_000, 12_000, True, 800), 12_000)

    def test_delta_accumulates(self):
        self.assertEqual(reconcile_session_tokens(50_000, 800, False, 900), 50_800)

    def test_estimate_accumulates(self):
        self.assertEqual(reconcile_session_tokens(50_000, None, False, 900), 50_900)


class ChatContextTests(unittest.TestCase):
    def test_counts_only_active_session_message_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = store.connect(os.path.join(tmp, "ellm.db"))
            try:
                store.log_event(conn, "old", "prompt", {"text": "x" * 400})
                store.log_event(conn, "active", "prompt", {"text": "abcd"})
                store.log_event(conn, "active", "response", {"text": "efgh"})
                self.assertEqual(store.session_chat_tokens(conn, "active", 4), 2)
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
