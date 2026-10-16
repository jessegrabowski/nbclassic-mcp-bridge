import pytest

from nbclassic_mcp_bridge_mcp.metadata import _TYPE_CHECKS, CATALOG, validate_patch


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
