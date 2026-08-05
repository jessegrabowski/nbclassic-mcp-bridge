import logging
from collections.abc import Callable

from nbclassic_mcp_bridge_mcp.relay_client import RelayClient


log = logging.getLogger(__name__)


class NotebookRegistry:
    """Own the relay connection for each attached notebook and track which one the tools target.

    An attachment is keyed by ``(jupyter_url, path)``, so the same path on two Jupyter servers is two
    distinct attachments and each ``RelayClient`` carries the endpoint it was attached against. The
    registry separately holds the *default* endpoint -- where ``attach`` connects and what discovery
    queries -- which ``retarget`` rewrites.

    Attaching closes every other attachment.

    Parameters
    ----------
    jupyter_url : str
        Base URL of the Jupyter server new attachments connect to.
    token : str
        Authentication token for that server.
    client_factory : callable, optional
        Builds the per-notebook relay client, called with ``jupyter_url`` and ``token`` keywords.
        Default ``RelayClient``.
    """

    def __init__(self, jupyter_url: str, token: str, client_factory: Callable[..., RelayClient] = RelayClient):
        self._jupyter_url = jupyter_url
        self._token = token
        self._client_factory = client_factory
        self._clients: dict[tuple[str, str], RelayClient] = {}
        self._current: tuple[str, str] | None = None

    @property
    def jupyter_url(self) -> str:
        """Base URL of the Jupyter server new attachments connect to."""
        return self._jupyter_url

    @property
    def token(self) -> str:
        """Authentication token for the default Jupyter server."""
        return self._token

    async def attach(self, path: str) -> RelayClient:
        """Connect to ``path`` on the default server and make it the notebook the tools target.

        Reuse the existing client when the same notebook is attached again, so a repeated
        ``use_notebook`` keeps its event log and its position in the relay's event numbering.
        """
        key = (self._jupyter_url, path)
        for stale in [existing for existing in self._clients if existing != key]:
            # Dropped before the new notebook is dialed, so a connect failure has to leave the
            # registry saying nothing is attached rather than naming a client it no longer holds.
            if stale == self._current:
                self._current = None
            await self._clients.pop(stale).close()
        client = self._clients.get(key)
        if client is None:
            client = self._clients[key] = self._client_factory(jupyter_url=self._jupyter_url, token=self._token)
        await client.connect(path)
        self._current = key
        return client

    def current(self) -> RelayClient:
        """Return the client for the notebook the tools target.

        Raise RuntimeError when nothing is attached.
        """
        if self._current is None:
            raise RuntimeError("not attached -- call use_notebook first")
        return self._clients[self._current]

    def events_since(self, cursor: int) -> tuple[list[dict], int]:
        """Return the attached notebook's events after ``cursor``, and the cursor to pass next.

        Polling before anything is attached yields nothing rather than raising, so an assistant can
        watch for edits without first committing to a notebook.
        """
        if self._current is None:
            return [], cursor
        return self.current().events_since(cursor)

    async def retarget(self, jupyter_url: str, token: str) -> None:
        """Point new attachments at a different Jupyter server, dropping the current attachments."""
        await self.close_all()
        self._jupyter_url = jupyter_url
        self._token = token

    async def close_all(self) -> None:
        """Drop every attachment. The browser tabs and their kernels are untouched."""
        # State first, so a close that raises cannot leave the registry pointing at a client whose
        # teardown only partly ran.
        clients = list(self._clients.values())
        self._clients.clear()
        self._current = None
        for client in clients:
            await client.close()
