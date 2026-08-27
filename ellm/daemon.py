"""daemon.py - per-instance long-running process.

Run:  python -m ellm.daemon <NAME>
Owns: backend session state, FIFO prompt queue, token accounting, leaps.
Protocol: JSON-lines over <instance>/daemon.sock.
  -> {"cmd":"prompt","text":"..."}   streams {"type":"chunk"}* then {"type":"done"|"error"}
  -> {"cmd":"status"}                one {"type":"status", ...}
  -> {"cmd":"stop"}                  shuts down (session persists; next client resumes it)
"""

import json
import os
import socket
import sys
import threading
import time

from . import leap as leap_mod
from . import router, store


class Daemon:
    def __init__(self, name: str):
        self.cfg = store.load_global_config()
        self.name = name
        self.inst_dir = store.ensure_instance(self.cfg, name)
        self.manifest = store.load_manifest(self.cfg, name)
        self.conn = store.connect(os.path.join(self.inst_dir, "ellm.db"))
        self.sock_path = os.path.join(self.inst_dir, "daemon.sock")
        self.pid_path = os.path.join(self.inst_dir, "daemon.pid")
        self.lock = threading.Lock()  # FIFO: one turn at a time
        self.db_lock = threading.Lock()  # serialize sqlite access across handler threads
        self.running = True
        self.last_active = time.time()
        self.log_fp = open(os.path.join(
            self.inst_dir, "logs",
            f"daemon-{time.strftime('%Y%m%d-%H%M%S')}.log"), "a", buffering=1)

    def log(self, msg: str):
        line = f"[{store.utcnow()}] {msg}"
        print(line, file=self.log_fp)

    # ---------------------------------------------------------- turn handling

    def handle_prompt(self, text: str, send):
        with self.lock, self.db_lock:
            manifest = store.load_manifest(self.cfg, self.name)  # re-read: edits apply live
            session_id = store.get_state(self.conn, "session_id")
            full_prompt = (manifest["turn_prompt"] + "\n\n" + text).strip() \
                if manifest["turn_prompt"] else text

            self.log(f"prompt ({len(text)} chars), session={session_id}")

            adapter = router.get_adapter(manifest["backend"])
            try:
                res = adapter.send(self.inst_dir, session_id, full_prompt,
                                   on_chunk=lambda c: send({"type": "chunk", "text": c}))
            except router.BackendError as e:
                store.log_event(self.conn, session_id or "none", "prompt", {"text": text})
                store.log_event(self.conn, session_id or "none", "error", {"error": str(e)})
                send({"type": "error", "message": str(e)})
                return

            # prompt logged after send so it carries the resolved session id
            store.log_event(self.conn, res.session_id, "prompt", {"text": text})
            store.log_event(self.conn, res.session_id, "response", {"text": res.text})
            if not session_id:
                store.set_state(self.conn, "session_id", res.session_id)

            # token accounting: prefer real usage, else chars/cpt estimate
            cpt = manifest["chars_per_token"]
            turn_tokens = res.usage_tokens or leap_mod.est_tokens(full_prompt + res.text, cpt)
            total = int(store.get_state(self.conn, "session_tokens", "0"))
            if res.usage_tokens:
                total = res.usage_tokens  # backend-reported cumulative: reconcile
            else:
                total += turn_tokens
            store.set_state(self.conn, "session_tokens", total)

            # leap between turns (before 'done' so the client sees it)
            final_session, final_tokens = res.session_id, total
            if total >= manifest["trigger_tokens"]:
                send({"type": "chunk", "text": "\n[ellm] context limit reached - leaping...\n"})
                try:
                    final_session = leap_mod.leap(self.conn, manifest, self.inst_dir,
                                                  log=self.log)
                    final_tokens = int(store.get_state(self.conn, "session_tokens", "0"))
                    send({"type": "chunk", "text": f"[ellm] leaped to session {final_session}\n"})
                except Exception as e:
                    self.log(f"leap failed: {e}")
                    store.log_event(self.conn, res.session_id, "error",
                                    {"error": f"leap failed: {e}"})
                    send({"type": "chunk", "text": f"[ellm] LEAP FAILED: {e}\n"})

            send({"type": "done", "ok": True, "session_id": final_session,
                  "session_tokens": final_tokens, "trigger": manifest["trigger_tokens"]})

    def handle_status(self, send):
        manifest = store.load_manifest(self.cfg, self.name)
        with self.db_lock:
            send({"type": "status",
                  "name": self.name,
                  "backend": manifest["backend"],
                  "session_id": store.get_state(self.conn, "session_id"),
                  "session_tokens": int(store.get_state(self.conn, "session_tokens", "0")),
                  "trigger_tokens": manifest["trigger_tokens"],
                  "leap_count": int(store.get_state(self.conn, "leap_count", "0")),
                  "pid": os.getpid()})

    # ---------------------------------------------------------- socket loop

    def serve_conn(self, conn: socket.socket):
        fp = conn.makefile("r")
        line = fp.readline()
        if not line:
            return
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            return
        wlock = threading.Lock()

        def send(obj):
            data = (json.dumps(obj, ensure_ascii=False) + "\n").encode()
            with wlock:
                conn.sendall(data)

        cmd = req.get("cmd")
        try:
            if cmd == "prompt":
                self.handle_prompt(req.get("text", ""), send)
            elif cmd == "status":
                self.handle_status(send)
            elif cmd == "stop":
                send({"type": "done", "ok": True, "message": "stopping"})
                self.running = False
        except Exception as e:
            self.log(f"handler error: {e}")
            try:
                send({"type": "error", "message": str(e)})
            except OSError:
                pass
        finally:
            self.last_active = time.time()
            try:
                conn.close()
            except OSError:
                pass

    def run(self):
        if os.path.exists(self.sock_path):
            os.unlink(self.sock_path)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(self.sock_path)
        srv.listen(8)
        srv.settimeout(1.0)
        with open(self.pid_path, "w") as f:
            f.write(str(os.getpid()))
        self.log(f"daemon up: name={self.name} backend={self.manifest['backend']} "
                 f"pid={os.getpid()}")
        idle = self.manifest.get("idle_timeout", 0)
        try:
            while self.running:
                try:
                    conn, _ = srv.accept()
                except socket.timeout:
                    if idle and time.time() - self.last_active > idle:
                        self.log("idle timeout, shutting down")
                        break
                    continue
                threading.Thread(target=self.serve_conn, args=(conn,), daemon=True).start()
        finally:
            srv.close()
            for p in (self.sock_path, self.pid_path):
                try:
                    os.unlink(p)
                except OSError:
                    pass
            self.log("daemon down")


def main():
    if len(sys.argv) != 2:
        print("usage: python -m ellm.daemon <NAME>", file=sys.stderr)
        sys.exit(2)
    Daemon(sys.argv[1]).run()


if __name__ == "__main__":
    main()
