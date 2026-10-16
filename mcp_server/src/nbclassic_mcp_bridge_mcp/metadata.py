from collections.abc import Callable, Iterator


# Written against nbclassic 1.3, nbconvert 7.17, and the RISE vendored in nbclassic-extras. Each
# entry records what a reader actually does with the key, so a change upstream means editing here.
_SLIDE_TYPES = {
    "slide": "start a new top-level slide, reached by moving across",
    "subslide": "start a new subslide under the current slide, reached by moving down",
    "fragment": "reveal this cell as the next step within the current subslide",
    "-": "continue the current slide, subslide, or fragment",
    "notes": "show only in the speaker notes view",
    "skip": "leave out of the slideshow",
}

# Roles that make RISE treat a notebook as prepared for slides; see outline_slides.
_SLIDE_ROLES = set(_SLIDE_TYPES) - {"-"}

_REVEAL_THEMES = [
    "beige",
    "black",
    "black-contrast",
    "blood",
    "dracula",
    "league",
    "moon",
    "night",
    "serif",
    "simple",
    "sky",
    "solarized",
    "white",
    "white-contrast",
]

_REVEAL_TRANSITIONS = ["none", "fade", "slide", "convex", "concave", "zoom", "cube", "page", "linear"]

_TAG_REMOVAL_NOTE = (
    "Download as LaTeX removes cells, inputs, or outputs only for tags named in the Jupyter "
    "server's TagRemovePreprocessor config (remove_cell_tags, remove_input_tags, "
    "remove_all_outputs_tags, remove_single_output_tags). All four are empty by default, so no "
    "tag changes the export until the server config names it."
)


def _rise_option(type_name: str, description: str, choices: list[str] | None = None) -> dict:
    entry = {"type": type_name, "read_by": ["RISE"], "description": description}
    if choices is not None:
        entry["choices"] = choices
    return entry


CATALOG = {
    "cell": {
        "slideshow.slide_type": {
            "type": "string",
            "choices": _SLIDE_TYPES,
            "read_by": ["RISE", "nbclassic Slideshow cell toolbar"],
            "description": (
                "The cell's place in the slideshow. Leaving it unset acts like '-', except in a "
                "notebook where no cell has a slide role (slide, subslide, fragment, notes, "
                "skip): there every unset cell starts its own slide. Call slide_outline to see "
                "the resulting layout."
            ),
        },
        "tags": {
            "type": "list of string",
            "read_by": ["nbclassic Tags cell toolbar", "nbconvert TagRemovePreprocessor"],
            "description": "Free-form labels. " + _TAG_REMOVAL_NOTE,
        },
        "raw_mimetype": {
            "type": "string",
            "suggested": [
                "text/latex",
                "text/restructuredtext",
                "text/html",
                "text/markdown",
                "text/x-python",
            ],
            "read_by": ["nbclassic Raw Cell Format toolbar", "nbconvert"],
            "description": (
                "Raw cells only: the format an exporter should treat the cell as. Download as "
                "LaTeX copies a raw cell into the .tex verbatim when this is text/latex or unset, "
                "and drops it otherwise. Values outside the suggestions are allowed."
            ),
        },
        "editable": {
            "type": "boolean",
            "read_by": ["nbclassic"],
            "description": "false stops the human editing the cell. true and unset are the same.",
        },
        "deletable": {
            "type": "boolean",
            "read_by": ["nbclassic"],
            "description": "false stops the human deleting the cell. true and unset are the same.",
        },
    },
    "notebook": {
        "title": {
            "type": "string",
            "read_by": ["nbconvert LaTeX template"],
            "description": "Document title in the LaTeX export, or the notebook's file name when unset.",
        },
        "authors": {
            "type": "list of {name: string}",
            "read_by": ["nbconvert LaTeX template"],
            "description": "Authors in the LaTeX export, joined with commas.",
        },
        "date": {
            "type": "string",
            "read_by": ["nbconvert LaTeX template"],
            "description": "Date in the LaTeX export, escaped as plain text. LaTeX prints today's date when unset.",
        },
        "rise.theme": _rise_option(type_name="string", description="reveal.js theme.", choices=_REVEAL_THEMES),
        "rise.transition": _rise_option(
            type_name="string", description="Animation between slides.", choices=_REVEAL_TRANSITIONS
        ),
        "rise.start_slideshow_at": _rise_option(
            type_name="string",
            description="Open the slideshow at the first slide or at the slide holding the selected cell.",
            choices=["beginning", "selected"],
        ),
        "rise.auto_select": _rise_option(
            type_name="string",
            description="Select the first code cell of each newly shown slide or fragment, or nothing.",
            choices=["code", "none"],
        ),
        "rise.auto_select_fragment": _rise_option(
            type_name="boolean",
            description="With auto_select, look only at the current fragment instead of the whole slide.",
        ),
        "rise.scroll": _rise_option(
            type_name="boolean", description="Make a (sub)slide taller than the screen scrollable."
        ),
        "rise.center": _rise_option(type_name="boolean", description="Center slide content vertically."),
        "rise.slideNumber": _rise_option(
            type_name="boolean or string",
            description="Number the slides. A string such as 'c/t' picks reveal.js's format.",
        ),
        "rise.controls": _rise_option(type_name="boolean", description="Show reveal.js navigation arrows."),
        "rise.progress": _rise_option(type_name="boolean", description="Show a progress bar along the bottom."),
        "rise.history": _rise_option(type_name="boolean", description="Push each slide change to the browser history."),
        "rise.width": _rise_option(
            type_name="string or number", description="Slide width, in pixels or a CSS length such as '100%'."
        ),
        "rise.height": _rise_option(
            type_name="string or number", description="Slide height, in pixels or a CSS length such as '100%'."
        ),
        "rise.header": _rise_option(
            type_name="string", description="HTML shown at the top of every slide, ignored when overlay is set."
        ),
        "rise.footer": _rise_option(
            type_name="string", description="HTML shown at the bottom of every slide, ignored when overlay is set."
        ),
        "rise.backimage": _rise_option(
            type_name="string",
            description="Relative path of a background image for every slide, ignored when overlay is set.",
        ),
        "rise.overlay": _rise_option(
            type_name="string", description="HTML placed on every slide, replacing header, footer, and backimage."
        ),
        "rise.show_buttons_on_startup": _rise_option(
            type_name="boolean", description="Show the exit and help buttons when the slideshow opens."
        ),
        "rise.enable_chalkboard": _rise_option(
            type_name="boolean", description="Turn on the chalkboard drawing plugin."
        ),
    },
}

_TYPE_CHECKS: dict[str, Callable[[object], bool]] = {
    "string": lambda value: isinstance(value, str),
    "boolean": lambda value: isinstance(value, bool),
    "list of string": lambda value: isinstance(value, list) and all(isinstance(item, str) for item in value),
    "list of {name: string}": lambda value: (
        isinstance(value, list) and all(isinstance(item, dict) and isinstance(item.get("name"), str) for item in value)
    ),
    "boolean or string": lambda value: isinstance(value, (bool, str)),
    "string or number": lambda value: isinstance(value, (str, int, float)) and not isinstance(value, bool),
}


def _patch_nodes(patch: dict, prefix: str = "") -> Iterator[tuple[str, object]]:
    """Yield every key a merge patch sets, nested ones included, as (dotted path, value)."""
    for key, value in patch.items():
        path = prefix + key
        yield path, value
        if isinstance(value, dict):
            yield from _patch_nodes(value, prefix=path + ".")


def _check_node(path: str, value: object, entries: dict) -> str | None:
    """Describe what is wrong with one patched key, or return None when it is acceptable."""
    entry = entries.get(path)
    if entry is not None:
        if not _TYPE_CHECKS[entry["type"]](value):
            return f"{path} must be {entry['type']}, got {value!r}"
        if "choices" in entry and value not in entry["choices"]:
            return f"{path} must be one of {', '.join(entry['choices'])}; got {value!r}"
        return None
    children = sorted(known for known in entries if known.startswith(path + "."))
    if children and not isinstance(value, dict):
        return f"{path} must be an object (holding {', '.join(children)}), got {value!r}"
    return None


def validate_patch(patch: dict, scope: str) -> None:
    """Reject a merge patch that sets a catalogued key to a value its reader does not accept.

    Keys outside the catalog pass untouched, and a ``None`` value deletes a key, so it is always
    accepted.

    Parameters
    ----------
    patch : dict
        RFC 7386 merge patch about to be applied.
    scope : {"cell", "notebook"}
        Which metadata the patch targets.
    """
    entries = CATALOG[scope]
    problems = [
        problem
        for path, value in _patch_nodes(patch)
        if value is not None and (problem := _check_node(path=path, value=value, entries=entries)) is not None
    ]
    if problems:
        raise ValueError("; ".join(problems) + " (see metadata_choices)")


def _raw_slide_type(cell: dict) -> object:
    slideshow = (cell.get("metadata") or {}).get("slideshow")
    return slideshow.get("slide_type") if isinstance(slideshow, dict) else None


def _applied_slide_type(cell: dict, prepared: bool) -> object:
    raw = _raw_slide_type(cell)
    if raw is None:
        return "-" if prepared else "slide"
    return "-" if raw in ("", "-") else raw


def _preview(source: str, limit: int = 60) -> str:
    first_line = next((line.strip() for line in source.splitlines() if line.strip()), "")
    return first_line if len(first_line) <= limit else first_line[: limit - 3] + "..."


def outline_slides(cells: list[dict]) -> list[dict]:
    """Place each cell in the slideshow the way RISE's markupSlides does.

    ``slide`` counts across, ``subslide`` counts down within a slide, and ``fragment`` counts the
    reveal steps within a subslide, all from 0. Fragment 0 is shown as soon as the subslide is.
    Slide, subslide, and fragment markers are ignored until a cell that is neither notes nor skip
    has been placed, so a deck opening on a marked cell does not start with an empty slide.

    Parameters
    ----------
    cells : list of dict
        Cells as the extension's snapshot returns them, with ``metadata`` and ``source``.

    Returns
    -------
    outline : list of dict
        One ``{cell_id, index, cell_type, slide_type, slide, subslide, fragment, preview}`` per
        cell, in notebook order. ``slide_type`` is the role RISE applied, with unset resolved.
    """
    # RISE resolves an unset slide_type per entry: it continues the current slide, unless no cell
    # in the notebook has a role at all, in which case each unset cell starts a slide. An explicit
    # '-' or '' always continues.
    prepared = any(_raw_slide_type(cell) in _SLIDE_ROLES for cell in cells)
    slide = subslide = fragment = 0
    content_placed = False
    outline = []
    for cell in cells:
        slide_type = _applied_slide_type(cell, prepared=prepared)
        if content_placed:
            if slide_type == "slide":
                slide, subslide, fragment = slide + 1, 0, 0
            elif slide_type == "subslide":
                subslide, fragment = subslide + 1, 0
            elif slide_type == "fragment":
                fragment += 1
        elif slide_type not in ("notes", "skip"):
            content_placed = True
        outline.append(
            {
                "cell_id": cell.get("cell_id"),
                "index": cell.get("index"),
                "cell_type": cell.get("cell_type"),
                "slide_type": slide_type,
                "slide": slide,
                "subslide": subslide,
                "fragment": fragment,
                "preview": _preview(str(cell.get("source", ""))),
            }
        )
    return outline
