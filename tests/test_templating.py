"""What a browser is handed when an agent's text goes through render_markdown."""

from __future__ import annotations

from html.parser import HTMLParser

from office.web.templating import render_markdown

#: The schemes a rendered document may address.
_SAFE_SCHEMES = ("http", "https", "mailto")

_LINKED = "https://example.invalid/page"


class _Tree(HTMLParser):
    """The elements and attributes a parser finds in a document."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[str] = []
        self.attributes: list[tuple[str, str, str | None]] = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        for name, value in attrs:
            self.attributes.append((tag, name, value))


def _parse(markup: str) -> _Tree:
    tree = _Tree()
    tree.feed(markup)
    tree.close()
    return tree


def _assert_inert(markup: str) -> None:
    """Fail unless the document holds nothing a browser would execute."""
    tree = _parse(markup)
    assert "script" not in tree.tags, markup
    for tag, name, value in tree.attributes:
        assert not name.startswith("on"), (tag, name, markup)
        if name in ("href", "src"):
            head, colon, _ = (value or "").partition(":")
            assert not colon or head.lower() in _SAFE_SCHEMES, (name, value, markup)


def test_render_markdown_hands_a_browser_nothing_to_execute():
    written_by_a_model = [
        "<script>alert(1)</script>",
        '<img src="x" onerror="alert(1)">',
        "[go](javascript:alert(1))",
        "[go](&#106;avascript:alert(1))",
        "[go](java\x01script:alert(1))",
    ]
    for text in written_by_a_model:
        _assert_inert(render_markdown(text))

    ordinary = render_markdown(f"[go]({_LINKED}) and *stress*")
    _assert_inert(ordinary)
    tree = _parse(ordinary)
    assert ("a", "href", _LINKED) in tree.attributes
    assert "em" in tree.tags


def test_render_markdown_carries_no_link_reference_into_the_next_call():
    declared = render_markdown(f"[ref]: {_LINKED}\n\n[go][ref]")
    assert ("a", "href", _LINKED) in _parse(declared).attributes

    render_markdown("[ref]: javascript:alert(1)\n\n[go][ref]")
    afterwards = render_markdown("[go][ref]")
    _assert_inert(afterwards)
    assert "a" not in _parse(afterwards).tags
