# sdk4 · ellm — eternal LLM

`ellm` wraps LLM CLI sessions (codex, kimi) so an LLM behaves like a **long-running
process with self-managing memory**. You talk to a named instance; when its context
fills up (~180k tokens), it *leaps*: the old session is compressed by K helper LLMs
and a fresh session continues with the compressed memory. The conversation never dies.

Not an agent — session infrastructure.

```
pip install nothing   # Python 3.10+ stdlib only
```

**Requirements:** Python 3.10+, and at least one backend CLI installed and authenticated:
`codex` (OpenAI Codex CLI) and/or `kimi` (Kimi Code CLI). A `mock` backend exists for testing.

---

## Quickstart

```bash
cd sdk4

# first use auto-creates the instance and its daemon
python3 ellm.py smith -p 'Remember: our codename is FALCON-9.'

# keeps the same backend session — full memory
python3 ellm.py smith -p 'What is our codename?'

python3 ellm.py smith --status
python3 ellm.py list
python3 ellm.py smith --stop     # stop daemon; session survives, next -p resumes it
```

Output: the model's reply on **stdout**, one status line on **stderr**
(`[smith | session <id> | ~tokens/trigger]`) — so stdout stays pipeable.

---

## Commands

| Command | What it does |
|---|---|
| `ellm.py <NAME> -p '<PROMPT>'` | Send a prompt. Auto-creates instance + daemon on first use. Streams the reply. |
| `ellm.py list` | All instances: running/stopped, session id, est. tokens, leap count |
| `ellm.py <NAME> --status` | Detail for one instance (works whether daemon is up or not) |
| `ellm.py <NAME> --stop` | Graceful daemon stop (finishes the in-flight turn). Session persists |

Notes:
- Prompts to the same instance are **queued FIFO** — safe to call from several terminals/agents.
- Instance name `list` is reserved.
- Errors exit non-zero; the stderr status line can be ignored or parsed.

## How a leap works (automatic)

1. Session token estimate ≥ `trigger-tokens` (default 180 000) → leap fires *between* turns.
2. Everything except the last `cut-tokens` (30k, kept verbatim = **CUT**) is split
   chronologically into **K** slices (K=3) on turn boundaries.
3. K one-shot compressor calls each condense their slice to `compressed-budget / K`
   (30k/3 = 10k tokens) using `compressor-prompt`.
4. `context.md` = compressed chunks + CUT (~60k total).
5. A new backend session is seeded with `post-leap-prompt` + `context.md`; the old
   session id is retired (its history stays in the DB forever).

You'll see leap progress on stderr. If a compressor fails twice, the slice is carried
truncated — a leap never blocks the session.

## Configuration

**Global defaults — `config.xml` (repo root):** backend, thresholds, K, chars-per-token,
compressor prompt, default prompts, `turn-timeout` (600s).

**Per instance — `ellms/<NAME>/ellm-manifest.xml`** (created on first use, hand-editable;
overrides global):

```xml
<ellm-instance>
  <name>smith</name>
  <backend>kimi</backend>                <!-- or codex; overrides global -->
  <!-- <trigger-tokens>150000</trigger-tokens> -->
  <turn-prompt><![CDATA[You are Smith... (injected with EVERY turn)]]></turn-prompt>
  <post-leap-prompt><![CDATA[You just leaped... (injected after EACH leap)]]></post-leap-prompt>
</ellm-instance>
```

Resolution order: manifest → `config.xml` → built-in defaults. Edits apply live
(re-read every turn). Useful overrides: `backend`, `compressor-backend` (cheaper model
for compression), `trigger-tokens`, `compressed-budget`, `cut-tokens`, `k`,
`chars-per-token`, `turn-timeout`.

## Instance folder anatomy — `ellms/<NAME>/`

| File | Contents |
|---|---|
| `ellm-manifest.xml` | Instance config/personality (edit me) |
| `ellm.db` | SQLite: `events` (every prompt/response/leap/error per session), `leaps` (old→new session, context path), `state` |
| `context.md` | Current session seed (chunks + CUT from the last leap) |
| `logs/` | Daemon logs + archived `context-leap-N.md` snapshots |
| `daemon.sock` / `daemon.pid` / `daemon.lock` | Runtime files; safe to ignore |

Inspect history directly:

```bash
sqlite3 ellms/smith/ellm.db 'SELECT ts, type, substr(data,1,80) FROM events;'
sqlite3 ellms/smith/ellm.db 'SELECT * FROM leaps;'
```

## Testing / dry runs

Use the `mock` backend (no API calls, deterministic) — set `<backend>mock</backend>`
in the manifest. Handy with tiny thresholds to watch leaps happen fast:

```xml
<backend>mock</backend>
<trigger-tokens>100</trigger-tokens>
<compressed-budget>60</compressed-budget>
<cut-tokens>50</cut-tokens>
```

Run the test suite: `python3 -m unittest discover -s tests`

## Troubleshooting

| Symptom | Fix |
|---|---|
| `error: daemon for 'X' did not start` | Check `ellms/X/logs/`; usually backend CLI missing/auth |
| codex: `exited 1 ... not logged in` | Run `codex login` once |
| Backend CLI not on PATH | `ELLM_CODEX_BIN=/path/to/codex` / `ELLM_KIMI_BIN=/path/to/kimi` |
| Stale state after kill -9 | Just run `-p` again — session resumes; daemon cleans its own socket |
| Leap too early/late | Tune `trigger-tokens` in the manifest (codex reports real window tokens incl. system overhead — ~25–35k per turn baseline) |

Design doc: [docs/ELLM-DESIGN.md](docs/ELLM-DESIGN.md)
