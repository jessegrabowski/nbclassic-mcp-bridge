# nbclassic-mcp-bridge

A live bridge between the classic Jupyter Notebook (`nbclassic`) and any MCP client, so an AI
assistant can read, edit, and run cells in the notebook you have open in the browser and follow
your edits as you make them.

The bridge is two-sided by design. Your notebook tab stays the source of truth: the assistant's
edits appear in it with a brief highlight, your edits stream back to the assistant as events, and a
toolbar control shows who is connected and can pause the bridge entirely. Every change goes through
the same `Jupyter.notebook` APIs your own clicks use, in view of the browser tab.

Two packages implement this: `nbclassic-mcp-bridge` installs into the Jupyter environment (the
frontend extension and the relay it talks through) and `nbclassic-mcp-bridge-mcp` installs into
the MCP client's environment (the MCP server and its tools). {doc}`get_started` covers setup;
{doc}`architecture` explains how the pieces fit.

```{toctree}
:caption: Usage
:maxdepth: 1

get_started
tools
```

```{toctree}
:caption: Design
:maxdepth: 1

architecture
protocol
internals
```

```{toctree}
:caption: Contributing
:maxdepth: 1

development
```
