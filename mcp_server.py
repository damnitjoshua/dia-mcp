#!/usr/bin/env python3
"""Dia Live Bridge — MCP server (stdio, stdlib only).

Exposes the live Dia tab controlled by the extension + bridge.py as MCP tools.
Transport: newline-delimited JSON-RPC 2.0 on stdin/stdout.

Every page-scoped tool auto-attaches to the most recently used web tab if nothing is
attached yet, so the agent never has to know about attach order. Each tool call is one
just-in-time action; nothing is cached except the attachment itself.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import urllib.request

BRIDGE = os.environ.get("DIA_BRIDGE_URL", "http://127.0.0.1:9777")
SERVER_INFO = {"name": "dia-live", "version": "0.1.0"}
PROTOCOL = "2024-11-05"
CALL_TIMEOUT = float(os.environ.get("DIA_CALL_TIMEOUT", "60"))


# ---------- bridge client ----------
_BRIDGE_STARTED = {"tried": False}


def _bridge_alive() -> bool:
    try:
        with urllib.request.urlopen(BRIDGE + "/health", timeout=1.5) as r:
            return r.status == 200
    except Exception:
        return False


def _ensure_bridge() -> None:
    """Start bridge.py serve if nothing is listening. One bridge serves the extension."""
    if _bridge_alive():
        return
    if _BRIDGE_STARTED["tried"]:
        return
    _BRIDGE_STARTED["tried"] = True
    bridge_py = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools", "bridge.py")
    if not os.path.exists(bridge_py):
        return
    import subprocess
    import time as _time
    subprocess.Popen(
        [sys.executable, bridge_py, "serve"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    for _ in range(20):
        if _bridge_alive():
            return
        _time.sleep(0.25)


def bridge(cmd: dict, timeout: float | None = None) -> dict:
    if not _bridge_alive():
        _ensure_bridge()
    payload = {"cmd": cmd, "timeout": timeout or CALL_TIMEOUT}
    req = urllib.request.Request(
        BRIDGE + "/request",
        data=json.dumps(payload).encode(),
        headers={"content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=(timeout or CALL_TIMEOUT) + 10) as r:
            return json.loads(r.read() or b"{}")
    except Exception as e:  # bridge down, extension not loaded, etc.
        return {
            "ok": False,
            "error": f"{type(e).__name__}: {e}. Is the Dia Live Bridge extension loaded in Dia "
                     f"(chrome://extensions, Developer mode, Load unpacked) and Dia running?",
        }


_state = {"attached": False}


def ensure_attached() -> dict | None:
    """Attach to the most recent web tab once. Returns an error dict on failure.

    Dia freezes background tabs: timers do not fire, fetches never complete and
    Runtime.evaluate with awaitPromise hangs until timeout. Bringing the tab to the
    front first is mandatory for anything async or interactive.
    """
    if _state["attached"]:
        return None
    res = bridge({"type": "attach"}, timeout=30)
    if res.get("ok"):
        _state["attached"] = True
        bridge({"type": "cdp", "method": "Page.bringToFront", "params": {}}, timeout=15)
        return None
    return res


def call(cmd: dict, needs_tab: bool = True, timeout: float | None = None) -> dict:
    if needs_tab:
        err = ensure_attached()
        if err:
            return err
    res = bridge(cmd, timeout)
    if res.get("ok"):
        return res["result"] if isinstance(res.get("result"), dict) else {"value": res.get("result")}
    return {"error": res.get("error", "unknown error")}


# ---------- tool implementations ----------
def t_status(_a):
    return call({"type": "status"}, needs_tab=False)


def t_list_tabs(_a):
    return call({"type": "list_tabs"}, needs_tab=False)


def t_attach(a):
    _state["attached"] = False
    res = call({"type": "attach", "tabId": a.get("tabId")}, needs_tab=False, timeout=30)
    if "error" not in res:
        _state["attached"] = True
    return res


def t_detach(_a):
    _state["attached"] = False
    return call({"type": "detach"}, needs_tab=False)


def t_snapshot(a):
    res = call({"type": "snapshot"})
    els = res.get("elements") or []
    if a.get("filter"):
        needle = str(a["filter"]).lower()
        els = [e for e in els if needle in (e.get("name", "") + " " + e.get("role", "")).lower()]
    return {"count": len(els), "elements": els[: int(a.get("limit") or 120)]}


def t_click(a):
    if a.get("text"):
        return call({"type": "click_text", "text": a["text"]})
    if not a.get("selector"):
        return {"error": "pass selector or text"}
    return call({"type": "click", "selector": a["selector"]})


def t_fill(a):
    if not a.get("selector"):
        return {"error": "selector required"}
    return call({"type": "fill", "selector": a["selector"], "value": a.get("value", "")})


def t_eval(a):
    if not a.get("expression"):
        return {"error": "expression required"}
    return call({"type": "eval", "expression": a["expression"], "awaitPromise": bool(a.get("awaitPromise"))})


def t_navigate(a):
    if not a.get("url"):
        return {"error": "url required"}
    res = call({"type": "navigate", "url": a["url"]}, timeout=90)
    return res


def t_network(a):
    return call({"type": "network", "filter": a.get("filter"), "limit": int(a.get("limit") or 50)})


def t_body(a):
    if not a.get("requestId"):
        return {"error": "requestId required"}
    return call({"type": "body", "requestId": a["requestId"], "max": int(a.get("max") or 4000)})


def t_console(a):
    return call({"type": "console", "limit": int(a.get("limit") or 100)})


def t_screenshot(a):
    fmt = a.get("format") or "png"
    res = call({"type": "shot", "format": fmt})
    if "error" in res:
        return res
    data = res.get("base64") or ""
    out_path = a.get("path")
    if out_path:
        try:
            with open(out_path, "wb") as fh:
                fh.write(base64.b64decode(data))
            return {"saved": out_path, "bytes": len(base64.b64decode(data))}
        except Exception as e:
            return {"error": f"save failed: {e}"}
    return {"_image": {"data": data, "mimeType": f"image/{'jpeg' if fmt == 'jpeg' else fmt}"}}


def t_cdp(a):
    if not a.get("method"):
        return {"error": "method required"}
    return call({"type": "cdp", "method": a["method"], "params": a.get("params") or {}})


TOOLS = {
    "dia_status": {"fn": t_status, "desc": "Bridge/extension status: attached tab, buffered network and console counts.",
                   "schema": {"type": "object", "properties": {}}},
    "dia_list_tabs": {"fn": t_list_tabs, "desc": "List the web tabs open in Dia right now (tabId, title, url, active).",
                      "schema": {"type": "object", "properties": {}}},
    "dia_attach": {"fn": t_attach, "desc": "Attach the debugger to a tab. Omit tabId to use the most recently used web tab.",
                   "schema": {"type": "object", "properties": {"tabId": {"type": "number"}}}},
    "dia_detach": {"fn": t_detach, "desc": "Detach from the current tab (removes the debug banner).",
                   "schema": {"type": "object", "properties": {}}},
    "dia_snapshot": {"fn": t_snapshot, "desc": "Read UI elements of the attached page as role + name pairs (accessibility tree). Use to find what to click or fill.",
                     "schema": {"type": "object", "properties": {"filter": {"type": "string"}, "limit": {"type": "number"}}}},
    "dia_click": {"fn": t_click, "desc": "Click an element by CSS selector, or by its visible text.",
                  "schema": {"type": "object", "properties": {"selector": {"type": "string"}, "text": {"type": "string"}}}},
    "dia_fill": {"fn": t_fill, "desc": "Set the value of an input/textarea via CSS selector, firing input+change events.",
                 "schema": {"type": "object", "properties": {"selector": {"type": "string"}, "value": {"type": "string"}}, "required": ["selector"]}},
    "dia_eval": {"fn": t_eval, "desc": "Evaluate JavaScript in the page and return the value (JSON-serialisable).",
                 "schema": {"type": "object", "properties": {"expression": {"type": "string"}, "awaitPromise": {"type": "boolean"}}, "required": ["expression"]}},
    "dia_navigate": {"fn": t_navigate, "desc": "Navigate the attached tab to a URL.",
                     "schema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}},
    "dia_network": {"fn": t_network, "desc": "Requests captured on the attached tab: method, status, type, bytes, url. Optional substring filter.",
                    "schema": {"type": "object", "properties": {"filter": {"type": "string"}, "limit": {"type": "number"}}}},
    "dia_network_body": {"fn": t_body, "desc": "Read the response body of a captured request by requestId.",
                         "schema": {"type": "object", "properties": {"requestId": {"type": "string"}, "max": {"type": "number"}}, "required": ["requestId"]}},
    "dia_console": {"fn": t_console, "desc": "Console output and JS errors captured on the attached tab.",
                    "schema": {"type": "object", "properties": {"limit": {"type": "number"}}}},
    "dia_screenshot": {"fn": t_screenshot, "desc": "Screenshot the attached tab. Omit path to get the image inline.",
                       "schema": {"type": "object", "properties": {"path": {"type": "string"}, "format": {"type": "string", "enum": ["png", "jpeg", "webp"]}}}},
    "dia_cdp": {"fn": t_cdp, "desc": "Escape hatch: send any raw CDP method to the attached tab (e.g. Emulation.setDeviceMetricsOverride).",
                "schema": {"type": "object", "properties": {"method": {"type": "string"}, "params": {"type": "object"}}, "required": ["method"]}},
}


# ---------- MCP plumbing ----------
def tool_list() -> list[dict]:
    return [
        {"name": name, "description": spec["desc"], "inputSchema": spec["schema"]}
        for name, spec in TOOLS.items()
    ]


def handle(msg: dict) -> dict | None:
    method = msg.get("method")
    mid = msg.get("id")

    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": PROTOCOL,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
        }}
    if method in ("notifications/initialized", "initialized"):
        return None
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": tool_list()}}
    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        spec = TOOLS.get(name)
        if not spec:
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "isError": True, "content": [{"type": "text", "text": f"unknown tool: {name}"}]}}
        try:
            out = spec["fn"](args)
        except Exception as e:
            out = {"error": f"{type(e).__name__}: {e}"}

        if isinstance(out, dict) and "_image" in out:
            img = out["_image"]
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "image", "data": img["data"], "mimeType": img["mimeType"]}]}}
        is_err = isinstance(out, dict) and "error" in out
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "isError": is_err,
            "content": [{"type": "text", "text": json.dumps(out, indent=1)[:12000]}],
        }}
    if mid is None:
        return None
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        resp = handle(msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
