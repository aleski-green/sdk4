"""router.py - backend adapters wrapping vendor CLIs (codex, kimi, mock).

Uniform interface:
    adapter.send(work_dir, session_id, prompt, on_chunk) -> TurnResult
    adapter.compress(work_dir, full_prompt) -> str

Session continuity uses each vendor's *native* session resume:
  codex: codex exec --json ...  /  codex exec resume <id> --json ...
  kimi:  kimi -p ... --output-format stream-json  /  kimi --session <id> -p ...
Context is owned by the vendor session store; nothing is re-fed by us.
"""

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Callable, Optional

OnChunk = Optional[Callable[[str], None]]


@dataclass
class TurnResult:
    text: str
    session_id: str
    usage_tokens: Optional[int] = None  # real tokens if the backend reports them


class BackendError(RuntimeError):
    pass


def _run(cmd, work_dir, on_chunk=None, parse=None):
    """Run a CLI, streaming stdout lines through `parse`. Returns (parsed_state, stderr, rc)."""
    proc = subprocess.Popen(
        cmd,
        cwd=work_dir,
        stdin=subprocess.DEVNULL,  # codex hangs on open stdin pipes
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    state = {}
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        if parse:
            parse(line, state, on_chunk)
    stderr = proc.stderr.read() if proc.stderr else ""
    rc = proc.wait()
    return state, stderr, rc


# ---------------------------------------------------------------- codex

class CodexAdapter:
    name = "codex"
    binary = os.environ.get("ELLM_CODEX_BIN", "codex")

    def _parse_jsonl(self, line, state, on_chunk):
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            return
        etype = ev.get("type", "")
        if etype == "thread.started" and ev.get("thread_id"):
            state["session_id"] = ev["thread_id"]
        elif etype in ("item.completed", "item.updated"):
            item = ev.get("item", {})
            if item.get("type") == "agent_message" and item.get("text"):
                state.setdefault("texts", [])
                if not state["texts"] or state["texts"][-1] != item["text"]:
                    state["texts"].append(item["text"])
                    if on_chunk:
                        on_chunk(item["text"])
        elif etype == "turn.completed":
            usage = ev.get("usage") or {}
            total = usage.get("total_tokens")
            if total is None and usage:
                total = sum(v for k, v in usage.items()
                            if isinstance(v, int) and k.endswith("tokens"))
            if total:
                state["usage_tokens"] = total

    def _exec(self, work_dir, prompt, session_id=None, ephemeral=False, on_chunk=None):
        with tempfile.NamedTemporaryFile("r", suffix=".txt", delete=False) as tf:
            out_file = tf.name
        try:
            if session_id:
                cmd = [self.binary, "exec", "resume", session_id,
                       "--json", "--skip-git-repo-check",
                       "-o", out_file, prompt]
            else:
                cmd = [self.binary, "exec",
                       "--json", "--skip-git-repo-check",
                       "-C", work_dir,
                       "-o", out_file, prompt]
                if ephemeral:
                    cmd.append("--ephemeral")
            state, stderr, rc = _run(cmd, work_dir, on_chunk, self._parse_jsonl)
            if rc != 0:
                raise BackendError(f"codex exited {rc}: {stderr[-500:]}")
            text = ""
            if os.path.exists(out_file):
                with open(out_file) as f:
                    text = f.read().strip()
            if not text and state.get("texts"):
                text = state["texts"][-1]
            if not session_id and not state.get("session_id"):
                raise BackendError(f"codex did not report a thread id: {stderr[-300:]}")
            return TurnResult(
                text=text,
                session_id=state.get("session_id") or session_id,
                usage_tokens=state.get("usage_tokens"),
            )
        finally:
            try:
                os.unlink(out_file)
            except OSError:
                pass

    def send(self, work_dir, session_id, prompt, on_chunk=None) -> TurnResult:
        return self._exec(work_dir, prompt, session_id=session_id, on_chunk=on_chunk)

    def compress(self, work_dir, full_prompt) -> str:
        res = self._exec(work_dir, full_prompt, ephemeral=True)
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
        # session id, if the stream exposes one
        for key in ("session_id", "sessionId"):
            if ev.get(key):
                state["session_id"] = ev[key]
        msg = ev.get("message") or ev
        role = msg.get("role") or ev.get("role") or ""
        mtype = ev.get("type", "")
        if role == "assistant" or "assistant" in mtype.lower():
            content = msg.get("content")
            if isinstance(content, str) and content:
                state.setdefault("texts", []).append(content)
                if on_chunk:
                    on_chunk(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text" and part.get("text"):
                        state.setdefault("texts", []).append(part["text"])
                        if on_chunk:
                            on_chunk(part["text"])
        usage = ev.get("usage") or (msg.get("usage") if isinstance(msg, dict) else None)
        if isinstance(usage, dict):
            total = usage.get("total_tokens") or usage.get("totalTokens")
            if total is None:
                total = sum(v for k, v in usage.items()
                            if isinstance(v, int) and "token" in k.lower())
            if total:
                state["usage_tokens"] = total

    def _exec(self, work_dir, prompt, session_id=None, on_chunk=None):
        cmd = [self.binary]
        if session_id and session_id != self.CONTINUE:
            cmd += ["--session", session_id]
        elif session_id == self.CONTINUE:
            cmd += ["--continue"]
        cmd += ["-p", prompt, "--output-format", "stream-json"]
        state, stderr, rc = _run(cmd, work_dir, on_chunk, self._parse_jsonl)
        if rc != 0:
            raise BackendError(f"kimi exited {rc}: {stderr[-500:]}")
        texts = state.get("texts") or []
        return TurnResult(
            text=texts[-1] if texts else "",
            # kimi sessions are per-cwd; if no id surfaced, resume via --continue
            session_id=state.get("session_id") or session_id or self.CONTINUE,
            usage_tokens=state.get("usage_tokens"),
        )

    def send(self, work_dir, session_id, prompt, on_chunk=None) -> TurnResult:
        return self._exec(work_dir, prompt, session_id=session_id, on_chunk=on_chunk)

    def compress(self, work_dir, full_prompt) -> str:
        # fresh one-shot session (no --continue) so compression never pollutes the main session
        res = self._exec(work_dir, full_prompt, session_id=None)
        return res.text


# ---------------------------------------------------------------- mock (tests)

class MockAdapter:
    """Deterministic fake backend for tests; no external CLI needed."""
    name = "mock"

    def send(self, work_dir, session_id, prompt, on_chunk=None) -> TurnResult:
        import uuid
        sid = session_id or f"mock-{uuid.uuid4().hex[:8]}"
        text = f"[mock reply to {len(prompt)} chars] {prompt[:60]!r}"
        if on_chunk:
            on_chunk(text)
        return TurnResult(text=text, session_id=sid, usage_tokens=None)

    def compress(self, work_dir, full_prompt) -> str:
        # deterministic "summary": marker + tail of the slice
        return f"[mock-compressed {len(full_prompt)} chars] ...{full_prompt[-200:]}"


ADAPTERS = {
    "codex": CodexAdapter,
    "kimi": KimiAdapter,
    "mock": MockAdapter,
}


def get_adapter(name: str):
    if name not in ADAPTERS:
        raise BackendError(f"unknown backend {name!r}; available: {', '.join(ADAPTERS)}")
    return ADAPTERS[name]()
