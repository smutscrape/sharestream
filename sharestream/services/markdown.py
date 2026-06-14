"""Markdown → safe HTML rendering.

Used for Stash video descriptions and for static pages under ``data/pages/``
(served at ``/{slug}``).
Output is sanitized so arbitrary HTML/script in the source cannot execute in the
browser.
"""
from __future__ import annotations

import re

import bleach
import markdown as md

# Tags markdown commonly emits; anything else is stripped after conversion.
_ALLOWED_TAGS = [
    "p", "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "li", "blockquote", "pre", "code",
    "em", "strong", "del", "a", "hr", "br",
    "table", "thead", "tbody", "tr", "th", "td",
]
_ALLOWED_ATTRS = {"a": ["href", "title"], "th": ["align"], "td": ["align"]}
_ALLOWED_PROTOCOLS = ["http", "https", "mailto"]

_TITLE_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
_STRIP_LEADING_H1_RE = re.compile(r"^\s*#\s+.+?\s*(?:\r?\n|$)", re.MULTILINE)
# GFM ~~strikethrough~~ (not part of Python-Markdown's ``extra`` bundle).
_STRIKE_RE = re.compile(r"~~([^~\n]+?)~~")
_CODE_SPAN_RE = re.compile(r"(`[^`\n]+`|```[\s\S]*?```)")


def _apply_strikethrough(source: str) -> str:
    """Convert ``~~text~~`` to ``<del>`` outside inline/fenced code spans."""
    parts = _CODE_SPAN_RE.split(source)
    for i in range(0, len(parts), 2):
        parts[i] = _STRIKE_RE.sub(r"<del>\1</del>", parts[i])
    return "".join(parts)


def render_markdown(text: str | None) -> str:
    """Convert ``text`` from Markdown to sanitized HTML, or return '' when empty."""
    if not text or not str(text).strip():
        return ""
    source = _apply_strikethrough(str(text))
    html = md.markdown(
        source,
        extensions=["extra", "sane_lists", "smarty"],
        output_format="html5",
    )
    return bleach.clean(
        html,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRS,
        protocols=_ALLOWED_PROTOCOLS,
        strip=True,
    )


def markdown_page_title(text: str, fallback: str) -> str:
    """Return the first ``# Heading`` in ``text``, or ``fallback``."""
    match = _TITLE_RE.search(text or "")
    if match:
        return match.group(1).strip()
    return fallback


def markdown_page_body(text: str) -> str:
    """Return page Markdown with a leading ``# title`` line removed (the page
    template renders that title separately)."""
    if not text:
        return ""
    return _STRIP_LEADING_H1_RE.sub("", text, count=1).lstrip("\n")
