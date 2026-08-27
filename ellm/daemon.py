"""daemon.py - per-instance long-running process.

Run:  python -m ellm.daemon <NAME>
Owns: backend session state, FIFO prompt queue, token accounting, leaps.
Protocol: JSON-lines over <instance>/daemon.sock.
  -> {"cmd":"prompt","text":"..."}   streams {"type":"chunk"}* then
                                     optional {"type":"leap",...} then {"type":"done"|"error"}
  -> {"cmd":"status"}                one {"type":"status", ...}
  -> {"cmd":"stop"}                  shuts down after in-flight turns (session persists)
"""

import collections
import fcntl
import json
import os
import socket
import sys
import threading
import time

from . import leap as leap_mod
from . import router, store


class FifoLock:
    """Fair mutex: waiters run in the order they called acquire()."""

    def __init__(self):
        self._cv = threading.Condition()
        self._waiters = collections.deque()

    def acquire(self):
        ticket = object()
        with self._cv:
            self._waiters.append(ticket)
            while self._waiters[0] is not ticket:
                self._cv.wait()

    def release(self):
        with self._cv:
            if not self._waiters:
                return
            self._waiters.popleft()
            self._cv.notify_all()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()


class Daemon:
    def __init__(self, name: str):
        self.cfg = store.load_global_config()
        self.name = name
        self.inst_dir = store.ensure_instance(self.cfg, name)
        self.manifest = store.load_manifest(self.cfg, name)
        self.conn = store.connect(os.path.join(self.inst_dir, "ellm.db"))
        self.sock_path = os.path.join(self.inst_dir, "daemon.sock")
        self.pid_path = os.path.join(self.inst_dir, "daemon.pid")
        self.lock_path = os.path.join(self.inst_dir, "daemon.lock")
        self.turn_lock = FifoLock()
        self.db_lock = threading.Lock()
        self.running = True
        self.last_active = time.time()
        self._active_handlers = 0
        self._handlers_cv = threading.Condition()
        self._lock_fp = None
        self.log_fp = open(os.path.join(
            self.inst_dir, "logs",
            f"daemon-{time.strftime('%Y%m%d-%H%M%S')}.log"), "a", buffering=1)

    def log(self, msg: str):
        line = f"[{store.utcnow()}] {msg}"
        print(line, file=self.log_fp)

    def _timeout(self, manifest):
        timeout = int(manifest.get("turn_timeout") or 0)
        return timeout if timeout > 0 else None

    # ---------------------------------------------------------- turn handling

    def handle_prompt(self, text: str, send):
        with self.turn_lock, self.db_lock:
            # A prompt may have been accepted just before a stop request and
            # then waited in the FIFO queue. Do not start it during shutdown:
            # stop waits only for the turn that was already in flight.
            if not self.running:
                send({"type": "error", "message": "daemon is stopping"})
                return

            manifest = store.load_manifest(self.cfg, self.name)
            session_id = store.get_state(self.conn, "session_id")
            full_prompt = (manifest["turn_prompt"] + "\n\n" + text).strip() \
                if manifest["turn_prompt"] else text

            self.log(f"prompt ({len(text)} chars), session={session_id}")

            adapter = router.get_adapter(manifest["backend"])
            timeout = self._timeout(manifest)
            try:
                res = adapter.send(self.inst_dir, session_id, full_prompt,
                                   on_chunk=lambda c: send({"type": "chunk", "text": c}),
                                   timeout=timeout)
            except router.BackendError as e:
                store.log_event(self.conn, session_id or "none", "prompt", {"text": text})
                store.log_event(self.conn, session_id or "none", "error", {"error": str(e)})
                send({"type": "error", "message": str(e)})
                return

            store.log_event(self.conn, res.session_id, "prompt", {"text": text})
            store.log_event(self.conn, res.session_id, "response", {"text": res.text})
            if not session_id:
                store.set_state(self.conn, "session_id", res.session_id)

            cpt = manifest["chars_per_token"]
            estimated = leap_mod.est_tokens(full_prompt + res.text, cpt)
            previous = int(store.get_state(self.conn, "session_tokens", "0"))
            total = router.reconcile_session_tokens(
                previous, res.usage_tokens, res.usage_is_window, estimated)
            store.set_state(self.conn, "session_tokens", total)

            final_session, final_tokens = res.session_id, total
            if total >= manifest["trigger_tokens"]:
                send({"type": "leap", "phase": "start",
                      "session_id": res.session_id, "session_tokens": total})
                try:
                    final_session = leap_mod.leap(
                        self.conn, manifest, self.inst_dir, log=self.log,
                        timeout=timeout)
                    final_tokens = int(store.get_state(self.conn, "session_tokens", "0"))
                    send({"type": "leap", "phase": "done",
                          "session_id": final_session, "session_tokens": final_tokens})
                except Exception as e:
                    self.log(f"leap failed: {e}")
                    store.log_event(self.conn, res.session_id, "error",
                                    {"error": f"leap failed: {e}"})
                    send({"type": "leap", "phase": "error", "message": str(e)})

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
        with self._handlers_cv:
            self._active_handlers += 1
        fp = conn.makefile("r")
        try:
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
                    if not self.running:
                        send({"type": "error", "message": "daemon is stopping"})
                        return
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
                fp.close()
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass
            with self._handlers_cv:
                self._active_handlers -= 1
                self._handlers_cv.notify_all()

    def _acquire_singleton_lock(self):
        self._lock_fp = open(self.lock_path, "a+")
        try:
            fcntl.flock(self._lock_fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f"error: daemon for '{self.name}' is already running", file=sys.stderr)
            sys.exit(0)
        self._lock_fp.seek(0)
        self._lock_fp.truncate()
        self._lock_fp.write(str(os.getpid()))
        self._lock_fp.flush()

    def run(self):
        self._acquire_singleton_lock()
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
            with self._handlers_cv:
                while self._active_handlers > 0:
                    self._handlers_cv.wait(timeout=1)
            for p in (self.sock_path, self.pid_path):
                try:
                    os.unlink(p)
                except OSError:
                    pass
            self.log("daemon down")
            try:
                self.log_fp.close()
            except OSError:
                pass
            if self._lock_fp is not None:
                try:
                    fcntl.flock(self._lock_fp.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
                try:
                    self._lock_fp.close()
                except OSError:
                    pass
                self._lock_fp = None


def main():
    if len(sys.argv) != 2:
        print("usage: python -m ellm.daemon <NAME>", file=sys.stderr)
        sys.exit(2)
    Daemon(sys.argv[1]).run()


if __name__ == "__main__":
    main()
