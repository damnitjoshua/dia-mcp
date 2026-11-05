#!/usr/bin/env python3
"""dia-mcp: a Dia Browser MCP server built with FastMCP.

Controls the Dia browser the user already has open, through a local bridge and a
browser extension. No launch flags, no restarts.

The tool surface deliberately mirrors chrome-devtools-mcp: same tool names, same
`pageId` routing, same `uid` element references. Anything chrome-devtools-mcp can
do that Dia cannot (Lighthouse, traces, screencast, heap snapshots) is present as a
tool and fails with an explicit message instead of silently missing.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import time
import urllib.request
from typing import Any, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.utilities.types import Image

BRIDGE = os.environ.get("DIA_BRIDGE_URL", "http://127.0.0.1:9777")
CALL_TIMEOUT = float(os.environ.get("DIA_CALL_TIMEOUT", "60"))
HERE = os.path.dirname(os.path.abspath(__file__))

mcp = FastMCP(
    name="dia-mcp",
    instructions=(
        "Controls the Dia browser the user already has open. Tool names, pageId routing "
        "and uid element references follow chrome-devtools-mcp. Start with list_pages, "
        "then take_snapshot to get uids. Page calls focus the tab, because Dia freezes "
        "background tabs. Call dia_detach when finished."
    ),
)

_state: dict[str, Any] = {"attached_tab": None, "current": None}
_pages: dict[int, int] = {}          # pageId -> tabId
_tab_to_page: dict[int, int] = {}    # tabId -> pageId
_next_page_id = 1
_uids: dict[str, int] = {}           # "page:uid" -> backendNodeId
_net_ids: dict[int, list[str]] = {}  # pageId -> [requestId]
_console_ids: dict[int, list[dict]] = {}
_bridge_spawn: dict[str, bool] = {"tried": False}


# --------------------------------------------------------------------------
# bridge transport
# --------------------------------------------------------------------------
def _bridge_alive() -> bool:
    try:
        with urllib.request.urlopen(BRIDGE + "/health", timeout=1.5) as r:
            return r.status == 200
    except Exception:
        return False


def _ensure_bridge() -> None:
    if _bridge_alive() or _bridge_spawn["tried"]:
        return
    _bridge_spawn["tried"] = True
    bridge_py = os.path.join(HERE, "tools", "bridge.py")
    if not os.path.exists(bridge_py):
        return
    subprocess.Popen(
        [sys.executable, bridge_py, "serve"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    for _ in range(20):
        if _bridge_alive():
            return
        time.sleep(0.25)


def _bridge(cmd: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
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
    except Exception as e:
        raise ToolError(
            f"bridge unreachable ({type(e).__name__}: {e}). Check that Dia is running with "
            "the dia-mcp extension loaded: chrome://extensions, Developer mode, Load unpacked."
        )


def _poller_age() -> float | None:
    """Seconds since the extension last polled the bridge, or None if unknown."""
    try:
        with urllib.request.urlopen(BRIDGE + "/health", timeout=2) as r:
            return json.loads(r.read() or b"{}").get("last_poll_seconds_ago")
    except Exception:
        return None


def _wake_dia() -> bool:
    """Bring Dia forward so Chrome revives the parked service worker.

    Manifest V3 kills an extension worker after ~30s without events, and a pending
    fetch does not count. Nothing wakes it while the browser sits in the background,
    but activating the app fires window and tab events. Set DIA_WAKE=0 to skip this.
    """
    if os.environ.get("DIA_WAKE", "1") == "0" or sys.platform != "darwin":
        return False
    try:
        subprocess.run(
            ["open", "-a", "Dia"],
            check=False,
            timeout=10,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except Exception:
        return False


def _call(cmd: dict[str, Any], timeout: float | None = None) -> Any:
    window = timeout or CALL_TIMEOUT
    age = _poller_age()
    if age is None or age > 10:
        # Parked worker: waking first turns a 30s stall into a normal call.
        _wake_dia()
    res = _bridge(cmd, window)
    if not res.get("ok") and "timeout waiting for extension" in str(res.get("error") or ""):
        _wake_dia()
        res = _bridge(cmd, max(45.0, window))
    if not res.get("ok"):
        error = str(res.get("error") or "unknown bridge error")
        if "timeout waiting for extension" in error:
            woke = "asked Dia to come forward" if _wake_dia() else "could not wake Dia"
            raise ToolError(
                "Dia's extension worker is parked and did not answer despite being woken "
                f"({woke}). Click any tab in Dia, then call again. If it never answers, reload "
                "the extension in chrome://extensions."
            )
        raise ToolError(error)
    return res.get("result")


def _cdp(method: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> dict[str, Any]:
    res = _call({"type": "cdp", "method": method, "params": params or {}}, timeout)
    if isinstance(res, dict):
        error = res.get("error")
        if error:
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise ToolError(f"{method} failed: {message}")
        if "result" in res:
            inner = res.get("result")
            return inner if isinstance(inner, dict) else {}
    return res or {}


def _ensure_dom() -> None:
    """Prime the DOM agent. resolveNode, getBoxModel and CSS all fail without it."""
    _cdp("DOM.getDocument", {"depth": 1})


# --------------------------------------------------------------------------
# pages and elements
# --------------------------------------------------------------------------
def _refresh_pages() -> dict[int, int]:
    global _next_page_id
    tabs = _call({"type": "list_tabs"}, timeout=20) or []
    live = {int(t["tabId"]): t for t in tabs}
    for tab_id in list(_tab_to_page):
        if tab_id not in live:
            pid = _tab_to_page.pop(tab_id)
            _pages.pop(pid, None)
    for tab_id in live:
        if tab_id not in _tab_to_page:
            _tab_to_page[tab_id] = _next_page_id
            _pages[_next_page_id] = tab_id
            _next_page_id += 1
    return _pages


def _use_page(pageId: int | None) -> int:
    """Attach to the page (default: current, else most recent) and focus it."""
    pages = _refresh_pages()
    pid = pageId if pageId is not None else _state["current"]
    if pid is None or pid not in pages:
        if not pages:
            raise ToolError("no web tabs open in Dia")
        pid = max(pages)
    tab_id = pages[pid]
    if _state["attached_tab"] != tab_id:
        _call({"type": "attach", "tabId": tab_id}, timeout=30)
        _state["attached_tab"] = tab_id
    _call({"type": "cdp", "method": "Page.bringToFront", "params": {}}, timeout=15)
    _state["current"] = pid
    return pid


def _page_line(pid: int, tab_id: int, info: dict[str, Any]) -> str:
    mark = " [selected]" if _state["current"] == pid else ""
    return f"{pid}: {info.get('title') or '(no title)'} ({info.get('url') or ''}){mark}"


def _pages_text() -> str:
    pages = _refresh_pages()
    tabs = {int(t["tabId"]): t for t in (_call({"type": "list_tabs"}, timeout=20) or [])}
    lines = [f"{pid}: {tabs[tid].get('title') or '(no title)'} ({tabs[tid].get('url') or ''})"
             + (" [selected]" if _state["current"] == pid else "")
             for pid, tid in sorted(pages.items()) if tid in tabs]
    return "## Pages\n" + ("\n".join(lines) if lines else "(none)")


def _uid_node(uid: str) -> int:
    backend = _uids.get(uid)
    if backend is None:
        raise ToolError(f"unknown uid {uid!r}. Call take_snapshot first and use a uid from it.")
    return backend


def _resolve_object(backend_node_id: int) -> str:
    _ensure_dom()
    res = _cdp("DOM.resolveNode", {"backendNodeId": backend_node_id})
    obj = (res.get("object") or {})
    if not obj.get("objectId"):
        raise ToolError("could not resolve that element; take a fresh snapshot")
    return obj["objectId"]


def _call_on_element(backend_node_id: int, function: str, args: list[Any] | None = None) -> Any:
    object_id = _resolve_object(backend_node_id)
    params: dict[str, Any] = {
        "objectId": object_id,
        "functionDeclaration": function,
        "returnByValue": True,
        "awaitPromise": True,
    }
    if args:
        params["arguments"] = [{"value": a} for a in args]
    res = _cdp("Runtime.callFunctionOn", params)
    if res.get("exceptionDetails"):
        detail = res["exceptionDetails"]
        raise ToolError(str((detail.get("exception") or {}).get("description") or detail.get("text")))
    return (res.get("result") or {}).get("value")


def _box(backend_node_id: int) -> dict[str, float]:
    _ensure_dom()
    model = _cdp("DOM.getBoxModel", {"backendNodeId": backend_node_id}).get("model") or {}
    quad = model.get("content") or model.get("border") or []
    if len(quad) < 8:
        raise ToolError("element has no box; is it visible?")
    xs, ys = quad[0::2], quad[1::2]
    x, y = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    return {"x": x, "y": y, "width": max(xs) - min(xs), "height": max(ys) - min(ys)}


def _mouse(event: str, x: float, y: float, button: str = "left", clicks: int = 1) -> None:
    _cdp("Input.dispatchMouseEvent", {
        "type": event, "x": x, "y": y, "button": button,
        "clickCount": clicks, "buttons": 1 if event != "mouseReleased" else 0,
    })


_KEY_CODES = {
    "Enter": (13, "\r"), "Tab": (9, "\t"), "Escape": (27, ""), "Backspace": (8, ""),
    "Delete": (46, ""), "ArrowUp": (38, ""), "ArrowDown": (40, ""),
    "ArrowLeft": (37, ""), "ArrowRight": (39, ""), "Home": (36, ""), "End": (35, ""),
    "PageUp": (33, ""), "PageDown": (34, ""), " ": (32, " "),
}
_MODIFIERS = {"Alt": 1, "Control": 2, "Meta": 4, "Shift": 8}


def _press_key(key: str) -> None:
    parts = key.split("+")
    mods = 0
    for p in parts[:-1]:
        if p in _MODIFIERS:
            mods |= _MODIFIERS[p]
    base = parts[-1]
    code, text = _KEY_CODES.get(base, (ord(base[0]) if len(base) == 1 else 0, base if len(base) == 1 else ""))
    common = {"key": base, "code": base, "windowsVirtualKeyCode": code,
              "nativeVirtualKeyCode": code, "modifiers": mods}
    if text and not mods:
        common["text"] = text
    _cdp("Input.dispatchKeyEvent", {**common, "type": "keyDown"})
    _cdp("Input.dispatchKeyEvent", {**common, "type": "keyUp"})


def _snapshot_text(page_id: int) -> str:
    global _uids
    res = _call({"type": "snapshot"}) or {}
    elements = res.get("elements") or []
    lines, index = [], 0
    for key in [k for k in _uids if k.startswith(f"{page_id}:")]:
        _uids.pop(key, None)
    lines.append(f"## Latest page snapshot")
    for el in elements:
        backend = el.get("backendNodeId")
        if backend is None:
            continue
        index += 1
        uid = f"{page_id}_{index}"
        _uids[uid] = int(backend)
        lines.append(f'uid={uid} {el.get("role", "generic")} "{str(el.get("name", ""))[:120]}"')
    return "\n".join(lines)


def _unsupported(what: str, why: str, alternative: str) -> ToolError:
    return ToolError(f"{what} is not available when driving Dia. {why} Use {alternative} instead.")


# --------------------------------------------------------------------------
# page tools
# --------------------------------------------------------------------------
@mcp.tool
def list_pages() -> str:
    """List the open pages in Dia.

    Each line is `pageId: title (url)`. Pass the pageId to the other tools.
    """
    return _pages_text()


@mcp.tool
def new_page(url: str, timeout: float = 15.0) -> str:
    """Open a URL in a new tab and switch to it.

    Args:
        url: Address to open.
        timeout: Seconds to wait for the tab to appear.
    """
    created = _call({"type": "create_tab", "url": url, "active": True}, timeout=timeout) or {}
    _state["attached_tab"] = None
    tab_id = int(created.get("tabId") or 0)
    # The new tab can take a moment to show up in the tab list.
    deadline = time.time() + max(2.0, timeout)
    page_id = None
    while time.time() < deadline:
        _refresh_pages()
        page_id = _tab_to_page.get(tab_id)
        if page_id is not None:
            break
        time.sleep(0.2)
    if page_id is None:
        raise ToolError("the new tab did not show up; call list_pages")
    _use_page(page_id)
    return _pages_text()


@mcp.tool
def close_page(pageId: int) -> str:
    """Close a page.

    Args:
        pageId: Page to close, from list_pages.
    """
    pages = _refresh_pages()
    tab_id = pages.get(pageId)
    if tab_id is None:
        raise ToolError(f"unknown pageId {pageId}")
    _call({"type": "close_tab", "tabId": tab_id}, timeout=20)
    if _state["attached_tab"] == tab_id:
        _state["attached_tab"] = None
        _state["current"] = None
    _tab_to_page.pop(tab_id, None)
    _pages.pop(pageId, None)
    return _pages_text()


@mcp.tool
def select_page(pageId: int, bringToFront: bool = True) -> str:
    """Make a page the current one for later calls.

    Args:
        pageId: Page to select.
        bringToFront: Show the tab in Dia.
    """
    _use_page(pageId)
    if not bringToFront:
        return f"selected page {pageId}"
    return f"selected page {pageId}"


@mcp.tool
def navigate_page(
    pageId: int | None = None,
    type: Literal["url", "back", "forward", "reload"] = "url",
    url: str | None = None,
    ignoreCache: bool = False,
    handleBeforeUnload: Literal["accept", "dismiss"] = "accept",
) -> str:
    """Navigate, go back, go forward, or reload a page.

    Args:
        pageId: Page to act on. Defaults to the selected page.
        type: What to do.
        url: Target URL, required when type is url.
        ignoreCache: Reload without the HTTP cache.
        handleBeforeUnload: How to answer an unload dialog.
    """
    pid = _use_page(pageId)
    for key in [k for k in list(_uids) if k.startswith(f"{pid}:")]:
        _uids.pop(key, None)
    if type == "url":
        if not url:
            raise ToolError("url is required when type is url")
        res = _call({"type": "navigate", "url": url}, timeout=90) or {}
        if res.get("errorText"):
            raise ToolError(f"navigation failed: {res['errorText']}")
    elif type == "reload":
        _cdp("Page.reload", {"ignoreCache": ignoreCache}, timeout=90)
    elif type in ("back", "forward"):
        expr = "history.back(); true" if type == "back" else "history.forward(); true"
        _call({"type": "eval", "expression": expr})
    else:
        raise ToolError(f"unknown navigation type {type}")
    return f"navigated page {pid} ({type})"


@mcp.tool
def take_snapshot(pageId: int | None = None, verbose: bool = False, filePath: str | None = None) -> str:
    """Capture the page as a list of elements with uids.

    Use the returned uids with click, fill, hover, upload_file and get_css_styles.

    Args:
        pageId: Page to snapshot. Defaults to the selected page.
        verbose: Include every element, not just named ones.
        filePath: Also write the snapshot here.
    """
    pid = _use_page(pageId)
    text = _snapshot_text(pid)
    if filePath:
        with open(filePath, "w") as fh:
            fh.write(text)
        return f"snapshot saved to {filePath} ({len(text)} chars)"
    return text


@mcp.tool
def click(pageId: int | None = None, uid: str = "", dblClick: bool = False, includeSnapshot: bool = False) -> str:
    """Click an element.

    Args:
        pageId: Page holding the element.
        uid: Element uid from take_snapshot.
        dblClick: Double click instead of single.
        includeSnapshot: Append a fresh snapshot to the result.
    """
    pid = _use_page(pageId)
    backend = _uid_node(uid)
    fn = (
        "function(times){ this.scrollIntoView({block:'center'}); "
        "for (let i=0;i<times;i++) this.click(); return (this.innerText||this.value||'').slice(0,80); }"
    )
    label = _call_on_element(backend, fn, [2 if dblClick else 1])
    out = f"clicked uid={uid} ({label})"
    return out + ("\n\n" + _snapshot_text(pid) if includeSnapshot else "")


@mcp.tool
def fill(pageId: int | None = None, uid: str = "", value: str = "", includeSnapshot: bool = False) -> str:
    """Type into an input, textarea, or contenteditable element.

    Args:
        pageId: Page holding the element.
        uid: Element uid from take_snapshot.
        value: Text to enter.
        includeSnapshot: Append a fresh snapshot to the result.
    """
    pid = _use_page(pageId)
    backend = _uid_node(uid)
    fn = """function(v){
      this.focus();
      if (this.isContentEditable) {
        this.textContent = v;
        this.dispatchEvent(new InputEvent('input', {bubbles: true, data: v}));
      } else {
        const proto = this.tagName === 'TEXTAREA'
          ? window.HTMLTextAreaElement.prototype
          : window.HTMLInputElement.prototype;
        const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
        setter.call(this, v);
        this.dispatchEvent(new Event('input', {bubbles: true}));
        this.dispatchEvent(new Event('change', {bubbles: true}));
      }
      return this.value !== undefined ? this.value : this.textContent;
    }"""
    result = _call_on_element(backend, fn, [value])
    out = f"filled uid={uid} with {result!r}"
    return out + ("\n\n" + _snapshot_text(pid) if includeSnapshot else "")


@mcp.tool
def fill_form(pageId: int | None = None, elements: list[dict[str, Any]] | None = None, includeSnapshot: bool = False) -> str:
    """Fill several fields in one call.

    Args:
        pageId: Page holding the elements.
        elements: List of objects with uid and value.
        includeSnapshot: Append a fresh snapshot to the result.
    """
    if not elements:
        raise ToolError("elements is required, for example [{\"uid\": \"1_3\", \"value\": \"hi\"}]")
    pid = _use_page(pageId)
    done = []
    for item in elements:
        uid = str(item.get("uid") or "")
        if not uid:
            raise ToolError("every element needs a uid")
        done.append(fill(pageId=pid, uid=uid, value=str(item.get("value", ""))))
    out = "\n".join(done)
    return out + ("\n\n" + _snapshot_text(pid) if includeSnapshot else "")


@mcp.tool
def hover(pageId: int | None = None, uid: str = "", includeSnapshot: bool = False) -> str:
    """Move the mouse over an element.

    Args:
        pageId: Page holding the element.
        uid: Element uid from take_snapshot.
        includeSnapshot: Append a fresh snapshot to the result.
    """
    pid = _use_page(pageId)
    box = _box(_uid_node(uid))
    _mouse("mouseMoved", box["x"], box["y"])
    out = f"hovered uid={uid}"
    return out + ("\n\n" + _snapshot_text(pid) if includeSnapshot else "")


@mcp.tool
def drag(pageId: int | None = None, fromUid: str = "", toUid: str = "", includeSnapshot: bool = False) -> str:
    """Drag one element onto another.

    Args:
        pageId: Page holding the elements.
        fromUid: Element to drag.
        toUid: Element to drop on.
        includeSnapshot: Append a fresh snapshot to the result.
    """
    pid = _use_page(pageId)
    start = _box(_uid_node(fromUid))
    end = _box(_uid_node(toUid))
    _mouse("mouseMoved", start["x"], start["y"])
    _mouse("mousePressed", start["x"], start["y"])
    for step in range(1, 6):
        _mouse("mouseMoved", start["x"] + (end["x"] - start["x"]) * step / 5,
               start["y"] + (end["y"] - start["y"]) * step / 5)
        time.sleep(0.02)
    _mouse("mouseReleased", end["x"], end["y"])
    out = f"dragged uid={fromUid} to uid={toUid}"
    return out + ("\n\n" + _snapshot_text(pid) if includeSnapshot else "")


@mcp.tool
def press_key(pageId: int | None = None, key: str = "Enter", includeSnapshot: bool = False) -> str:
    """Press a key or a combination like Control+A.

    Args:
        pageId: Page to send the key to.
        key: Key name, optionally with modifiers.
        includeSnapshot: Append a fresh snapshot to the result.
    """
    pid = _use_page(pageId)
    _press_key(key)
    out = f"pressed {key}"
    return out + ("\n\n" + _snapshot_text(pid) if includeSnapshot else "")


@mcp.tool
def type_text(pageId: int | None = None, text: str = "", submitKey: str | None = None, includeSnapshot: bool = False) -> str:
    """Type text into the focused element.

    Args:
        pageId: Page to type into.
        text: Text to insert.
        submitKey: Key to press afterwards, for example Enter.
        includeSnapshot: Append a fresh snapshot to the result.
    """
    pid = _use_page(pageId)
    _cdp("Input.insertText", {"text": text})
    if submitKey:
        _press_key(submitKey)
    out = f"typed {len(text)} characters"
    return out + ("\n\n" + _snapshot_text(pid) if includeSnapshot else "")


@mcp.tool
def evaluate_script(
    pageId: int | None = None,
    function: str = "",
    args: list[dict[str, Any]] | None = None,
    filePath: str | None = None,
) -> str:
    """Run a JavaScript function in the page and return its value.

    Args:
        pageId: Page to evaluate in.
        function: Function to call, for example "() => document.title".
        args: Arguments, either plain values or objects with a uid key.
        filePath: Write the JSON result here instead of returning it.
    """
    pid = _use_page(pageId)
    if not function:
        raise ToolError("function is required")
    uid_args = [a for a in (args or []) if isinstance(a, dict) and "uid" in a]
    if uid_args:
        object_id = _resolve_object(_uid_node(str(uid_args[0]["uid"])))
        res = _cdp("Runtime.callFunctionOn", {
            "objectId": object_id,
            "functionDeclaration": f"function(){{ return ({function})(this); }}",
            "returnByValue": True,
            "awaitPromise": True,
        })
        if res.get("exceptionDetails"):
            raise ToolError(str((res["exceptionDetails"].get("exception") or {}).get("description")))
        value = (res.get("result") or {}).get("value")
    else:
        plain = [a.get("value") if isinstance(a, dict) else a for a in (args or [])]
        expr = f"({function})(...{json.dumps(plain)})"
        res = _call({"type": "eval", "expression": expr, "awaitPromise": True}) or {}
        value = res.get("value")
    if filePath:
        with open(filePath, "w") as fh:
            fh.write(json.dumps(value, indent=2, default=str))
        return f"result saved to {filePath}"
    return json.dumps({"pageId": pid, "result": value}, default=str)[:12000]


@mcp.tool
def take_screenshot(
    pageId: int | None = None,
    format: Literal["png", "jpeg", "webp"] = "png",
    quality: int | None = None,
    uid: str | None = None,
    fullPage: bool = False,
    filePath: str | None = None,
) -> Any:
    """Take a screenshot of a page or of one element.

    Args:
        pageId: Page to capture.
        format: Image format.
        quality: Compression quality for jpeg and webp.
        uid: Capture only this element.
        fullPage: Capture the whole scrollable page.
        filePath: Save the image here instead of returning it inline.
    """
    _use_page(pageId)
    params: dict[str, Any] = {"format": format}
    if quality is not None and format != "png":
        params["quality"] = quality
    if uid:
        box = _box(_uid_node(uid))
        params["clip"] = {"x": box["x"], "y": box["y"], "width": box["width"],
                          "height": box["height"], "scale": 1}
    elif fullPage:
        size = (_call({"type": "eval", "expression":
                       "({w: document.documentElement.scrollWidth, h: document.documentElement.scrollHeight})"}) or {}).get("value") or {}
        if size.get("w"):
            params["clip"] = {"x": 0, "y": 0, "width": size["w"], "height": size["h"], "scale": 1}
    res = _cdp("Page.captureScreenshot", params, timeout=90)
    data = res.get("data") or ""
    if not data:
        raise ToolError("the browser returned no image data")
    raw = base64.b64decode(data)
    if filePath:
        with open(filePath, "wb") as fh:
            fh.write(raw)
        return f"screenshot saved to {filePath} ({len(raw)} bytes)"
    return Image(data=raw, format=format)


@mcp.tool
def list_network_requests(
    pageId: int | None = None,
    pageSize: int = 20,
    pageIdx: int = 0,
    types: list[str] | None = None,
) -> str:
    """List the network requests a page has made since you attached.

    Args:
        pageId: Page to read.
        pageSize: Requests per page.
        pageIdx: Zero-based page number.
        types: Only these resource types, for example ["xhr", "fetch", "document"].
    """
    pid = _use_page(pageId)
    res = _call({"type": "network", "limit": 500}) or {}
    all_reqs = res.get("requests") or []
    # reqids are full-list positions so they stay stable across filtered listings.
    _net_ids[pid] = [str(r.get("requestId")) for r in all_reqs]
    indexed = list(enumerate(all_reqs, start=1))
    if types:
        wanted = {t.lower() for t in types}
        indexed = [(i, r) for i, r in indexed if str(r.get("type") or "").lower() in wanted]
    if not indexed:
        return "## Network requests\n(none captured yet; reload the page after attaching)"
    start = max(0, pageIdx) * pageSize
    window = indexed[start:start + pageSize]
    lines = [f"## Network requests (showing {start + 1}-{start + len(window)} of {len(indexed)})"]
    for reqid, r in window:
        lines.append(
            f"reqid={reqid} {r.get('method') or 'GET'} {r.get('url')} "
            f"[{r.get('status') or 'pending'}, {r.get('type') or '?'}, {r.get('bytes') or 0} bytes]"
        )
    return "\n".join(lines)


@mcp.tool
def get_network_request(pageId: int | None = None, reqid: int | None = None) -> str:
    """Show one captured request with headers and response body.

    Args:
        pageId: Page the request belongs to.
        reqid: reqid from list_network_requests. Omit for the most recent request.
    """
    pid = _use_page(pageId)
    ids = _net_ids.get(pid) or []
    if not ids:
        list_network_requests(pageId=pid, pageSize=200)
        ids = _net_ids.get(pid) or []
    if not ids:
        raise ToolError("no requests captured yet; reload the page after attaching")
    index = (reqid or len(ids)) - 1
    if index < 0 or index >= len(ids):
        raise ToolError(f"reqid {reqid} out of range, there are {len(ids)} requests")
    request_id = ids[index]
    meta = (_call({"type": "network", "limit": 500}) or {}).get("requests") or []
    record = next((r for r in meta if str(r.get("requestId")) == request_id), {})
    try:
        body = (_call({"type": "body", "requestId": request_id, "max": 4000}) or {}).get("body", "")
    except ToolError:
        body = "(no body available: empty response, 204, or blocked request)"
    head = [f"## Request {index + 1}", f"{record.get('method')} {record.get('url')}",
            f"status: {record.get('status')}  type: {record.get('type')}  bytes: {record.get('bytes')}"]
    return "\n".join(head) + "\n\n### Response body\n" + (body or "(empty)")[:4000]


@mcp.tool
def list_console_messages(
    pageId: int | None = None,
    pageSize: int = 20,
    pageIdx: int = 0,
    types: list[str] | None = None,
) -> str:
    """List console messages and JavaScript errors.

    Args:
        pageId: Page to read.
        pageSize: Messages per page.
        pageIdx: Zero-based page number.
        types: Only these levels, for example ["error", "warning"].
    """
    pid = _use_page(pageId)
    entries = (_call({"type": "console", "limit": 500}) or {}).get("entries") or []
    # msgids are full-list positions so they stay stable across filtered listings.
    _console_ids[pid] = entries
    indexed = list(enumerate(entries, start=1))
    if types:
        wanted = {t.lower() for t in types}
        indexed = [(i, e) for i, e in indexed if str(e.get("level") or "").lower() in wanted]
    if not indexed:
        return "## Console messages\n(none)"
    start = max(0, pageIdx) * pageSize
    window = indexed[start:start + pageSize]
    lines = [f"## Console messages (showing {start + 1}-{start + len(window)} of {len(indexed)})"]
    for msgid, e in window:
        lines.append(f"msgid={msgid} [{e.get('level')}] {str(e.get('text'))[:300]}")
    return "\n".join(lines)


@mcp.tool
def get_console_message(pageId: int | None = None, msgid: int = 1) -> str:
    """Show one console message in full.

    Args:
        pageId: Page the message belongs to.
        msgid: msgid from list_console_messages.
    """
    pid = _use_page(pageId)
    entries = _console_ids.get(pid) or []
    if not entries:
        list_console_messages(pageId=pid, pageSize=500)
        entries = _console_ids.get(pid) or []
    if msgid < 1 or msgid > len(entries):
        raise ToolError(f"msgid {msgid} out of range, there are {len(entries)} messages")
    entry = entries[msgid - 1]
    return json.dumps(entry, indent=2, default=str)[:4000]


@mcp.tool
def handle_dialog(pageId: int | None = None, action: Literal["accept", "dismiss"] = "accept", promptText: str | None = None) -> str:
    """Answer a JavaScript dialog that is blocking the page.

    Args:
        pageId: Page with the dialog.
        action: Accept or dismiss it.
        promptText: Text for a prompt dialog.
    """
    _use_page(pageId)
    _call({"type": "dialog", "accept": action == "accept", "promptText": promptText}, timeout=20)
    return f"dialog {action}ed"


@mcp.tool
def wait_for(pageId: int | None = None, text: list[str] | None = None, timeout: float = 30000) -> str:
    """Wait until any of the given strings appears on the page.

    Args:
        pageId: Page to watch.
        text: Strings to look for.
        timeout: Milliseconds to wait.
    """
    if not text:
        raise ToolError("text is required, for example [\"Checkout\"]")
    pid = _use_page(pageId)
    deadline = time.time() + (timeout / 1000.0)
    needle = json.dumps(text)
    expr = (
        f"(() => {{ const t = {needle}; const body = document.body ? document.body.innerText : ''; "
        "return t.find(s => body.includes(s)) || null; })()"
    )
    while time.time() < deadline:
        hit = (_call({"type": "eval", "expression": expr}) or {}).get("value")
        if hit:
            return f"found {hit!r} on page {pid}"
        time.sleep(0.25)
    raise ToolError(f"none of {text} appeared within {timeout} ms")


@mcp.tool
def emulate(
    pageId: int | None = None,
    networkConditions: Literal["Offline", "Slow 3G", "Fast 3G", "Slow 4G", "Fast 4G"] | None = None,
    cpuThrottlingRate: float | None = None,
    geolocation: str | None = None,
    userAgent: str | None = None,
    colorScheme: Literal["dark", "light"] | None = None,
    forcedColors: Literal["active", "none"] | None = None,
    prefersContrast: Literal["more", "no-preference"] | None = None,
    prefersReducedMotion: Literal["reduce", "no-preference"] | None = None,
    viewport: str | None = None,
    extraHttpHeaders: dict[str, str] | None = None,
) -> str:
    """Emulate device and network conditions.

    Args:
        pageId: Page to emulate on.
        networkConditions: Preset throttling profile.
        cpuThrottlingRate: CPU slowdown factor, 1 is off.
        geolocation: "lat,long" to fake.
        userAgent: User agent string to report.
        colorScheme: Force dark or light mode.
        forcedColors: Force high contrast.
        prefersContrast: Contrast media feature.
        prefersReducedMotion: Reduced motion media feature.
        viewport: "WIDTHxHEIGHT", for example 390x844.
        extraHttpHeaders: Extra headers for requests from this page.
    """
    pid = _use_page(pageId)
    applied = []
    profiles = {
        "Offline": (True, 0, 0, 0),
        "Slow 3G": (False, 400, 400 * 1024 / 8, 400 * 1024 / 8),
        "Fast 3G": (False, 150, 1.6 * 1024 * 1024 / 8, 750 * 1024 / 8),
        "Slow 4G": (False, 100, 3 * 1024 * 1024 / 8, 1.5 * 1024 * 1024 / 8),
        "Fast 4G": (False, 20, 40 * 1024 * 1024 / 8, 10 * 1024 * 1024 / 8),
    }
    if networkConditions:
        if networkConditions not in profiles:
            raise ToolError(f"unknown networkConditions {networkConditions}")
        offline, latency, down, up = profiles[networkConditions]
        _cdp("Network.emulateNetworkConditions", {
            "offline": offline, "latency": latency,
            "downloadThroughput": down, "uploadThroughput": up,
        })
        applied.append(f"network={networkConditions}")
    if cpuThrottlingRate is not None:
        _cdp("Emulation.setCPUThrottlingRate", {"rate": cpuThrottlingRate})
        applied.append(f"cpu={cpuThrottlingRate}x")
    if geolocation:
        lat, _, lon = geolocation.partition(",")
        _cdp("Emulation.setGeolocationOverride", {"latitude": float(lat), "longitude": float(lon), "accuracy": 1})
        applied.append("geolocation")
    if userAgent:
        _cdp("Emulation.setUserAgentOverride", {"userAgent": userAgent})
        applied.append("userAgent")
    if extraHttpHeaders:
        _cdp("Network.setExtraHTTPHeaders", {"headers": extraHttpHeaders})
        applied.append("headers")
    media = []
    if colorScheme:
        media.append({"name": "prefers-color-scheme", "value": colorScheme})
    if forcedColors:
        media.append({"name": "forced-colors", "value": forcedColors})
    if prefersContrast:
        media.append({"name": "prefers-contrast", "value": prefersContrast})
    if prefersReducedMotion:
        media.append({"name": "prefers-reduced-motion", "value": prefersReducedMotion})
    if media:
        _cdp("Emulation.setEmulatedMedia", {"features": media})
        applied.append("media")
    if viewport:
        w, _, h = viewport.partition("x")
        _cdp("Emulation.setDeviceMetricsOverride", {
            "width": int(w), "height": int(h), "deviceScaleFactor": 1, "mobile": False,
        })
        applied.append(f"viewport={viewport}")
    return f"emulated on page {pid}: " + (", ".join(applied) if applied else "nothing")


@mcp.tool
def resize_page(pageId: int | None = None, width: int = 1280, height: int = 800) -> str:
    """Set the page viewport size.

    Dia rejects window resizing, so this emulates device metrics instead.

    Args:
        pageId: Page to resize.
        width: Viewport width in CSS pixels.
        height: Viewport height in CSS pixels.
    """
    pid = _use_page(pageId)
    _cdp("Emulation.setDeviceMetricsOverride", {
        "width": int(width), "height": int(height), "deviceScaleFactor": 1, "mobile": False,
    })
    return f"page {pid} viewport is now {width}x{height}"


@mcp.tool
def get_css_styles(pageId: int | None = None, uid: str = "") -> str:
    """Show the CSS rules that apply to an element.

    Args:
        pageId: Page holding the element.
        uid: Element uid from take_snapshot.
    """
    _use_page(pageId)
    backend = _uid_node(uid)
    _ensure_dom()
    pushed = _cdp("DOM.pushNodesByBackendIdsToFrontend", {"backendNodeIds": [backend]})
    node_ids = pushed.get("nodeIds") or []
    if not node_ids:
        raise ToolError("could not map that element to a DOM node")
    _cdp("CSS.enable")
    styles = _cdp("CSS.getMatchedStylesForNode", {"nodeId": node_ids[0]})
    lines = [f"## Styles for uid={uid}"]
    for rule in (styles.get("matchedCSSRules") or [])[:20]:
        r = rule.get("rule") or {}
        selector = (r.get("selectorList") or {}).get("text") or "?"
        lines.append(f"\n{selector} {{")
        for prop in (r.get("style") or {}).get("cssProperties", [])[:25]:
            if prop.get("value"):
                lines.append(f"  {prop.get('name')}: {prop.get('value')};")
        lines.append("}")
    if len(lines) == 1:
        lines.append("(no matched rules reported)")
    return "\n".join(lines)


@mcp.tool
def upload_file(pageId: int | None = None, uid: str = "", filePath: str = "") -> str:
    """Attach a local file to a file input element.

    Args:
        pageId: Page holding the input.
        uid: Element uid of the file input.
        filePath: Absolute path to the file on this machine.
    """
    _use_page(pageId)
    if not filePath or not os.path.exists(filePath):
        raise ToolError(f"file not found: {filePath}")
    _cdp("DOM.setFileInputFiles", {"files": [filePath], "backendNodeId": _uid_node(uid)})
    return f"attached {filePath} to uid={uid}"


# --------------------------------------------------------------------------
# tools that Dia cannot support, kept so the surface matches
# --------------------------------------------------------------------------
@mcp.tool
def lighthouse_audit(pageId: int | None = None, mode: str = "navigation") -> str:
    """Run a Lighthouse audit. Not available on Dia.

    Args:
        pageId: Page to audit.
        mode: Audit mode.
    """
    raise _unsupported(
        "lighthouse_audit",
        "Lighthouse needs to drive its own browser instance, which Dia cannot offer.",
        "chrome-devtools-mcp with Chrome, or `npx lighthouse` against the URL",
    )


@mcp.tool
def performance_start_trace(pageId: int | None = None, reload: bool = True, autoStop: bool = False) -> str:
    """Start a performance trace. Not available on Dia.

    Args:
        pageId: Page to trace.
        reload: Reload during the trace.
        autoStop: Stop automatically.
    """
    raise _unsupported(
        "performance_start_trace",
        "Trace collection needs the browser-level Tracing domain, which the extension cannot reach.",
        "dia_cdp or DevTools open on that tab",
    )


@mcp.tool
def performance_stop_trace(pageId: int | None = None) -> str:
    """Stop a performance trace. Not available on Dia.

    Args:
        pageId: Page being traced.
    """
    raise _unsupported(
        "performance_stop_trace",
        "No trace can be started without the Tracing domain.",
        "dia_cdp or DevTools open on that tab",
    )


@mcp.tool
def performance_analyze_insight(pageId: int | None = None, insightName: str = "") -> str:
    """Explain one performance insight. Not available on Dia.

    Args:
        pageId: Page the trace was taken from.
        insightName: Insight identifier.
    """
    raise _unsupported(
        "performance_analyze_insight",
        "There is no trace to analyse when driving Dia.",
        "the Chrome DevTools Performance panel",
    )


@mcp.tool
def screencast_start(pageId: int | None = None, filePath: str = "") -> str:
    """Record the page as video. Not available on Dia.

    Args:
        pageId: Page to record.
        filePath: Output path.
    """
    raise _unsupported(
        "screencast_start",
        "Frame streaming needs continuous events from the tab beyond the extension bridge.",
        "dia_screenshot in a loop, or QuickTime screen recording",
    )


@mcp.tool
def screencast_stop(pageId: int | None = None) -> str:
    """Stop a screencast. Not available on Dia.

    Args:
        pageId: Page being recorded.
    """
    raise _unsupported("screencast_stop", "No screencast can be started.", "dia_screenshot")


@mcp.tool
def take_heapsnapshot(pageId: int | None = None, filePath: str = "") -> str:
    """Capture a JS heap snapshot. Not available on Dia.

    Args:
        pageId: Page to snapshot.
        filePath: Output path.
    """
    raise _unsupported(
        "take_heapsnapshot",
        "The heap profiler streams chunks over browser-level events the bridge does not carry "
        "(it also fails on Dia when using chrome-devtools-mcp directly).",
        "the Chrome DevTools Memory panel",
    )


# --------------------------------------------------------------------------
# extras beyond the chrome-devtools-mcp surface
# --------------------------------------------------------------------------
@mcp.tool
def dia_status() -> dict[str, Any]:
    """Diagnostics: attachment state, buffered traffic, page id map.

    Not part of chrome-devtools-mcp; useful when something looks wrong.
    """
    res = _call({"type": "status"}, timeout=20) or {}
    return {
        "attached_tab": _state["attached_tab"],
        "selected_page": _state["current"],
        "pages": _refresh_pages(),
        "bridge": res,
    }


@mcp.tool
def dia_detach() -> str:
    """Detach from the page and clear the debug banner.

    Not part of chrome-devtools-mcp. Call it when you are done so the user can
    open DevTools on that tab again.
    """
    _state["attached_tab"] = None
    _state["current"] = None
    _uids.clear()
    _call({"type": "detach"}, timeout=20)
    return "detached"


@mcp.tool
def dia_cdp(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Send a raw CDP command to the selected page.

    Not part of chrome-devtools-mcp. Escape hatch for anything the tools above do
    not cover.

    Args:
        method: CDP method, for example Page.bringToFront.
        params: Parameters for the method.
    """
    _use_page(None)
    return _cdp(method, params or {})


if __name__ == "__main__":
    # No banner: FastMCP prints it to stdout, which pollutes JSON-RPC for CLI clients
    # such as `mcp-inspector --cli ... | jq`.
    mcp.run(show_banner=False)
