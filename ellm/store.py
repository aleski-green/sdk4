"""store.py - config/manifest parsing and sqlite persistence for ellm instances."""

import json
import os
import sqlite3
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

TOOL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BUILTIN_DEFAULTS = {
    "backend": "codex",
    "compressor_backend": "",
    # ELLM-managed conversation length that triggers a leap.
    "trigger_tokens": 180_000,
    "compressed_budget": 30_000,
    "cut_tokens": 30_000,
    "k": 3,
    "chars_per_token": 4,
    "idle_timeout": 0,
    "turn_timeout": 600,
    "compressor_prompt": "Compress this conversation slice to at most {CHUNK_TOKENS} tokens. Output only the summary.",
    "turn_prompt": "",
    "post_leap_prompt": "You are continuing an eternal session. Your memory follows.",
}

# xml tag -> config key (global config.xml)
_GLOBAL_MAP = {
    "backend": "backend",
    "compressor-backend": "compressor_backend",
    "trigger-tokens": "trigger_tokens",
    "compressed-budget": "compressed_budget",
    "cut-tokens": "cut_tokens",
    "k": "k",
    "chars-per-token": "chars_per_token",
    "idle-timeout": "idle_timeout",
    "turn-timeout": "turn_timeout",
    "compressor-prompt": "compressor_prompt",
    "default-turn-prompt": "turn_prompt",
    "default-post-leap-prompt": "post_leap_prompt",
}

_INT_KEYS = {"trigger_tokens", "compressed_budget", "cut_tokens", "k", "chars_per_token", "idle_timeout", "turn_timeout"}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(el, tag):
    child = el.find(tag)
    if child is not None and child.text is not None:
        return child.text.strip()
    return None


def load_global_config(path=None) -> dict:
    """Load tool-root config.xml over builtin defaults."""
    cfg = dict(BUILTIN_DEFAULTS)
    path = path or os.environ.get("ELLM_CONFIG") or os.path.join(TOOL_ROOT, "config.xml")
    if os.path.exists(path):
        root = ET.parse(path).getroot()
        for tag, key in _GLOBAL_MAP.items():
            val = _text(root, tag)
            if val is not None and val != "":
                cfg[key] = int(val) if key in _INT_KEYS else val
        inst = _text(root, "instances-dir")
        cfg["instances_dir"] = inst or "ellms/"
    else:
        cfg["instances_dir"] = "ellms/"
    if not os.path.isabs(cfg["instances_dir"]):
        cfg["instances_dir"] = os.path.join(TOOL_ROOT, cfg["instances_dir"])
    return cfg


def instance_dir(cfg: dict, name: str) -> str:
    return os.path.join(cfg["instances_dir"], name)


MANIFEST_TEMPLATE = """<ellm-instance>
  <name>{name}</name>
  <!-- optional overrides (defaults come from global config.xml): -->
  <!-- <backend>kimi</backend> -->
  <!-- <trigger-tokens>150000</trigger-tokens> -->
  <turn-prompt><![CDATA[{turn_prompt}]]></turn-prompt>
  <post-leap-prompt><![CDATA[{post_leap_prompt}]]></post-leap-prompt>
</ellm-instance>
"""

_MANIFEST_MAP = {
    "backend": "backend",
    "compressor-backend": "compressor_backend",
    "trigger-tokens": "trigger_tokens",
    "compressed-budget": "compressed_budget",
    "cut-tokens": "cut_tokens",
    "k": "k",
    "chars-per-token": "chars_per_token",
    "turn-prompt": "turn_prompt",
    "post-leap-prompt": "post_leap_prompt",
    "compressor-prompt": "compressor_prompt",
    "idle-timeout": "idle_timeout",
    "turn-timeout": "turn_timeout",
}


def ensure_instance(cfg: dict, name: str) -> str:
    """Create instance folder + manifest + db if missing. Returns instance dir."""
    d = instance_dir(cfg, name)
    os.makedirs(os.path.join(d, "logs"), exist_ok=True)
    manifest = os.path.join(d, "ellm-manifest.xml")
    if not os.path.exists(manifest):
        with open(manifest, "w") as f:
            f.write(MANIFEST_TEMPLATE.format(
                name=name,
                turn_prompt=cfg["turn_prompt"],
                post_leap_prompt=cfg["post_leap_prompt"],
            ))
    db = os.path.join(d, "ellm.db")
    if not os.path.exists(db):
        conn = connect(db)
        conn.close()
    return d


def load_manifest(cfg: dict, name: str) -> dict:
    """Effective instance config: manifest over global defaults."""
    eff = dict(cfg)
    path = os.path.join(instance_dir(cfg, name), "ellm-manifest.xml")
    if os.path.exists(path):
        root = ET.parse(path).getroot()
        for tag, key in _MANIFEST_MAP.items():
            val = _text(root, tag)
            if val is not None and val != "":
                eff[key] = int(val) if key in _INT_KEYS else val
    eff["name"] = name
    if not eff.get("compressor_backend"):
        eff["compressor_backend"] = eff["backend"]
    return eff


SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  ts         TEXT NOT NULL,
  type       TEXT NOT NULL,
  data       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS leaps (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  ts             TEXT NOT NULL,
  old_session_id TEXT NOT NULL,
  new_session_id TEXT NOT NULL,
  new_context    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS state (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id, id)")
    conn.commit()
    return conn


def log_event(conn, session_id: str, etype: str, data: dict):
    conn.execute(
        "INSERT INTO events (session_id, ts, type, data) VALUES (?,?,?,?)",
        (session_id, utcnow(), etype, json.dumps(data, ensure_ascii=False)),
    )
    conn.commit()


def log_leap(conn, old_session: str, new_session: str, context_path: str):
    conn.execute(
        "INSERT INTO leaps (ts, old_session_id, new_session_id, new_context) VALUES (?,?,?,?)",
        (utcnow(), old_session, new_session, context_path),
    )
    conn.commit()


def get_state(conn, key: str, default=None):
    row = conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_state(conn, key: str, value):
    conn.execute("INSERT OR REPLACE INTO state (key, value) VALUES (?,?)", (key, str(value)))
    conn.commit()


def session_events(conn, session_id: str):
    """All prompt/response events of a session, chronological."""
    rows = conn.execute(
        "SELECT ts, type, data FROM events WHERE session_id=? AND type IN ('prompt','response') ORDER BY id",
        (session_id,),
    ).fetchall()
    out = []
    for ts, etype, data in rows:
        d = json.loads(data)
        out.append({"ts": ts, "type": etype, "text": d.get("text", "")})
    return out


def render_transcript(events) -> str:
    parts = []
    for e in events:
        role = "User" if e["type"] == "prompt" else "Assistant"
        parts.append(f"[{e['ts']}] {role}:\n{e['text']}")
    return "\n\n---\n\n".join(parts)


def estimate_tokens(text: str, chars_per_token: int) -> int:
    """Text-only token estimate used for ELLM's retained chat context."""
    return max(1, len(text) // max(1, chars_per_token)) if text else 0


def session_chat_tokens(conn, session_id: str, chars_per_token: int) -> int:
    """Estimate the ELLM-managed context retained in one backend session.

    This deliberately counts only persisted prompt/response text. It excludes
    the backend's hidden system, tool, and reasoning context, which is exposed
    separately as the provider window metric.
    """
    if not session_id:
        return 0
    # Normal resumed turns send native messages, not the timestamped transcript
    # representation used only while building a leap seed. A seed itself is
    # stored as one prompt event, so its full serialized text is included here.
    text = "".join(event["text"] for event in session_events(conn, session_id))
    return estimate_tokens(text, chars_per_token)
