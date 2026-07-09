# nbclassic-mcp-bridge

The Jupyter-side half of [nbclassic-mcp-bridge](https://github.com/jessegrabowski/nbclassic-mcp-bridge):
an `nbclassic` frontend extension plus a relay server extension that give an MCP server a live handle on a
notebook open in the classic Notebook UI.

```bash
pip install nbclassic-mcp-bridge      # in the Jupyter environment
```

Installing registers both the relay and the frontend extension automatically — nothing else to enable.
Pair it with the [`nbclassic-mcp-bridge-mcp`](https://pypi.org/project/nbclassic-mcp-bridge-mcp/) package
in your MCP client's environment. See the repository README for setup and usage.
