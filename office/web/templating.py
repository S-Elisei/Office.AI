"""Shared Jinja2Templates instance plus small display helpers."""

from __future__ import annotations

import html as _html
import re
import xml.etree.ElementTree as _etree
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote as _quote

import markdown as _markdown
from fastapi.templating import Jinja2Templates
from markdown.extensions import Extension as _MarkdownExtension
from markdown.inlinepatterns import InlineProcessor as _InlineProcessor
from markdown.preprocessors import Preprocessor as _Preprocessor
from markdown.treeprocessors import Treeprocessor as _Treeprocessor
from markdown.util import AtomicString as _AtomicString

TEMPLATES_DIR = Path(__file__).parent / "templates"

_MARKDOWN_EXTENSIONS = ["fenced_code", "tables", "sane_lists", "nl2br"]

_SAFE_URL_SCHEMES = {"http", "https", "mailto"}
_URL_SCHEME_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.\-]*):")
_URL_NOISE_RE = re.compile(r"[\x00-\x20\x7f]")

_LIST_ITEM_RE = re.compile(r"^ {0,3}(?:[-*+]|[0-9]{1,9}[.)])[ \t]")

_WINDOWS_PATH_RE = re.compile(r"[A-Za-z]:[\\/]")
_BARE_PATH_RE = r"(?<!\w)[A-Za-z]:[\\/][^\s<>\"'`*|]*\w"
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
_AUDIO_SUFFIXES = {".wav", ".mp3", ".ogg", ".flac"}

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def fmt_time(value: str | None) -> str:
    """'2026-09-11T14:32:01.123Z' -> '11.09 17:32' in the machine's own zone.
    Empty/None -> ''.

    Also accepts a raw Unix epoch (seconds) as a numeric string.
    """
    if not value:
        return ""
    if value.isdigit():
        try:
            dt = datetime.fromtimestamp(int(value), tz=timezone.utc)
            return dt.astimezone().strftime("%d.%m %H:%M")
        except (ValueError, OSError, OverflowError):
            return value
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().strftime("%d.%m %H:%M")


def pct(used: int | None, limit: int | None) -> int | None:
    """Context fill as a rounded percentage, or None when there is no data."""
    if used is None or limit is None or limit <= 0:
        return None
    return round(100 * used / limit)


def fmt_duration(seconds: float | None) -> str:
    """4 -> '4s', 95 -> '1m 35s', 7400 -> '2h 3m'. None -> ''."""
    if seconds is None:
        return ""
    total = int(max(0, seconds))
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m {total % 60}s"
    return f"{total // 3600}h {(total % 3600) // 60}m"


class _ListsInterruptParagraphs(_Preprocessor):
    """Opens a blank line in front of a list that starts right after a sentence.

    Runs after the fenced-code preprocessor. A list already inside
    a list is left alone: the blank line only goes in when the run of lines
    above holds no list item of its own.
    """

    def run(self, lines):
        out: list[str] = []
        block_has_list = False
        for line in lines:
            if not line.strip():
                block_has_list = False
                out.append(line)
                continue
            is_item = bool(_LIST_ITEM_RE.match(line))
            if is_item and not block_has_list and out and out[-1].strip():
                out.append("")
            block_has_list = block_has_list or is_item
            out.append(line)
        return out


class _SafeUrls(_Treeprocessor):
    """Drops href/src attributes whose scheme is not one of _SAFE_URL_SCHEMES."""

    def run(self, root):
        for element in root.iter():
            for attribute in ("href", "src"):
                url = element.get(attribute)
                if url is None:
                    continue
                probe = _URL_NOISE_RE.sub("", _html.unescape(url))
                match = _URL_SCHEME_RE.match(probe)
                if match and match.group(1).lower() not in _SAFE_URL_SCHEMES:
                    del element.attrib[attribute]


def _file_nodes(path: str, inner: str | _etree.Element) -> list[_etree.Element]:
    """The link to an existing file at `path`, wrapping `inner` (the text or the
    element it is shown as), followed by the preview its extension gets."""
    url = f"/file?path={_quote(path, safe='')}"
    link = _etree.Element("a", {"href": url, "target": "_blank"})
    if isinstance(inner, str):
        link.text = inner
    else:
        link.append(inner)
    nodes = [link]
    suffix = Path(path).suffix.lower()
    if suffix in _IMAGE_SUFFIXES:
        image_link = _etree.Element("a", {"href": url, "target": "_blank"})
        _etree.SubElement(
            image_link, "img",
            {"class": "file-preview", "src": url, "loading": "lazy", "alt": Path(path).name},
        )
        nodes.append(image_link)
    elif suffix in _AUDIO_SUFFIXES or suffix == ".glb":
        button = _etree.Element("button", {
            "type": "button",
            "class": "file-preview " + ("listen" if suffix in _AUDIO_SUFFIXES else "view-3d"),
            "data-src": url,
            "data-name": Path(path).name,
        })
        button.text = "Listen" if suffix in _AUDIO_SUFFIXES else "View 3D"
        nodes.append(button)
    return nodes


class _BarePathPattern(_InlineProcessor):
    """An absolute Windows path in running text that names an existing file."""

    def handleMatch(self, m, data):  # noqa: N802
        path = m.group(0)
        if not Path(path).is_file():
            return None, None, None
        wrapper = _etree.Element("span")
        wrapper.extend(_file_nodes(path, _AtomicString(path)))
        return wrapper, m.start(0), m.end(0)


class _CodeSpanFiles(_Treeprocessor):
    """Links a code span whose whole text is the path of an existing file."""

    def run(self, root):
        for parent in list(root.iter()):
            if parent.tag == "pre":
                continue
            for index, child in reversed(list(enumerate(parent))):
                if child.tag != "code":
                    continue
                path = (child.text or "").strip()
                if not _WINDOWS_PATH_RE.match(path) or not Path(path).is_file():
                    continue
                nodes = _file_nodes(path, child)
                nodes[-1].tail = child.tail
                child.tail = None
                parent[index:index + 1] = nodes


class _OfficeMarkdown(_MarkdownExtension):
    """The office's house rules."""

    def extendMarkdown(self, md):  # noqa: N802
        md.inlinePatterns.register(_BarePathPattern(_BARE_PATH_RE, md), "office_bare_path", 185)
        md.treeprocessors.register(_CodeSpanFiles(md), "office_code_span_files", 15)
        md.preprocessors.register(
            _ListsInterruptParagraphs(md), "office_list_interrupt", 20
        )
        md.treeprocessors.register(_SafeUrls(md), "office_safe_urls", 1)


def render_markdown(text: str | None) -> str:
    """Agent-written text -> HTML. The office's one text renderer."""
    if not text:
        return ""
    md = _markdown.Markdown(extensions=[*_MARKDOWN_EXTENSIONS, _OfficeMarkdown()])
    md.preprocessors.deregister("html_block")
    md.inlinePatterns.deregister("html")
    return md.convert(text)


from office.web.routes.nav import nav_counts as _nav_counts  # noqa: E402
from office.web.routes.nav import office_switch as _office_switch  # noqa: E402

templates.env.globals["nav_counts"] = _nav_counts
templates.env.globals["office_switch"] = _office_switch
templates.env.filters["fmt_time"] = fmt_time
templates.env.filters["fmt_duration"] = fmt_duration
templates.env.filters["pct"] = pct
templates.env.filters["markdown"] = render_markdown
