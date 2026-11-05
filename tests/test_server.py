#!/usr/bin/env python3
"""Tests for dia-mcp, in the shape FastMCP documents (Client + pytest).

Two ways to run:

    python tests/test_server.py                 # no extra dependencies
    pip install pytest pytest-asyncio && pytest # with asyncio_mode = auto

The offline tests stub the bridge, so they never touch Dia. The live tests are
skipped unless DIA_LIVE_TESTS=1 and the extension is loaded.
"""
from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import os
import sys

from fastmcp import Client
from fastmcp.exceptions import ToolError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "mcp_server.py")

_spec = importlib.util.spec_from_file_location("dia_server", SERVER)
assert _spec is not None and _spec.loader is not None
server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(server)

CHROME_TOOLS = """click close_page drag emulate evaluate_script fill fill_form get_console_message
get_css_styles get_network_request handle_dialog hover lighthouse_audit list_console_messages
list_network_requests list_pages navigate_page new_page performance_analyze_insight
performance_start_trace performance_stop_trace press_key resize_page screencast_start
screencast_stop select_page take_heapsnapshot take_screenshot take_snapshot type_text
upload_file wait_for""".split()

TABS = [
    {"tabId": 101, "title": "Example", "url": "https://example.com/", "active": True},
    {"tabId": 202, "title": "Other", "url": "https://other.test/", "active": False},
]
SNAPSHOT = {"elements": [
    {"role": "RootWebArea", "name": "Example", "backendNodeId": 11},
    {"role": "textbox", "name": "Email", "backendNodeId": 22},
    {"role": "button", "name": "Sign in", "backendNodeId": 33},
]}
NETWORK = {"requests": [
    {"requestId": "r1", "method": "GET", "status": 200, "type": "Document", "bytes": 120,
     "url": "https://example.com/"},
    {"requestId": "r2", "method": "POST", "status": 201, "type": "Fetch", "bytes": 8,
     "url": "https://example.com/api/login"},
]}
CONSOLE = {"entries": [
    {"level": "error", "text": "boom"},
    {"level": "log", "text": "hello"},
]}
PNG = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGA"
       "hKmMIQAAAABJRU5ErkJggg==")


class Stub:
    """Records bridge commands and answers them like a healthy Dia would."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def call(self, cmd, timeout=None):
        self.calls.append(cmd)
        kind = cmd.get("type")
        if kind == "list_tabs":
            return list(TABS)
        if kind == "attach":
            return {"tabId": cmd.get("tabId")}
        if kind == "create_tab":
            return {"tabId": 303}
        if kind == "snapshot":
            return SNAPSHOT
        if kind == "network":
            return NETWORK
        if kind == "body":
            return {"body": "<html>ok</html>"}
        if kind == "console":
            return CONSOLE
        if kind == "eval":
            return {"value": "ok"}
        if kind == "status":
            return {"attachedTabId": None, "network": 2, "console": 2}
        if kind == "cdp":
            return self.cdp(cmd.get("method"), cmd.get("params"))
        return {}

    def cdp(self, method, params=None, timeout=None):
        self.calls.append({"type": "cdp-inner", "method": method, "params": params or {}})
        replies = {
            "DOM.resolveNode": {"object": {"objectId": "obj-1"}},
            "Runtime.callFunctionOn": {"result": {"value": "Sign in"}},
            "DOM.getBoxModel": {"model": {"content": [10, 20, 60, 20, 60, 40, 10, 40]}},
            "DOM.pushNodesByBackendIdsToFrontend": {"nodeIds": [7]},
            "CSS.getMatchedStylesForNode": {"matchedCSSRules": [
                {"rule": {"selectorList": {"text": "button.primary"},
                          "style": {"cssProperties": [{"name": "color", "value": "red"}]}}}]},
            "Page.captureScreenshot": {"data": PNG},
            "DOM.getDocument": {"root": {"backendNodeId": 1}},
        }
        return replies.get(method, {})

    def methods(self) -> list[str]:
        return [c["method"] for c in self.calls if c["type"] == "cdp-inner"]


def reset() -> None:
    server._pages.clear()
    server._tab_to_page.clear()
    server._uids.clear()
    server._net_ids.clear()
    server._console_ids.clear()
    server._state["attached_tab"] = None
    server._state["current"] = None
    setattr(server, "_next_page_id", 1)


def install_stub() -> Stub:
    stub = Stub()
    reset()
    setattr(server, "_call", stub.call)
    setattr(server, "_cdp", stub.cdp)
    return stub


ORIGINAL_CALL = server._call
ORIGINAL_CDP = server._cdp


def uninstall_stub() -> None:
    """Restore the real bridge transport, so the live tests talk to Dia."""
    setattr(server, "_call", ORIGINAL_CALL)
    setattr(server, "_cdp", ORIGINAL_CDP)
    reset()


async def call(name: str, args: dict | None = None):
    """Call a tool through an in-memory client, returning text or the ToolError."""
    async with Client(server.mcp) as client:
        try:
            result = await client.call_tool(name, args or {})
        except ToolError as e:
            return e
    return result


def text_of(result) -> str:
    if isinstance(result, Exception):
        return str(result)
    for block in result.content:
        if getattr(block, "type", None) == "text":
            return block.text
    return ""


async def tool_names() -> list[str]:
    async with Client(server.mcp) as client:
        return sorted(t.name for t in await client.list_tools())


def tool_schema(name: str) -> dict:
    return server.mcp._tool_manager._tools[name].parameters if hasattr(server.mcp, "_tool_manager") else {}


# ---------------------------------------------------------------- offline tests
async def test_every_chrome_devtools_tool_is_present():
    names = await tool_names()
    missing = [n for n in CHROME_TOOLS if n not in names]
    assert not missing, f"missing chrome-devtools-mcp tools: {missing}"
    assert len(names) == 35, names


async def test_tool_schemas_expose_page_id_and_uid():
    async with Client(server.mcp) as client:
        tools = {t.name: t for t in await client.list_tools()}

    def schema(tool) -> dict:
        return getattr(tool, "input_schema", None) or tool.inputSchema

    click_props = schema(tools["click"])["properties"]
    assert "pageId" in click_props and "uid" in click_props
    navigate = schema(tools["navigate_page"])["properties"]["type"]
    assert navigate["enum"] == ["url", "back", "forward", "reload"]
    assert schema(tools["list_pages"])["properties"] == {}


async def test_list_pages_returns_page_ids():
    install_stub()
    text = text_of(await call("list_pages"))
    assert text.startswith("## Pages")
    assert "1: Example (https://example.com/)" in text


async def test_take_snapshot_mints_uids_per_page():
    install_stub()
    text = text_of(await call("take_snapshot", {"pageId": 1}))
    assert 'uid=1_1 RootWebArea "Example"' in text
    assert 'uid=1_3 button "Sign in"' in text
    assert server._uids["1_2"] == 22


async def test_click_resolves_uid_through_the_dom_agent():
    stub = install_stub()
    await call("take_snapshot", {"pageId": 1})
    text = text_of(await call("click", {"pageId": 1, "uid": "1_3"}))
    assert "clicked uid=1_3" in text, text
    methods = stub.methods()
    for expected in ("DOM.getDocument", "DOM.resolveNode", "Runtime.callFunctionOn"):
        assert expected in methods, methods


async def test_fill_sets_the_native_value():
    stub = install_stub()
    await call("take_snapshot", {"pageId": 1})
    text = text_of(await call("fill", {"pageId": 1, "uid": "1_2", "value": "tomsmith"}))
    assert "filled uid=1_2" in text, text
    call_fn = [c for c in stub.calls if c.get("method") == "Runtime.callFunctionOn"][-1]
    assert call_fn["params"]["arguments"] == [{"value": "tomsmith"}]


async def test_unknown_uid_is_a_clear_error():
    install_stub()
    result = await call("click", {"uid": "77_1"})
    assert isinstance(result, ToolError)
    assert "take_snapshot" in str(result)


async def test_network_reqids_and_type_filter():
    install_stub()
    text = text_of(await call("list_network_requests", {"pageId": 1, "pageSize": 10}))
    assert "reqid=2 POST https://example.com/api/login [201, Fetch, 8 bytes]" in text, text
    filtered = text_of(await call("list_network_requests", {"pageId": 1, "types": ["document"]}))
    assert "api/login" not in filtered and "reqid=1" in filtered
    detail = text_of(await call("get_network_request", {"pageId": 1, "reqid": 2}))
    assert "## Request 2" in detail and "<html>ok</html>" in detail, detail


async def test_console_messages_and_levels():
    install_stub()
    text = text_of(await call("list_console_messages", {"pageId": 1}))
    assert "msgid=1 [error] boom" in text, text
    only_errors = text_of(await call("list_console_messages", {"pageId": 1, "types": ["error"]}))
    assert "hello" not in only_errors


async def test_screenshot_returns_an_image():
    install_stub()
    result = await call("take_screenshot", {"pageId": 1, "format": "png"})
    assert not isinstance(result, Exception), result
    assert result.content[0].type == "image"


async def test_css_styles_are_formatted():
    install_stub()
    await call("take_snapshot", {"pageId": 1})
    text = text_of(await call("get_css_styles", {"pageId": 1, "uid": "1_3"}))
    assert "button.primary {" in text and "color: red;" in text, text


async def test_unavailable_tools_explain_themselves():
    install_stub()
    for name in ("lighthouse_audit", "performance_start_trace", "screencast_start",
                 "take_heapsnapshot"):
        result = await call(name)
        assert isinstance(result, ToolError), name
        assert "not available when driving Dia" in str(result), name


async def test_extras_are_the_only_ones_beyond_chrome():
    names = set(await tool_names())
    extras = names - set(CHROME_TOOLS)
    assert extras == {"dia_status", "dia_detach", "dia_cdp"}, extras


# ------------------------------------------------------------------ live tests
async def test_live_list_pages():
    if os.environ.get("DIA_LIVE_TESTS") != "1":
        return "skipped: set DIA_LIVE_TESTS=1 with Dia and the extension running"
    uninstall_stub()
    text = text_of(await call("list_pages"))
    assert text.startswith("## Pages") and "timeout waiting for extension" not in text
    return text.splitlines()[1][:80]


async def test_live_status_reports_the_bridge():
    if os.environ.get("DIA_LIVE_TESTS") != "1":
        return "skipped: set DIA_LIVE_TESTS=1"
    uninstall_stub()
    payload = text_of(await call("dia_status"))
    data = json.loads(payload)
    assert "attached_tab" in data and "bridge" in data
    return f"attached={data['attached_tab']} pages={len(data.get('pages') or {})}"


# -------------------------------------------------------------------- no pytest
async def run_all() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and inspect.iscoroutinefunction(f)]
    failures = 0
    for name, fn in tests:
        try:
            detail = await fn()
            print(f"[PASS] {name}" + (f"  {detail}" if isinstance(detail, str) and detail else ""))
        except AssertionError as e:
            failures += 1
            print(f"[FAIL] {name}  {e}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"[ERROR] {name}  {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} tests passed")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    if "--live" in sys.argv:
        os.environ["DIA_LIVE_TESTS"] = "1"
    sys.exit(asyncio.run(run_all()))
