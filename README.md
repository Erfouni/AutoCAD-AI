# AutoCAD MCP server

Lets an AI assistant draw in the AutoCAD running on this machine. A small Python
server exposes AutoCAD's drawing commands as MCP tools over HTTP; a tunnel makes
that reachable by cloud assistants like ChatGPT and Manus.

Built and verified on 2026-07-30 against AutoCAD 2027 (R26.0), Python 3.13.

## How it fits together

```
ChatGPT / Manus  --HTTPS-->  ngrok  -->  127.0.0.1:8770  -->  AutoCAD 2027
                                          server.py           (via COM)
```

The server talks to AutoCAD through COM, which means AutoCAD has to be running
on this machine with a drawing open, and the server has to run as the same
Windows user.

## Relationship to the Codex AutoCAD project

There is a second, much larger AutoCAD MCP system on this Desktop under
`AUTOCAD Codex` (C#/.NET, native AutoCAD plug-in, OAuth, per-change approvals).
It listens on **8765**. This project is deliberately simpler and listens on
**8770** so the two can coexist. Only one ngrok agent can run at a time on the
free plan, so only one of them can be tunnelled at any moment.

Pick this one when you want an AI to just draw without approving every change.
Pick Codex when you want the safety controls.

## Setup

```bash
pip install -r requirements.txt
```

Both dependencies were already installed on this machine; the file pins the
versions this was verified against.

## Running it

1. Open AutoCAD and make sure a drawing is open.
2. Start the server:

```bash
powershell -ExecutionPolicy Bypass -File .\run.ps1
```

It prints its URL, which contains a long random path segment — that path *is*
the password, so the whole URL is a secret. It is generated once and saved to
`.secret`, so restarting the server does not invalidate URLs you already gave to
ChatGPT or Manus.

3. **Connect the VPN**, then open the tunnel in a second window:

```bash
powershell -ExecutionPolicy Bypass -File .\start_ngrok.ps1
```

It prints the full public URL to paste into your AI client.

> The VPN is not optional. ngrok refuses agent connections from Iranian IP
> addresses (`ERR_NGROK_9040`), and the tunnel dies whenever the VPN drops.

## Connecting an AI client

**ChatGPT** (needs Plus or Pro, web only): Settings → Apps & Connectors →
Advanced settings → turn on Developer mode. Back in Apps & Connectors, add a
custom connector with the URL from `start_ngrok.ps1` and authentication set to
**No authentication** — ChatGPT connectors cannot send a custom header, which is
exactly why the secret lives in the URL. In a chat, enable it from the `+` menu.

**Manus**: Settings → Connectors → Add connectors → Custom MCP → Direct
configuration. Same URL. If you set `ACAD_MCP_AUTH_TOKEN`, add the header
`Authorization: Bearer <token>` as well.

**Claude Code on this machine** needs no tunnel at all — point it straight at
the local URL:

```json
{ "mcpServers": { "autocad": { "type": "http", "url": "http://127.0.0.1:8770/<secret>/mcp" } } }
```

## What the AI can do

24 tools. Coordinates are drawing units, angles are degrees, everything is 2D at
Z=0. Drawing tools return an entity handle, which the query, hatch and erase
tools take.

| Area | Tools |
|---|---|
| Draw | `draw_line` `draw_polyline` `draw_rectangle` `draw_circle` `draw_arc` `draw_text` `draw_hatch` |
| Batch | `draw_batch` — many shapes in one call |
| Dimensions | `dim_linear` `dim_aligned` |
| Layers | `create_layer` `set_current_layer` |
| Inspect | `list_entities` `get_entity_info` `get_drawing_info` **`capture_view`** |
| Edit | `erase_entity` |
| Files | `new_drawing` `save_drawing` `open_drawing` `export_pdf` |
| Lifecycle | `autocad_status` `start_autocad` `close_autocad` |
| Command line | `send_command` — only when `ACAD_MCP_ENABLE_SEND_COMMAND=1` |

The tool set is deliberately small. Every tool's schema is loaded into the model's
context on every single request, so a tool that is rarely the right answer makes the
model measurably worse at choosing between the ones that are. Ellipses, point markers,
paragraph text and standalone zoom were removed for that reason — `draw_batch` still
accepts `mtext` and `point` operations, `capture_view` zooms to fit on its own, and
anything genuinely exotic is one `send_command` away.

### Seeing the drawing

`capture_view` is what turns this from a one-way pipe into a loop. It plots the
current view through AutoCAD's `PublishToWeb PNG.pc3` raster plotter, downscales the
result with Pillow, and returns it as an actual image in the tool response — so the
model looks at the drawing rather than guessing from coordinates.

A typical view costs about 5 KB at 1100 px, which is cheap enough to call after every
drawing step. The plot rotation is pinned to zero deliberately; left alone AutoCAD
rotates the view to fit the paper and the model then reasons about a sideways drawing.
The tool also restores the layout's previous plotter config, so `export_pdf` keeps
working afterwards.

`draw_batch` matters more than it looks: every tool call is a network round trip
to a cloud AI, so drawing a floor plan one line at a time is painfully slow.
One batch call draws the lot.

## Configuration

All optional, all environment variables.

| Variable | Default | Purpose |
|---|---|---|
| `ACAD_MCP_PORT` | `8770` | Listen port. 8765 is Codex, 8766 is Windows-reserved here. |
| `ACAD_MCP_HOST` | `127.0.0.1` | Leave it. Binding wider exposes AutoCAD to your LAN. |
| `ACAD_MCP_SAVE_DIR` | `%USERPROFILE%\AutoCAD-MCP-Out` | The only folder the server may write to. Kept out of OneDrive because sync locks files AutoCAD has just written. |
| `ACAD_MCP_AUTH_TOKEN` | *(unset)* | If set, any request that *does* send an `Authorization` header must match it. |
| `ACAD_MCP_SECRET_PATH` | from `.secret` | Override the secret URL segment. |
| `ACAD_MCP_ENABLE_SEND_COMMAND` | *(off)* | `1` adds a tool that runs arbitrary AutoCAD commands and AutoLISP. See the warning below. |
| `ACAD_MCP_COM_TIMEOUT` | `120` | Seconds before a stuck AutoCAD call gives up. |

## Security

The honest summary: anyone who has the URL can draw in your AutoCAD and write
files into the output folder.

- The secret path is the real gate, because ChatGPT connectors support only
  OAuth or no authentication and cannot send a token header.
- The server listens on loopback only, so the tunnel is the sole way in.
- File writes are confined to `ACAD_MCP_SAVE_DIR`; path traversal is stripped.
- `send_command` is off by default. Turning it on lets a remote AI run arbitrary
  AutoLISP, which is equivalent to running code on this machine. Leave it off
  unless you have a specific reason.
- Every tool call is logged to `logs/toolcalls.jsonl` with its arguments.
- If the URL leaks, delete `.secret`, restart, and re-paste the new URL.

Unrelated but worth doing: the ngrok authtoken sits in plaintext in
`%USERPROFILE%\mcp_setup\ngrok.yml`. Rotate it at dashboard.ngrok.com.

## Testing without any AI

```bash
python test_draw.py
```

Draws a rectangle, circle, arc, text, dimension and hatch straight through the
COM bridge — no MCP, no network. If this fails, nothing else will work, and the
failure message tells you why.

## Troubleshooting

**"AutoCAD stayed busy for 10 seconds and refused the request"** — AutoCAD is
showing a dialog or waiting at the command line. Switch to it, press Escape,
close the dialog. The server retries automatically for about 10 seconds first.

**"AutoCAD is not running"** — open it, with a drawing.

**ngrok reports no tunnel** — VPN down, or another ngrok agent is already
running (the Codex project's one). The free plan allows a single session.

**`export_pdf` says the file is not on disk** — background plotting was still
running. Check the output folder a moment later.

**Port bind fails with WinError 10013** — that port is in a Windows-reserved
range. `netsh interface ipv4 show excludedportrange protocol=tcp` lists them;
pick another and set `ACAD_MCP_PORT`.

## Layout

| File | What it is |
|---|---|
| `server.py` | FastMCP app, secret-path gate, call logging |
| `tools.py` | The 24 tools |
| `acad.py` | COM bridge: one dedicated thread, retry-when-busy, reconnect |
| `config.py` | Environment configuration and secret generation |
| `test_draw.py` | Smoke test with no MCP involved |

`acad.py` is the part worth understanding. AutoCAD's COM interface only accepts
calls from the thread that initialised COM, and rejects them outright while it
is busy, so every tool hands its work to a single dedicated thread that retries
with backoff and reconnects if AutoCAD restarts.
