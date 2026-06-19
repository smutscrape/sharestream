"""Static Markdown pages from ``data/pages/``.

Each ``{slug}.md`` file is rendered to HTML and served at ``/{slug}`` via the
catch-all short-URL router (after share lookups). Legacy ``/pages/{slug}`` URLs
redirect permanently to the top-level path.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

from sharestream.config import BASE_DOMAIN
from sharestream.core.branding import site_context
from sharestream.core.templates import render
from sharestream.services.markdown import (
    first_markdown_image,
    markdown_page_body,
    markdown_page_title,
    render_markdown,
)
from sharestream.services.slugs import normalize_custom_slug, resolve_markdown_page_path

logger = logging.getLogger(__name__)

router = APIRouter()


def render_markdown_page(slug: str, request=None) -> HTMLResponse | None:
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
    body = markdown_page_body(source)
    context = site_context(request)
    # Social-embed image: the first image embedded in the page's Markdown (made
    # absolute), else the site thumbnail. A bare path/relative URL is resolved
    # against BASE_DOMAIN so crawlers fetch a fully-qualified URL.
    page_image = first_markdown_image(body)
    if page_image and not page_image.startswith(("http://", "https://")):
        page_image = f"{BASE_DOMAIN}/{page_image.lstrip('/')}"
    context.update(
        page_title=title,
        content_html=render_markdown(body),
        og_title=title,
        og_image=page_image or context["og_site_image"],
        page_url=f"{BASE_DOMAIN}/{canonical}",
    )
    return HTMLResponse(render("markdown-page.html", **context))


@router.get("/pages/{slug}", response_class=RedirectResponse)
async def legacy_markdown_page_redirect(slug: str):
    """Permanent redirect for bookmarks/links created before top-level pages."""
    if resolve_markdown_page_path(slug) is None:
        raise HTTPException(status_code=404, detail="Page not found")
    return RedirectResponse(url=f"/{normalize_custom_slug(slug)}", status_code=301)
