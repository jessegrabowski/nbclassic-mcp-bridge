import json

from jupyter_server.base.handlers import APIHandler, JupyterHandler
from jupyter_server.base.websocket import WebSocketMixin
from tornado import web
from tornado.websocket import WebSocketClosedError, WebSocketHandler


# Key under which the shared Switchboard is stored in web_app.settings.
SWITCHBOARD_KEY = "nbclassic_mcp_bridge_switchboard"

# A notebook snapshot with image outputs runs to several MiB; lift the relay's
# frame ceiling well past tornado's 10 MiB default so it can carry one.
MAX_FRAME_BYTES = 128 * 1024 * 1024


class BridgeHandler(WebSocketMixin, WebSocketHandler, JupyterHandler):
    """WebSocket transport adapter: a *peer* in the Switchboard's vocabulary.

    All routing lives in ``Switchboard``; this class only carries frames on and off the socket.
    ``role`` and ``notebook`` are set by the switchboard during the ``hello`` handshake.
    """

    @property
    def max_message_size(self):
        return MAX_FRAME_BYTES

    def open(self):
        self.role: str | None = None
        self.notebook: str | None = None
        self._switchboard = self.settings[SWITCHBOARD_KEY]

    async def pre_get(self):
        # Reject unauthenticated upgrades. Identity comes from the Jupyter token,
        # resolved by the IdentityProvider before the handler body runs.
        if self.current_user is None:
            raise web.HTTPError(403)

    async def get(self, *args, **kwargs):
        await self.pre_get()
        return await super().get(*args, **kwargs)

    def on_message(self, raw):
        self._switchboard.route(self, raw)

    def on_close(self):
        self._switchboard.leave(self)

    def send(self, text):
        try:
            future = self.write_message(text)
        except WebSocketClosedError:
            self.log.warning("nbclassic-mcp-bridge: dropped a frame, peer socket closed")
            return

        # write_message's Future fails asynchronously if the socket closes mid-write; observe
        # that outcome (so asyncio does not log an orphaned task) and surface the dropped frame.
        def log_write_failure(f):
            if not f.cancelled() and f.exception() is not None:
                self.log.warning("nbclassic-mcp-bridge: dropped a frame, write failed: %s", f.exception())

        future.add_done_callback(log_write_failure)

    def check_xsrf_cookie(self):
        # WebSocket upgrades carry no XSRF cookie; auth is the token, enforced in pre_get.
        pass


class RoomsHandler(APIHandler):
    """Report which notebooks have live bridge rooms, so MCP clients can discover open tabs.

    The APIHandler base makes ``@web.authenticated`` answer an unauthenticated GET with a hard
    403 instead of a redirect to the login page.
    """

    @web.authenticated
    def get(self):
        switchboard = self.settings[SWITCHBOARD_KEY]
        self.finish(json.dumps({"rooms": switchboard.presence()}))
