"""leap.py - compress a full session into a fresh one.

Flow (defaults: trigger=180k, compressed budget=30k, cut=30k, K=3):
  1. render the old session transcript from the events table
  2. CUT: keep the last `cut_tokens` verbatim
  3. split the rest chronologically into K slices
  4. K one-shot compressor calls, each <= compressed_budget/K tokens
  5. write context.md = chunks + CUT
  6. seed a brand-new backend session with post_leap_prompt + context.md
  7. record the leap; caller switches to the new session id
"""

import os
from datetime import datetime, timezone

from . import router, store


def est_tokens(text: str, chars_per_token: int) -> int:
    return max(1, len(text) // max(1, chars_per_token))


def _split_chronological(text: str, k: int) -> list:
    """Split text into k near-equal parts on paragraph boundaries where possible."""
    if k <= 1:
        return [text]
    part = max(1, len(text) // k)
    slices, start = [], 0
    for i in range(k - 1):
        end = min(len(text), start + part)
        # prefer breaking at a blank line within +10% of the target
        window = text[end:end + part // 10]
        nl = window.find("\n\n")
        if nl != -1:
            end += nl + 2
        slices.append(text[start:end])
        start = end
    slices.append(text[start:])
    return [s for s in slices if s.strip()]


def leap(conn, manifest: dict, inst_dir: str, log=print) -> str:
    """Perform a leap for the instance's current session. Returns the NEW session id."""
    old_session = store.get_state(conn, "session_id")
    if not old_session:
        raise RuntimeError("no active session to leap from")

    cpt = manifest["chars_per_token"]
    k = manifest["k"]
    cut_chars = manifest["cut_tokens"] * cpt
    chunk_tokens = manifest["compressed_budget"] // k
    chunk_chars = chunk_tokens * cpt

    events = store.session_events(conn, old_session)
    transcript = store.render_transcript(events)
    log(f"[leap] session {old_session}: ~{est_tokens(transcript, cpt):,} est tokens, "
        f"{len(events)} events, K={k}")

    # 2. CUT = last cut_chars verbatim (whole transcript if shorter)
    cut = transcript[-cut_chars:]
    head = transcript[:-cut_chars] if len(transcript) > cut_chars else ""

    # 3+4. slice and compress
    slices = _split_chronological(head, k) if head.strip() else []
    adapter = router.get_adapter(manifest["compressor_backend"])
    chunks = []
    for i, sl in enumerate(slices, 1):
        prompt = manifest["compressor_prompt"].format(
            CHUNK_TOKENS=chunk_tokens, CHUNK_CHARS=chunk_chars,
        ) + "\n\n--- CONVERSATION SLICE %d/%d ---\n\n" % (i, len(slices)) + sl
        try:
            chunk = adapter.compress(inst_dir, prompt)
        except Exception as e:  # never block the session on a failed compressor
            log(f"[leap] compressor {i} failed ({e}); carrying truncated slice")
            chunk = f"[compression failed: {e}]\n" + sl[:chunk_chars]
        if len(chunk) > chunk_chars * 2:  # keep runaway compressors in budget-ish
            chunk = chunk[: chunk_chars * 2]
        chunks.append(chunk)
        log(f"[leap] chunk {i}/{len(slices)}: {len(sl):,} -> {len(chunk):,} chars")

    # 5. context.md
    leap_no = int(store.get_state(conn, "leap_count", "0")) + 1
    ts = datetime.now(timezone.utc).isoformat()
    context = (
        f"# Leap {leap_no} - {ts}\n\n"
        f"## Compressed memory\n\n" + "\n\n".join(chunks) +
        f"\n\n## Recent context (verbatim)\n\n" + cut + "\n"
    )
    context_path = os.path.join(inst_dir, "context.md")
    with open(context_path, "w") as f:
        f.write(context)
    archive = os.path.join(inst_dir, "logs", f"context-leap-{leap_no}.md")
    with open(archive, "w") as f:
        f.write(context)

    # 6. seed the new session
    seed = (manifest["post_leap_prompt"] + "\n\n" + context).strip()
    session_adapter = router.get_adapter(manifest["backend"])
    store.log_event(conn, old_session, "leap", {
        "leap": leap_no, "chunks": len(chunks),
        "context_chars": len(context),
        "est_tokens": est_tokens(context, cpt),
    })
    res = session_adapter.send(inst_dir, None, seed)
    new_session = res.session_id

    # 7. record + switch state
    store.log_leap(conn, old_session, new_session, context_path)
    store.set_state(conn, "session_id", new_session)
    store.set_state(conn, "session_tokens", res.usage_tokens or est_tokens(seed + res.text, cpt))
    store.set_state(conn, "leap_count", str(leap_no))
    store.log_event(conn, new_session, "prompt", {"text": seed, "seed": True})
    store.log_event(conn, new_session, "response", {"text": res.text})
    log(f"[leap] done: {old_session} -> {new_session} (context.md ~"
        f"{est_tokens(context, cpt):,} est tokens)")
    return new_session
