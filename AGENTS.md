# AGENTS.md

Setup instructions for an AI agent. Read this before touching anything.

Your job: get `dia-mcp` installed and working on this machine, then prove it with a real
test. The user should end up with an agent that can read and drive their own Dia tabs.

## Guardrails

- Do not quit, restart, or relaunch Dia. This project attaches to the running browser; a
  restart is never required and destroys the user's session.
- Do not read, copy, or upload the user's browsing data. Only touch pages the user asks
  about.
- Ask before editing any agent config file (`~/.hermes/config.yaml`, Claude Desktop config,
  and similar). Show the exact block first.
- One step needs the user's hands: loading the unpacked extension in Dia. Never pretend it
  is done; ask them to do it and wait.
- Point the extension at the folder in place. Unpacked extensions store absolute paths, so a
  later move of this repo breaks the extension until it is removed and re-added.

## Preflight

```bash
uname -s                                  # expect: Darwin
ls -d /Applications/Dia.app               # Dia must exist
/usr/libexec/PlistBuddy -c "Print :CFBundleShortVersionString" \
  /Applications/Dia.app/Contents/Info.plist   # expect 1.56 or newer
python3 --version                         # expect 3.10 or newer
python3 -c "import fastmcp; print(fastmcp.__version__)"   # expect 4.x, else pip install fastmcp
```

If Dia is missing, stop and tell the user. The extension cannot be loaded anywhere else.

## Step 0: install FastMCP

```bash
python3 -m pip install fastmcp            # the server is built with FastMCP
```

The bridge uses only the standard library; only `mcp_server.py` needs FastMCP.

## Step 1: start the bridge

```bash
python3 tools/bridge.py serve
```

Run it in the background and keep it alive. Then verify:

```bash
curl -s http://127.0.0.1:9777/health        # expect {"ok": true, ...}
```

If the port is already taken by an older copy, that is fine, leave it alone.

## Step 2: load the extension (ask the user)

Tell the user, in their own words, to do this:

1. Open Dia, go to `chrome://extensions`
2. Turn on Developer mode
3. Click "Load unpacked" and pick the `extension` folder from this repo

Then verify that the extension is talking to the bridge:

```bash
curl -s -X POST http://127.0.0.1:9777/request \
  -H 'content-type: application/json' \
  -d '{"cmd":{"type":"status"},"timeout":15}'
```

Expect `"ok": true`. `timeout waiting for extension` means the extension is not loaded, or
was loaded from a path that has since moved.

## Step 3: register the MCP server

Hermes:

```bash
hermes mcp add dia-mcp --command "$(which python3)" --args "$(pwd)/mcp_server.py"
```

Other clients: add this to their config and replace the path with the absolute one.

```json
{"mcpServers": {"dia-mcp": {"command": "python3", "args": ["/absolute/path/to/mcp_server.py"]}}}
```

The server has no dependencies. It starts the bridge itself if step 1 is skipped.

## Step 4: prove it works

```bash
python3 tools/selftest.py
```

Pass condition: `18/18 checks passed`. It attaches to the user's most recent tab, focuses it,
reads the page, injects and removes its own test widget, fires one request, reads the
response body, captures console output, screenshots, and detaches.

If the user's agent is already live, also ask it to list the Dia tabs and compare with what
is on screen.

## Rules the tools already handle, and what they mean

| Behaviour | Consequence |
|---|---|
| Dia freezes background tabs (timers, fetches, awaited JS stall) | Every page tool fronts the tab first. If a call times out, the tab was not focused. |
| One debugger per tab | If DevTools is open on the tab, attach fails. Close DevTools or use another tab. |
| `chrome://` and extension pages cannot be attached | They are filtered out of `dia_list_tabs` by design. |
| Attachments can drop when Dia suspends the extension worker | Re-run `dia_attach`. |
| Dia's content blocker cancels some requests | They appear as `net::ERR_BLOCKED_BY_CONTENT_BLOCKER`; not a bug. |

## Failure table

| Symptom | Cause | Fix |
|---|---|---|
| `timeout waiting for extension` | extension not loaded, or repo moved after loading | reload the extension from the current path |
| `Another debugger is already attached` | DevTools open on that tab | close DevTools or attach elsewhere |
| `not attached to a tab` | worker slept, or tab closed | `dia_attach` again |
| `cannot attach to chrome://...` | privileged page | pick a regular web tab |
| `dia_network` is empty | attached after the traffic happened | reload the page, then read again |
| Bridge not reachable | process died | restart `tools/bridge.py serve` |

## Done means

- `curl /health` returns ok
- the status call returns `attachedTabId` (or `null` before first use, which is fine)
- `tools/selftest.py` prints 18/18
- the user's agent lists the correct tabs
