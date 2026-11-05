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

Ask your agent: "list my Dia tabs". You should see the tabs that are open right now.

Or test the bridge directly:

```bash
python3 tools/selftest.py
```

It prints one line per check and ends with `18/18 checks passed`.

## Tools

| Tool | What it does |
|---|---|
| `dia_status` | Which tab is attached, and how many network and console entries are buffered |
| `dia_list_tabs` | Your open web tabs: ids, titles, URLs |
| `dia_attach` | Connects to one tab; leave the id out to use the tab you looked at last |
| `dia_detach` | Disconnects and removes the debug banner |
| `dia_snapshot` | The page as a list of elements (role and name), for finding what to click |
| `dia_click` | Clicks an element by CSS selector or by visible text |
| `dia_fill` | Types into a field by CSS selector |
| `dia_eval` | Runs JavaScript in the page and returns the result |
| `dia_navigate` | Sends the attached tab to a URL |
| `dia_network` | Requests the tab made, with status, type, size, and URL |
| `dia_network_body` | The response body of one request |
| `dia_console` | Console messages and JavaScript errors |
| `dia_screenshot` | A screenshot, inline or saved to a file |
| `dia_cdp` | Any raw CDP command, for anything the tools above do not cover |

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

| Capability | chrome-devtools-mcp | dia-mcp |
|---|---|---|
| Read page structure as elements | `take_snapshot` | `dia_snapshot` |
| Click, type, press keys | `click`, `fill`, `type_text` | `dia_click`, `dia_fill` |
| Run JavaScript | `evaluate_script` | `dia_eval` |
| Navigate, back, forward, reload | `navigate_page` | `dia_navigate`, `dia_eval` |
| List network requests | `list_network_requests` | `dia_network` |
| Read a response body | `get_network_request` | `dia_network_body` |
| Console messages and errors | `list_console_messages` | `dia_console` |
| Screenshots | `take_screenshot` | `dia_screenshot` |
| Device metrics, user agent, throttling | `emulate` | `dia_cdp` |
| Anything not covered | not exposed | `dia_cdp` raw passthrough |

### What dia mcp does not do

| Missing | Why | Workaround |
|---|---|---|
| Launch or own a browser | Dia cannot take a debug flag without a restart | use `chrome-devtools-mcp` for throwaway browsing |
| Lighthouse audits | not implemented | run Lighthouse in Chrome |
| Performance traces with a summary | not implemented | `dia_cdp` with the `Tracing` domain |
| Heap snapshots, screencast, PWA tools | not implemented | `dia_cdp` covers some of it |
| CSS matched rules as a tidy report | not implemented | `dia_cdp` with `CSS.getMatchedStylesForNode` |
| Parallel work on many tabs | one `chrome.debugger` attachment at a time | attach, act, attach elsewhere |
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
