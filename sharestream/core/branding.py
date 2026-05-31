"""Branding / theming helpers: logo resolution, favicon, custom fonts, and the
common template context shared by every themed page.

These helpers centralize the operator-localization lookups (prefer
``static/localized/*`` assets, fall back to the bundled defaults) so routers and
the error handler don't each reimplement them.
"""
from __future__ import annotations

import os
from pathlib import Path

from sharestream.config import (
    BASE_DOMAIN,
    DISCLAIMER,
    SITE_MOTTO,
    SITE_NAME,
    SOCIAL_LINKS,
)

# Drop-in custom font overrides. Place a .woff2 with one of the names below in
# static/localized/fonts/ and it overrides that slot automatically — no CSS
# editing. @font-face is emitted ONLY for files that exist, so empty slots fall
# back to the web-font defaults in styles.css with no 404s.
FONT_SLOTS = {
    "base_font.woff2": "CustomBase",
    "title_font.woff2": "CustomTitle",
    "button_font.woff2": "CustomButton",
    "motto_font.woff2": "CustomMotto",
    "disclaimer_font.woff2": "CustomDisclaimer",
}

# Candidate favicon paths in priority order (operator-localized first, then the
# bundled default).
FAVICON_CANDIDATES = ("static/localized/favicon.ico", "static/favicon.ico")
FAVICON_PNG_CANDIDATES = ("static/localized/favicon.png", "static/favicon.png")


def resolve_logo():
    """Return (logo_path, srcset) for the site logo.

    Prefers a localized SVG (resolution-independent and tiny — no srcset and no
    heavy @2x/@3x PNGs needed), then localized PNGs, then the bundled default
    (static/sharestream.svg).
    """
    if os.path.exists("static/localized/logo.svg"):
        return "/static/localized/logo.svg", ""
    if os.path.exists("static/logo.svg"):
        return "/static/logo.svg", ""
    for base in ("static/localized/logo", "static/logo"):
        if os.path.exists(f"{base}.png"):
            prefix = "/" + base
            srcset = ", ".join(p for p in [
                f"{prefix}.png 1x",
                os.path.exists(f"{base}@2x.png") and f"{prefix}@2x.png 2x",
                os.path.exists(f"{base}@3x.png") and f"{prefix}@3x.png 3x",
            ] if p)
            return f"{prefix}.png", srcset
    return "/static/sharestream.svg", ""


def _first_existing(candidates) -> str | None:
    """Return the first existing, non-empty path from candidates, or None."""
    for p in candidates:
        if os.path.exists(p) and os.path.getsize(p) > 0:
            return p
    return None


def resolve_favicon_path() -> str | None:
    """Return the localized .ico favicon if present, else the bundled default."""
    return _first_existing(FAVICON_CANDIDATES)


def resolve_favicon_png_path() -> str | None:
    """Return the localized .png favicon if present, else the bundled default."""
    return _first_existing(FAVICON_PNG_CANDIDATES)


def build_fonts_css() -> str:
    """Emit @font-face blocks only for custom font files that actually exist."""
    fonts_dir = Path("static/localized/fonts")
    blocks = []
    for fname, family in FONT_SLOTS.items():
        f = fonts_dir / fname
        if f.exists() and f.stat().st_size > 0:
            blocks.append(
                f"@font-face{{font-family:'{family}';"
                f"src:url('/static/localized/fonts/{fname}') format('woff2');"
                f"font-weight:normal;font-style:normal;font-display:swap;}}"
            )
    return "\n".join(blocks)


def site_context() -> dict:
    """Common template variables shared by every themed page (logo, site name,
    motto, social links, base domain, disclaimer)."""
    logo_path, srcset = resolve_logo()
    return {
        "logo_path": logo_path,
        "srcset": srcset,
        "site_name": SITE_NAME,
        "site_motto": SITE_MOTTO,
        "social_links": SOCIAL_LINKS,
        "base_domain": BASE_DOMAIN,
        "disclaimer": DISCLAIMER,
    }
