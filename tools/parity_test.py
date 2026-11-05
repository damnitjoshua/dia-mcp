#!/usr/bin/env python3
"""Live parity test for the chrome-devtools-mcp-compatible tools.

Drives the MCP tools directly (no client needed) against the running Dia bridge.
It works on a page it opens itself, injects a throwaway widget, exercises it, then
closes the page. Your other tabs are only read, never modified.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(os.path.dirname(HERE), "mcp_server.py")

spec = importlib.util.spec_from_file_location("dia_mcp_server", SERVER)
assert spec is not None and spec.loader is not None, f"cannot load {SERVER}"
mcp_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mcp_mod)

results: list[tuple[str, bool, str]] = []


def check(name: str, fn) -> None:
    try:
        detail = fn()
        results.append((name, True, str(detail)[:140]))
        print(f"[PASS] {name}  {str(detail)[:110]}")
    except Exception as e:
        results.append((name, False, f"{type(e).__name__}: {e}"))
        print(f"[FAIL] {name}  {type(e).__name__}: {str(e)[:140]}")


def find_uid(snapshot: str, needle: str) -> str:
    for line in snapshot.splitlines():
        if needle in line:
            hit = re.search(r"uid=(\S+)", line)
            if hit:
                return hit.group(1)
    raise RuntimeError(f"no uid containing {needle!r}")


def main() -> int:
    print("== pages ==")
    check("list_pages", lambda: mcp_mod.list_pages().replace("\n", " | ")[:120])

    state = {}

    def open_page():
        text = mcp_mod.new_page("https://example.com/")
        first = text.splitlines()[1]
        state["page"] = int(first.split(":")[0])
        if "[selected]" not in first:
            raise RuntimeError(f"new page not selected: {first}")
        return f"page {state['page']} open and selected"

    check("new_page", open_page)
    page = state.get("page")

    def restore_viewport():
        mcp_mod.resize_page(pageId=page, width=1280, height=800)
        return "1280x800"

    check("resize_page", restore_viewport)
    check("emulate viewport", lambda: mcp_mod.emulate(pageId=page, viewport="1024x768", colorScheme="dark"))

    def inject():
        js = """() => {
          document.getElementById('dia-parity')?.remove();
          const d = document.createElement('div');
          d.id = 'dia-parity';
          d.style.cssText = 'position:fixed;left:8px;bottom:8px;z-index:2147483647;background:#111;color:#fff;padding:8px';
          d.innerHTML = '<input id="dia-parity-input" aria-label="Dia parity field">' +
                        '<button id="dia-parity-btn" aria-label="Dia parity button">Go</button>' +
                        '<span id="dia-parity-out"></span>' +
                        '<input id="dia-parity-file" type="file" aria-label="Dia parity file">';
          document.body.appendChild(d);
          document.getElementById('dia-parity-btn').onclick = () => {
            document.getElementById('dia-parity-out').textContent =
              'clicked:' + document.getElementById('dia-parity-input').value;
          };
          return true;
        }"""
        mcp_mod.evaluate_script(pageId=page, function=js)
        return "widget injected"

    check("evaluate_script (inject)", inject)

    snap = {}

    def snapshot():
        snap["text"] = mcp_mod.take_snapshot(pageId=page)
        return f"{len(snap['text'].splitlines()) - 1} elements"

    check("take_snapshot", snapshot)

    def fill():
        uid = find_uid(snap["text"], "Dia parity field")
        mcp_mod.fill(pageId=page, uid=uid, value="hello parity")
        return f"filled {uid}"

    check("fill (by uid)", fill)

    def click():
        uid = find_uid(snap["text"], "Dia parity button")
        mcp_mod.click(pageId=page, uid=uid)
        out = mcp_mod.evaluate_script(pageId=page, function="() => document.getElementById('dia-parity-out').textContent")
        value = json.loads(out)["result"]
        if value != "clicked:hello parity":
            raise RuntimeError(f"roundtrip was {value!r}")
        return value

    check("click (by uid) and roundtrip", click)
    check("hover", lambda: mcp_mod.hover(pageId=page, uid=find_uid(snap["text"], "Dia parity button")))
    check("press_key", lambda: mcp_mod.press_key(pageId=page, key="Tab"))
    check("type_text", lambda: mcp_mod.type_text(pageId=page, text="typed"))

    def css():
        text = mcp_mod.get_css_styles(pageId=page, uid=find_uid(snap["text"], "Dia parity button"))
        if "## Styles for" not in text:
            raise RuntimeError("no styles section")
        return f"{len(text.splitlines())} lines"

    check("get_css_styles", css)

    def upload():
        probe = "/tmp/dia-mcp-parity-upload.txt"
        with open(probe, "w") as fh:
            fh.write("parity")
        mcp_mod.upload_file(pageId=page, uid=find_uid(snap["text"], "Dia parity file"), filePath=probe)
        out = mcp_mod.evaluate_script(
            pageId=page,
            function="() => document.getElementById('dia-parity-file').files.length",
        )
        count = json.loads(out)["result"]
        if count != 1:
            raise RuntimeError(f"file input has {count} files")
        return "1 file attached"

    check("upload_file", upload)

    def network():
        mcp_mod.evaluate_script(pageId=page, function="() => fetch('/manifest.json?x=1', {cache:'no-store'}).then(r => r.status)")
        time.sleep(1.2)
        listing = mcp_mod.list_network_requests(pageId=page, pageSize=100)
        if "reqid=" not in listing:
            raise RuntimeError("no requests captured")
        detail = mcp_mod.get_network_request(pageId=page)
        if "## Request" not in detail:
            raise RuntimeError("request detail missing")
        return listing.splitlines()[0]

    check("list_network_requests + get_network_request", network)

    def console():
        mcp_mod.evaluate_script(pageId=page, function="() => { console.error('dia parity probe'); return 1; }")
        listing = mcp_mod.list_console_messages(pageId=page, pageSize=50)
        if "dia parity probe" not in listing:
            raise RuntimeError("console probe not captured")
        return listing.splitlines()[0]

    check("list_console_messages", console)

    def wait_for():
        mcp_mod.evaluate_script(pageId=page, function="() => { document.title = 'parity ready'; return 1; }")
        return mcp_mod.wait_for(pageId=page, text=["nonexistent-needle", "Example Domain"], timeout=3000)

    check("wait_for", wait_for)
    check("take_screenshot", lambda: mcp_mod.take_screenshot(pageId=page, filePath="/tmp/dia-mcp-parity.png"))

    def unsupported():
        try:
            mcp_mod.lighthouse_audit(pageId=page)
        except Exception as e:
            if "not available when driving Dia" in str(e):
                return "lighthouse fails with a clear reason"
        raise RuntimeError("lighthouse did not fail as expected")

    check("unsupported tool reports a reason", unsupported)

    def cleanup():
        mcp_mod.evaluate_script(pageId=page, function="() => { document.getElementById('dia-parity')?.remove(); return 1; }")
        mcp_mod.close_page(pageId=page)
        return "page closed"

    check("close_page", cleanup)
    check("dia_detach", lambda: mcp_mod.dia_detach())

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{passed}/{len(results)} parity checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
