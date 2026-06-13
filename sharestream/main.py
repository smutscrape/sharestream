"""Application factory and assembled FastAPI app.

``create_app()`` builds the FastAPI application: mounts /static, registers
middleware and error handlers, runs DB bootstrap/migrations, and includes the
routers. Routers are registered so explicit routes always precede the catch-all
short-URL routes, which are included LAST.

``app = create_app()`` is the ASGI entry point (e.g. ``uvicorn sharestream.main:app``).
"""
from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from sharestream.core.errors import register_error_handlers
from sharestream.core.http_client import close_http_client
from sharestream.db.migrations import init_db
from sharestream.routers import (
    admin,
    auth,
    dmca,
    embeds,
    media,
    pages,
    public,
    shares,
    short_urls,
    site,
    tags,
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    app = FastAPI()

    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # Adjust for production
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Static assets
    app.mount("/static", StaticFiles(directory="static"), name="static")

    # Error handling
    register_error_handlers(app)

    # Database bootstrap + idempotent migrations
    init_db()

    # Routers — explicit routes first; short_urls (catch-all) LAST.
    app.include_router(site.router)
    app.include_router(auth.router)
    app.include_router(admin.router)
    app.include_router(shares.router)
    app.include_router(tags.router)
    app.include_router(media.router)
    app.include_router(embeds.router)
    app.include_router(dmca.router)
    app.include_router(pages.router)
    app.include_router(public.router)
    app.include_router(short_urls.router)  # MUST be included last

    # Cleanly close the shared HTTP client on shutdown.
    app.add_event_handler("shutdown", close_http_client)

    return app


app = create_app()
