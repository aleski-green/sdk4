#!/usr/bin/env python3
"""ellm - eternal llm CLI client.

Usage:
  ellm.py <NAME> -p '<PROMPT>'   send a prompt (auto-creates instance + daemon)
  ellm.py list                   list all instances
  ellm.py <NAME> --status        instance status
  ellm.py <NAME> --stop          stop the instance daemon (session persists)
"""

import argparse
import json
import os
import socket
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ellm import store  # noqa: E402

TOOL_ROOT = os.path.dirname(os.path.abspath(__file__))


def socket_path(cfg, name):
    return os.path.join(store.instance_dir(cfg, name), "daemon.sock")


def is_running(cfg, name) -> bool:
    sp = socket_path(cfg, name)
    if not os.path.exists(sp):
        return False
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(2)
        s.connect(sp)
        s.close()
        return True
    except OSError:
        return False


def ensure_daemon(cfg, name):
    store.ensure_instance(cfg, name)
    if is_running(cfg, name):
        return
    # stale socket cleanup is handled by the daemon itself
    subprocess.Popen(
        [sys.executable, "-m", "ellm.daemon", name],
        cwd=TOOL_ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    sp = socket_path(cfg, name)
    for _ in range(150):
        if os.path.exists(sp) and is_running(cfg, name):
            return
        time.sleep(0.1)
    print(f"error: daemon for '{name}' did not start; check {store.instance_dir(cfg, name)}/logs/",
          file=sys.stderr)
    sys.exit(1)


def rpc(cfg, name, obj, stream=False):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(socket_path(cfg, name))
    s.sendall((json.dumps(obj) + "\n").encode())
    fp = s.makefile("r")
    result = None
    for line in fp:
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        mtype = msg.get("type")
        if mtype == "chunk" and stream:
            print(msg["text"], end="", flush=True)
        elif mtype == "leap":
            phase = msg.get("phase")
            if phase == "start":
                print("\n[ellm] context limit reached — leaping...", file=sys.stderr)
            elif phase == "done":
                print("[ellm] leaped to session %s" % msg.get("session_id"), file=sys.stderr)
            elif phase == "error":
                print("[ellm] leap failed: %s" % msg.get("message"), file=sys.stderr)
        elif mtype == "error":
            print(f"\nerror: {msg.get('message')}", file=sys.stderr)
            result = msg
            break
        elif mtype in ("done", "status"):
            result = msg
            break
    s.close()
    return result


def cmd_prompt(cfg, name, prompt):
    ensure_daemon(cfg, name)
    res = rpc(cfg, name, {"cmd": "prompt", "text": prompt}, stream=True)
    if res is None:
        # daemon may have been mid-shutdown; wait, re-ensure, retry once
        time.sleep(1.5)
        ensure_daemon(cfg, name)
        res = rpc(cfg, name, {"cmd": "prompt", "text": prompt}, stream=True)
    if res is None:
        print("error: daemon closed the connection without a reply", file=sys.stderr)
        sys.exit(1)
    if res and res.get("type") == "error":
        sys.exit(1)
    if res and res.get("ok"):
        print()  # newline after stream
        st, tr = res.get("session_tokens", 0), res.get("trigger", 0)
        print(f"[{name} | session {res.get('session_id')} | ~{st:,}/{tr:,} tokens]",
              file=sys.stderr)


def cmd_status(cfg, name):
    if not os.path.isdir(store.instance_dir(cfg, name)):
        print(f"instance '{name}' does not exist", file=sys.stderr)
        sys.exit(1)
    if not is_running(cfg, name):
        d = store.instance_dir(cfg, name)
        conn = store.connect(os.path.join(d, "ellm.db"))
        print(f"{name}: daemon stopped")
        print(f"  session_id:     {store.get_state(conn, 'session_id')}")
        print(f"  session_tokens: ~{int(store.get_state(conn, 'session_tokens', '0')):,}")
        print(f"  leaps:          {store.get_state(conn, 'leap_count', '0')}")
        conn.close()
        return
    res = rpc(cfg, name, {"cmd": "status"})
    if res:
        print(f"{name}: running (pid {res['pid']})")
        print(f"  backend:        {res['backend']}")
        print(f"  session_id:     {res['session_id']}")
        print(f"  session_tokens: ~{res['session_tokens']:,} / {res['trigger_tokens']:,}")
        print(f"  leaps:          {res['leap_count']}")


def cmd_stop(cfg, name):
    if not is_running(cfg, name):
        print(f"{name}: not running")
        return
    rpc(cfg, name, {"cmd": "stop"})
    for _ in range(100):
        if not is_running(cfg, name):
            break
        time.sleep(0.1)
    print(f"{name}: stopped (session persists; next -p resumes it)")


def cmd_list(cfg):
    root = cfg["instances_dir"]
    if not os.path.isdir(root):
        print("(no instances)")
        return
    names = sorted(d for d in os.listdir(root)
                   if os.path.isdir(os.path.join(root, d)))
    if not names:
        print("(no instances)")
        return
    for name in names:
        d = os.path.join(root, name)
        conn = store.connect(os.path.join(d, "ellm.db"))
        running = "running" if is_running(cfg, name) else "stopped"
        sid = store.get_state(conn, "session_id") or "-"
        toks = int(store.get_state(conn, "session_tokens", "0"))
        leaps = store.get_state(conn, "leap_count", "0")
        print(f"{name:<20} {running:<8} session={sid:<38} ~{toks:>8,} tokens  leaps={leaps}")
        conn.close()


RESERVED_NAMES = {"list"}


def main():
    ap = argparse.ArgumentParser(prog="ellm", description="eternal llm")
    ap.add_argument("name", nargs="?", help="instance name (or 'list')")
    ap.add_argument("-p", "--prompt", help="prompt to send")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--stop", action="store_true")
    args = ap.parse_args()

    cfg = store.load_global_config()
    wants_instance = args.prompt is not None or args.status or args.stop
    if args.name == "list" or not args.name:
        if wants_instance and args.name == "list":
            print("error: instance name 'list' is reserved; use a different name",
                  file=sys.stderr)
            sys.exit(2)
        cmd_list(cfg)
    elif args.prompt is not None:
        if args.name in RESERVED_NAMES:
            print("error: instance name %r is reserved" % args.name, file=sys.stderr)
            sys.exit(2)
        cmd_prompt(cfg, args.name, args.prompt)
    elif args.status:
        cmd_status(cfg, args.name)
    elif args.stop:
        cmd_stop(cfg, args.name)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
