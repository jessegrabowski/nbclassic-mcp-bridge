// Loads static/main.js under node by faking its AMD environment: a chainable jQuery stub, a
// jQuery-style event bus, a scriptable Jupyter.notebook, and a capturing WebSocket. Everything a
// test needs comes back from loadBridge().
"use strict";
const path = require("node:path");

const MAIN_JS = path.join(__dirname, "..", "..", "static", "main.js");

function chainableStub() {
    const stub = {};
    for (const method of ["attr", "find", "css", "text", "appendTo", "append", "on", "removeClass", "addClass"]) {
        stub[method] = () => stub;
    }
    stub.length = 0;
    return stub;
}

class FakeEvents {
    constructor() {
        this.handlers = new Map();
    }

    on(name, handler) {
        if (!this.handlers.has(name)) {
            this.handlers.set(name, []);
        }
        this.handlers.get(name).push(handler);
    }

    off(name, handler) {
        const registered = this.handlers.get(name) || [];
        this.handlers.set(name, registered.filter((h) => h !== handler));
    }

    one(name, handler) {
        const once = (evt, data) => {
            this.off(name, once);
            handler(evt, data);
        };
        this.on(name, once);
    }

    trigger(name, data) {
        for (const handler of [...(this.handlers.get(name) || [])]) {
            handler({}, data);
        }
    }
}

class FakeWebSocket {
    static CONNECTING = 0;
    static OPEN = 1;

    constructor(url) {
        this.url = url;
        this.readyState = FakeWebSocket.CONNECTING;
        this.sent = [];
        FakeWebSocket.instances.push(this);
    }

    send(text) {
        this.sent.push(JSON.parse(text));
    }

    open() {
        this.readyState = FakeWebSocket.OPEN;
        if (this.onopen) {
            this.onopen();
        }
    }

    receive(frame) {
        this.onmessage({ data: JSON.stringify(frame) });
    }

    close(code, reason) {
        this.readyState = 3;
        if (this.onclose) {
            this.onclose({ code: code, reason: reason });
        }
    }
}

function makeCell(id, source, options = {}) {
    const cell = {
        id: id,
        cell_type: options.cell_type || "code",
        rendered: options.rendered || false,
        _text: source,
        outputs: options.outputs || [],
        element: null,
        get_text() {
            return this._text;
        },
        set_text(text) {
            this._text = text;
            cell.notebook_events.trigger("change.Cell", { cell: cell });
        },
        render() {
            this.rendered = true;
        },
        toJSON() {
            return { id: this.id, source: this._text, cell_type: this.cell_type, outputs: this.outputs };
        },
        fromJSON(data) {
            this.id = data.id;
            this._text = data.source;
            this.outputs = data.outputs;
        },
        execute() {
            cell.executed = true;
        },
        output_area: {
            toJSON() {
                // Shallow-copies each output the way nbclassic does: the data dict stays shared.
                return cell.outputs.map((output) => ({ ...output }));
            },
        },
    };
    return cell;
}

function makeNotebook(events, cells) {
    const notebook = {
        notebook_path: "fake.ipynb",
        base_url: "/",
        _fully_loaded: true,
        cells: cells,
        kernel: {
            name: "python3",
            connected: true,
            interrupted: false,
            is_connected() {
                return this.connected;
            },
            interrupt() {
                this.interrupted = true;
            },
            execute() {},
        },
        get_cells() {
            return this.cells;
        },
        find_cell_index(cell) {
            return this.cells.indexOf(cell);
        },
        insert_cell_at_index(cell_type, index) {
            const cell = makeCell(`generated-${this.cells.length}`, "", { cell_type: cell_type });
            cell.notebook_events = events;
            this.cells.splice(Math.min(index, this.cells.length), 0, cell);
            events.trigger("create.Cell", { cell: cell, index: index });
            return cell;
        },
        delete_cell(index) {
            const [removed] = this.cells.splice(index, 1);
            events.trigger("delete.Cell", { cell: removed });
        },
    };
    return notebook;
}

function loadBridge(seed = [["c1", "print(1)"], ["c2", "print(2)"]]) {
    const events = new FakeEvents();
    const cells = seed.map(([id, source, options]) => {
        const cell = makeCell(id, source, options);
        cell.notebook_events = events;
        return cell;
    });
    const notebook = makeNotebook(events, cells);
    const jupyter = { notebook: notebook, toolbar: { element: chainableStub() } };

    FakeWebSocket.instances = [];
    global.WebSocket = FakeWebSocket;
    global.window = {
        location: { protocol: "http:", host: "localhost:8888" },
        addEventListener() {},
    };
    global.document = { addEventListener() {}, hidden: false };
    let bridge;
    global.define = (deps, factory) => {
        bridge = factory(() => chainableStub(), jupyter, events);
    };

    delete require.cache[require.resolve(MAIN_JS)];
    require(MAIN_JS);
    bridge.load_ipython_extension();
    return { events: events, notebook: notebook, cells: cells, sockets: FakeWebSocket.instances };
}

module.exports = { loadBridge: loadBridge, makeCell: makeCell, FakeWebSocket: FakeWebSocket };
