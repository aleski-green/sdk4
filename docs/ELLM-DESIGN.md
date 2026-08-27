# ELLM — Eternal LLM · Design Document (v0.1.1)

Status: **implemented** (M1–M3 plus robustness fixes). Tests: `python -m unittest discover -s tests`.

## 1. What ellm is

`ellm` is a long-running wrapper over LLM CLI sessions (codex, kimi, later deepseek/zai/...).
It lets agents, humans, and tools talk to an LLM **as if it were a persistent process** with
automatic context and memory management:

```
>> ellm '<NAME>' -p '<PROMPT>'
```

It is **not an agent**. It is session infrastructure: one named instance = one continuous
"eternal" conversation that never dies — when context fills up, the session *leaps* into a
fresh one carrying a compressed memory of the old.

## 2. Core concepts

| Term | Meaning |
|---|---|
| **instance** | A named ellm (`<NAME>`). Lives in its own folder with its own DB, manifest, logs. |
| **session** | One continuous backend-LLM conversation. Bounded by the context trigger. |
| **leap** | The transition: compress old session → seed new session. Old session ends, new one begins. |
| **CUT** | The last `cut_tokens` (default 30k) of the old session, carried verbatim into the new one. |
| **chunk** | Compressed summary of one chronological slice of the old session. K chunks per leap. |
| **context.md** | The seed document of a new session: K chunks + CUT. Written to the instance folder. |

## 3. Architecture

```
            ┌────────────────────────────────────────────┐
            │              ellm instance folder           │
            │  ellms/<NAME>/                              │
            │   ├─ ellm-manifest.xml   (instance config)  │
            │   ├─ ellm.db             (sqlite)           │
            │   ├─ context.md          (current seed)     │
            │   ├─ daemon.sock / daemon.pid               │
            │   └─ logs/               (raw stream logs)  │
            └────────────────────────────────────────────┘

  ellm <NAME> -p '...'         (thin client, one-shot)
        │  unix socket
        ▼
  ┌───────────┐   turn    ┌──────────┐   non-interactive CLI call   ┌─────────────┐
  │  daemon   │ ────────► │ router   │ ───────────────────────────► │ codex / kimi │
  │ (per NAME)│ ◄──────── │  .py     │ ◄─────────────────────────── │ CLI session  │
  └───────────┘  stream   └──────────┘      (session-resumed)       └─────────────┘
        │                                            ▲
        │ trigger ≥ 180k                             │ compressors (K one-shot calls)
        ▼                                            │
  ┌───────────┐   chunks + CUT   ┌──────────┐        │
  │  leap.py  │ ───────────────► │ new       │ ───────┘
  │           │                  │ session   │
  └───────────┘                  └──────────┘
```

**Components**

- `ellm.py` — CLI entry point + thin client. Parses args, talks to the instance daemon over
  a Unix socket, streams the reply to stdout, exits.
- `daemon.py` — the actual long-running process (one per active instance). Owns the session
  state, the prompt queue, token accounting, and fires leaps. Auto-spawned on first use.
- `router.py` — backend abstraction. One adapter per vendor CLI (`codex`, `kimi`), exposing a
  uniform `send(session, prompt) -> stream` / `compress(prompt, text) -> str` interface.
- `leap.py` — leap logic: slicing, compressor fan-out, chunk assembly, new-session seeding.
- `store.py` — sqlite access (events, leaps), manifest/config parsing.
- `config.xml` — global defaults at tool root.

## 4. Session model — key design decision

**Requirement:** the session stays *alive* until ~180k tokens; we never re-feed history per turn.

Two ways to achieve "alive":

| | A. PTY: keep the interactive TUI process running | B. Native session-resume per turn (**recommended**) |
|---|---|---|
| Mechanism | Spawn `codex` / `kimi` TUI under a pseudo-terminal, pipe prompts in, scrape output | Each turn = one non-interactive call bound to the vendor's own session: `codex exec resume <SESSION_ID> "<prompt>"` / `kimi --session <SESSION_ID> -p "<prompt>"` |
| Context ownership | In the live process | In the vendor's session store (same native context, nothing re-fed by us) |
| Output parsing | Fragile (ANSI, redraws, spinners) | Clean: `--json` (codex) / `--output-format stream-json` (kimi) |
| Crash recovery | Process death = session loss | Session is on disk; daemon restart is harmless |
| Token accounting | Screen scraping | Structured usage events from JSONL stream |

**Decision (default, overridable): option B.** The *daemon* is the long-running process; the
backend session is continuous via native resume. Semantically identical to "one hot session",
vastly more robust. Both CLIs fully support this today:

- codex: `codex exec "<prompt>"` then `codex exec resume <SESSION_ID> "<prompt>"`
  (always with stdin `</dev/null` — codex hangs otherwise; `--json` for event stream)
- kimi: `kimi -p "<prompt>"` then `kimi --session <SESSION_ID> -p "<prompt>"`
  (`--output-format stream-json`; note `-p` implies auto-permission, can't combine with `--yolo`)

The adapter also works with `cwd` pinned to the instance folder, which both CLIs use for
session scoping. Option A can be added later behind the same router interface if ever needed.

## 5. Turn flow

1. `ellm <NAME> -p '<PROMPT>'` → client connects to `daemon.sock` (spawning the daemon if absent).
2. Daemon wraps the prompt with the instance's **`<turn-prompt>`** (injected with every turn).
3. Router calls the backend with the current `session_id`, streams output back to the client
   and into `logs/` + the `events` table.
4. Two token metrics are reported:
   - Codex `turn.completed.usage` is per-turn. Window ≈ `input_tokens + output_tokens +
     reasoning_output_tokens`. `cached_input_tokens` is a subset of input and is **not** added.
   - This **provider window** includes backend-controlled system, tool, and agent context. When
     reported, `session_tokens` is replaced with that value; it is never summed across turns and
     is diagnostic only.
   - **Chat tokens** are ELLM's text-only estimate of the persisted prompts and responses in the
     active session. After a leap, only the new seed prompt and its acknowledgement are counted.
     This is the equivalent of the visible chat length, but is approximate (`chars-per-token`).
   - When it does not, we accumulate `chars / chars_per_token` over this turn's prompt+reply.
   - Raw backend usage is also saved as `usage` events for diagnosis.
   - Codex prompts (including leap seeds) are sent on stdin via `codex exec -` so they cannot
     hit `ARG_MAX`.
5. If ELLM's visible-chat estimate reaches `trigger_tokens` (default 180k) → **leap** (below),
   then the *next*
   turn goes to the new session. Current turn always completes first — leap happens between turns.
   Leap progress is a `{type:"leap", phase:...}` protocol event printed on stderr, never mixed
   into the model stdout stream.

## 6. Leap algorithm (leap.py)

Triggered when the ELLM-managed chat context reaches `trigger-tokens` (default 180k):

1. **Extract** the full ordered transcript of the current session (from our `events` log —
   every prompt and response is already stored, so no scraping needed).
2. **CUT**: split off the *last* `cut_tokens` (default 30k) verbatim, on **turn boundaries**
   (never mid-message).
3. **Slice** the remaining ~150k chronologically into `K` equal parts (default K=3), also on
   turn boundaries.
4. **Compress**: spawn K independent one-shot compressor calls (same backend by default,
   `compressor_backend` overridable — e.g. a cheaper model). Each gets:
   `compressor_prompt` (from config) + its slice, instructed to produce ≤ `chunk_tokens`
   (default `compressed_budget / K` = 30k/3 = **10k tokens**). Retry once; on second failure
   carry the slice truncated to the chunk budget. Compressor cwd is isolated so it cannot
   steal the main session (`--ephemeral` on Codex; a temp dir on Kimi).
5. **Assemble** `context.md`:
   ```
   # Leap <N> — <timestamp>
   ## Compressed memory
   <chunk 1> … <chunk K>            (total ≤ 30k)
   ## Recent context (verbatim)
   <CUT>                            (≤ 30k)
   ```
6. **Spawn new session**: first call of the new session is seeded with `context.md` content
   prepended with the instance's **`<post-leap-prompt>`** (injected after each leap).
7. **Record**: row in `leaps` table (old session, new session, path to context.md); old
   session id is retired but its events stay in the DB forever.

Post-leap new-session size ≈ 60k tokens, leaving ~120k of headroom before the next leap.

## 7. Configuration — two XML files (resolved)

**Rationale:** thresholds, backend, and defaults are *tool-level policy*; prompts and
identity are *instance personality*. So:

### 7.1 Global `config.xml` (tool root) — defaults only

```xml
<ellm>
  <backend>codex</backend>              <!-- codex | kimi | ... -->
  <compressor-backend></compressor-backend>  <!-- empty = same as backend -->
  <trigger-tokens>180000</trigger-tokens>
  <compressed-budget>30000</compressed-budget>
  <cut-tokens>30000</cut-tokens>
  <k>3</k>
  <chars-per-token>4</chars-per-token>
  <instances-dir>ellms/</instances-dir>
  <idle-timeout>0</idle-timeout>     <!-- seconds; 0 = never -->
  <turn-timeout>600</turn-timeout>   <!-- per-turn CLI timeout; 0 = never -->
  <compressor-prompt><![CDATA[
    You are a memory compressor. Compress the following conversation
    slice into a dense factual summary of at most {CHUNK_TOKENS} tokens.
    Preserve: decisions, facts, code, commitments, open questions.
    Drop: pleasantries, dead ends, redundant reasoning.
  ]]></compressor-prompt>
  <default-turn-prompt><![CDATA[]]></default-turn-prompt>
  <default-post-leap-prompt><![CDATA[
    You are continuing an eternal session. Below is your compressed memory
    and recent context. Acknowledge briefly and continue seamlessly.
  ]]></default-post-leap-prompt>
</ellm>
```

### 7.2 Instance `ellm-manifest.xml` (inside `ellms/<NAME>/`) — personality + overrides

Created at instance creation, copied from global defaults, then freely editable:

```xml
<ellm-instance>
  <name>smith</name>
  <backend>kimi</backend>               <!-- override; optional -->
  <!-- optional per-instance threshold overrides: -->
  <!-- <trigger-tokens>150000</trigger-tokens> -->
  <turn-prompt><![CDATA[
    You are Smith, a long-running coding assistant...
    (injected with EVERY turn)
  ]]></turn-prompt>
  <post-leap-prompt><![CDATA[
    You just leaped. Your memory follows. Continue as Smith.
    (injected into context after EACH leap)
  ]]></post-leap-prompt>
</ellm-instance>
```

Resolution order: instance manifest → global config → built-in defaults.

## 8. Storage (sqlite, per instance: `ellms/<NAME>/ellm.db`)

```sql
CREATE TABLE events (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,          -- backend session id
  ts         TEXT NOT NULL,          -- ISO-8601
  type       TEXT NOT NULL,          -- prompt | response | leap | error | system
  data       TEXT NOT NULL           -- JSON payload
);

CREATE TABLE leaps (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  ts             TEXT NOT NULL,
  old_session_id TEXT NOT NULL,
  new_session_id TEXT NOT NULL,
  new_context    TEXT NOT NULL       -- path to context.md (not inline text)
);
```

Plus a tiny `state` table (current session id, cumulative token count) — or kept in the
manifest; default: `state` table, manifest stays human-authored only.

## 9. CLI surface (v1)

```
ellm <NAME> -p '<PROMPT>'     send prompt (auto-creates instance + daemon on first use)
ellm list                     all instances + status (running, session #, tokens, leaps)
ellm <NAME> --status          detail: session id, est. tokens, headroom, last leap
ellm <NAME> --stop            stop daemon (session persists; next -p resumes it)
```

- Concurrent prompts to the same instance are **queued** FIFO by the daemon (ticket lock, not
  `threading.Lock`).
- `-p` streams the response to stdout as it arrives; leap notices go to stderr; exit code 0 on success.
- `list` is a reserved instance name.
- REPL mode, `--leap-now`, `--history`: **post-v1**.

## 10. Repo layout

```
sdk4/
  .gitignore
  config.xml                 # global defaults
  ellm.py                    # CLI entry (client)
  ellm/
    __init__.py
    daemon.py                # long-running per-instance process
    router.py                # backend adapters (codex, kimi)
    leap.py                  # leap / compression logic
    store.py                 # sqlite + xml
    __main__.py              # python -m ellm
  tests/                     # stdlib unittest
  docs/ELLM-DESIGN.md        # this doc
  ellms/                     # instances (gitignored)
```

Stack: Python 3.10+, **stdlib only** (subprocess, sqlite3, socket, xml.etree, argparse).
Installable `ellm` console script via pyproject.toml — v1.1, v1 runs as `python ellm.py`.

## 11. Edge cases & decisions taken

- **Leap mid-queue**: queued prompts wait; leap runs; they continue on the new session.
- **Daemon crash / double-start**: exclusive `fcntl` lock on `daemon.lock`; stale sockets are
  unlinked only by the process that holds the lock. Stop waits for in-flight turns before
  unlinking the socket.
- **Token estimate drift**: Codex window size is `input+output+reasoning` (never cached). The
  180k trigger is deliberately conservative vs. real model limits.
- **Compressor failure**: retry once; on second failure carry the slice *truncated* to its
  chunk budget and log an `error` event — leap never blocks the session. Prompts may contain
  extra `{braces}`; only `{CHUNK_TOKENS}` / `{CHUNK_CHARS}` are substituted.
- **CLI hang / deadlock**: stderr is drained on a side thread; each turn has a timeout
  (default 600s). `turn.failed` and JSONL `error` events fail the turn.
- **First turn ever**: session starts fresh; if manifest has identity prompt, it's the
  turn-prompt doing that job — no separate system prompt needed.

## 12. Milestones

1. **M1** — router (codex adapter) + store + daemon + `-p` turn flow, no leap. Manual test: multi-turn memory.
2. **M2** — leap.py end-to-end with fake 180k trigger (small thresholds) + `leaps` table + context.md.
3. **M3** — kimi adapter, manifest overrides, `list`/`--status`/`--stop`.
4. **M4** — polish: pyproject console script, real-usage reconciliation, docs. *(reconciliation
   and tests landed in 0.1.1; console script still open)*

## 13. Defaults I chose where you said "idk / I guess" (flag if you disagree)

| # | Decision |
|---|---|
| 4a | Session continuity via **native resume**, not a live PTY process (§4) |
| 5a | Two XMLs: global defaults + per-instance manifest holding both prompts (§7) |
| 5b | `state` table for mutable runtime state; manifest stays hand-editable only |
| 6b | leaps.new_context stores a **path** to context.md, not inline text |
| 7a | Auto-create instance on first `-p`; queue concurrent prompts |
| 7b | No REPL in v1 |
