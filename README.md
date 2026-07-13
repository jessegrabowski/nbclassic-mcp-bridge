# nbclassic-mcp-bridge

A live bridge between the classic Jupyter Notebook (`nbclassic`) and an MCP server, so an AI
assistant can read, edit, and run cells in a notebook you have open in the browser — and pick up
your edits as you make them.

Documentation — setup, tool reference, architecture, and the wire protocol — lives at
[nbclassic-mcp-bridge.readthedocs.io](https://nbclassic-mcp-bridge.readthedocs.io).

## Install

Two packages, one per environment:

```bash
pip install nbclassic-mcp-bridge       # in the Jupyter environment
pip install nbclassic-mcp-bridge-mcp   # in the MCP client's environment
```

The [getting started](https://nbclassic-mcp-bridge.readthedocs.io/en/latest/get_started.html)
page covers registering the MCP server with a client and driving a first session.

## Contributing

PRs are welcome. Run `pre-commit run --all-files` and the test suite before opening one; the
[development](https://nbclassic-mcp-bridge.readthedocs.io/en/latest/development.html) page covers
the layout, pixi tasks, and test tiers.
