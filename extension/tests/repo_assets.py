import shutil
from pathlib import Path


_REPO_STATIC = Path(__file__).resolve().parents[1] / "static"


def jupyter_path_serving_the_repo(nbdir):
    """Build a Jupyter data dir whose nbextension is the working tree's main.js; return its path.

    Installing the extension *copies* static/main.js into the environment's nbextensions dir, so a
    browser otherwise runs whatever JS was current when the environment was built. A browser test
    that silently exercises a stale extension proves nothing, so point Jupyter at the repo instead.

    Parameters
    ----------
    nbdir : str or Path
        Notebook directory the server runs in. The data dir is created inside it, dot-prefixed so
        Jupyter's file browser does not list it alongside the notebooks under test.
    """
    data_dir = Path(nbdir, ".jupyter_data")
    shutil.copytree(_REPO_STATIC, data_dir / "nbextensions" / "nbclassic_mcp_bridge", dirs_exist_ok=True)
    return str(data_dir)
