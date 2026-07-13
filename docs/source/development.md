# Development

## Layout

```text
extension/    nbclassic-mcp-bridge: frontend extension (static/main.js) + relay (switchboard, handlers)
mcp_server/   nbclassic-mcp-bridge-mcp: the MCP server (tools, RelayClient, discovery)
docs/         this site
pyproject.toml  tooling-only root: ruff, pytest, and pixi config (not an installable package)
```

## Environment and tasks

[pixi](https://pixi.sh) manages everything:

```bash
pixi run unit        # Python unit + integration tests
pixi run js-test     # the frontend extension under node --test (no browser)
pixi run e2e         # real server + Chromium + kernel (pixi run install-browsers once first)
pixi run tests       # all of the above
pixi run -e lint lint
pixi run docs        # build this site into docs/build
pixi run serve-docs  # build, then serve at http://127.0.0.1:8899
```

pixi editable-installs both packages into its environment; to work outside pixi, install them the
same way by hand with `pip install -e extension/ -e mcp_server/`.

Tests shuffle by default (`pytest-randomly`); reproduce an ordering with `--randomly-seed=N`.

## Test tiers

- **Unit** — the switchboard through a fake peer, the RelayClient against a scriptable in-process
  relay, pure helpers.
- **JS** — `main.js` runs headless under node with a faked AMD/Jupyter/WebSocket environment
  (`extension/tests/js/harness.js`); this is where op dispatch, echo suppression, and undo
  semantics are pinned.
- **Integration** — real websockets against a real Jupyter server, no browser.
- **e2e** — Playwright-driven Chromium against a live server and kernel, including one test that
  drives the actual MCP server binary over stdio with the MCP client SDK.

Two rules the suite holds to: never use a bare sleep as synchronization (poll the condition
instead), and any test that mutates shared long-lived state (such as the module-scoped kernel)
must restore it in a `finally`.

## Editing the frontend extension

`extension/static/main.js` is wheel shared-data: pip *copies* it into
`share/jupyter/nbextensions/` at install time, even for editable installs. After editing it,
re-copy (or reinstall) and refresh the browser tab — the server does not need a restart, but the
tab serves whatever file is in the share directory.

## Releases

One git tag `vX.Y.Z` versions both packages (hatch-vcs). A published GitHub release builds and
inspects both distributions and publishes them to PyPI via trusted publishing, gated by the
`release` environment.
