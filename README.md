# dia mcp

Let an AI agent use the Dia browser you already have open.

Most browser tools start their own copy of Chrome. dia mcp connects to the Dia window on
your screen instead, so the agent works with your real tabs and your real logins. An agent
can read a page, click and type, watch network requests, read console errors, and take
screenshots.

```
your agent  →  mcp_server.py  →  bridge.py (127.0.0.1:9777)  →  Dia extension  →  chrome.debugger  →  your tab
```

Nothing is launched or restarted. Dia stays exactly as it is.

## Requirements

| | |
|---|---|
| System | macOS |
| Browser | Dia 1.56 or newer, already open |
| Python | 3.9 or newer (no packages to install) |
| Agent | any MCP client, for example Hermes, Claude Desktop, Cursor |

## Setup

Three steps, about two minutes.

### 1. Start the bridge

```bash
python3 tools/bridge.py serve
```

Leave it running. This is the local channel between the extension and your agent.

If you skip this step the MCP server will start it for you the first time an agent calls a
tool, so it is optional.

### 2. Add the extension to Dia

1. Open Dia and go to `chrome://extensions`
2. Turn on **Developer mode** in the top right corner
3. Click **Load unpacked**
4. Choose the `extension` folder inside this repo

That is a one time step. Dia keeps the extension after a restart.

### 3. Connect your agent

For Hermes:

```bash
hermes mcp add dia-mcp \
  --command "$(which python3)" \
  --args "$(pwd)/mcp_server.py"
```

Then run `/reload-mcp` in a session, or start a new session.

For any other MCP client, add this to its config file and replace the path:

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

Ask your agent: "list my Dia tabs". You should get the tabs that are open right now.

You can also test the bridge on its own:

```bash
python3 tools/selftest.py
```

It prints one line per check and should end with `18/18 checks passed`.

## Tools

| Tool | What it does |
|---|---|
| `dia_status` | Shows which tab is attached and how many network and console entries are buffered |
| `dia_list_tabs` | Lists your open web tabs with their ids, titles, and URLs |
| `dia_attach` | Connects to one tab; leave the id out to use the tab you looked at last |
| `dia_detach` | Disconnects and removes the debug banner |
| `dia_snapshot` | Reads the page as a list of elements (role and name), good for finding what to click |
| `dia_click` | Clicks an element by CSS selector or by its visible text |
| `dia_fill` | Types into a field by CSS selector |
| `dia_eval` | Runs JavaScript in the page and returns the result |
| `dia_navigate` | Sends the attached tab to a URL |
| `dia_network` | Lists the requests the tab made, with status, type, and size |
| `dia_network_body` | Returns the response body of one request |
| `dia_console` | Returns console messages and JavaScript errors |
| `dia_screenshot` | Takes a screenshot, shown inline or saved to a file |
| `dia_cdp` | Sends any raw CDP command when you need something the tools above do not cover |

## What is in this repo

| Path | Role |
|---|---|
| `extension/` | The Dia extension. Talks to the bridge and drives one tab at a time |
| `tools/bridge.py` | The local bridge. Also usable from a terminal as `cmd`, `eval`, or `shot` |
| `mcp_server.py` | The MCP server your agent connects to; starts the bridge if needed |
| `tools/selftest.py` | 18 live checks against your own browser |
| `AGENTS.md` | Instructions for an AI agent to do this setup for you |

## License

MIT. Use it, change it, ship it, sell it. Keep the copyright notice with the code.
