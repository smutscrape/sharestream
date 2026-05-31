"""Friendly error handling.

Browser navigations to a missing / expired / forbidden page get a themed HTML
page; API / AJAX callers (which don't ask for HTML) still get JSON.
"""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from sharestream.core.branding import site_context
from sharestream.core.templates import render

logger = logging.getLogger(__name__)


async def friendly_error_handler(request: Request, exc: StarletteHTTPException):
    """Show a themed page for 'page not found / expired' on browser navigations,
    while still returning JSON for API / AJAX callers (which don't ask for HTML)."""
    accept = request.headers.get("accept") or ""
    if exc.status_code in (403, 404, 410) and "text/html" in accept:
        try:
            html = render("404.html", **site_context())
            return HTMLResponse(html, status_code=exc.status_code)
        except Exception as e:
            logger.error(f"Error rendering friendly error page: {e}")
    return JSONResponse(
        {"detail": exc.detail},
        status_code=exc.status_code,
        headers=getattr(exc, "headers", None),
    )


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(StarletteHTTPException, friendly_error_handler)
