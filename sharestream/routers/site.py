"""Site-level helper routes: config JSON, favicon, and custom fonts CSS."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import FileResponse

from sharestream.config import BASE_DOMAIN, SITE_MOTTO, SITE_NAME, SOCIAL_LINKS
from sharestream.core.branding import (
    build_fonts_css,
    resolve_favicon_path,
    resolve_favicon_png_path,
    resolve_logo,
)

router = APIRouter()


@router.get("/site_config")
async def get_site_config():
    # logo_path/logo_srcset come from the same resolve_logo() the public pages
    # use, so the (static, non-templated) admin page can resolve its logo
    # consistently instead of relying on a brittle svg-only onerror fallback.
    logo_path, logo_srcset = resolve_logo()
    return {
        "site_name": SITE_NAME,
        "site_motto": SITE_MOTTO,
        "social_links": SOCIAL_LINKS,
        "base_domain": BASE_DOMAIN,
        "logo_path": logo_path,
        "logo_srcset": logo_srcset,
    }


@router.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """Serve the site favicon. Browsers request /favicon.ico automatically, so this
    makes the icon appear on every page. Prefers an operator-provided localized
    icon, falling back to the bundled default."""
    path = resolve_favicon_path()
    if path:
        return FileResponse(path, media_type="image/x-icon",
                            headers={"Cache-Control": "public, max-age=86400"})
    raise HTTPException(status_code=404, detail="No favicon")


@router.get("/favicon.png", include_in_schema=False)
async def favicon_png():
    """Serve the PNG site favicon. Prefers an operator-provided localized icon
    (static/localized/favicon.png), falling back to the bundled default
    (static/favicon.png)."""
    path = resolve_favicon_png_path()
    if path:
        return FileResponse(path, media_type="image/png",
                            headers={"Cache-Control": "public, max-age=86400"})
    raise HTTPException(status_code=404, detail="No favicon")


@router.get("/fonts.css", include_in_schema=False)
async def custom_fonts_css():
    """Drop-in custom font overrides. @font-face is emitted ONLY for files that
    exist, so empty slots fall back to the web-font defaults in styles.css with
    no 404s."""
    return Response(
        content=build_fonts_css(),
        media_type="text/css",
        headers={"Cache-Control": "public, max-age=60"},
    )
