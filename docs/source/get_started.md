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

## Working in several notebooks

`use_notebook` does not detach whatever came before it, so calling it again adds a second notebook
and makes it *current*. Every notebook-scoped tool takes an optional `notebook` argument naming
another attached one; omit it and the tool acts on the current notebook. Names are matched the
same loose way `use_notebook` matches them, so `execute_cell("c1", notebook="eda")` finds
`notebooks/2026/eda.ipynb`. Naming a notebook also makes it current, and every tool that changes a
notebook reports which one it changed — so the assistant is told its target on each call rather
than having to remember it. A name matching two attached notebooks is refused rather than guessed.

`list_notebooks` marks which notebooks are `attached` and which one is `current`. `detach_notebook`
drops one, leaving its browser tab and kernel untouched; detaching the current notebook leaves no
current notebook until the next `use_notebook`.

`poll_events` stays a single call however many notebooks are attached: one stream, one cursor, and
every event tagged with the notebook it came from, in the order things happened. Pass `notebook` to
narrow what comes back — the cursor still tracks the whole stream, so a filtered poll does not
re-deliver the other notebooks' events later.

`use_server` and `use_project` change where the *next* `use_notebook` looks without disturbing
anything already attached, so notebooks on two Jupyter servers can be held at once. In that case
`list_notebooks` adds a `jupyter_url` to each record, and a name attached on both servers has to be
disambiguated; a single-server session never sees a URL. A server that has gone away contributes an
error record to the listing instead of failing it, so the notebooks on the others stay visible.

## Creating a notebook

`create_notebook` writes a new `.ipynb` on the Jupyter server, optionally seeded with cells, and
refuses to overwrite anything already at that path. By default it creates the file only — open it
yourself, then `use_notebook`.

With `open=True` it also asks a tab you already have open to open the new notebook in a tab of its
own, then attaches to it and makes it current. That needs an attached tab on the same server to ask,
and a browser willing to open the tab; when either is missing, the reply says so and gives you the
URL to open by hand. Nothing is lost in that case — the file exists either way, and `use_notebook`
finishes the job once the tab is up.

Writing a file directly is the one place the bridge bypasses the browser, and it is deliberately
limited to files that do not exist yet: there is no in-browser buffer to clobber in a notebook that
has never been opened.

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
