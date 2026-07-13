# Getting started

## Install

Two packages, one per environment:

```bash
pip install nbclassic-mcp-bridge       # in the Jupyter environment
pip install nbclassic-mcp-bridge-mcp   # in the MCP client's environment
```

Installing the Jupyter-side package registers both the relay server extension and the frontend
extension automatically — there is nothing to enable.

## Register the MCP server

Start the classic notebook and note the token it prints:

```bash
jupyter nbclassic
```

Any MCP client works: run the `nbclassic-mcp-bridge` command with `JUPYTER_URL` and
`JUPYTER_TOKEN` set. With Claude Code, for example:

```bash
claude mcp add nbclassic-mcp-bridge \
  -e JUPYTER_URL=http://localhost:8888 \
  -e JUPYTER_TOKEN=<token> \
  -- nbclassic-mcp-bridge
```

Add `-s user` to register the server for every project. If `nbclassic-mcp-bridge-mcp` is installed
in a non-default environment, pass the absolute path of the executable in place of the trailing
`nbclassic-mcp-bridge` (find it with `which nbclassic-mcp-bridge`).

## First session

Open a notebook in the browser. From the assistant:

1. `use_notebook()` — with no arguments it finds the open notebook automatically; `list_notebooks`
   shows everything the server has open, and inexact paths resolve fuzzily.
2. `read_notebook` for an outline, then `insert_cell`, `set_cell_source`, `execute_cell`,
   `run_cells` to work.
3. `poll_events` reports what the *human* changed in the meantime — assistant commands are never
   echoed back as events.

A plug icon at the right end of the notebook toolbar shows the bridge's state: green when an
assistant is connected, orange when paused. Clicking it toggles pause; while paused, every
assistant command is rejected and none of your edits stream out.

Concurrent edits to the same cell are last-write-wins, which suits one human and one assistant
taking turns. The one guard: the assistant cannot overwrite the cell you are currently editing.

## Configuration

`JUPYTER_URL`
: Base URL of the Jupyter server. Default `http://localhost:8888`.

`JUPYTER_TOKEN`
: The server's access token. Required.

`ALLOW_IMG_OUTPUT`
: Set to `1` to include base64 image payloads in text responses. Off by default: image payloads
  are large, an output's text representation is always kept, and `read_cell_image` returns any
  image as a viewable image regardless of this flag.

`LOG_LEVEL`
: MCP server log verbosity. Default `INFO`.
