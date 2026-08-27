"""router.py - backend adapters wrapping vendor CLIs (codex, kimi, mock).

Uniform interface:
    adapter.send(work_dir, session_id, prompt, on_chunk, timeout) -> TurnResult
    adapter.compress(work_dir, full_prompt, timeout) -> str

Session continuity uses each vendor's *native* session resume:
  codex: codex exec --json ...  /  codex exec resume <id> --json ...
  kimi:  kimi -p ... --output-format stream-json  /  kimi --session <id> -p ...
Context is owned by the vendor session store; nothing is re-fed by us.

Codex prompts are always sent on stdin (`codex exec -`) so leap seeds cannot
hit ARG_MAX. stdin is a closed pipe/file — leaving an unused stdin open hangs Codex.
"""

import json
import os
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from typing import Callable, Optional

OnChunk = Optional[Callable[[str], None]]

# Fields that are subsets of another usage counter, not extra window tokens.
_CACHED_USAGE_KEYS = {
    "cached_input_tokens", "cachedInputTokens",
    "cached_output_tokens", "cachedOutputTokens",
    "cache_creation_input_tokens", "cache_read_input_tokens",
}


@dataclass
class TurnResult:
    text: str
    session_id: str
    usage_tokens: Optional[int] = None
    # True when usage_tokens is the current context window after this turn
    # (Codex input+output). False when it is a per-turn delta to accumulate.
    usage_is_window: bool = False


class BackendError(RuntimeError):
    pass


def window_tokens_from_usage(usage):
    """Current context window after a turn, from a vendor usage object.

    Codex `turn.completed.usage` is per-turn, not cumulative, and has no
    `total_tokens`. Typical fields:
      input_tokens              full prompt/history this turn
      cached_input_tokens       subset of input_tokens (do NOT add)
      output_tokens
      reasoning_output_tokens
    Window ≈ input + output + reasoning.
    """
    if not isinstance(usage, dict) or not usage:
        return None
    explicit = usage.get("total_tokens")
    if explicit is None:
        explicit = usage.get("totalTokens")
    if isinstance(explicit, int) and explicit > 0:
        return explicit

    inp = usage.get("input_tokens")
    if inp is None:
        inp = usage.get("inputTokens")
    out = usage.get("output_tokens")
    if out is None:
        out = usage.get("outputTokens")
    reason = usage.get("reasoning_output_tokens")
    if reason is None:
        reason = usage.get("reasoningOutputTokens")

    if isinstance(inp, int):
        total = inp
        if isinstance(out, int):
            total += out
        if isinstance(reason, int):
            total += reason
        return total if total > 0 else None

    total, found = 0, False
    for key, value in usage.items():
        if not isinstance(value, int):
            continue
        lower = key.lower()
        if "token" not in lower:
            continue
        if key in _CACHED_USAGE_KEYS or "cached" in lower:
            continue
        total += value
        found = True
    return total if found and total > 0 else None


def reconcile_session_tokens(previous, usage_tokens, usage_is_window, estimated_turn):
    """Update session token count after a turn.

    Window-sized usage (Codex) replaces the counter — it already includes history.
    Otherwise accumulate this turn's new tokens.
    """
    previous = int(previous or 0)
    if usage_tokens:
        if usage_is_window:
            return int(usage_tokens)
        return previous + int(usage_tokens)
    return previous + int(estimated_turn)


def apply_agent_text(state, text, on_chunk):
    """Emit only newly arrived assistant text. Handles cumulative snapshots."""
    if not text:
        return
    prev = state.get("emitted", "")
    if text == prev:
        return
    if prev and text.startswith(prev):
        piece = text[len(prev):]
        state["emitted"] = text
        if piece and on_chunk:
            on_chunk(piece)
    elif prev and prev.startswith(text):
        return
    else:
        state["emitted"] = text
        if on_chunk:
            on_chunk(text)
    state.setdefault("texts", []).append(state["emitted"])


def apply_agent_delta(state, delta, on_chunk):
    if not delta:
        return
    state["emitted"] = state.get("emitted", "") + delta
    state.setdefault("texts", []).append(state["emitted"])
    if on_chunk:
        on_chunk(delta)


def apply_agent_message_event(state, ev, on_chunk):
    """Handle Codex JSONL item.* events for assistant text, including deltas."""
    item = ev.get("item") or {}
    itype = item.get("type") or ""
    if itype and itype != "agent_message":
        return
    etype = ev.get("type") or ""
    delta = ev.get("delta") if not isinstance(ev.get("delta"), dict) else ev["delta"].get("text")
    if delta is None and isinstance(item.get("delta"), str):
        delta = item.get("delta")
    elif delta is None and isinstance(item.get("delta"), dict):
        delta = item["delta"].get("text")
    if etype in ("item.delta", "response.output_text.delta") and isinstance(delta, str) and delta:
        apply_agent_delta(state, delta, on_chunk)
        return
    text = item.get("text")
    if isinstance(text, str) and text:
        apply_agent_text(state, text, on_chunk)


def _run(cmd, work_dir, on_chunk=None, parse=None, timeout=None, stdin_data=None):
    """Run a CLI, streaming stdout lines through `parse`.

    stderr is drained on a side thread so a noisy CLI cannot deadlock the pipe.
    """
    proc = subprocess.Popen(
        cmd,
        cwd=work_dir,
        stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    stderr_buf = []

    def _drain_stderr():
        try:
            stderr_buf.append(proc.stderr.read() or "")
        except OSError:
            stderr_buf.append("")

    stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
    stderr_thread.start()

    if stdin_data is not None:
        try:
            proc.stdin.write(stdin_data)
        finally:
            proc.stdin.close()

    timed_out = []
    killer = None
    if timeout:
        def _kill():
            timed_out.append(True)
            try:
                proc.kill()
            except OSError:
                pass
        killer = threading.Timer(timeout, _kill)
        killer.daemon = True
        killer.start()

    state = {}
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            if parse:
                parse(line, state, on_chunk)
    finally:
        if killer is not None:
            killer.cancel()

    stderr_thread.join(timeout=2)
    rc = proc.wait()
    stderr = "".join(stderr_buf)
    if timed_out:
        raise BackendError("timed out after %ss: %s" % (timeout, stderr[-500:]))
    return state, stderr, rc


# ---------------------------------------------------------------- codex

class CodexAdapter:
    name = "codex"
    binary = os.environ.get("ELLM_CODEX_BIN", "codex")
    usage_is_window = True

    def _parse_jsonl(self, line, state, on_chunk):
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            return
        etype = ev.get("type", "")
        if etype == "thread.started" and ev.get("thread_id"):
            state["session_id"] = ev["thread_id"]
        elif etype in ("item.completed", "item.updated", "item.delta",
                       "response.output_text.delta"):
            apply_agent_message_event(state, ev, on_chunk)
        elif etype == "turn.completed":
            tokens = window_tokens_from_usage(ev.get("usage") or {})
            if tokens:
                state["usage_tokens"] = tokens
        elif etype == "turn.failed":
            err = ev.get("error") or ev.get("message") or "turn.failed"
            if isinstance(err, dict):
                err = err.get("message") or err.get("error") or json.dumps(err)
            state["error"] = str(err)
        elif etype == "error":
            msg = ev.get("message") or ev.get("error") or line
            if isinstance(msg, dict):
                msg = msg.get("message") or json.dumps(msg)
            # transient reconnect notices are not fatal
            if isinstance(msg, str) and msg.startswith("Reconnecting"):
                return
            state["error"] = str(msg)

    def _exec(self, work_dir, prompt, session_id=None, ephemeral=False,
              on_chunk=None, timeout=None):
        with tempfile.NamedTemporaryFile("r", suffix=".txt", delete=False) as tf:
            out_file = tf.name
        try:
            # Prompt goes on stdin via `-` so large leap seeds cannot hit ARG_MAX.
            if session_id:
                cmd = [self.binary, "exec", "resume", session_id,
                       "--json", "--skip-git-repo-check",
                       "-o", out_file, "-"]
            else:
                cmd = [self.binary, "exec",
                       "--json", "--skip-git-repo-check",
                       "-C", work_dir,
                       "-o", out_file, "-"]
                if ephemeral:
                    cmd.append("--ephemeral")
            state, stderr, rc = _run(
                cmd, work_dir, on_chunk, self._parse_jsonl,
                timeout=timeout, stdin_data=prompt if prompt.endswith("\n") else prompt + "\n",
            )
            if state.get("error") and rc == 0:
                raise BackendError("codex: %s" % state["error"])
            if rc != 0:
                raise BackendError("codex exited %s: %s" % (rc, (state.get("error") or stderr)[-500:]))
            text = ""
            if os.path.exists(out_file):
                with open(out_file) as f:
                    text = f.read().strip()
            if not text and state.get("emitted"):
                text = state["emitted"]
            elif not text and state.get("texts"):
                text = state["texts"][-1]
            if not session_id and not state.get("session_id"):
                raise BackendError("codex did not report a thread id: %s" % stderr[-300:])
            return TurnResult(
                text=text,
                session_id=state.get("session_id") or session_id,
                usage_tokens=state.get("usage_tokens"),
                usage_is_window=True,
            )
        finally:
            try:
                os.unlink(out_file)
            except OSError:
                pass

    def send(self, work_dir, session_id, prompt, on_chunk=None, timeout=None) -> TurnResult:
        return self._exec(work_dir, prompt, session_id=session_id,
                          on_chunk=on_chunk, timeout=timeout)

    def compress(self, work_dir, full_prompt, timeout=None) -> str:
        res = self._exec(work_dir, full_prompt, ephemeral=True, timeout=timeout)
        return res.text


# ---------------------------------------------------------------- kimi

class KimiAdapter:
    name = "kimi"
    binary = os.environ.get("ELLM_KIMI_BIN", "kimi")

    CONTINUE = "__continue__"  # sentinel: resume last session for this cwd

    def _parse_jsonl(self, line, state, on_chunk):
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            return
        for key in ("session_id", "sessionId"):
            if ev.get(key):
                state["session_id"] = ev[key]
        if ev.get("type") in ("error", "turn.failed"):
            err = ev.get("message") or ev.get("error") or line
            state["error"] = str(err)
            return
        msg = ev.get("message") or ev
        role = msg.get("role") or ev.get("role") or ""
        mtype = ev.get("type", "")
        if role == "assistant" or "assistant" in mtype.lower():
            content = msg.get("content")
            if isinstance(content, str) and content:
                apply_agent_text(state, content, on_chunk)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text" and part.get("text"):
                        apply_agent_text(state, part["text"], on_chunk)
        usage = ev.get("usage") or (msg.get("usage") if isinstance(msg, dict) else None)
        tokens = window_tokens_from_usage(usage) if isinstance(usage, dict) else None
        if tokens:
            state["usage_tokens"] = tokens
            # kimi usage is treated as a window when input_tokens is present
            state["usage_is_window"] = "input_tokens" in usage or "inputTokens" in usage

    def _exec(self, work_dir, prompt, session_id=None, on_chunk=None, timeout=None):
        cmd = [self.binary]
        if session_id and session_id != self.CONTINUE:
            cmd += ["--session", session_id]
        elif session_id == self.CONTINUE:
            cmd += ["--continue"]
        cmd += ["-p", prompt, "--output-format", "stream-json"]
        state, stderr, rc = _run(cmd, work_dir, on_chunk, self._parse_jsonl, timeout=timeout)
        if state.get("error") and rc == 0:
            raise BackendError("kimi: %s" % state["error"])
        if rc != 0:
            raise BackendError("kimi exited %s: %s" % (rc, (state.get("error") or stderr)[-500:]))
        text = state.get("emitted") or ((state.get("texts") or [""])[-1])
        return TurnResult(
            text=text,
            session_id=state.get("session_id") or session_id or self.CONTINUE,
            usage_tokens=state.get("usage_tokens"),
            usage_is_window=bool(state.get("usage_is_window")),
        )

    def send(self, work_dir, session_id, prompt, on_chunk=None, timeout=None) -> TurnResult:
        return self._exec(work_dir, prompt, session_id=session_id,
                          on_chunk=on_chunk, timeout=timeout)

    def compress(self, work_dir, full_prompt, timeout=None) -> str:
        # Isolate cwd so a one-shot compress cannot become the `--continue` target.
        tmp = tempfile.mkdtemp(prefix="ellm-compress-", dir=work_dir)
        try:
            res = self._exec(tmp, full_prompt, session_id=None, timeout=timeout)
            return res.text
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- mock (tests)

class MockAdapter:
    """Deterministic fake backend for tests; no external CLI needed."""
    name = "mock"
    compress_failures_left = 0
    last_compress_cwd = None
    next_usage_tokens = None
    next_usage_is_window = False

    def send(self, work_dir, session_id, prompt, on_chunk=None, timeout=None) -> TurnResult:
        import uuid
        sid = session_id or "mock-%s" % uuid.uuid4().hex[:8]
        text = "[mock reply to %s chars] %r" % (len(prompt), prompt[:60])
        if on_chunk:
            on_chunk(text)
        usage = MockAdapter.next_usage_tokens
        window = MockAdapter.next_usage_is_window
        MockAdapter.next_usage_tokens = None
        MockAdapter.next_usage_is_window = False
        return TurnResult(text=text, session_id=sid, usage_tokens=usage,
                          usage_is_window=window)

    def compress(self, work_dir, full_prompt, timeout=None) -> str:
        MockAdapter.last_compress_cwd = work_dir
        if MockAdapter.compress_failures_left > 0:
            MockAdapter.compress_failures_left -= 1
            raise RuntimeError("mock compressor failed")
        return "[mock-compressed %s chars] ...%s" % (len(full_prompt), full_prompt[-200:])


ADAPTERS = {
    "codex": CodexAdapter,
    "kimi": KimiAdapter,
    "mock": MockAdapter,
}


def get_adapter(name: str):
    if name not in ADAPTERS:
        raise BackendError("unknown backend %r; available: %s" % (name, ", ".join(ADAPTERS)))
    return ADAPTERS[name]()
