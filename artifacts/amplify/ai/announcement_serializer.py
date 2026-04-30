"""HTML <-> Slate.js block converters for in-app announcements.

Amplify's contenteditable composer produces HTML (paragraphs, headings,
lists, images, videos, links, dividers, simple inline marks). The
Chartmetric web app stores announcement bodies as Slate.js JSON blocks.
This module converts in both directions so:

  * On save, the marketer's HTML is serialized to the Slate shape that
    chartmetric-api expects.
  * When editing an existing post fetched from chartmetric-api, the
    Slate JSON is parsed back into HTML so the contenteditable can
    render and edit it.

The shape follows the spec in
``docs/chartmetric-announcement-admin-api.md`` §3.
"""
from __future__ import annotations

import logging
import re
from html.parser import HTMLParser
from typing import Any, Iterable

logger = logging.getLogger("amplify.announcement_serializer")


SLATE_BLOCK_TYPES = (
    "paragraph",
    "heading-one",
    "heading-two",
    "heading-three",
    "bulleted-list",
    "numbered-list",
    "list-item",
    "image",
    "video",
    "divider",
)
INLINE_MARK_TAGS = {
    "strong": "bold", "b": "bold",
    "em": "italic", "i": "italic",
    "u": "underline",
    "code": "code",
}
HEADING_MAP = {
    "h1": "heading-one",
    "h2": "heading-two",
    "h3": "heading-three",
    "h4": "heading-three",
    "h5": "heading-three",
    "h6": "heading-three",
}
VOID_BLOCKS = {"image", "video", "divider"}
# Blocks whose children must be other blocks (lists wrap list-items only).
CONTAINER_BLOCKS = {"bulleted-list", "numbered-list"}


def _empty_text() -> dict:
    return {"text": ""}


def _is_text_node(n: Any) -> bool:
    return isinstance(n, dict) and "text" in n and "type" not in n


def _is_block(n: Any, t: str) -> bool:
    return isinstance(n, dict) and n.get("type") == t


# ---------------------------------------------------------------------------
# HTML -> Slate
# ---------------------------------------------------------------------------

class _HtmlToSlate(HTMLParser):
    """Streaming HTML parser that emits Slate.js blocks.

    Behavior:
      * Top-level <p>, <h1..h6>, <ul>, <ol>, <hr>, <img>, <video>, <iframe>
        produce one block each.
      * <li> within <ul>/<ol> produces a list-item child.
      * Inline <strong>/<em>/<u>/<code> become marks on text leaves.
      * <a> becomes a `link` inline element with `url` attribute.
      * <br> introduces a "\\n" inside the current text leaf, preserved
        as separate text node so re-rendering keeps the line break.
      * Unknown / unsupported tags are flattened to their text content.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[dict] = []
        # Stack of (block_type, block_dict) used to re-enter parents on close.
        self._block_stack: list[tuple[str, dict]] = []
        # Active inline marks (set of mark names like "bold").
        self._marks: list[str] = []
        # Active link wrapper, if any. We collapse links into a single inline
        # element in the current block's children list.
        self._link_stack: list[dict] = []

    # --- helpers ---------------------------------------------------------

    def _current_container(self) -> dict | None:
        if self._link_stack:
            return self._link_stack[-1]
        if self._block_stack:
            return self._block_stack[-1][1]
        return None

    def _open_block(self, btype: str, **extra: Any) -> dict:
        block: dict[str, Any] = {"type": btype, "children": []}
        block.update(extra)
        if not self._block_stack:
            self.blocks.append(block)
        else:
            parent = self._block_stack[-1][1]
            parent.setdefault("children", []).append(block)
        self._block_stack.append((btype, block))
        return block

    def _close_block(self, btype: str) -> None:
        # Close blocks until we pop btype (or the stack empties).
        while self._block_stack:
            top_t, top_b = self._block_stack.pop()
            self._normalize_children(top_b)
            if top_t == btype:
                break

    def _normalize_children(self, block: dict) -> None:
        btype = block.get("type")
        children = block.get("children") or []
        if btype in VOID_BLOCKS:
            block["children"] = [_empty_text()]
            return
        if btype in CONTAINER_BLOCKS:
            # Lists hold only list-item blocks. Drop any stray text/inline.
            block["children"] = [
                c for c in children
                if isinstance(c, dict) and c.get("type") == "list-item"
            ] or [{"type": "list-item", "children": [_empty_text()]}]
            return
        if not children:
            block["children"] = [_empty_text()]
            return
        # Slate requires at least one text-like node; ensure first/last are
        # text or it'll be hard for the editor to place a cursor.
        if not _is_text_node(children[0]) and not children[0].get("type") == "link":
            children.insert(0, _empty_text())
        if not _is_text_node(children[-1]) and not children[-1].get("type") == "link":
            children.append(_empty_text())
        block["children"] = children

    def _append_text(self, raw: str) -> None:
        if not raw:
            return
        container = self._current_container()
        if container is None:
            # Stray text at top level — wrap in a paragraph.
            self._open_block("paragraph")
            container = self._current_container()
        leaf: dict[str, Any] = {"text": raw}
        for mark in self._marks:
            leaf[mark] = True
        container.setdefault("children", []).append(leaf)

    # --- HTMLParser hooks ------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attr_dict = {k.lower(): (v or "") for k, v in attrs}

        if tag == "br":
            self._append_text("\n")
            return
        if tag in INLINE_MARK_TAGS:
            self._marks.append(INLINE_MARK_TAGS[tag])
            return
        if tag == "a":
            url = attr_dict.get("href", "")
            link_block: dict[str, Any] = {"type": "link", "url": url, "children": []}
            container = self._current_container()
            if container is None:
                self._open_block("paragraph")
                container = self._current_container()
            container.setdefault("children", []).append(link_block)
            self._link_stack.append(link_block)
            return
        if tag == "p":
            self._open_block("paragraph")
            return
        if tag in HEADING_MAP:
            self._open_block(HEADING_MAP[tag])
            return
        if tag == "ul":
            self._open_block("bulleted-list")
            return
        if tag == "ol":
            self._open_block("numbered-list")
            return
        if tag == "li":
            self._open_block("list-item")
            return
        if tag == "hr":
            self._open_block("divider")
            self._close_block("divider")
            return
        if tag == "img":
            url = attr_dict.get("src", "")
            self._open_block("image", url=url, alt=attr_dict.get("alt", ""))
            self._close_block("image")
            return
        if tag in ("video", "iframe", "source"):
            url = attr_dict.get("src", "")
            if tag == "source" and self._block_stack and self._block_stack[-1][0] == "video":
                self._block_stack[-1][1]["url"] = url
                return
            if tag == "video":
                self._open_block("video", url=url)
                return
            # iframe -> treat as video link
            self._open_block("video", url=url)
            self._close_block("video")
            return
        # Unknown tags: ignore (their text content is still emitted).

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in INLINE_MARK_TAGS:
            mark = INLINE_MARK_TAGS[tag]
            try:
                # Remove the most recent matching mark.
                idx = len(self._marks) - 1 - self._marks[::-1].index(mark)
                self._marks.pop(idx)
            except ValueError:
                pass
            return
        if tag == "a":
            if self._link_stack:
                link = self._link_stack.pop()
                if not link.get("children"):
                    link["children"] = [_empty_text()]
            return
        if tag == "p":
            self._close_block("paragraph")
            return
        if tag in HEADING_MAP:
            self._close_block(HEADING_MAP[tag])
            return
        if tag == "ul":
            self._close_block("bulleted-list")
            return
        if tag == "ol":
            self._close_block("numbered-list")
            return
        if tag == "li":
            self._close_block("list-item")
            return
        if tag == "video":
            self._close_block("video")
            return

    def handle_data(self, data: str) -> None:
        if not data:
            return
        self._append_text(data)

    def close(self) -> None:  # type: ignore[override]
        super().close()
        # Close anything still open so blocks are normalized.
        while self._block_stack:
            top_t, top_b = self._block_stack.pop()
            self._normalize_children(top_b)


_WHITESPACE_TEXT_RE = re.compile(r"^[\s\u00a0]+$")


def html_to_slate(html: str | None) -> list[dict]:
    """Parse marketer-authored HTML into a list of Slate.js blocks.

    Returns at least one paragraph block (Slate.js requires the document
    to be non-empty). Whitespace-only input becomes a single empty
    paragraph.
    """
    if not html or _WHITESPACE_TEXT_RE.match(html):
        return [{"type": "paragraph", "children": [_empty_text()]}]
    parser = _HtmlToSlate()
    try:
        parser.feed(html)
        parser.close()
    except Exception as e:
        logger.warning("html_to_slate failed (%s); returning plaintext fallback", e)
        return [{"type": "paragraph", "children": [{"text": html}]}]
    blocks = parser.blocks
    if not blocks:
        return [{"type": "paragraph", "children": [_empty_text()]}]
    return blocks


# ---------------------------------------------------------------------------
# Slate -> HTML
# ---------------------------------------------------------------------------

def _escape_html(s: str) -> str:
    return (
        (s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _render_text_leaf(leaf: dict) -> str:
    text = _escape_html(leaf.get("text", "")).replace("\n", "<br>")
    if leaf.get("code"):
        text = f"<code>{text}</code>"
    if leaf.get("underline"):
        text = f"<u>{text}</u>"
    if leaf.get("italic"):
        text = f"<em>{text}</em>"
    if leaf.get("bold"):
        text = f"<strong>{text}</strong>"
    return text


def _render_inline(node: dict) -> str:
    if _is_text_node(node):
        return _render_text_leaf(node)
    t = node.get("type")
    if t == "link":
        url = _escape_html(node.get("url", ""))
        inner = "".join(_render_inline(c) for c in node.get("children") or [])
        return f'<a href="{url}" target="_blank" rel="noopener noreferrer">{inner}</a>'
    # Unknown inline -> just render its children plainly
    return "".join(_render_inline(c) for c in node.get("children") or [])


def _render_block(block: dict) -> str:
    t = block.get("type", "paragraph")
    children = block.get("children") or []
    if t == "image":
        url = _escape_html(block.get("url", ""))
        alt = _escape_html(block.get("alt", ""))
        return f'<img src="{url}" alt="{alt}">'
    if t == "video":
        url = _escape_html(block.get("url", ""))
        return f'<video src="{url}" controls></video>'
    if t == "divider":
        return "<hr>"
    inner_blocks: list[str] = []
    inline_buf: list[str] = []
    for c in children:
        if isinstance(c, dict) and c.get("type") in SLATE_BLOCK_TYPES:
            if inline_buf:
                inner_blocks.append("".join(inline_buf))
                inline_buf = []
            inner_blocks.append(_render_block(c))
        else:
            inline_buf.append(_render_inline(c))
    if inline_buf:
        inner_blocks.append("".join(inline_buf))
    inner = "".join(inner_blocks)
    if t == "paragraph":
        return f"<p>{inner}</p>"
    if t == "heading-one":
        return f"<h1>{inner}</h1>"
    if t == "heading-two":
        return f"<h2>{inner}</h2>"
    if t == "heading-three":
        return f"<h3>{inner}</h3>"
    if t == "bulleted-list":
        return f"<ul>{inner}</ul>"
    if t == "numbered-list":
        return f"<ol>{inner}</ol>"
    if t == "list-item":
        return f"<li>{inner}</li>"
    return f"<div>{inner}</div>"


def slate_to_html(blocks: Iterable[dict] | None) -> str:
    if not blocks:
        return ""
    return "".join(_render_block(b) for b in blocks if isinstance(b, dict))


# ---------------------------------------------------------------------------
# Translation walker
# ---------------------------------------------------------------------------

def walk_text_leaves(blocks: Iterable[dict]) -> list[dict]:
    """Return every text leaf (node with a "text" key and no "type") found
    anywhere inside ``blocks``. Useful for plucking strings out for
    Claude translation while preserving block structure for re-insertion.
    """
    leaves: list[dict] = []

    def _walk(nodes: Iterable[dict]) -> None:
        for n in nodes or []:
            if not isinstance(n, dict):
                continue
            if "text" in n and "type" not in n:
                leaves.append(n)
                continue
            kids = n.get("children")
            if isinstance(kids, list):
                _walk(kids)

    _walk(blocks)
    return leaves


def deepcopy_blocks(blocks: Iterable[dict]) -> list[dict]:
    """Cheap deepcopy via JSON round-trip (Slate blocks are pure JSON)."""
    import json
    return json.loads(json.dumps(list(blocks or [])))
