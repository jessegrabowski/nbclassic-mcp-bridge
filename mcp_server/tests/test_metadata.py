import pytest

from nbclassic_mcp_bridge_mcp.metadata import _TYPE_CHECKS, CATALOG, outline_slides, validate_patch


def _cells(*slide_types):
    cells = []
    for index, slide_type in enumerate(slide_types):
        metadata = {} if slide_type is None else {"slideshow": {"slide_type": slide_type}}
        cells.append({"cell_id": f"c{index}", "index": index, "source": f"cell {index}", "metadata": metadata})
    return cells


def _positions(outline):
    return [(cell["slide"], cell["subslide"], cell["fragment"]) for cell in outline]


def test_outline_counts_slides_across_subslides_down_and_fragments_within():
    outline = outline_slides(_cells("slide", "-", "fragment", "subslide", "fragment", "fragment", "slide", "-"))
    assert _positions(outline) == [
        (0, 0, 0),
        (0, 0, 0),
        (0, 0, 1),
        (0, 1, 0),
        (0, 1, 1),
        (0, 1, 2),
        (1, 0, 0),
        (1, 0, 0),
    ]


def test_outline_ignores_markers_until_visible_content_is_placed():
    # Notes place nothing visible, so the subslide marker after them would open onto an empty
    # opening slide and is ignored; that cell is the first content, so later markers count.
    outline = outline_slides(_cells("notes", "subslide", "fragment", "slide"))
    assert _positions(outline) == [(0, 0, 0), (0, 0, 0), (0, 0, 1), (1, 0, 0)]


def test_unset_cells_continue_in_a_prepared_notebook_and_start_slides_otherwise():
    prepared = outline_slides(_cells("slide", None, "-"))
    assert [cell["slide_type"] for cell in prepared] == ["slide", "-", "-"]
    assert _positions(prepared) == [(0, 0, 0)] * 3

    unprepared = outline_slides(_cells(None, None, "-"))
    assert [cell["slide_type"] for cell in unprepared] == ["slide", "slide", "-"]
    assert _positions(unprepared) == [(0, 0, 0), (1, 0, 0), (1, 0, 0)]


def test_notes_and_skip_cells_keep_the_position_of_the_cell_before_them():
    outline = outline_slides(_cells("slide", "fragment", "notes", "skip", "-", "subslide", "skip"))
    assert _positions(outline) == [(0, 0, 0), (0, 0, 1), (0, 0, 1), (0, 0, 1), (0, 0, 1), (0, 1, 0), (0, 1, 0)]


def test_outline_previews_the_first_nonblank_line_and_shortens_long_ones():
    short, long = outline_slides(
        [
            {"cell_id": "c0", "index": 0, "source": "\n  # Title  \nbody", "metadata": {}},
            {"cell_id": "c1", "index": 1, "source": "x" * 100, "metadata": {}},
        ]
    )
    assert short["preview"] == "# Title"
    assert long["preview"] == "x" * 57 + "..."


@pytest.mark.parametrize(
    "scope, patch",
    [
        ("cell", {"slideshow": {"slide_type": "subslide"}, "tags": ["a"], "custom": {"anything": 1}}),
        ("cell", {"slideshow": None, "tags": None}),
        ("cell", {"raw_mimetype": "text/x-custom"}),
        ("notebook", {"rise": {"theme": "night", "width": 1280, "slideNumber": "c/t"}}),
        ("notebook", {"authors": [{"name": "A"}], "rise": {"chalkboard": {"theme": "whiteboard"}}}),
    ],
)
def test_validate_accepts_known_values_deletions_and_unknown_keys(scope, patch):
    validate_patch(patch, scope=scope)


@pytest.mark.parametrize(
    "scope, patch, message",
    [
        ("cell", {"slideshow": {"slide_type": "Slide"}}, "slideshow.slide_type must be one of"),
        ("cell", {"slideshow": "slide"}, "slideshow must be an object"),
        ("cell", {"tags": "hide-input"}, "tags must be list of string"),
        ("cell", {"editable": "false"}, "editable must be boolean"),
        ("notebook", {"rise": {"transition": "wipe"}}, "rise.transition must be one of"),
        ("notebook", {"authors": ["A"]}, "authors must be list of {name: string}"),
    ],
)
def test_validate_rejects_bad_values_and_names_the_choices(scope, patch, message):
    with pytest.raises(ValueError, match="metadata_choices") as raised:
        validate_patch(patch, scope=scope)
    assert message in str(raised.value)


def test_every_catalog_entry_has_a_checkable_type():
    # A type name with no check would raise KeyError the first time an agent set that key.
    types = {entry["type"] for entries in CATALOG.values() for entry in entries.values()}
    assert types <= set(_TYPE_CHECKS)
