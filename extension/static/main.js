// nbclassic frontend extension: gives the nbclassic-mcp-bridge relay a live handle on Jupyter.notebook
// so an MCP client can read/edit/run cells, and pushes the human's edits back so the assistant stays current.
define([
    "jquery",
    "base/js/namespace",
    "base/js/events",
], function ($, Jupyter, events) {
    "use strict";

    var PROTOCOL_VERSION = 0;
    var SOURCE_DEBOUNCE_MS = 400;
    var EXECUTE_TIMEOUT_MS = 120000;
    var RECONNECT_MIN_MS = 2000;
    var RECONNECT_MAX_MS = 30000;

    // Close reason the relay sends when another tab takes over this notebook's room; the evicted tab
    // must stand down instead of reconnecting, or the two tabs evict each other forever.
    var EVICTED_REASON = "replaced by a newer connection";

    // Sentinel: the op sends its own reply later (execute_cell), so applyCommand must not reply for it.
    var DEFERRED = {};

    var ws = null;
    var evicted = false;            // stood down after another tab took the room
    var paused = false;             // human suspended the bridge; cmds rejected, no events emitted
    var kernelState = "unknown";    // last kernel lifecycle event seen; drives fail-fast messages
    var assistantConnected = false; // an mcp peer shares the room (tracked from status frames)
    var applying = false;           // an mcp command is mutating the notebook; suppress the event echo
    var pendingExecutes = {};       // cell_id -> true while an mcp execute_cell awaits its finish
    var reconnectDelay = RECONNECT_MIN_MS;
    var focusedCellId = null;       // cell the human is editing; drives the set_source skip
    var dirtyCells = {};            // cell_id -> true, awaiting a debounced source_changed
    var lastAgentWrite = {};        // cell_id -> source the mcp side last wrote, to drop its echo
    var debounceTimer = null;


    function sendFrame(obj) {
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify(obj));
        }
    }

    function emit(name, data) {
        if (paused) { return; }
        sendFrame({ kind: "event", name: name, data: data });
    }


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

    function asText(value) {
        return Array.isArray(value) ? value.join("") : String(value == null ? "" : value);
    }

    // Every agent write must register itself in lastAgentWrite, or its debounced change event
    // echoes back to the assistant as a human edit.
    function writeAgentSource(cell, source) {
        cell.set_text(source);
        lastAgentWrite[cell.id] = source;
    }

    // Fading highlight on any cell the assistant touches, so its edits are visible as they land.
    // The animation runs once and reverts by itself; the leftover class is inert, so no cleanup
    // timer is needed (one would truncate a re-flash of the same cell).
    function flashCell(cell) {
        var element = cell.element;
        if (!element || !element.length) { return; }
        element.removeClass("mcp-bridge-agent-touch");
        void element[0].offsetWidth; // restart the animation when the same cell is touched twice
        element.addClass("mcp-bridge-agent-touch");
    }

    // Payload-free description of one output; mirrors the MCP server's _summarize_output.
    function summarizeOutput(output) {
        var kind = output.output_type;
        if (kind === "stream") {
            return { output_type: kind, name: output.name, chars: asText(output.text).length };
        }
        if (kind === "error") {
            return { output_type: kind, ename: output.ename, evalue: output.evalue };
        }
        if (kind === "display_data" || kind === "execute_result") {
            return { output_type: kind, mime_types: Object.keys(output.data || {}).sort() };
        }
        return { output_type: kind };
    }

    // Events carry a stub instead of megabytes of base64; read_cell still returns images on request.
    // Each output from output_area.toJSON() is a shallow copy sharing its data dict with the live cell:
    // replace the dict, never mutate its entries, or the stub corrupts the notebook itself.
    function stripImagePayloads(outputs) {
        outputs.forEach(function (output) {
            var data = output.data;
            if (!data) { return; }
            var stripped = {};
            Object.keys(data).forEach(function (mime) {
                stripped[mime] = mime.indexOf("image/") === 0
                    ? "<" + mime + " omitted from event; read the cell's outputs to fetch it>"
                    : data[mime];
            });
            output.data = stripped;
        });
        return outputs;
    }


    function requireLiveKernel() {
        var kernel = Jupyter.notebook.kernel;
        if (!kernel || (kernel.is_connected && !kernel.is_connected())) {
            throw new Error("kernel is not connected (state: " + kernelState + ")");
        }
        return kernel;
    }

    // runOp executes with the echo of its own mutations suppressed: the create/delete/change handlers
    // it triggers fire synchronously inside it; the asynchronous cell_executed echo is handled by
    // pendingExecutes instead.
    function applyCommand(msg) {
        if (paused) {
            sendFrame({ kind: "reply", id: msg.id, ok: false, error: "bridge paused by the user" });
            return;
        }
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
        case "snapshot": {
            var cells = nb.get_cells().map(cellToProto);
            if (args.outputs === "summary") {
                cells.forEach(function (cell) {
                    cell.output_summary = cell.outputs.map(summarizeOutput);
                    delete cell.outputs;
                });
            }
            return cells;
        }
        case "read_cell":
            return cellToProto(requireCell(args.cell_id));
        case "insert_cell": {
            var created = nb.insert_cell_at_index(args.cell_type, args.index);
            if (!created) { throw new Error("could not insert a " + args.cell_type + " cell"); }
            writeAgentSource(created, args.source || "");
            flashCell(created);
            return { cell_id: created.id, index: nb.find_cell_index(created) };
        }
        case "set_source": {
            if (args.cell_id === focusedCellId) {
                return { cell_id: args.cell_id, status: "skipped", reason: "focused" };
            }
            var edited = requireCell(args.cell_id);
            // set_text drops a rendered markdown cell back to its raw source; restore the view it was in.
            var wasRendered = edited.rendered;
            writeAgentSource(edited, args.source || "");
            if (wasRendered) { edited.render(); }
            flashCell(edited);
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
            executeCell(args.cell_id, id, args.timeout_ms);
            return DEFERRED;
        case "interrupt_kernel":
            requireLiveKernel().interrupt();
            return { status: "interrupt requested" };
        case "kernel_info": {
            var kernel = nb.kernel;
            return {
                state: kernelState,
                connected: !!(kernel && (!kernel.is_connected || kernel.is_connected())),
                kernel_name: kernel ? kernel.name : null,
            };
        }
        default:
            throw new Error("unknown op: " + op);
        }
    }

    // No index-based move exists in nbclassic; delete then reinsert. fromJSON restores id, source and
    // outputs, so the cell keeps its identity.
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
        flashCell(moved);
        return { cell_id: cellId, index: nb.find_cell_index(moved) };
    }

    // execute_cell replies later: the reply waits for finished_execute.CodeCell to carry real outputs.
    function executeCell(cellId, id, timeoutMs) {
        var cell = requireCell(cellId);
        flashCell(cell);
        if (cell.cell_type !== "code") {
            cell.execute();
            sendFrame({ kind: "reply", id: id, ok: true,
                        result: { cell_id: cellId, outputs: [] } });
            return;
        }
        requireLiveKernel();
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
        }, timeoutMs > 0 ? timeoutMs : EXECUTE_TIMEOUT_MS);
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
            renderStatus();
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
            if (msg.kind === "status" && msg.peer === "mcp") {
                assistantConnected = msg.state === "joined";
                renderStatus();
            }
        };
        ws.onclose = function (ev) {
            ws = null;
            assistantConnected = false;
            renderStatus();
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
            emit("cell_executed", {
                cell_id: data.cell.id,
                outputs: stripImagePayloads(cellOutputs(data.cell)),
            });
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

    // Lifecycle states worth pushing to the assistant: failures, and the recovery after one.
    // Routine busy/idle flips would double the event traffic of every execution, so they only
    // update kernelState for kernel_info and fail-fast messages.
    var KERNEL_EVENTS = {
        "kernel_starting.Kernel": "starting",
        "kernel_connected.Kernel": "connected",
        "kernel_ready.Kernel": "idle",
        "kernel_busy.Kernel": "busy",
        "kernel_idle.Kernel": "idle",
        "kernel_interrupting.Kernel": "interrupting",
        "kernel_restarting.Kernel": "restarting",
        "kernel_autorestarting.Kernel": "autorestarting",
        "kernel_reconnecting.Kernel": "reconnecting",
        "kernel_disconnected.Kernel": "disconnected",
        "kernel_connection_failed.Kernel": "connection_failed",
        "kernel_dead.Kernel": "dead",
        "kernel_killed.Kernel": "killed",
    };
    var KERNEL_ALERT_STATES = {
        autorestarting: true, disconnected: true, connection_failed: true, dead: true, killed: true,
    };

    // The alert sticks until the kernel is operational again: intermediate lifecycle states
    // (starting, reconnecting) land between a failure and idle, so comparing against the previous
    // state alone would drop the recovery notification.
    var kernelAlertActive = false;

    function wireKernelEvents() {
        Object.keys(KERNEL_EVENTS).forEach(function (eventName) {
            events.on(eventName, function () {
                var state = KERNEL_EVENTS[eventName];
                kernelState = state;
                if (KERNEL_ALERT_STATES[state]) {
                    kernelAlertActive = true;
                    emit("kernel_status", { state: state });
                } else if (kernelAlertActive && (state === "idle" || state === "connected")) {
                    kernelAlertActive = false;
                    emit("kernel_status", { state: state });
                }
            });
        });
    }

    // One toolbar control doubles as the status light and the pause toggle. data-state carries the
    // machine-readable state for styling and tests: assistant | ready | paused | disconnected.
    var STATUS_UI = {
        assistant:    { icon: "fa-plug", color: "#2e8540",
                        title: "Assistant connected. Click to pause the bridge." },
        ready:        { icon: "fa-plug", color: "#888",
                        title: "Bridge ready; no assistant connected. Click to pause." },
        paused:       { icon: "fa-pause", color: "#c77c11",
                        title: "Bridge paused; assistant commands are rejected. Click to resume." },
        disconnected: { icon: "fa-chain-broken", color: "#888",
                        title: "Bridge not connected; another tab may own this notebook. Click to reconnect." },
    };

    function bridgeState() {
        if (paused) { return "paused"; }
        if (!ws || ws.readyState !== WebSocket.OPEN) { return "disconnected"; }
        return assistantConnected ? "assistant" : "ready";
    }

    function renderStatus() {
        var button = $("#mcp-bridge-status button");
        if (!button.length) { return; }
        var state = bridgeState();
        var ui = STATUS_UI[state];
        $("#mcp-bridge-status").attr("data-state", state);
        button.attr("title", ui.title);
        button.find("i").attr("class", "fa " + ui.icon).css("color", ui.color);
    }

    function setPaused(next) {
        paused = next;
        // Bypasses emit() on purpose: pause transitions must reach the assistant even though
        // ordinary events are suppressed while paused.
        sendFrame({ kind: "event", name: paused ? "bridge_paused" : "bridge_resumed", data: {} });
        renderStatus();
    }

    function onStatusClick() {
        if (!paused && ws === null) {
            connect();
            renderStatus();
            return;
        }
        setPaused(!paused);
    }

    function buildStatusUI() {
        $("<style>").text(
            "@keyframes mcp-bridge-flash { from { background-color: rgba(66, 133, 244, 0.25); }" +
            " to { background-color: transparent; } }" +
            " .mcp-bridge-agent-touch { animation: mcp-bridge-flash 1.4s ease-out; }"
        ).appendTo("head");
        var button = $('<button class="btn btn-default"><i class="fa fa-plug"></i></button>');
        button.on("click", onStatusClick);
        $('<div class="btn-group" id="mcp-bridge-status"></div>').append(button)
            .appendTo(Jupyter.toolbar.element);
        renderStatus();
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
        wireKernelEvents();
        try {
            patchMoves();
        } catch (e) {
            console.warn("nbclassic-mcp-bridge: could not hook cell moves; they will not be reported", e);
        }
        try {
            buildStatusUI();
        } catch (e) {
            console.warn("nbclassic-mcp-bridge: could not build the toolbar status control", e);
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
