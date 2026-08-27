"""leap.py - compress a full session into a fresh one.

Flow (defaults: trigger=180k, compressed budget=30k, cut=30k, K=3):
  1. render the old session transcript from the events table
  2. CUT: keep the last `cut_tokens` verbatim, split on turn boundaries
  3. split the rest chronologically into K slices (also on turn boundaries)
  4. K one-shot compressor calls (retry once), each <= compressed_budget/K tokens
  5. write context.md = chunks + CUT
  6. seed a brand-new backend session with post_leap_prompt + context.md
  7. record the leap; caller switches to the new session id
"""

import os
from datetime import datetime, timezone

from . import router, store


def est_tokens(text: str, chars_per_token: int) -> int:
    return max(1, len(text) // max(1, chars_per_token))


def fill_compressor_prompt(template: str, chunk_tokens: int, chunk_chars: int) -> str:
    """Substitute known placeholders without str.format (prompts may contain braces)."""
    return (template
            .replace("{CHUNK_TOKENS}", str(chunk_tokens))
            .replace("{CHUNK_CHARS}", str(chunk_chars)))


def _event_size(event) -> int:
    return len(store.render_transcript([event]))


def cut_and_head(events, cut_tokens: int, chars_per_token: int):
    """Split events so CUT is the trailing turns covering ~cut_tokens. Never bisects a turn."""
    if not events:
        return [], []
    cut_chars = cut_tokens * chars_per_token
    acc = 0
    cut_start = len(events)
    sep = len("\n\n---\n\n")
    for i in range(len(events) - 1, -1, -1):
        extra = sep if i < len(events) - 1 else 0
        acc += _event_size(events[i]) + extra
        cut_start = i
        if acc >= cut_chars:
            break
    return events[:cut_start], events[cut_start:]


def split_events(events, k: int):
    """Split events into k chronological slices by character weight, on turn boundaries."""
    if not events:
        return []
    if k <= 1:
        return [store.render_transcript(events)]
    weights = [_event_size(e) for e in events]
    total = sum(weights) or 1
    target = total / float(k)
    slices, cur, acc = [], [], 0
    for event, weight in zip(events, weights):
        if len(slices) < k - 1 and acc >= target and cur:
            slices.append(store.render_transcript(cur))
            cur, acc = [], 0
        cur.append(event)
        acc += weight
    if cur:
        slices.append(store.render_transcript(cur))
    return [s for s in slices if s.strip()]


def _compress_slice(adapter, inst_dir, prompt, fallback, chunk_chars, index, total, log, timeout):
    last_err = None
    for attempt in range(2):
        try:
            chunk = adapter.compress(inst_dir, prompt, timeout=timeout)
            return chunk
        except Exception as e:
            last_err = e
            log("[leap] compressor %s/%s attempt %s failed (%s)" % (
                index, total, attempt + 1, e))
    log("[leap] compressor %s/%s giving up; carrying truncated slice" % (index, total))
    return "[compression failed: %s]\n%s" % (last_err, fallback[:chunk_chars])


def leap(conn, manifest: dict, inst_dir: str, log=print, timeout=None) -> str:
    """Perform a leap for the instance's current session. Returns the NEW session id."""
    old_session = store.get_state(conn, "session_id")
    if not old_session:
        raise RuntimeError("no active session to leap from")

    cpt = manifest["chars_per_token"]
    k = manifest["k"]
    chunk_tokens = max(1, manifest["compressed_budget"] // max(1, k))
    chunk_chars = chunk_tokens * cpt

    events = store.session_events(conn, old_session)
    transcript = store.render_transcript(events)
    log("[leap] session %s: ~%s est tokens, %s events, K=%s" % (
        old_session, f"{est_tokens(transcript, cpt):,}", len(events), k))

    head_events, cut_events = cut_and_head(events, manifest["cut_tokens"], cpt)
    cut = store.render_transcript(cut_events)
    slices = split_events(head_events, k)
    adapter = router.get_adapter(manifest["compressor_backend"])
    chunks = []
    for i, sl in enumerate(slices, 1):
        prompt = fill_compressor_prompt(
            manifest["compressor_prompt"], chunk_tokens, chunk_chars,
        ) + "\n\n--- CONVERSATION SLICE %d/%d ---\n\n" % (i, len(slices)) + sl
        chunk = _compress_slice(
            adapter, inst_dir, prompt, sl, chunk_chars, i, len(slices), log, timeout)
        if len(chunk) > chunk_chars * 2:
            chunk = chunk[: chunk_chars * 2]
        chunks.append(chunk)
        log("[leap] chunk %s/%s: %s -> %s chars" % (
            i, len(slices), f"{len(sl):,}", f"{len(chunk):,}"))

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
    os.makedirs(os.path.dirname(archive), exist_ok=True)
    with open(archive, "w") as f:
        f.write(context)

    seed = (manifest["post_leap_prompt"] + "\n\n" + context).strip()
    session_adapter = router.get_adapter(manifest["backend"])
    store.log_event(conn, old_session, "leap", {
        "leap": leap_no, "chunks": len(chunks),
        "context_chars": len(context),
        "est_tokens": est_tokens(context, cpt),
    })
    res = session_adapter.send(inst_dir, None, seed, timeout=timeout)
    new_session = res.session_id

    store.log_leap(conn, old_session, new_session, context_path)
    store.set_state(conn, "session_id", new_session)
    store.set_state(conn, "leap_count", str(leap_no))
    store.log_event(conn, new_session, "prompt", {"text": seed, "seed": True})
    store.log_event(conn, new_session, "response", {"text": res.text})
    log("[leap] done: %s -> %s (context.md ~%s est tokens)" % (
        old_session, new_session, f"{est_tokens(context, cpt):,}"))
    return new_session
