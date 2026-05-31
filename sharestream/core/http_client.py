"""Shared async HTTP client used for all upstream Stash calls.

A single ``AsyncClient`` (with its connection pool) is reused for every upstream
Stash call. Together with httpx's async I/O this is what lets concurrent
viewers' Stash requests run in parallel instead of head-of-line blocking the
event loop the way the old synchronous ``requests`` calls did.
"""
from __future__ import annotations

import re

import httpx

# Redacts secret query params (apikey, pwd) before a URL is logged, so Stash API
# keys / share passwords never land in logs or journald.
_SECRET_QS_RE = re.compile(r"([?&](?:apikey|pwd)=)[^&]+", re.IGNORECASE)


def redact_url(url: str) -> str:
    """Return ``url`` with any apikey/pwd query value replaced by <redacted>."""
    return _SECRET_QS_RE.sub(r"\1<redacted>", url)

HTTP_TIMEOUT = httpx.Timeout(connect=15.0, read=60.0, write=60.0, pool=15.0)
# Media proxying must not impose a read timeout (a long/slow download would be
# killed mid-stream), but we still bound how long we'll wait to connect.
STREAM_TIMEOUT = httpx.Timeout(connect=15.0, read=None, write=None, pool=15.0)

http_client = httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True)


async def open_stash_stream(url: str, headers: dict | None = None) -> httpx.Response:
    """Open a streaming GET to Stash. The returned response has its status and
    headers available but its body unread; the caller MUST close it (the
    streaming generators all do so in a ``finally``)."""
    req = http_client.build_request("GET", url, headers=headers or {}, timeout=STREAM_TIMEOUT)
    return await http_client.send(req, stream=True)


def segment_headers(response: httpx.Response) -> dict:
    """Build HLS-segment response headers, mirroring upstream Content-Length only
    when present. Passing a None header value to StreamingResponse raises, which
    would 500 an otherwise-fine segment whenever Stash omits Content-Length."""
    headers = {
        "Accept-Ranges": "bytes",
        "Access-Control-Allow-Origin": "*",
        "Cache-Control": "public, max-age=3600",
    }
    content_length = response.headers.get("Content-Length")
    if content_length:
        headers["Content-Length"] = content_length
    return headers


async def close_http_client() -> None:
    await http_client.aclose()
