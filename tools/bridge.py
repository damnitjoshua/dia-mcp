#!/usr/bin/env python3
"""Dia Live Bridge — local half.

One process, two roles:
  * serves the extension: GET /cmd (long-poll for work), POST /result, POST /report
  * serves callers/agents: POST /request (send a command, block for its result)

Run it in the background, then drive it:
    python bridge.py serve
    python bridge.py cmd list_tabs
    python bridge.py cmd eval '{"expression": "document.title"}'
    python bridge.py shot /tmp/shot.png

Commands understood by the extension: status, list_tabs, attach, detach, eval, cdp,
navigate, snapshot, click, click_text, fill, network, body, console, shot.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import threading
import time
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST, PORT = "127.0.0.1", 9777
BASE = f"http://{HOST}:{PORT}"
QUEUE_WAIT_S = 25
CALL_WAIT_S = 90

cond = threading.Condition()
queue: list[dict] = []
results: dict[str, dict] = {}
events: list[dict] = []


def enqueue(cmd: dict) -> str:
    cid = cmd.get("id") or uuid.uuid4().hex[:12]
    cmd["id"] = cid
    with cond:
        queue.append(cmd)
        cond.notify_all()
    return cid


def wait_result(cid: str, timeout: float) -> dict:
    deadline = time.time() + timeout
    with cond:
        while cid not in results:
            left = deadline - time.time()
            if left <= 0:
                return {"ok": False, "error": "timeout waiting for extension"}
            cond.wait(min(left, 0.5))
        return results.pop(cid)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.send_header("access-control-allow-origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        n = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            return json.loads(raw or b"{}")
        except Exception:
            return {}

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("access-control-allow-origin", "*")
        self.send_header("access-control-allow-methods", "GET, POST, OPTIONS")
        self.send_header("access-control-allow-headers", "content-type")
        self.send_header("content-length", "0")
        self.end_headers()

    def do_GET(self):
        if self.path.startswith("/health"):
            return self._json(200, {"ok": True, "queued": len(queue)})
        if self.path.startswith("/events"):
            return self._json(200, {"events": events[-50:]})
        if self.path.startswith("/cmd"):
            # Long-poll: hand the extension one command, or 204 after QUEUE_WAIT_S.
            deadline = time.time() + QUEUE_WAIT_S
            with cond:
                while not queue:
                    left = deadline - time.time()
                    if left <= 0:
                        self.send_response(204)
                        self.send_header("content-length", "0")
                        self.send_header("access-control-allow-origin", "*")
                        self.end_headers()
                        return
                    cond.wait(left)
                cmd = queue.pop(0)
            return self._json(200, cmd)
        self._json(404, {"error": "not found"})

    def do_POST(self):
        payload = self._read_json()
        if self.path.startswith("/result"):
            cid = payload.get("id")
            with cond:
                results[cid] = payload
                cond.notify_all()
            return self._json(200, {"ok": True})
        if self.path.startswith("/report"):
            events.append(payload)
            print(f"[event] {json.dumps(payload)[:200]}", flush=True)
            return self._json(200, {"ok": True})
        if self.path.startswith("/request"):
            cmd = payload.get("cmd") or {}
            timeout = float(payload.get("timeout") or CALL_WAIT_S)
            cid = enqueue(cmd)
            res = wait_result(cid, timeout)
            res.setdefault("id", cid)
            return self._json(200, res)
        self._json(404, {"error": "not found"})

    def log_message(self, *a):
        pass


def serve() -> None:
    print(f"bridge on {BASE}  (extension must be loaded in Dia)", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


# ---------- CLI ----------
def _post(path: str, payload: dict, timeout: float = CALL_WAIT_S + 10) -> dict:
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode(), headers={"content-type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def _call(cmd: dict, timeout: float = CALL_WAIT_S) -> dict:
    return _post("/request", {"cmd": cmd, "timeout": timeout}, timeout + 10)


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    mode = argv[1]

    if mode == "serve":
        serve()
        return 0

    if mode == "cmd":
        ctype = argv[2]
        args = json.loads(argv[3]) if len(argv) > 3 else {}
        res = _call({"type": ctype, **args})
        print(json.dumps(res, indent=1)[:6000])
        return 0 if res.get("ok") else 1

    if mode == "shot":
        out = argv[2] if len(argv) > 2 else "shot.png"
        res = _call({"type": "shot", "format": os.path.splitext(out)[1].lstrip(".") or "png"})
        if not res.get("ok"):
            print(json.dumps(res))
            return 1
        raw = base64.b64decode(res["result"].get("base64") or "")
        with open(out, "wb") as fh:
            fh.write(raw)
        print(f"saved {out} ({len(raw)} bytes)")
        return 0

    if mode == "eval":
        res = _call({"type": "eval", "expression": argv[2]})
        print(json.dumps(res.get("result", res), indent=1)[:6000])
        return 0 if res.get("ok") else 1

    print(f"unknown mode: {mode}")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
