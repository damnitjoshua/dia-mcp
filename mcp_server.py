#!/usr/bin/env python3
"""dia-mcp: a Dia Browser MCP server built with FastMCP.

Controls the user's running Dia browser through a local bridge and a browser
extension. No launch flags, no restarts: the extension asks Dia for debugger
access on one tab at a time.

Transport is stdio, so any MCP client can run it.
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
        "Controls the Dia browser the user already has open. "
        "Start with dia_list_tabs, then dia_attach. Page tools focus the tab "
        "automatically because Dia freezes background tabs. Call dia_detach when done."
    ),
)

_state: dict[str, Any] = {"attached": False}
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
    """Start tools/bridge.py serve when nothing is listening."""
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
    """Send one command to the bridge and return its raw envelope."""
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
            f"bridge unreachable ({type(e).__name__}: {e}). Check that Dia is running "
            "with the dia-mcp extension loaded: chrome://extensions, Developer mode, "
            "Load unpacked, then pick the extension folder."
        )


def _attach_if_needed() -> None:
    """Attach once, then focus the tab, because Dia freezes background tabs."""
    if _state["attached"]:
        return
    res = _bridge({"type": "attach"}, timeout=30)
    if not res.get("ok"):
        raise ToolError(str(res.get("error") or "could not attach to a Dia tab"))
    _state["attached"] = True
    try:
        _bridge({"type": "cdp", "method": "Page.bringToFront", "params": {}}, timeout=15)
    except ToolError:
        pass


def _call(cmd: dict[str, Any], needs_tab: bool = True, timeout: float | None = None) -> Any:
    """Run a bridge command that must succeed, or raise a tool error."""
    if needs_tab:
        _attach_if_needed()
    res = _bridge(cmd, timeout)
    if not res.get("ok"):
        raise ToolError(str(res.get("error") or "unknown bridge error"))
    return res.get("result")


# --------------------------------------------------------------------------
# tools
# --------------------------------------------------------------------------
@mcp.tool
def dia_status() -> dict[str, Any]:
    """Report the bridge and attachment state.

    Use this first when something looks wrong. It says whether a tab is attached
    and how many network and console entries are buffered.
    """
    return _call({"type": "status"}, needs_tab=False)


@mcp.tool
def dia_list_tabs() -> list[dict[str, Any]]:
    """List the web tabs open in Dia right now.

    Returns tab ids, titles, URLs, and which tab is active. Privileged pages
    (chrome:// and extensions) are left out because they cannot be debugged.
    """
    return _call({"type": "list_tabs"}, needs_tab=False) or []


@mcp.tool
def dia_attach(tabId: int | None = None) -> dict[str, Any]:
    """Attach to one Dia tab and get ready to work on it.

    Args:
        tabId: Tab to attach to, taken from dia_list_tabs. Omit to use the most
            recently used web tab.
    """
    _state["attached"] = False
    res = _call({"type": "attach", "tabId": tabId}, needs_tab=False, timeout=30)
    _state["attached"] = True
    try:
        _bridge({"type": "cdp", "method": "Page.bringToFront", "params": {}}, timeout=15)
    except ToolError:
        pass
    return res or {}


@mcp.tool
def dia_detach() -> dict[str, Any]:
    """Disconnect from the tab and clear the debugger banner.

    Call this when you are done with the page, so the user can open DevTools on
    that tab again.
    """
    _state["attached"] = False
    return _call({"type": "detach"}, needs_tab=False) or {}


@mcp.tool
def dia_snapshot(filter: str | None = None, limit: int = 120) -> dict[str, Any]:
    """Read the page as a list of interactive elements.

    Each element has a role (button, link, textbox), an accessible name, and a
    backend node id. Use it to find what to click or fill.

    Args:
        filter: Only keep elements whose role or name contains this text.
        limit: Maximum number of elements to return.
    """
    res = _call({"type": "snapshot"}) or {}
    elements = res.get("elements") or []
    if filter:
        needle = filter.lower()
        elements = [
            e for e in elements
            if needle in f"{e.get('role', '')} {e.get('name', '')}".lower()
        ]
    return {"count": len(elements), "elements": elements[:limit]}


@mcp.tool
def dia_click(selector: str | None = None, text: str | None = None) -> dict[str, Any]:
    """Click an element in the attached tab.

    Args:
        selector: CSS selector for the element.
        text: Visible text of the element, used when selector is left out.
    """
    if text:
        return _call({"type": "click_text", "text": text}) or {}
    if not selector:
        raise ToolError("pass either selector or text")
    return _call({"type": "click", "selector": selector}) or {}


@mcp.tool
def dia_fill(selector: str, value: str = "") -> dict[str, Any]:
    """Type into an input or textarea in the attached tab.

    Sets the value the way a user would and fires input and change events, so
    frameworks like React notice it.

    Args:
        selector: CSS selector for the field.
        value: Text to put in the field.
    """
    return _call({"type": "fill", "selector": selector, "value": value}) or {}


@mcp.tool
def dia_eval(expression: str, awaitPromise: bool = False) -> Any:
    """Run JavaScript in the attached tab and return the result.

    The cheapest way to scrape: return a JSON string. Set awaitPromise when the
    expression returns a promise, for example a fetch.

    Args:
        expression: JavaScript to evaluate.
        awaitPromise: Wait for the returned promise to resolve.
    """
    res = _call({"type": "eval", "expression": expression, "awaitPromise": awaitPromise}) or {}
    return res.get("value")


@mcp.tool
def dia_navigate(url: str) -> dict[str, Any]:
    """Send the attached tab to a URL.

    Args:
        url: Address to load.
    """
    return _call({"type": "navigate", "url": url}, timeout=90) or {}


@mcp.tool
def dia_network(filter: str | None = None, limit: int = 50) -> dict[str, Any]:
    """List the network requests the attached tab has made.

    Entries carry method, status, type, size, and URL. Traffic is only captured
    after you attach, so reload the page first if the list is empty.

    Args:
        filter: Only keep requests whose URL contains this text.
        limit: Maximum number of requests, newest last.
    """
    return _call({"type": "network", "filter": filter, "limit": limit}) or {}


@mcp.tool
def dia_network_body(requestId: str, max: int = 4000) -> dict[str, Any]:
    """Read the response body of one captured request.

    Args:
        requestId: The requestId reported by dia_network.
        max: Characters to return.
    """
    return _call({"type": "body", "requestId": requestId, "max": max}) or {}


@mcp.tool
def dia_console(limit: int = 100) -> dict[str, Any]:
    """Read console messages and JavaScript errors from the attached tab.

    Args:
        limit: Maximum number of entries, newest last.
    """
    return _call({"type": "console", "limit": limit}) or {}


@mcp.tool
def dia_screenshot(
    path: str | None = None,
    format: Literal["png", "jpeg", "webp"] = "png",
) -> Any:
    """Take a screenshot of the attached tab.

    Args:
        path: Save the image here and return the path. Leave out to get the image
            inline instead.
        format: Image format.
    """
    res = _call({"type": "shot", "format": format}, timeout=90) or {}
    data = res.get("base64") or ""
    if not data:
        raise ToolError("the browser returned no image data")
    raw = base64.b64decode(data)
    if path:
        with open(path, "wb") as fh:
            fh.write(raw)
        return {"saved": path, "bytes": len(raw), "format": format}
    return Image(data=raw, format=format)


@mcp.tool
def dia_cdp(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Send a raw Chrome DevTools Protocol command to the attached tab.

    Use this for anything the other tools do not expose, for example
    Emulation.setDeviceMetricsOverride for a mobile viewport.

    Args:
        method: CDP method name, for example Page.bringToFront.
        params: Parameters for the method.
    """
    return _call({"type": "cdp", "method": method, "params": params or {}}) or {}


if __name__ == "__main__":
    mcp.run()
