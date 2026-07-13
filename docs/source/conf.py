import os
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version


project = "nbclassic-mcp-bridge"
copyright = "2026, Jesse Grabowski"
author = "Jesse Grabowski"
language = "en"
html_baseurl = "https://nbclassic-mcp-bridge.readthedocs.io"

try:
    version = package_version("nbclassic-mcp-bridge")
except PackageNotFoundError:
    version = "dev"

rtd_version = os.environ.get("READTHEDOCS_VERSION", "")
if os.environ.get("READTHEDOCS"):
    if rtd_version.lower() == "stable":
        version = version.split("+")[0]
    elif rtd_version.lower() == "latest":
        version = "dev"
    else:
        version = rtd_version
release = version

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.autosummary",
    "sphinx.ext.autosectionlabel",
    "numpydoc",
    "myst_parser",
    "sphinx_copybutton",
    "sphinx_design",
]
autodoc_typehints = "description"
numpydoc_show_class_members = False
myst_enable_extensions = ["colon_fence", "deflist", "fieldlist"]
autosectionlabel_prefix_document = True

html_theme = "pydata_sphinx_theme"
html_theme_options = {
    "github_url": "https://github.com/jessegrabowski/nbclassic-mcp-bridge",
    "use_edit_page_button": False,
    "navbar_end": ["theme-switcher", "navbar-icon-links"],
}
html_title = "nbclassic-mcp-bridge"

source_suffix = {".rst": "restructuredtext", ".md": "markdown"}
exclude_patterns = ["_build"]
