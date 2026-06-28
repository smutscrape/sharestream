"""Report form: the page and its submission handler."""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from sharestream.core.branding import site_context
from sharestream.core.security import client_ip
from sharestream.core.templates import render
from sharestream.schemas.report import ReportRequest
from sharestream.services.report import report_rate_limited, send_report_request

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/report", response_class=HTMLResponse)
async def report_page():
    """Display the content report / DMCA form"""
    try:
        html = render("report-form.html", **site_context())
        return HTMLResponse(html)
    except Exception as e:
        logger.error(f"Error displaying report form: {e}")
        raise HTTPException(status_code=500, detail="Failed to display report form")


@router.post("/report/submit")
async def submit_report(request: ReportRequest, http_request: Request):
    """Handle report form submission"""
    try:
        ip = client_ip(http_request)
        if report_rate_limited(ip):
            logger.warning(f"Report submission rate-limited for IP {ip}")
            raise HTTPException(status_code=429, detail="Too many requests. Please try again later.")

        return await send_report_request(request)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error processing report submission: {e}")
        raise HTTPException(status_code=500, detail="Failed to process report request")
