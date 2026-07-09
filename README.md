# nbclassic-mcp-bridge

A live bridge between the classic Jupyter Notebook (`nbclassic`) and an MCP server, so an AI assistant can read, edit, and run cells in a notebook you have open in the browser — and pick up your edits as you make them.

Two packages in one repo:

- `extension/` — installs into the Jupyter environment (an `nbclassic` frontend extension plus
  a relay server extension).
- `mcp_server/` — installs into the MCP client's environment (the MCP server itself).

## Install

```bash
pip install -e extension/      # in the Jupyter environment
pip install -e mcp_server/     # in the MCP client's environment
```

Nothing else to enable — installing `extension` registers the relay and the frontend
extension with Jupyter automatically.

## Usage

1. Start the classic notebook: `jupyter nbclassic`, and note the `?token=...` it prints.
2. Register the MCP server with Claude Code, passing that token:

   ```bash
   claude mcp add nbclassic-mcp-bridge \
     -e JUPYTER_URL=http://localhost:8888 \
     -e JUPYTER_TOKEN=<token> \
     -- nbclassic-mcp-bridge
   ```

   Add `-s user` to make it available in every project. If `mcp_server` is installed in a
   non-default environment, pass the absolute path in place of the trailing `nbclassic-mcp-bridge`
   (find it with `which nbclassic-mcp-bridge`). Any other MCP client works too — it just needs to
   run the `nbclassic-mcp-bridge` command with those two environment variables.
3. Open a notebook in the browser. From the assistant, `use_notebook("<path>")` to attach,
   then `read_notebook`, `insert_cell`, `set_cell_source`, `execute_cell`, `move_cell`,
   `delete_cell` to drive it, `read_cell_image` to look at a plot, and `poll_events` to
   see what you have changed.

Two more optional environment variables: `ALLOW_IMG_OUTPUT=1` includes base64 image
payloads in text responses (off by default — they are large and burn tokens; an output's
text representation is always kept, and `read_cell_image` returns any image as a proper
viewable image regardless of this flag), and `LOG_LEVEL` sets the MCP server's log
verbosity (default `INFO`).

Concurrent edits to the same cell are last-write-wins — fine for one human and one assistant
taking turns.

## Development

```bash
pip install -e "extension[test]" -e "mcp_server[test]"
playwright install chromium                 # one-time, for the browser e2e test
pytest extension mcp_server -m "not e2e"    # relay + client unit tests
pytest extension -m e2e                     # end-to-end browser test
```

## Contributing

PRs are welcome. Please run `pre-commit run --all-files` and the test suite before opening one.
