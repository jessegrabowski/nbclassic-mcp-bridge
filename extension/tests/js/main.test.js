"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");

const { loadBridge, makeCell } = require("./harness");

function connect(bridge) {
    const socket = bridge.sockets[0];
    socket.open();
    return socket;
}

function lastReply(socket, id) {
    const replies = socket.sent.filter((frame) => frame.kind === "reply" && frame.id === id);
    assert.equal(replies.length, 1, `expected exactly one reply for id ${id}`);
    return replies[0];
}

function eventsSent(socket, name) {
    return socket.sent.filter((frame) => frame.kind === "event" && frame.name === name);
}

test("hello carries the protocol version and notebook path", () => {
    const bridge = loadBridge();
    const socket = connect(bridge);
    assert.deepEqual(socket.sent[0], {
        kind: "hello",
        protocol: 0,
        role: "extension",
        notebook: "fake.ipynb",
    });
});

test("snapshot returns every cell; summary mode swaps outputs for summaries", () => {
    const outputs = [{ output_type: "stream", name: "stdout", text: "hi\n" }];
    const bridge = loadBridge([["c1", "print(1)", { outputs }]]);
    const socket = connect(bridge);

    socket.receive({ kind: "cmd", id: 1, op: "snapshot", args: {} });
    const full = lastReply(socket, 1);
    assert.equal(full.ok, true);
    assert.deepEqual(full.result[0].outputs, outputs);

    socket.receive({ kind: "cmd", id: 2, op: "snapshot", args: { outputs: "summary" } });
    const summary = lastReply(socket, 2).result[0];
    assert.equal(summary.outputs, undefined);
    assert.deepEqual(summary.output_summary, [{ output_type: "stream", name: "stdout", chars: 3 }]);
});

test("set_source writes unfocused cells and skips the cell the human is editing", () => {
    const bridge = loadBridge();
    const socket = connect(bridge);

    socket.receive({ kind: "cmd", id: 1, op: "set_source", args: { cell_id: "c1", source: "edited" } });
    assert.equal(lastReply(socket, 1).result.status, "written");
    assert.equal(bridge.cells[0].get_text(), "edited");

    bridge.events.trigger("edit_mode.Cell", { cell: bridge.cells[1] });
    socket.receive({ kind: "cmd", id: 2, op: "set_source", args: { cell_id: "c2", source: "clobber" } });
    assert.equal(lastReply(socket, 2).result.status, "skipped");
    assert.equal(bridge.cells[1].get_text(), "print(2)");
});

test("agent mutations emit no events; human ones do", (t) => {
    t.mock.timers.enable({ apis: ["setTimeout"] });
    const bridge = loadBridge();
    const socket = connect(bridge);

    socket.receive({ kind: "cmd", id: 1, op: "insert_cell", args: { index: 0, cell_type: "code", source: "new" } });
    socket.receive({ kind: "cmd", id: 2, op: "delete_cell", args: { cell_id: lastReply(socket, 1).result.cell_id } });
    t.mock.timers.tick(1000);
    assert.deepEqual(eventsSent(socket, "cell_created"), []);
    assert.deepEqual(eventsSent(socket, "cell_deleted"), []);
    assert.deepEqual(eventsSent(socket, "source_changed"), []);

    const humanCell = makeCell("h1", "human()");
    humanCell.notebook_events = bridge.events;
    bridge.events.trigger("create.Cell", { cell: humanCell, index: 0 });
    assert.equal(eventsSent(socket, "cell_created").length, 1);
});

test("a human edit surfaces after the debounce; the agent's own write does not echo", (t) => {
    t.mock.timers.enable({ apis: ["setTimeout"] });
    const bridge = loadBridge();
    const socket = connect(bridge);

    socket.receive({ kind: "cmd", id: 1, op: "set_source", args: { cell_id: "c1", source: "agent text" } });
    t.mock.timers.tick(1000);
    assert.deepEqual(eventsSent(socket, "source_changed"), []);

    bridge.cells[0].set_text("human text");
    t.mock.timers.tick(1000);
    const changes = eventsSent(socket, "source_changed");
    assert.equal(changes.length, 1);
    assert.equal(changes[0].data.source, "human text");
});

test("human cell_executed events carry image stubs without corrupting the cell", () => {
    const imageOutput = {
        output_type: "display_data",
        data: { "image/png": "BASE64", "text/plain": "<Figure>" },
    };
    const bridge = loadBridge([["c1", "plot()", { outputs: [imageOutput] }]]);
    const socket = connect(bridge);

    bridge.events.trigger("finished_execute.CodeCell", { cell: bridge.cells[0] });
    const [executed] = eventsSent(socket, "cell_executed");
    assert.match(executed.data.outputs[0].data["image/png"], /omitted from event/);
    assert.equal(executed.data.outputs[0].data["text/plain"], "<Figure>");
    assert.equal(imageOutput.data["image/png"], "BASE64", "the live output must stay intact");
});

test("an evicted socket stands down instead of reconnecting", (t) => {
    t.mock.timers.enable({ apis: ["setTimeout"] });
    const bridge = loadBridge();
    const socket = connect(bridge);

    socket.close(1001, "replaced by a newer connection");
    t.mock.timers.tick(120000);
    assert.equal(bridge.sockets.length, 1);
});

test("an ordinary disconnect reconnects on backoff", (t) => {
    t.mock.timers.enable({ apis: ["setTimeout"] });
    const bridge = loadBridge();
    const socket = connect(bridge);

    socket.close(1006, "");
    t.mock.timers.tick(120000);
    assert.ok(bridge.sockets.length > 1, "expected a reconnect attempt");
});

test("execute fails fast when the kernel is not connected", () => {
    const bridge = loadBridge();
    const socket = connect(bridge);
    bridge.notebook.kernel.connected = false;

    socket.receive({ kind: "cmd", id: 1, op: "execute_cell", args: { cell_id: "c1" } });
    const reply = lastReply(socket, 1);
    assert.equal(reply.ok, false);
    assert.match(reply.error, /kernel is not connected/);
});

test("interrupt_kernel reaches the kernel and unknown ops error", () => {
    const bridge = loadBridge();
    const socket = connect(bridge);

    socket.receive({ kind: "cmd", id: 1, op: "interrupt_kernel", args: {} });
    assert.equal(lastReply(socket, 1).ok, true);
    assert.equal(bridge.notebook.kernel.interrupted, true);

    socket.receive({ kind: "cmd", id: 2, op: "nonsense", args: {} });
    assert.match(lastReply(socket, 2).error, /unknown op/);
});

test("kernel failures push kernel_status; recovery pushes exactly once", () => {
    const bridge = loadBridge();
    const socket = connect(bridge);

    bridge.events.trigger("kernel_busy.Kernel", {});
    assert.deepEqual(eventsSent(socket, "kernel_status"), [], "busy/idle churn must not be pushed");

    bridge.events.trigger("kernel_dead.Kernel", {});
    bridge.events.trigger("kernel_starting.Kernel", {});
    bridge.events.trigger("kernel_ready.Kernel", {});
    bridge.events.trigger("kernel_idle.Kernel", {});
    const pushed = eventsSent(socket, "kernel_status").map((frame) => frame.data.state);
    assert.deepEqual(pushed, ["dead", "idle"]);
});

test("move_cell keeps identity and emits nothing", () => {
    const bridge = loadBridge();
    const socket = connect(bridge);

    socket.receive({ kind: "cmd", id: 1, op: "move_cell", args: { cell_id: "c1", index: 1 } });
    const reply = lastReply(socket, 1);
    assert.equal(reply.ok, true);
    assert.deepEqual(reply.result, { cell_id: "c1", index: 1 });
    assert.deepEqual(bridge.notebook.get_cells().map((cell) => cell.id), ["c2", "c1"]);
    assert.deepEqual(eventsSent(socket, "cell_deleted"), []);
    assert.deepEqual(eventsSent(socket, "cell_created"), []);
});


test("undo_last reverses set/insert/delete/move in reverse order", () => {
    const bridge = loadBridge();
    const socket = connect(bridge);

    socket.receive({ kind: "cmd", id: 1, op: "set_source", args: { cell_id: "c1", source: "edited" } });
    socket.receive({ kind: "cmd", id: 2, op: "insert_cell", args: { index: 2, cell_type: "code", source: "new" } });
    const insertedId = lastReply(socket, 2).result.cell_id;
    socket.receive({ kind: "cmd", id: 3, op: "delete_cell", args: { cell_id: "c2" } });
    socket.receive({ kind: "cmd", id: 4, op: "move_cell", args: { cell_id: "c1", index: 1 } });

    socket.receive({ kind: "cmd", id: 5, op: "undo_last", args: {} });
    assert.equal(lastReply(socket, 5).result.status, "undone");
    assert.equal(bridge.notebook.find_cell_index(bridge.cells.find((c) => c.id === "c1")), 0);

    socket.receive({ kind: "cmd", id: 6, op: "undo_last", args: {} });
    assert.equal(lastReply(socket, 6).result.status, "undone");
    const restored = bridge.notebook.get_cells().find((c) => c.id === "c2");
    assert.equal(restored.get_text(), "print(2)");

    socket.receive({ kind: "cmd", id: 7, op: "undo_all", args: {} });
    const summary = lastReply(socket, 7).result.results.map((r) => r.status);
    assert.deepEqual(summary, ["undone", "undone"]);
    assert.equal(bridge.notebook.get_cells().find((c) => c.id === insertedId), undefined);
    assert.equal(bridge.notebook.get_cells().find((c) => c.id === "c1").get_text(), "print(1)");

    socket.receive({ kind: "cmd", id: 8, op: "undo_last", args: {} });
    assert.equal(lastReply(socket, 8).result.status, "nothing to undo");
});

test("undo skips entries the human has touched since", () => {
    const bridge = loadBridge();
    const socket = connect(bridge);

    socket.receive({ kind: "cmd", id: 1, op: "set_source", args: { cell_id: "c1", source: "agent" } });
    bridge.cells[0]._text = "human overwrote"; // bypass set_text so no change event fires

    socket.receive({ kind: "cmd", id: 2, op: "undo_last", args: {} });
    const reply = lastReply(socket, 2).result;
    assert.equal(reply.status, "skipped");
    assert.match(reply.reason, /changed since/);
    assert.equal(bridge.cells[0].get_text(), "human overwrote");
});

test("undoing a delete restores the cell's identity, outputs, and position", () => {
    const outputs = [{ output_type: "stream", name: "stdout", text: "kept\n" }];
    const bridge = loadBridge([["c1", "print(1)"], ["c2", "print(2)", { outputs }]]);
    const socket = connect(bridge);

    socket.receive({ kind: "cmd", id: 1, op: "delete_cell", args: { cell_id: "c2" } });
    socket.receive({ kind: "cmd", id: 2, op: "undo_last", args: {} });
    assert.equal(lastReply(socket, 2).result.status, "undone");
    const restored = bridge.notebook.get_cells()[1];
    assert.equal(restored.id, "c2");
    assert.deepEqual(restored.outputs, outputs);
});

test("undo actions emit no human events", (t) => {
    t.mock.timers.enable({ apis: ["setTimeout"] });
    const bridge = loadBridge();
    const socket = connect(bridge);

    socket.receive({ kind: "cmd", id: 1, op: "set_source", args: { cell_id: "c1", source: "agent" } });
    socket.receive({ kind: "cmd", id: 2, op: "undo_last", args: {} });
    t.mock.timers.tick(1000);
    assert.deepEqual(socket.sent.filter((f) => f.kind === "event"), []);
});

test("the undo stack is bounded", () => {
    const bridge = loadBridge();
    const socket = connect(bridge);
    for (let n = 0; n < 60; n++) {
        socket.receive({ kind: "cmd", id: 100 + n, op: "set_source", args: { cell_id: "c1", source: `v${n}` } });
    }
    socket.receive({ kind: "cmd", id: 999, op: "undo_all", args: {} });
    assert.equal(lastReply(socket, 999).result.results.length, 50);
});


test("undo guards refuse to delete or move cells the human drifted", () => {
    const bridge = loadBridge();
    const socket = connect(bridge);

    socket.receive({ kind: "cmd", id: 1, op: "insert_cell", args: { index: 0, cell_type: "code", source: "mine" } });
    const insertedId = lastReply(socket, 1).result.cell_id;
    bridge.notebook.get_cells()[0]._text = "the human built on this";
    socket.receive({ kind: "cmd", id: 2, op: "undo_last", args: {} });
    assert.equal(lastReply(socket, 2).result.status, "skipped");
    assert.ok(bridge.notebook.get_cells().find((c) => c.id === insertedId), "the edited cell must survive");

    socket.receive({ kind: "cmd", id: 3, op: "move_cell", args: { cell_id: "c1", index: 2 } });
    const moved = bridge.notebook.get_cells().find((c) => c.id === "c1");
    bridge.notebook.cells.splice(bridge.notebook.cells.indexOf(moved), 1);
    bridge.notebook.cells.unshift(moved); // the human moved it somewhere else since
    socket.receive({ kind: "cmd", id: 4, op: "undo_last", args: {} });
    assert.equal(lastReply(socket, 4).result.status, "skipped");
    assert.equal(bridge.notebook.find_cell_index(moved), 0, "the human's placement must survive");
});
