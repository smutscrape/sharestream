"""Footer link configuration from ``config.yaml``.

``footer.action_links`` render as pill buttons (``primary`` or ``secondary``).
``footer.links`` render as small inline text links separated by middots.
When the ``footer`` key is omitted entirely, a single secondary Report button
is shown so existing installs keep working.
"""
from __future__ import annotations

_VALID_ACTION_STYLES = frozenset({"primary", "secondary"})


def _normalize_href(href: str) -> str | None:
    href = (href or "").strip()
    if not href:
        return None
    if href.startswith("/"):
        return href
    if href.startswith("http://") or href.startswith("https://"):
        return href
    return None


def _normalize_action_link(item) -> dict | None:
    if not isinstance(item, dict):
        return None
    label = (item.get("label") or "").strip()
    href = _normalize_href(item.get("href"))
    if not label or not href:
        return None
    style = str(item.get("style") or "secondary").strip().lower()
    if style not in _VALID_ACTION_STYLES:
        style = "secondary"
    return {"label": label, "href": href, "style": style}


def _normalize_inline_link(item) -> dict | None:
    if not isinstance(item, dict):
        return None
    label = (item.get("label") or "").strip()
    href = _normalize_href(item.get("href"))
    if not label or not href:
        return None
    return {"label": label, "href": href}


def parse_footer_config(config: dict) -> dict:
    """Return ``{action_links: [...], links: [...]}`` from the loaded YAML config."""
    if "footer" not in config:
        return {
            "action_links": [
                {"label": "Report Content", "href": "/report", "style": "secondary"},
            ],
            "links": [],
        }

    footer = config.get("footer") or {}
    action_links = [
        link for item in (footer.get("action_links") or [])
        if (link := _normalize_action_link(item)) is not None
    ]
    links = [
        link for item in (footer.get("links") or [])
        if (link := _normalize_inline_link(item)) is not None
    ]
    return {"action_links": action_links, "links": links}
