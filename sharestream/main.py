"""Application factory and assembled FastAPI app.

``create_app()`` builds the FastAPI application: mounts /static, registers
middleware and error handlers, runs DB bootstrap/migrations, and includes the
routers. Routers are registered so explicit routes always precede the catch-all
short-URL routes, which are included LAST.

``app = create_app()`` is the ASGI entry point (e.g. ``uvicorn sharestream.main:app``).
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response

from sharestream.config import LIMIT_TO_TAG
from sharestream.core.errors import register_error_handlers
from sharestream.core.http_client import close_http_client
from sharestream.db.migrations import run_migrations
from sharestream.routers import (
    admin,
    auth,
    dmca,
    embeds,
    filedrop,
    media,
    pages,
    public,
    shares,
    short_urls,
    site,
    tags,
    video,
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class ImmutableStaticFiles(StaticFiles):
    """Serve /static with a far-future immutable cache. Safe because templates
    reference these files through the ``asset()`` helper, which appends an mtime
    query string — so an edited file gets a new URL and is never served stale."""

    def file_response(self, *args, **kwargs) -> Response:
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return resp


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await close_http_client()


def create_app() -> FastAPI:
    app = FastAPI(lifespan=lifespan)

    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # Adjust for production
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Static assets
    app.mount("/static", ImmutableStaticFiles(directory="static"), name="static")

    # Error handling
    register_error_handlers(app)

    # Database schema: stamp legacy DBs, then alembic upgrade head
    run_migrations()

    # limit_to_tag is retired as an access gate (Phase 2): scene visibility is now
    # governed by visibility_tags. Warn once at startup if it's still configured.
    if LIMIT_TO_TAG:
        logger.warning(
            "stash.limit_to_tag is set but is DEPRECATED as an access-control "
            "mechanism. Scene visibility is now governed by stash.visibility_tags "
            "(public/listed/hidden). limit_to_tag now only scopes curated Gallery "
            "surfaces; configure visibility_tags and remove limit_to_tag when ready."
        )

    # Routers — explicit routes first; short_urls (catch-all) LAST.
    app.include_router(site.router)
    app.include_router(auth.router)
    app.include_router(admin.router)
    app.include_router(shares.router)
    app.include_router(tags.router)
    app.include_router(video.router)
    app.include_router(media.router)
    app.include_router(embeds.router)
    app.include_router(dmca.router)
    app.include_router(filedrop.router)
    app.include_router(pages.router)
    app.include_router(public.router)
    app.include_router(short_urls.router)  # MUST be included last

    return app


app = create_app()
