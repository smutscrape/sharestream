"""DMCA takedown form: the page and its submission handler."""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from sharestream.core.branding import site_context
from sharestream.core.security import client_ip
from sharestream.core.templates import render
from sharestream.schemas.dmca import DMCARequest
from sharestream.services.dmca import dmca_rate_limited, send_dmca_request

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/dmca", response_class=HTMLResponse)
async def dmca_page():
    """Display the DMCA takedown request form"""
    try:
        html = render("dmca-form.html", **site_context())
        return HTMLResponse(html)
    except Exception as e:
        logger.error(f"Error displaying DMCA form: {e}")
        raise HTTPException(status_code=500, detail="Failed to display DMCA form")


@router.post("/dmca/submit")
async def submit_dmca(request: DMCARequest, http_request: Request):
    """Handle DMCA takedown form submission"""
    try:
        ip = client_ip(http_request)
        if dmca_rate_limited(ip):
            logger.warning(f"DMCA submission rate-limited for IP {ip}")
            raise HTTPException(status_code=429, detail="Too many requests. Please try again later.")

        return await send_dmca_request(request)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error processing DMCA submission: {e}")
        raise HTTPException(status_code=500, detail="Failed to process DMCA request")
