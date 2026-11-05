# dia mcp

**Dia Browser MCP server.** Let an AI agent use the Dia browser you already have open: your
tabs, your logins, your session. No debug port, no restart, no second browser.

`dia-mcp` is the Dia equivalent of `chrome-devtools-mcp`. It exposes the Chrome DevTools
Protocol of a **live** Dia tab over MCP, so any agent client (Claude Desktop, Cursor, Hermes,
VS Code) can read pages, click, type, inspect network traffic and console errors, and take
screenshots. Built with [FastMCP](https://gofastmcp.com).

Also searched for as: Dia MCP, Dia Browser MCP, Dia MCP DevTools, Dia MCP server, Dia DevTools
MCP.

```
your agent  →  mcp_server.py  →  bridge.py (127.0.0.1:9777)  →  Dia extension  →  chrome.debugger  →  your tab
```

The browser is never launched or restarted. Dia stays exactly as it is.

## Requirements

| | |
|---|---|
| System | macOS (Dia is macOS only) |
| Browser | Dia 1.56 or newer, already open |
| Python | 3.10 or newer, plus `fastmcp` |
| Agent | any MCP client: Hermes, Claude Desktop, Cursor, VS Code, Codex |

## Setup

Three steps, about two minutes.

### 0. Install FastMCP

```bash
python3 -m pip install fastmcp
```

The bridge itself has no dependencies; only the MCP server needs FastMCP.

### 1. Start the bridge

```bash
python3 tools/bridge.py serve
```

Leave it running. This is the local channel between the extension and your agent. If you skip
it, the MCP server starts it for you on first use.

### 2. Add the extension to Dia

1. Open Dia and go to `chrome://extensions`
2. Turn on **Developer mode** in the top right corner
3. Click **Load unpacked**
4. Choose the `extension` folder inside this repo

One time step. Dia keeps it after a restart.

### 3. Connect your agent

Hermes:

```bash
hermes mcp add dia-mcp \
  --command "$(which python3)" \
  --args "$(pwd)/mcp_server.py"
```

Then run `/reload-mcp` in a session, or start a new one.

Any other MCP client, add this and replace the path:

```json
{
  "mcpServers": {
    "dia-mcp": {
      "command": "python3",
      "args": ["/absolute/path/to/mcp_server.py"]
    }
  }
}
```

## Check that it works

Ask your agent: "call list_pages and tell me the page ids". You should see the tabs that are
open right now.

Or test the bridge directly:

```bash
python3 tools/selftest.py
```

It prints one line per check and ends with `18/18 checks passed`.

## Tools

The names and arguments are the same as `chrome-devtools-mcp`, so instructions and prompts
written for that server work here unchanged. Pages are addressed by `pageId` and elements by
`uid` from `take_snapshot`, exactly as there.

**Pages**

| Tool | What it does |
|---|---|
| `list_pages` | Open pages as `pageId: title (url)` |
| `new_page` | Open a URL in a new tab and select it |
| `close_page` | Close a page |
| `select_page` | Make a page current; also focuses the tab |
| `navigate_page` | Navigate, back, forward, or reload |

**Inspection**

| Tool | What it does |
|---|---|
| `take_snapshot` | Elements with `uid`s to act on |
| `get_css_styles` | CSS rules that apply to a `uid` |

**Interaction**

| Tool | What it does |
|---|---|
| `click` | Click a `uid`, single or double |
| `fill` | Type into an input, textarea, or contenteditable |
| `fill_form` | Fill several fields at once |
| `hover` | Move the mouse over a `uid` |
| `drag` | Drag one `uid` onto another |
| `press_key` | Key or combination, for example `Control+A` |
| `type_text` | Type into whatever has focus |
| `upload_file` | Attach a local file to a file input |
| `handle_dialog` | Accept or dismiss a JS dialog |
| `wait_for` | Wait until text appears |

**Network and console**

| Tool | What it does |
|---|---|
| `list_network_requests` | Requests with status, type, size, URL |
| `get_network_request` | One request with headers and response body |
| `list_console_messages` | Console output and JS errors |
| `get_console_message` | One message in full |

**Emulation and capture**

| Tool | What it does |
|---|---|
| `emulate` | Network, CPU, geolocation, user agent, media, viewport, headers |
| `resize_page` | Viewport size (emulated, because Dia rejects window resizing) |
| `take_screenshot` | Page, `uid`, or full page; inline or to a file |
| `evaluate_script` | Run a function in the page and return the value |

**Present for parity, unavailable on Dia** (each fails with a clear message)

| Tool | Why |
|---|---|
| `lighthouse_audit` | Lighthouse needs to drive its own browser |
| `performance_start_trace`, `performance_stop_trace`, `performance_analyze_insight` | tracing needs browser-level events the extension cannot reach |
| `screencast_start`, `screencast_stop` | continuous frame streaming |
| `take_heapsnapshot` | heap profiler chunks (this also fails on Dia via chrome-devtools-mcp) |

**Extras beyond chrome-devtools-mcp**

| Tool | What it does |
|---|---|
| `dia_status` | Attachment state, buffered traffic, page map |
| `dia_detach` | Disconnect and clear the debug banner |
| `dia_cdp` | Raw CDP command escape hatch |

## dia mcp vs Chrome DevTools MCP

`chrome-devtools-mcp` is the official Chrome server. It is excellent, and on most tasks it is
the right tool. The difference is where it gets its browser.

| | chrome-devtools-mcp | dia-mcp |
|---|---|---|
| Browser it controls | Chrome, launched by the server | The Dia window you already have open |
| Setup cost | none, it starts its own instance | one `Load unpacked` click |
| Your existing tabs and logins | no, unless you relaunch with `--remote-debugging-port` | yes, immediately |
| Restart required | yes, for your own session | never |
| Headless | yes | no, Dia is a GUI app |
| Several tabs at once | yes, routed by page id | one tab at a time |

### What dia mcp does the same

Same tool names, same arguments, same `pageId` and `uid` semantics. The differences are
under the hood:

| | chrome-devtools-mcp | dia-mcp |
|---|---|---|
| Page addressing | `pageId` 1..N | `pageId` 1..N |
| Element addressing | `uid` from `take_snapshot` | `uid` from `take_snapshot` |
| Snapshot shape | indented accessibility tree | flat `uid role "name"` list |
| Click and typing | synthesised input events | `element.click()`, native value setter, plus real `Input` events for hover, drag and keys |
| One-shot HTTP calls | `get_network_request` | same, with the body included |
| Failures | tool error | `ToolError` with the reason |

### What dia mcp does not do

Every unavailable tool still exists in the list and fails with an explicit reason, so an agent
that expects the full surface gets a clear answer instead of a missing tool.

| Missing | Why | Workaround |
|---|---|---|
| Launch or own a browser | Dia cannot take a debug flag without a restart | use `chrome-devtools-mcp` for throwaway browsing |
| `lighthouse_audit` | Lighthouse needs to drive its own browser instance | run Lighthouse in Chrome |
| `performance_start_trace`, `performance_stop_trace`, `performance_analyze_insight` | tracing needs browser-level events the extension cannot reach | the DevTools Performance panel |
| `screencast_start`, `screencast_stop` | continuous frame streaming | `take_screenshot` in a loop |
| `take_heapsnapshot` | heap profiler chunks; this also fails on Dia through chrome-devtools-mcp | the DevTools Memory panel |
| Parallel work on many tabs | one `chrome.debugger` attachment at a time | act on one page, then `select_page` another |
| `chrome://` pages | the protocol forbids it | none |
| Working while DevTools is open on the tab | one debugger per tab | close DevTools on that tab |

### What about chrome-devtools-mcp pointed at Dia?

It mostly works. Launch Dia once with `--remote-debugging-port=9222`, then run that server
with `--browserUrl http://127.0.0.1:9222`. In testing on Dia 1.56, 28 of its 32 tools worked.
`resize_page` and `take_heapsnapshot` failed, and background tabs behave oddly until they are
focused. The catch is the flag: Dia ignores it when the browser is already running, so your
session has to be closed and reopened. If you are fine with that, that route gives you more
tools. If you want to attach to the browser you are in, use this repo.

## FAQ

**Is this a fork of chrome-devtools-mcp?**
No. It is a small MCP server built with FastMCP, a local bridge, and a browser extension. It
uses the same underlying protocol, `chrome.debugger`.

**Does it need `--remote-debugging-port`?**
No. That flag only works at launch. Dia accepts the request through an extension instead, so
no restart is needed.

**Can it see sites I am logged into?**
Yes, that is the point, and the trade-off. The agent only reaches the tab you attach to, and
only while the bridge is running. Call `dia_detach`, or stop the bridge, when you are done.

**Does it work with Claude Desktop, Cursor, or Hermes?**
Yes. It is a plain stdio MCP server, so any client that speaks MCP can run it.

**Windows or Linux?**
No. Dia is macOS only.

**Why does a call time out on a tab I am not looking at?**
Dia freezes background tabs, so timers and requests stall there. Every page tool focuses the
tab first. If you still see a timeout, the extension is not loaded, or the repo moved after
it was loaded.

## What is in this repo

| Path | Role |
|---|---|
| `extension/` | The Dia extension. Drives one tab at a time through `chrome.debugger` |
| `tools/bridge.py` | Local bridge, also usable from a terminal as `cmd`, `eval`, `shot` |
| `mcp_server.py` | FastMCP server your agent connects to; starts the bridge if needed |
| `requirements.txt` | The one Python dependency: `fastmcp` |
| `tools/selftest.py` | 18 live checks against your own browser |
| `AGENTS.md` | Instructions for an AI agent to do this setup for you |

## License

MIT. Use it, change it, ship it, sell it. Keep the copyright notice with the code.
