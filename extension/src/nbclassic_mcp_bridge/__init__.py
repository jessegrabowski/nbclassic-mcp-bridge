from importlib.metadata import version

from jupyter_server.utils import url_path_join

from nbclassic_mcp_bridge.relay import SWITCHBOARD_KEY, BridgeHandler, RoomsHandler
from nbclassic_mcp_bridge.switchboard import Switchboard


__version__ = version("nbclassic-mcp-bridge")


def _jupyter_server_extension_points():
    return [{"module": "nbclassic_mcp_bridge"}]


def _load_jupyter_server_extension(server_app):
    web_app = server_app.web_app
    web_app.settings[SWITCHBOARD_KEY] = Switchboard()
    route = url_path_join(web_app.settings["base_url"], "mcp-bridge")
    web_app.add_handlers(
        ".*$",
        [
            (route, BridgeHandler),
            (url_path_join(route, "rooms"), RoomsHandler),
        ],
    )
    server_app.log.info("nbclassic-mcp-bridge: relay mounted at %s", route)
