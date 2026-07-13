# Architecture

## Three components

```text
MCP client ──stdio──> nbclassic-mcp-bridge-mcp ──websocket──> relay ──websocket──> browser tab
                      (RelayClient, tools)                   (switchboard,          (frontend
                                                              jupyter server         extension,
                                                              extension)             main.js)
```

The **switchboard** is a transport-agnostic router living in the Jupyter server: a *room* is one
notebook path holding at most one `extension` peer (the browser tab) and one `mcp` peer (the
assistant). Commands flow mcp → extension, replies and events flow back, and the relay answers
commands itself when no tab is connected so callers never block. All notebook manipulation happens
in the browser through the same `Jupyter.notebook` APIs the human's own clicks use: the bridge
has no second, hidden path to the file. {doc}`protocol` specifies the frames.

## Room ownership

One tab owns a notebook's room at a time. A newer connection evicts the older one, and the evicted
tab *stands down* instead of reconnecting — an immediate retry would evict the replacement and the
two tabs would trade the room indefinitely. The evicted tab reclaims the room when it next regains
focus, so the tab the human is actually using holds the bridge.

Eviction applies to the `mcp` slot too, but without the stand-down: a second assistant evicts the
first, and the first reattaches — evicting back — the next time it issues a command. One assistant
per notebook is the supported arrangement.

## Events: human actions only

The extension pushes events when the *human* changes the notebook — never echoes of assistant
commands. Three mechanisms enforce this: synchronous handlers are suppressed while a command runs;
debounced source changes are compared against what the assistant last wrote; and completions of
assistant-triggered executions are consumed before the event handler sees them. Human cell moves,
which nbclassic performs as raw DOM reordering with no notebook event, are detected by wrapping
the notebook's move methods and diffing indices.

The relay stamps each event with a per-room sequence number and retains the last 1000. A
reconnecting assistant declares its last-seen position in the hello and receives exactly what it
missed; positions count only under the `log_id` they were learned from, so a restarted relay's
renumbered events are never mistaken for duplicates.

## Capabilities

The extension's hello advertises the ops it implements — the list is literally the keys of its
dispatch table, so it cannot drift. The relay attaches it to the `joined` status frame, and the
MCP client rejects undeclared ops with *"refresh the browser tab to load the updated extension"*
instead of a round-trip to `unknown op`. Legacy peers on either side degrade to the old behavior.

## Undo and checkpoints

Every assistant mutation records its inverse in a bounded stack in the browser: prior source for
edits, the full cell (identity and outputs included) for deletions, positions for moves. Undo
verifies the notebook still matches what the assistant left before reverting anything — an entry
the human has built on is skipped, not applied. Checkpoint/restore operates at the file level
through Jupyter's contents API, with restore reloading the tab from disk.

## Kernel integration

The extension tracks kernel lifecycle events; executions fail fast when the kernel is dead, and
failures (plus the recovery after one) are pushed as `kernel_status` events — routine busy/idle
flips are not, since they would double the traffic of every execution. `inspect_kernel` evaluates
with `store_history` disabled so the assistant can look at variables without leaving any trace in
the notebook.

## Trust and visibility

The human's tab renders a toolbar control showing bridge state (assistant connected / ready /
paused / disconnected) that doubles as a pause toggle: while paused, every command is rejected
with a distinct error and no edit events leave the browser. Cells the assistant touches flash
briefly. Authentication rides on the Jupyter token: the websocket and every HTTP call the MCP
server makes carry it, with HTTP using an `Authorization` header so the token can never leak into
URL-bearing error messages.

## Degradation

Every protocol addition is optional-field additive: an old extension against a new MCP server (or
vice versa) falls back to the previous behavior — no capability pre-checks, full event replay,
plain status frames. A server restart empties the relay's event buffer (it is in-process), so
replay covers client-side reconnects, not server restarts.
