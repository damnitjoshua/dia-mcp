# Dia Live Bridge

Control the **running** Dia browser from an agent: no `--remote-debugging-port`, no restart,
no second profile. Works on the tabs you already have open, with your existing logins.

```
agent → mcp_server.py (MCP stdio, 14 tools) → bridge.py :9777 → Dia extension → chrome.debugger → live tab
```

## Why not the CDP port

Chromium only accepts `--remote-debugging-port` at launch, and Dia ignores `open -a Dia --args`
when it is already running. Dia's `chrome://inspect#remote-debugging` toggle exists but writes
no `DevToolsActivePort`, so `chrome-devtools-mcp --autoConnect` has nothing to discover. An
extension with the `debugger` permission is the only way into a session that is already live.

## Files

| Path | Role |
|---|---|
| `extension/` | MV3 extension, loaded unpacked in Dia. Long-polls the bridge, executes CDP via `chrome.debugger`. |
| `tools/bridge.py` | Local command channel (`serve`) and CLI (`cmd`, `eval`, `shot`). |
| `mcp_server.py` | MCP server exposing 14 tools; auto-starts the bridge if it is down. |
| `tools/selftest.py` | 18 live checks. Read-only apart from an injected throwaway widget. |

## Install

```bash
# 1. bridge (optional: mcp_server.py starts it on demand)
python3 tools/bridge.py serve

# 2. Dia → chrome://extensions → Developer mode → Load unpacked → this repo's extension/

# 3. register with Hermes (any MCP client works: this is a plain stdio server)
hermes mcp add dia-mcp \
  --command "$(pwd)/../env/.venv/bin/python" \
  --args "$(pwd)/mcp_server.py"
# then /reload-mcp in a session, or start a new one
```

No Python dependencies, stdlib only.

## Tools

`dia_status`, `dia_list_tabs`, `dia_attach`, `dia_detach`, `dia_snapshot`, `dia_click`,
`dia_fill`, `dia_eval`, `dia_navigate`, `dia_network`, `dia_network_body`, `dia_console`,
`dia_screenshot`, `dia_cdp`.

## Gotchas (learned the hard way)

- **Dia freezes background tabs.** Not frontmost means `visibilityState: hidden`, timers
  frozen, `fetch` never resolving, `awaitPromise` evaluations timing out. Attach and every
  page-scoped tool call `Page.bringToFront`; manual `dia_cdp` callers must do it themselves.
- **One debugger per tab.** DevTools open on the tab blocks attach.
- **`chrome://` and extension pages are off limits** to `chrome.debugger`.
- **The attachment can drop** when Dia suspends the extension worker; re-attach on
  `not attached to a tab`.
- **Detach when done** so the "being debugged" banner goes away.
- **Some requests are killed by Dia's content blocker** (`net::ERR_BLOCKED_BY_CONTENT_BLOCKER`).
- Editing `extension/` needs a reload click in `chrome://extensions`; `bridge.py` and
  `mcp_server.py` changes only need that process restarted.
