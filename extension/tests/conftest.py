import socket
import time
import urllib.request


def free_port():
    """Return an OS-assigned free TCP port."""
    s = socket.socket()
    s.bind(("", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_until_up(port, token, timeout=30, label="jupyter server"):
    """Block until the Jupyter server on ``port`` answers ``/api/status``."""
    deadline = time.time() + timeout
    url = f"http://localhost:{port}/api/status?token={token}"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as r:
                if r.status == 200:
                    return
        except OSError:
            time.sleep(0.3)
    raise RuntimeError(f"{label} on port {port} did not come up")
