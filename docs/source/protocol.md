# Wire protocol reference

The normative reference for every message the three components exchange. Each component
hard-codes `PROTOCOL_VERSION`, and the relay rejects a handshake on mismatch.

```
PROTOCOL_VERSION = 0
```

Transport: one WebSocket per peer to the relay at `<base_url>/mcp-bridge`. All frames are
UTF-8 JSON objects with a `kind` field. A "room" is one notebook path; it has at most one
`extension` peer (the browser) and one `mcp` peer (the assistant). The relay forwards
`cmd` / `reply` / `event` frames to the other peer in the same room unchanged.

## `hello` — sent by each peer immediately on connect

```
{ "kind": "hello", "protocol": 0, "role": "extension" | "mcp", "notebook": "<path>",
  "capabilities": ["<op>", ...], "last_event_seq": <int>, "log_id": "<str>" }
```

`last_event_seq` and `log_id` (mcp role, optional) tell the relay where the peer left off in the
room's event stream; see the event-replay paragraph below. `capabilities` (extension role,
optional) lists the ops the extension implements; the relay attaches it to the `joined` status
frame it sends the mcp peer, which rejects undeclared ops client-side with a "refresh the
browser tab" error instead of round-tripping to the extension's "unknown op".

The relay validates `protocol`, registers the peer under `(notebook, role)`, and closes the
socket with code 1002 on a version mismatch. It also closes with 1002 on a malformed or duplicate
hello (or any frame sent before one), and with 1003 on a frame that is not a JSON object. The
extension does not reconnect after a 1002 — its JS can only change with a page refresh, so
retrying could never succeed.

A second connection for an occupied `(notebook, role)` slot evicts the current holder: the
relay closes it with code 1001 and reason `replaced by a newer connection`. An evicted peer
must NOT immediately reconnect — that would evict its replacement and the two would fight for
the room forever. The extension stands down and reclaims the room only when its tab regains
focus, so the tab the human is actually using holds the bridge.

## `cmd` — MCP server → extension

```
{ "kind": "cmd", "id": <int>, "op": "<name>", "args": { ... } }
```

`id` is a per-connection monotonic integer; the matching `reply` echoes it.

| `op`           | `args`                                        | `result` |
|----------------|-----------------------------------------------|----------|
| `snapshot`     | `{outputs?}`                                  | full notebook: list of cells |
| `read_cell`    | `{cell_id}`                                   | one cell |
| `insert_cell`  | `{index, cell_type, source}`                  | the created cell (`{cell_id, index}`) |
| `set_source`   | `{cell_id, source}`                           | `{cell_id, status: "written"}` — or `{cell_id, status: "skipped", reason: "focused"}` if the human has that cell focused |
| `delete_cell`  | `{cell_id}`                                   | `{cell_id}` |
| `move_cell`    | `{cell_id, index}`                            | `{cell_id, index}` |
| `execute_cell` | `{cell_id, timeout_ms?}`                      | `{cell_id, outputs}` |
| `inspect`      | `{code, timeout_ms?}`                         | `{status, outputs}` — runs in the kernel with `store_history: false`; no cell is touched |
| `run_cells`    | `{cell_ids, timeout_ms?}`                     | `{results}` — per-cell `{cell_id, outputs}` in request order; executes queue FIFO like Run All, and unfinished cells are marked `"timed out"` |
| `interrupt_kernel` | `{}`                                      | `{status}` |
| `restart_kernel` | `{timeout_ms?}`                             | `{status, kernel_name}` — replies once the new kernel is ready, not when the restart is requested; in-flight `execute_cell`/`run_cells` commands lose their kernel and fail on their own timeouts |
| `kernel_info`  | `{}`                                          | `{state, connected, kernel_name}` |
| `undo_last`    | `{}`                                          | `{status, undid?, reason?}` — reverts the newest recorded mcp mutation; skipped if the human touched that cell since |
| `undo_all`     | `{}`                                          | `{results}` — one `undo_last` result per entry, newest first |
| `reload_notebook` | `{}`                                       | `{status}` — re-reads the notebook from disk, discarding the browser buffer and the undo stack |
| `open_notebook` | `{path}`                                     | `{opened, url}` — opens another notebook in a new browser tab; a popup blocker refusing gives `{opened: false, reason: "popup blocked", url}`, and `url` is absolute so a human can open it by hand |

A cell is `{cell_id, index, cell_type, source, outputs}`. With `snapshot`'s
`outputs: "summary"`, each cell instead carries `output_summary` — a compact,
payload-free description of every output — so plot-heavy notebooks don't push
megabytes of base64 through the relay just to be outlined.

`execute_cell`'s `timeout_ms` bounds how long the extension waits for the cell
to finish before replying with an error (default 120000; the cell keeps
running).

If a `cmd` arrives while the `extension` peer is not connected, the relay answers it directly
with `{kind: "reply", id, ok: false, error: "no extension peer connected"}` so the caller
never blocks. A `reply` or `event` addressed to an absent peer is dropped (events stay in the
replay buffer).

## `reply` — extension → MCP server

```
{ "kind": "reply", "id": <int>, "ok": <bool>, "result": <any>, "error": "<str>" }
```

`error` is present only when `ok` is `false`.

## `event` — extension → MCP server (unsolicited)

Pushed when the human changes the notebook, so the assistant stays current. The extension
suppresses the events its own `cmd` handling would otherwise fire, so these reflect human
actions only — an mcp `set_source` does not echo back as a `source_changed`.

```
{ "kind": "event", "name": "<name>", "data": { ... }, "seq": <int>, "log_id": "<str>" }
```

The extension sends events without `seq` and `log_id`; the relay stamps both before forwarding
(see the replay paragraph below).

| `name`           | `data` |
|------------------|--------|
| `cell_created`   | `{cell_id, index, cell_type, source}` |
| `cell_deleted`   | `{cell_id}` |
| `cell_moved`     | `{cell_id, index}` — one event per cell whose index changed, so a single move emits one per displaced cell |
| `cell_executed`  | `{cell_id, outputs}` — image payloads replaced with a stub; fetch via `read_cell` |
| `source_changed` | `{cell_id, source}` — debounced / on blur, not per keystroke |
| `focus_changed`  | `{cell_id}` or `{cell_id: null}` — drives the `set_source` skip rule |
| `bridge_paused`  | `{}` — the human paused the bridge; every `cmd` is answered `ok: false` until resume |
| `bridge_resumed` | `{}` — the human resumed the bridge |
| `kernel_status`  | `{state}` — pushed only for failures (dead, killed, disconnected, connection_failed, autorestarting) and the first idle or connected state after one; routine busy/idle flips are not pushed |

nbclassic fires no notebook event for a human move (it reorders the DOM directly), so the
extension wraps the notebook's move methods and derives `cell_moved` by diffing indices.

The relay stamps each forwarded event with a per-room monotonic `seq` and its own `log_id`, and
retains a bounded buffer (1000 events) per room. A joining `mcp` peer whose hello carries a
`last_event_seq` learned under the same `log_id` receives exactly the buffered events it missed;
any other position (fresh client, or a `log_id` from a previous relay process) replays the whole
buffer. The buffer lives and dies with the room, and a relay restart empties it — replay covers
client-side reconnects, not server restarts.

That numbering is per room and never leaves it. An assistant attached to several notebooks holds
one socket per room, each tracking its own `seq` and `log_id` so a reconnect on one notebook
cannot make the relay skip another's events. The `seq` and `log_id` are consumed by the MCP
server and stripped before an event reaches the assistant, which sees a single merged stream
under its own cursor — see `poll_events` in the tool reference.

## `status` — relay → peer

Relay-originated. Tells one peer that the other has joined or left, so each side knows
whether its counterpart is reachable. Sent to a peer when its counterpart connects (and to
the freshly-connected peer if the counterpart was already present), and when the counterpart
disconnects.

```
{ "kind": "status", "peer": "extension" | "mcp", "state": "joined" | "left",
  "capabilities": ["<op>", ...] }
```

`capabilities` appears only when `peer` is `extension`, `state` is `joined`, and the extension's
hello declared them — it is the relayed copy of that declaration.

## `GET <base_url>/mcp-bridge/rooms` — discovery over HTTP

Authenticated like any Jupyter API endpoint. Returns the relay's live rooms so an MCP client
can see which notebooks have a connected browser tab before attaching:

```
{ "rooms": { "<notebook path>": ["extension", "mcp"] } }
```

