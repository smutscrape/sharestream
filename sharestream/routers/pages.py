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


def get_home_page_context() -> dict:
    """Return template context for the optional home Markdown page
    (``data/pages/home.md``), or an empty dict when absent.

    The home page is rendered on ``/`` between the featured tag collections and
    the featured-videos gallery. Mirrors :func:`render_markdown_page`'s rendering
    (title extraction, leading-``# title`` strip, sanitized Markdown→HTML) but
    returns the pieces for the home template instead of a full page response, and
    never raises — a missing/erroring file simply means no home page is shown.
    """
    source = read_home_page_source()
    if source is None:
        return {}
    title = markdown_page_title(source, "")
    body = markdown_page_body(source)
    return {
        "home_page_title": title,
        "home_page_html": render_markdown(body),
    }


def read_home_page_source() -> str | None:
    """Return the raw text of ``data/pages/home.md`` when it exists, else None.
    Log and swallow read errors so a broken file never takes the home page down.
    """
    path = resolve_markdown_page_path("home")
    if path is None:
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError as e:
        logger.error(f"Error reading home page {path}: {e}")
        return None


@router.get("/pages/{slug}", response_class=RedirectResponse)
async def legacy_markdown_page_redirect(slug: str):
    """Permanent redirect for bookmarks/links created before top-level pages."""
    if resolve_markdown_page_path(slug) is None:
        raise HTTPException(status_code=404, detail="Page not found")
    return RedirectResponse(url=f"/{normalize_custom_slug(slug)}", status_code=301)
