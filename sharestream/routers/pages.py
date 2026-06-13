"""Static Markdown pages from ``data/pages/``.

Each ``{slug}.md`` file is rendered to HTML and served at ``/{slug}`` via the
catch-all short-URL router (after share lookups). Legacy ``/pages/{slug}`` URLs
redirect permanently to the top-level path.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

from sharestream.core.branding import site_context
from sharestream.core.templates import render
from sharestream.services.markdown import markdown_page_body, markdown_page_title, render_markdown
from sharestream.services.slugs import normalize_custom_slug, resolve_markdown_page_path

logger = logging.getLogger(__name__)

router = APIRouter()


def render_markdown_page(slug: str) -> HTMLResponse | None:
    """Render a Markdown page for ``slug``, or return None if no such page exists."""
    path = resolve_markdown_page_path(slug)
    if path is None:
        return None
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as e:
        logger.error(f"Error reading page {path}: {e}")
        raise HTTPException(status_code=500, detail="Failed to load page")
    canonical = normalize_custom_slug(slug)
    title = markdown_page_title(source, canonical.replace("-", " ").replace("_", " ").title())
    context = site_context()
    context.update(
        page_title=title,
        content_html=render_markdown(markdown_page_body(source)),
    )
    return HTMLResponse(render("markdown-page.html", **context))


@router.get("/pages/{slug}", response_class=RedirectResponse)
async def legacy_markdown_page_redirect(slug: str):
    """Permanent redirect for bookmarks/links created before top-level pages."""
    if resolve_markdown_page_path(slug) is None:
        raise HTTPException(status_code=404, detail="Page not found")
    return RedirectResponse(url=f"/{normalize_custom_slug(slug)}", status_code=301)
