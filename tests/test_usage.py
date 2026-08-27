import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ellm import store


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
