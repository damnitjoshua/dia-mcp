#!/usr/bin/env python3
"""Live self-test for the Dia bridge.

Read-only where possible: it attaches to an existing tab, injects its own throwaway
test widget into that page to exercise click/fill, then removes it. It never navigates
the user's tab away and never touches their content.
"""
from __future__ import annotations

import base64
import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:9777"


def call(cmd: dict, timeout: float = 40) -> dict:
    req = urllib.request.Request(
        BASE + "/request",
        data=json.dumps({"cmd": cmd, "timeout": timeout}).encode(),
        headers={"content-type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout + 10) as r:
        return json.loads(r.read() or b"{}")


def ok(name: str, res: dict, detail: str = "") -> bool:
    good = bool(res.get("ok"))
    mark = "PASS" if good else "FAIL"
    print(f"[{mark}] {name}" + (f"  {detail}" if detail else ""))
    if not good:
        print("        ", str(res.get("error"))[:200])
    return good


def main() -> int:
    results = []

    res = call({"type": "attach"}, timeout=20)
    attached = res.get("ok")
    results.append(ok("attach to live tab", res, json.dumps(res.get("result"))[:110]))
    if not attached:
        return 1

    # Dia freezes background tabs; nothing async runs until the tab is fronted.
    res = call({"type": "cdp", "method": "Page.bringToFront", "params": {}})
    results.append(ok("focus tab (Page.bringToFront)", res))
    time.sleep(0.5)

    res = call({"type": "eval", "expression": "document.visibilityState"})
    results.append(ok("tab is visible after focus", res, str((res.get("result") or {}).get("value"))))

    res = call({"type": "eval", "expression": "new Promise(r=>setTimeout(()=>r('timer ran'),50))", "awaitPromise": True})
    results.append(ok("timers run (not frozen)", res, str((res.get("result") or {}).get("value"))))

    res = call({"type": "eval", "expression": "JSON.stringify({title:document.title,url:location.href,w:innerWidth,h:innerHeight,els:document.querySelectorAll('*').length})"})
    val = (res.get("result") or {}).get("value") if res.get("ok") else None
    results.append(ok("eval / read page state", res, str(val)[:150]))

    res = call({"type": "snapshot"})
    els = (res.get("result") or {}).get("elements") or []
    results.append(ok("snapshot (accessibility elements)", res, f"{len(els)} elements, e.g. {[e['role'] for e in els[:6]]}"))

    # Inject a private widget, fill it, click it, read the effect, then clean up.
    inject = """(function(){
      document.getElementById('dia-bridge-test')?.remove();
      const d=document.createElement('div'); d.id='dia-bridge-test';
      d.style.cssText='position:fixed;right:8px;bottom:8px;z-index:2147483647;background:#111;color:#fff;padding:8px;font:12px system-ui';
      d.innerHTML='<input id="dia-bridge-input" placeholder="x"><button id="dia-bridge-btn">Go</button><span id="dia-bridge-out"></span>';
      document.body.appendChild(d);
      document.getElementById('dia-bridge-btn').onclick=()=>{document.getElementById('dia-bridge-out').textContent='clicked:'+document.getElementById('dia-bridge-input').value;};
      return true;})()"""
    res = call({"type": "eval", "expression": inject})
    results.append(ok("inject throwaway widget", res))

    res = call({"type": "fill", "selector": "#dia-bridge-input", "value": "hello from the agent"})
    val = (res.get("result") or {}).get("value") if res.get("ok") else None
    results.append(ok("fill input via native setter", res, str(val)))

    res = call({"type": "click", "selector": "#dia-bridge-btn"})
    results.append(ok("click element", res, json.dumps(res.get("result"))[:100]))

    res = call({"type": "eval", "expression": "document.getElementById('dia-bridge-out').textContent"})
    val = (res.get("result") or {}).get("value") if res.get("ok") else None
    results.append(ok("click+fill roundtrip", res, str(val)))
    if val != "clicked:hello from the agent":
        print("        unexpected roundtrip value")
        results[-1] = False

    res = call({"type": "eval", "expression": "document.getElementById('dia-bridge-test').remove(); true"})
    results.append(ok("clean up injected widget", res))

    # Network: fire a same-origin request, then read it back plus its body.
    res = call({"type": "eval", "expression": "fetch('/manifest.json',{cache:'no-store'}).then(r=>r.status).catch(e=>'ERR '+e)", "awaitPromise": True})
    results.append(ok("fetch completes on focused tab", res, str((res.get("result") or {}).get("value"))[:40]))

    time.sleep(1)
    res = call({"type": "network", "limit": 20})
    reqs = (res.get("result") or {}).get("requests") or []
    target = next((r for r in reversed(reqs) if r.get("status")), None)
    results.append(ok("network capture", res, f"{len(reqs)} requests; last with status={str((target or {}).get('url'))[:60]} status={(target or {}).get('status')}"))

    if target and target.get("requestId"):
        res = call({"type": "body", "requestId": target["requestId"], "max": 200})
        body = (res.get("result") or {}).get("body", "") if res.get("ok") else ""
        results.append(ok("read response body", res, f"{len(body)} chars: {body[:60]!r}"))

    res = call({"type": "eval", "expression": "console.log('dia-bridge console probe'); 'ok'"})
    results.append(ok("emit console message", res))
    res = call({"type": "console", "limit": 10})
    entries = (res.get("result") or {}).get("entries") or []
    hits = [e for e in entries if "dia-bridge" in (e.get("text") or "")]
    results.append(ok("console capture", res, f"{len(entries)} entries, probe found={bool(hits)}"))

    res = call({"type": "shot", "format": "png"}, timeout=60)
    data = (res.get("result") or {}).get("base64") if res.get("ok") else None
    if data:
        raw = base64.b64decode(data)
        path = "/tmp/dia-live-selftest.png"
        with open(path, "wb") as fh:
            fh.write(raw)
        results.append(ok("screenshot", res, f"{len(raw)} bytes -> {path}"))
    else:
        results.append(ok("screenshot", res))

    res = call({"type": "detach"})
    results.append(ok("detach (banner cleared)", res))

    passed = sum(1 for r in results if r)
    print(f"\n{passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
