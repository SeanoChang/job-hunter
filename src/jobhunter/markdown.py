"""L0: HTML -> Markdown, the only canonical text. Deterministic; versioned as NORMALIZER_VERSION.

Handles the dialect ATS postings use: headings, paragraphs, div wrappers, nested ul/ol,
bold/italic, links, br, hr, blockquote. Drops script/style/img. NFKC-normalises. The visible
text of the output equals the visible text of the input (tested).
"""

from __future__ import annotations

import re
import unicodedata
from html.parser import HTMLParser

NORMALIZER_VERSION = "md/1"

_HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
_BLOCK_BOUNDARY = {"p", "div", "section", "article", "header", "footer", "main", "aside",
                   "table", "tr", "thead", "tbody", "blockquote", "pre", "figure", "nav"}
_SKIP = {"script", "style", "noscript", "template", "head", "title"}
_WS = re.compile(r"\s+")
_LEAD_WS = re.compile(r"^\s+")
_TRAIL_WS = re.compile(r"\s+$")
_BR = "\x00"  # placeholder for <br>, survives whitespace collapsing


class _Converter(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[str] = []
        self.buf: list[str] = []
        self.lists: list[list[object]] = []  # each: [kind, counter]
        self.li_depth = 0
        self.heading: int | None = None
        self.skip = 0
        self.links: list[str | None] = []
        self.quote = 0
        self.just_opened = False

    # -- helpers
    def _open_mark(self, mark: str) -> None:
        """Open an inline span; leading whitespace is moved out of it by handle_data."""
        self.buf.append(mark)
        self.just_opened = True

    def _close_mark(self, mark: str) -> None:
        """Close an inline span, moving trailing whitespace outside the marker.

        `<strong>Minimum education: </strong>x` must become `**Minimum education:** x`,
        not `**Minimum education: **x`, which no Markdown reader renders as bold.
        """
        tail = ""
        while self.buf:
            last = self.buf[-1]
            m = _TRAIL_WS.search(last)
            if m is None:
                break
            head = last[: m.start()]
            self.buf.pop()
            tail = m.group(0) + tail
            if head:
                self.buf.append(head)
                break
        self.buf.append(mark)
        if tail:
            self.buf.append(tail)

    def _flush(self) -> None:
        text = "".join(self.buf)
        self.buf = []
        self.just_opened = False
        text = _WS.sub(" ", text).strip()
        text = text.replace(f" {_BR} ", "\n").replace(f"{_BR} ", "\n").replace(f" {_BR}", "\n")
        text = text.replace(_BR, "\n").strip()
        if not text:
            self.heading = None
            return
        if self.heading:
            self.blocks.append("#" * self.heading + " " + text)
            self.heading = None
        elif self.li_depth:
            kind, n = self.lists[-1]
            indent = "  " * (len(self.lists) - 1)
            marker = "- " if kind == "ul" else f"{n}. "
            self.blocks.append(indent + marker + text)
        elif self.quote:
            self.blocks.append("\n".join("> " + ln for ln in text.split("\n")))
        else:
            self.blocks.append(text)

    # -- parser callbacks
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP:
            self.skip += 1
            return
        if self.skip:
            return
        self.just_opened = False
        if tag in _HEADINGS:
            self._flush()
            self.heading = _HEADINGS[tag]
        elif tag in ("ul", "ol"):
            self._flush()
            self.lists.append([tag, 0])
        elif tag == "li":
            self._flush()
            if not self.lists:
                self.lists.append(["ul", 0])
            self.lists[-1][1] = int(self.lists[-1][1]) + 1  # type: ignore[call-overload]
            self.li_depth += 1
        elif tag == "br":
            self.buf.append(_BR)
        elif tag == "hr":
            self._flush()
            self.blocks.append("---")
        elif tag in ("strong", "b"):
            self._open_mark("**")
        elif tag in ("em", "i"):
            self._open_mark("*")
        elif tag == "a":
            href = dict(attrs).get("href")
            self.links.append(href)
            self._open_mark("[")
        elif tag == "blockquote":
            self._flush()
            self.quote += 1
        elif tag in _BLOCK_BOUNDARY:
            if self.li_depth:
                self.buf.append(" ")  # paragraphs inside a list item stay on the item's line
            else:
                self._flush()

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        self.just_opened = False
        if tag in _HEADINGS:
            self._flush()
        elif tag == "li":
            self._flush()
            self.li_depth = max(0, self.li_depth - 1)
        elif tag in ("ul", "ol"):
            self._flush()
            if self.lists:
                self.lists.pop()
        elif tag in ("strong", "b"):
            self._close_mark("**")
        elif tag in ("em", "i"):
            self._close_mark("*")
        elif tag == "a":
            href = self.links.pop() if self.links else None
            self._close_mark(f"]({href})" if href else "]")
        elif tag == "blockquote":
            self._flush()
            self.quote = max(0, self.quote - 1)
        elif tag in _BLOCK_BOUNDARY:
            if self.li_depth:
                self.buf.append(" ")
            else:
                self._flush()

    def handle_data(self, data: str) -> None:
        if self.skip:
            return
        if self.just_opened:
            self.just_opened = False
            m = _LEAD_WS.match(data)
            lead = m.group(0) if m else ""
            if lead and self.buf:
                mark = self.buf.pop()
                self.buf.append(lead)
                self.buf.append(mark)
                data = data[len(lead) :]
        self.buf.append(data)

    def result(self) -> str:
        self._flush()
        return "\n\n".join(self.blocks)


def _tidy(md: str) -> str:
    md = unicodedata.normalize("NFKC", md)
    # empty emphasis produced by empty tags
    md = md.replace("****", "").replace("**  **", "").replace("* *", "")
    lines = [ln.rstrip() for ln in md.split("\n")]
    out = "\n".join(lines)
    out = re.sub(r"\n{3,}", "\n\n", out)
    # consecutive list items are separated by single newlines, not blank lines
    out = re.sub(r"\n\n(?=(?: {2})*(?:- |\d+\. ))", "\n", out)
    out = re.sub(r"^(> .*)\n\n(?=> )", r"\1\n", out, flags=re.M)
    return out.strip()


def to_markdown(html: str) -> str:
    conv = _Converter()
    conv.feed(html)
    conv.close()
    return _tidy(conv.result())


class _TextOnly(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def _boundary(self, tag: str) -> bool:
        return (
            tag in _BLOCK_BOUNDARY
            or tag in _HEADINGS
            or tag in ("li", "br", "ul", "ol", "hr")
        )

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP:
            self.skip += 1
        elif self._boundary(tag):
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP:
            self.skip = max(0, self.skip - 1)
        elif self._boundary(tag):
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.parts.append(data)


def visible_text(html: str) -> str:
    p = _TextOnly()
    p.feed(html)
    p.close()
    return _WS.sub(" ", unicodedata.normalize("NFKC", "".join(p.parts))).strip()


_MD_MARK = re.compile(
    r"^(?:#{1,6} |(?: {2})*(?:- |\d+\. )|> )|"  # line-leading structure
    r"\*\*|(?<!\w)\*(?=\S)|(?<=\S)\*(?!\w)|"      # emphasis toggles
    r"^---$",
    re.M,
)
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")


def strip_markdown(md: str) -> str:
    text = _MD_LINK.sub(r"\1", md)
    text = _MD_MARK.sub("", text)
    return _WS.sub(" ", text).strip()
