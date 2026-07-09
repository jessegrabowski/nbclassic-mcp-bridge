# nbclassic-mcp-bridge-mcp

The MCP-server half of [nbclassic-mcp-bridge](https://github.com/jessegrabowski/nbclassic-mcp-bridge):
lets an AI assistant read, edit, and run cells in a notebook you have open in the classic Jupyter
Notebook (`nbclassic`) UI, and follow your edits as you make them.

```bash
pip install nbclassic-mcp-bridge-mcp  # in the MCP client's environment
```

Register with your MCP client as the `nbclassic-mcp-bridge` command, passing `JUPYTER_URL` and
`JUPYTER_TOKEN`. Requires the [`nbclassic-mcp-bridge`](https://pypi.org/project/nbclassic-mcp-bridge/)
package installed in the Jupyter environment. See the repository README for setup and usage.
