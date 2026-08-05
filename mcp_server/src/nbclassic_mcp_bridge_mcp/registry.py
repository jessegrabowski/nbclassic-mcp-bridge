from collections.abc import Callable
from dataclasses import dataclass

from nbclassic_mcp_bridge_mcp.relay_client import RelayClient


@dataclass(frozen=True)
class Attachment:
    """One attached notebook: a path on a particular Jupyter server."""

    jupyter_url: str
    path: str


class NotebookRegistry:
    """Own the relay connection for each attached notebook and track which one the tools target.

    Attachments are keyed by ``Attachment``, so the same path on two Jupyter servers is two distinct
    entries and each ``RelayClient`` carries the endpoint it was attached against. The registry
    separately holds the *default* endpoint -- where ``attach`` connects and what discovery queries
    first -- which ``retarget`` rewrites without disturbing anything already attached.

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
        self._clients: dict[Attachment, RelayClient] = {}
        self._current: Attachment | None = None

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

        Leave every other attachment alone. Reuse the existing client when the same notebook is
        attached again, so a repeated ``use_notebook`` keeps its event log and its position in the
        relay's event numbering. A failed connect registers nothing and moves nothing.
        """
        attachment = Attachment(self._jupyter_url, path)
        client = self._clients.get(attachment)
        if client is None:
            client = self._client_factory(jupyter_url=self._jupyter_url, token=self._token)
        await client.connect(path)
        self._clients[attachment] = client
        self._current = attachment
        return client

    async def detach(self, attachment: Attachment) -> None:
        """Drop one attachment, leaving the browser tab and its kernel untouched.

        Detaching the current notebook leaves no current notebook.
        """
        # Popped before the close is awaited: a teardown that raises must not leave the registry
        # still holding a client whose socket is half gone.
        client = self._clients.pop(attachment, None)
        if client is None:
            raise RuntimeError(f"{attachment.path} is not attached")
        if self._current == attachment:
            self._current = None
        await client.close()

    def current(self) -> RelayClient:
        """Return the client for the notebook the tools target.

        Raise RuntimeError when there is no current notebook, naming what is attached so the caller
        can pick one.
        """
        if self._current is None:
            if self._clients:
                raise RuntimeError(
                    f"no current notebook; attached: {self.attached_listing()} -- call use_notebook to pick one"
                )
            raise RuntimeError("not attached -- call use_notebook first")
        return self._clients[self._current]

    def attachments(self) -> list[Attachment]:
        """Return every attached notebook, in the order it was first attached."""
        return list(self._clients)

    def is_attached(self, attachment: Attachment) -> bool:
        """Report whether ``attachment`` is attached."""
        return attachment in self._clients

    def is_current(self, attachment: Attachment) -> bool:
        """Report whether ``attachment`` is the notebook the tools target by default."""
        return self._current == attachment

    def label(self, attachment: Attachment) -> str:
        """Name ``attachment`` for the assistant, qualified by server only when another attachment
        shares its path."""
        collides = any(
            other.path == attachment.path and other.jupyter_url != attachment.jupyter_url for other in self._clients
        )
        return f"{attachment.path} on {attachment.jupyter_url}" if collides else attachment.path

    def attached_listing(self) -> str:
        """Name every attached notebook as a sorted comma-separated list, or ``"none"``."""
        return ", ".join(sorted(self.label(attachment) for attachment in self._clients)) or "none"

    def matching(self, path: str) -> list[Attachment]:
        """Return every attachment whose path is exactly ``path``."""
        return [attachment for attachment in self._clients if attachment.path == path]

    def endpoints(self) -> list[tuple[str, str]]:
        """Return ``(jupyter_url, token)`` for the default server and every attached notebook's.

        The default comes first, and a server holding several attachments appears once.
        """
        endpoints = [(self._jupyter_url, self._token)]
        endpoints += [(client.jupyter_url, client.token) for client in self._clients.values()]
        return list(dict.fromkeys(endpoints))

    def events_since(self, cursor: int) -> tuple[list[dict], int]:
        """Return the current notebook's events after ``cursor``, and the cursor to pass next.

        Polling before anything is attached yields nothing rather than raising, so an assistant can
        watch for edits without first committing to a notebook.
        """
        if self._current is None:
            return [], cursor
        return self.current().events_since(cursor)

    def retarget(self, jupyter_url: str, token: str) -> None:
        """Point new attachments at a different Jupyter server.

        Leave existing attachments connected to the server they were attached against, so notebooks
        on two servers can be held at once.
        """
        self._jupyter_url = jupyter_url
        self._token = token
