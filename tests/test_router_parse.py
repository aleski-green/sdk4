import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ellm.router import CodexAdapter, apply_agent_text


class AgentTextTests(unittest.TestCase):
    def test_cumulative_snapshot_emits_suffix_only(self):
        state, chunks = {}, []
        apply_agent_text(state, "Hello", chunks.append)
        apply_agent_text(state, "Hello world", chunks.append)
        apply_agent_text(state, "Hello world", chunks.append)
        self.assertEqual(chunks, ["Hello", " world"])
        self.assertEqual(state["emitted"], "Hello world")

    def test_shorter_prefix_ignored(self):
        state, chunks = {}, []
        apply_agent_text(state, "Hello world", chunks.append)
        apply_agent_text(state, "Hello", chunks.append)
        self.assertEqual(chunks, ["Hello world"])


class CodexParseTests(unittest.TestCase):
    def _feed(self, events):
        adapter = CodexAdapter()
        state, chunks = {}, []
        for ev in events:
            adapter._parse_jsonl(json.dumps(ev), state, chunks.append)
        return state, chunks

    def test_thread_id_and_message_text(self):
        state, chunks = self._feed([
            {"type": "thread.started", "thread_id": "tid-1"},
            {"type": "item.completed",
             "item": {"type": "agent_message", "text": "hi"}},
        ])
        self.assertEqual(state["session_id"], "tid-1")
        self.assertEqual(chunks, ["hi"])

    def test_delta_then_completed_does_not_repeat(self):
        state, chunks = self._feed([
            {"type": "item.delta", "item": {"type": "agent_message"}, "delta": "Hel"},
            {"type": "item.delta", "item": {"type": "agent_message", "delta": "lo"}},
            {"type": "item.completed",
             "item": {"type": "agent_message", "text": "Hello"}},
        ])
        self.assertEqual("".join(chunks), "Hello")
        self.assertEqual(state["emitted"], "Hello")

    def test_completed_tool_call_is_counted_once(self):
        state, _ = self._feed([
            {"type": "item.updated",
             "item": {"type": "command_execution", "command": "pwd"}},
            {"type": "item.completed",
             "item": {"type": "command_execution", "command": "pwd"}},
        ])
        self.assertEqual(state["tool_calls"], 1)

    def test_turn_failed_recorded(self):
        state, _ = self._feed([
            {"type": "turn.failed", "error": {"message": "boom"}},
        ])
        self.assertEqual(state["error"], "boom")

    def test_reconnect_notice_ignored(self):
        state, _ = self._feed([
            {"type": "error", "message": "Reconnecting... 1/3"},
        ])
        self.assertNotIn("error", state)

    def test_prompt_sent_on_stdin_not_argv(self):
        import tempfile
        import ellm.router as router_mod

        captured = {}

        def fake_run(cmd, work_dir, on_chunk=None, parse=None, timeout=None, stdin_data=None):
            captured["cmd"] = list(cmd)
            captured["stdin"] = stdin_data
            return {"session_id": "tid-stdin", "emitted": "ok"}, "", 0

        orig = router_mod._run
        router_mod._run = fake_run
        try:
            adapter = CodexAdapter()
            with tempfile.TemporaryDirectory() as tmp:
                res = adapter.send(tmp, None, "hello world")
                self.assertEqual(res.session_id, "tid-stdin")
                self.assertIn("--sandbox", captured["cmd"])
                self.assertIn("danger-full-access", captured["cmd"])
                self.assertEqual(captured["cmd"][-1], "-")
                self.assertNotIn("hello world", captured["cmd"])
                self.assertTrue(captured["stdin"].startswith("hello world"))

                captured.clear()
                adapter.send(tmp, "tid-stdin", "follow up")
                self.assertIn("resume", captured["cmd"])
                self.assertIn("--sandbox", captured["cmd"])
                self.assertIn("danger-full-access", captured["cmd"])
                self.assertEqual(captured["cmd"][-1], "-")
                self.assertNotIn("follow up", captured["cmd"])
        finally:
            router_mod._run = orig


class KimiCompressTests(unittest.TestCase):
    def test_compress_uses_isolated_cwd(self):
        import tempfile
        from ellm.router import KimiAdapter

        adapter = KimiAdapter()
        seen = {}

        def fake_exec(work_dir, prompt, session_id=None, on_chunk=None, timeout=None):
            seen["work_dir"] = work_dir
            seen["session_id"] = session_id
            from ellm.router import TurnResult
            return TurnResult(text="summary", session_id="kimi-x")

        adapter._exec = fake_exec
        with tempfile.TemporaryDirectory() as tmp:
            out = adapter.compress(tmp, "slice text")
            self.assertEqual(out, "summary")
            self.assertIsNone(seen["session_id"])
            self.assertTrue(seen["work_dir"].startswith(tmp))
            self.assertNotEqual(seen["work_dir"], tmp)
            self.assertFalse(os.path.exists(seen["work_dir"]))


if __name__ == "__main__":
    unittest.main()
