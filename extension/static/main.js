// nbclassic frontend extension: gives the nbclassic-mcp-bridge relay a live
// handle on Jupyter.notebook so an MCP client can read/edit/run cells, and
// pushes the human's edits back so the assistant stays current.
define([
    "base/js/namespace",
    "base/js/events",
], function (Jupyter, events) {
    "use strict";

    var PROTOCOL_VERSION = 0;
    var SOURCE_DEBOUNCE_MS = 400;
    var EXECUTE_TIMEOUT_MS = 120000;
    var RECONNECT_MIN_MS = 2000;
    var RECONNECT_MAX_MS = 30000;

    // Close reason the relay sends when another tab takes over this notebook's room; the evicted tab
    // must stand down instead of reconnecting, or the two tabs evict each other forever.
    var EVICTED_REASON = "replaced by a newer connection";

    // Sentinel: the op sends its own reply later (execute_cell), so applyCommand
    // must not reply for it.
    var DEFERRED = {};

    var ws = null;
    var evicted = false;            // stood down after another tab took the room
    var applying = false;           // an mcp command is mutating the notebook; suppress the event echo
    var pendingExecutes = {};       // cell_id -> true while an mcp execute_cell awaits its finish
    var reconnectDelay = RECONNECT_MIN_MS;
    var focusedCellId = null;       // cell the human is editing; drives the set_source skip
    var dirtyCells = {};            // cell_id -> true, awaiting a debounced source_changed
    var lastAgentWrite = {};        // cell_id -> source the mcp side last wrote, to drop its echo
    var debounceTimer = null;

    // --- frame I/O ---------------------------------------------------------

    function sendFrame(obj) {
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify(obj));
        }
    }

    function emit(name, data) {
        sendFrame({ kind: "event", name: name, data: data });
    }

    // --- cell helpers ------------------------------------------------------

    function cellById(id) {
        var cells = Jupyter.notebook.get_cells();
        for (var i = 0; i < cells.length; i++) {
            if (cells[i].id === id) { return cells[i]; }
        }
        return null;
    }

    function requireCell(id) {
        var cell = cellById(id);
        if (!cell) { throw new Error("no cell with id " + id); }
        return cell;
    }

    function indexById(id) {
        return Jupyter.notebook.find_cell_index(requireCell(id));
    }

    function cellOutputs(cell) {
        return cell.output_area ? cell.output_area.toJSON() : [];
    }

    function cellToProto(cell) {
        return {
            cell_id: cell.id,
            index: Jupyter.notebook.find_cell_index(cell),
            cell_type: cell.cell_type,
            source: cell.get_text(),
            outputs: cellOutputs(cell),
        };
    }

    // Every agent write must register itself in lastAgentWrite, or its debounced change event
    // echoes back to the assistant as a human edit.
    function writeAgentSource(cell, source) {
        cell.set_text(source);
        lastAgentWrite[cell.id] = source;
    }

    // --- command dispatch --------------------------------------------------

    // runOp executes with the echo of its own mutations suppressed: the create/delete/change handlers
    // it triggers fire synchronously inside it; the asynchronous cell_executed echo is handled by
    // pendingExecutes instead.
    function applyCommand(msg) {
        var reply = { kind: "reply", id: msg.id, ok: true };
        applying = true;
        try {
            var result = runOp(msg.op, msg.args || {}, msg.id);
            if (result === DEFERRED) { return; }
            reply.result = result;
        } catch (e) {
            reply.ok = false;
            reply.error = String(e && e.message ? e.message : e);
        } finally {
            applying = false;
        }
        sendFrame(reply);
    }

    function runOp(op, args, id) {
        var nb = Jupyter.notebook;
        switch (op) {
        case "snapshot":
            return nb.get_cells().map(cellToProto);
        case "read_cell":
            return cellToProto(requireCell(args.cell_id));
        case "insert_cell": {
            var created = nb.insert_cell_at_index(args.cell_type, args.index);
            if (!created) { throw new Error("could not insert a " + args.cell_type + " cell"); }
            writeAgentSource(created, args.source || "");
            return { cell_id: created.id, index: nb.find_cell_index(created) };
        }
        case "set_source": {
            if (args.cell_id === focusedCellId) {
                return { cell_id: args.cell_id, status: "skipped", reason: "focused" };
            }
            var edited = requireCell(args.cell_id);
            // set_text drops a rendered markdown cell back to its raw source;
            // restore whichever view the cell was in.
            var wasRendered = edited.rendered;
            writeAgentSource(edited, args.source || "");
            if (wasRendered) { edited.render(); }
            return { cell_id: args.cell_id, status: "written" };
        }
        case "delete_cell": {
            var deleteIndex = indexById(args.cell_id);
            nb.delete_cell(deleteIndex);
            return { cell_id: args.cell_id };
        }
        case "move_cell":
            return moveCell(args.cell_id, args.index);
        case "execute_cell":
            executeCell(args.cell_id, id);
            return DEFERRED;
        default:
            throw new Error("unknown op: " + op);
        }
    }

    // No index-based move exists in nbclassic; delete then reinsert. fromJSON
    // restores id, source and outputs, so the cell keeps its identity.
    function moveCell(cellId, index) {
        var nb = Jupyter.notebook;
        var cell = requireCell(cellId);
        var from = nb.find_cell_index(cell);
        var json = cell.toJSON();
        var type = cell.cell_type;
        var wasRendered = cell.rendered;
        nb.delete_cell(from);
        var dest = index > from ? index - 1 : index;
        var moved = nb.insert_cell_at_index(type, dest);
        moved.fromJSON(json);
        lastAgentWrite[cellId] = moved.get_text();
        // The reinserted cell starts unrendered; keep the view it had.
        if (wasRendered) { moved.render(); }
        return { cell_id: cellId, index: nb.find_cell_index(moved) };
    }

    // execute_cell is async: the reply waits for finished_execute.CodeCell so it
    // can carry real outputs.
    function executeCell(cellId, id) {
        var cell = requireCell(cellId);
        if (cell.cell_type !== "code") {
            cell.execute();
            sendFrame({ kind: "reply", id: id, ok: true,
                        result: { cell_id: cellId, outputs: [] } });
            return;
        }
        // A second execute while one is in flight would double-subscribe onFinished and resolve both
        // replies with the first run's outputs.
        if (pendingExecutes[cellId]) {
            throw new Error("cell is already executing");
        }
        pendingExecutes[cellId] = true;
        var done = false;
        var timer = setTimeout(function () {
            if (done) { return; }
            done = true;
            // Clear the mark so the eventual finish surfaces as a cell_executed event.
            delete pendingExecutes[cellId];
            events.off("finished_execute.CodeCell", onFinished);
            sendFrame({ kind: "reply", id: id, ok: false, error: "execute_cell timed out" });
        }, EXECUTE_TIMEOUT_MS);
        function onFinished(evt, data) {
            if (done || data.cell !== cell) { return; }
            done = true;
            clearTimeout(timer);
            events.off("finished_execute.CodeCell", onFinished);
            sendFrame({ kind: "reply", id: id, ok: true,
                        result: { cell_id: cellId, outputs: cellOutputs(cell) } });
        }
        events.on("finished_execute.CodeCell", onFinished);
        cell.execute();
    }

    // --- connection --------------------------------------------------------

    function relayUrl() {
        var loc = window.location;
        var scheme = loc.protocol === "https:" ? "wss:" : "ws:";
        return scheme + "//" + loc.host + Jupyter.notebook.base_url + "mcp-bridge";
    }

    function connect() {
        evicted = false;
        ws = new WebSocket(relayUrl());
        ws.onopen = function () {
            reconnectDelay = RECONNECT_MIN_MS;
            sendFrame({
                kind: "hello",
                protocol: PROTOCOL_VERSION,
                role: "extension",
                notebook: Jupyter.notebook.notebook_path,
            });
        };
        ws.onmessage = function (ev) {
            var msg;
            try {
                msg = JSON.parse(ev.data);
            } catch (e) {
                console.warn("nbclassic-mcp-bridge: dropped a non-JSON frame", e);
                return;
            }
            if (msg.kind === "cmd") { applyCommand(msg); }
        };
        ws.onclose = function (ev) {
            ws = null;
            if (ev && ev.code === 1001 && ev.reason === EVICTED_REASON) {
                // Another tab owns the room now; reclaimEvicted takes it back if the human returns here.
                evicted = true;
                console.info("nbclassic-mcp-bridge: bridge moved to a newer tab of this notebook");
                return;
            }
            if (ev && ev.code === 1002) {
                // Handshake rejected (protocol mismatch); only a page refresh loads JS that could succeed.
                console.error("nbclassic-mcp-bridge: relay rejected the connection ("
                              + ev.reason + "); refresh the page to reload the extension");
                return;
            }
            setTimeout(connect, reconnectDelay);
            reconnectDelay = Math.min(reconnectDelay * 2, RECONNECT_MAX_MS);
        };
        ws.onerror = function () { /* onclose fires next; reconnect happens there */ };
    }

    // The tab the human works in should own the room: an evicted tab reclaims it on focus, and the tab
    // it evicts stands down in turn.
    function reclaimEvicted() {
        if (evicted && ws === null) { connect(); }
    }

    // --- human-edit events -------------------------------------------------

    function flushDirty() {
        if (debounceTimer) { clearTimeout(debounceTimer); debounceTimer = null; }
        Object.keys(dirtyCells).forEach(function (id) {
            var cell = cellById(id);
            if (!cell) { return; }
            var source = cell.get_text();
            // CodeMirror delivers change events asynchronously, past the applying window; a change that
            // matches exactly what the mcp side last wrote is its echo, not a human edit.
            if (lastAgentWrite[id] === source) { return; }
            delete lastAgentWrite[id];
            emit("source_changed", { cell_id: id, source: source });
        });
        dirtyCells = {};
    }

    function wireEvents() {
        events.on("create.Cell", function (evt, data) {
            if (applying) { return; }
            emit("cell_created", {
                cell_id: data.cell.id,
                index: data.index,
                cell_type: data.cell.cell_type,
                source: data.cell.get_text(),
            });
        });
        events.on("delete.Cell", function (evt, data) {
            if (applying) { return; }
            emit("cell_deleted", { cell_id: data.cell.id });
        });
        events.on("finished_execute.CodeCell", function (evt, data) {
            // An mcp execute_cell owns this completion and its reply carries the outputs. This handler
            // registered first (at init), so consuming the mark here cannot race the per-command reply.
            if (pendingExecutes[data.cell.id]) {
                delete pendingExecutes[data.cell.id];
                return;
            }
            emit("cell_executed", { cell_id: data.cell.id, outputs: cellOutputs(data.cell) });
        });
        events.on("change.Cell", function (evt, data) {
            if (applying) { return; }
            dirtyCells[data.cell.id] = true;
            if (debounceTimer) { clearTimeout(debounceTimer); }
            debounceTimer = setTimeout(flushDirty, SOURCE_DEBOUNCE_MS);
        });
        events.on("edit_mode.Cell", function (evt, data) {
            focusedCellId = data.cell.id;
            emit("focus_changed", { cell_id: focusedCellId });
        });
        events.on("command_mode.Cell", function () {
            flushDirty();
            if (focusedCellId !== null) {
                focusedCellId = null;
                emit("focus_changed", { cell_id: null });
            }
        });
    }

    // nbclassic moves cells by raw DOM reordering with no notebook event, so wrap the prototype's move
    // methods (toolbar, menu, and keyboard all funnel through them) and diff indices into cell_moved
    // events. The instance is sealed, hence the prototype; mcp move_cell uses delete+insert and never
    // enters here, so these are human moves only.
    function patchMoves() {
        var proto = Object.getPrototypeOf(Jupyter.notebook);
        ["move_selection_up", "move_selection_down"].forEach(function (name) {
            var original = proto[name];
            if (typeof original !== "function") { return; }
            proto[name] = function () {
                var before = {};
                this.get_cells().forEach(function (cell, i) { before[cell.id] = i; });
                var result = original.apply(this, arguments);
                this.get_cells().forEach(function (cell, i) {
                    if (before[cell.id] !== i) {
                        emit("cell_moved", { cell_id: cell.id, index: i });
                    }
                });
                return result;
            };
        });
    }

    function init() {
        connect();
        wireEvents();
        try {
            patchMoves();
        } catch (e) {
            console.warn("nbclassic-mcp-bridge: could not hook cell moves; they will not be reported", e);
        }
        window.addEventListener("focus", reclaimEvicted);
        document.addEventListener("visibilitychange", function () {
            if (!document.hidden) { reclaimEvicted(); }
        });
    }

    // Join only after the notebook finishes loading: an earlier snapshot would see a half-empty
    // notebook, and the initial cell population would fire a cell_created event per seed cell.
    function load_ipython_extension() {
        if (Jupyter.notebook && Jupyter.notebook._fully_loaded) {
            init();
        } else {
            events.one("notebook_loaded.Notebook", init);
        }
    }

    return { load_ipython_extension: load_ipython_extension };
});
