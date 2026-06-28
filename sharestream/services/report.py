"""Report form: per-IP rate limiting and SMTP delivery.

The Report endpoint is an unauthenticated, side-effecting (email-sending) route,
so it is rate limited per IP (in-memory, per-process — see the multi-worker
note). SMTP I/O is blocking and is run off the event loop by the caller-facing
:func:`send_report_request`.
"""
from __future__ import annotations

import asyncio
import datetime
import logging
import smtplib
import ssl
import time
from datetime import timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from threading import Lock

from fastapi import HTTPException

from sharestream.config import (
    SITE_NAME,
    SMTP_HOST,
    SMTP_MAILTO,
    SMTP_PASS,
    SMTP_PORT,
    SMTP_USER,
)
from sharestream.schemas.report import ReportRequest

logger = logging.getLogger(__name__)

# --- Report form rate limiting (per-IP, in-memory) ---
REPORT_MAX_PER_WINDOW = 3
REPORT_WINDOW_SECONDS = 60 * 60  # 1 hour
_report_attempts: dict[str, list[float]] = {}  # ip -> list[timestamp]
_report_lock = Lock()


def report_rate_limited(ip: str) -> bool:
    """Record a submission attempt and return True if the IP is over the limit."""
    now = time.time()
    with _report_lock:
        times = [t for t in _report_attempts.get(ip, []) if now - t < REPORT_WINDOW_SECONDS]
        if len(times) >= REPORT_MAX_PER_WINDOW:
            _report_attempts[ip] = times
            return True
        times.append(now)
        _report_attempts[ip] = times
        return False


def _send_report_email_sync(msg: MIMEMultipart) -> None:
    """Blocking SMTP send. Runs in a worker thread so smtplib's network I/O
    (connect/STARTTLS/login/send) never stalls the async event loop."""
    context = ssl.create_default_context()
    if SMTP_PORT == 465:  # SMTPS (SSL from the start)
        server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=context)
    elif SMTP_PORT == 587:  # STARTTLS
        server = smtplib.SMTP(SMTP_HOST, SMTP_PORT)
        server.ehlo()
        server.starttls(context=context)
        server.ehlo()
    else:
        raise ValueError(f"Unsupported SMTP port {SMTP_PORT}; use 465 (SMTPS) or 587 (STARTTLS)")
    try:
        if SMTP_USER and SMTP_PASS:  # Only login if credentials are provided
            server.login(SMTP_USER, SMTP_PASS)
        server.send_message(msg)
    finally:
        try:
            server.quit()
        except Exception:
            pass


async def send_report_request(request: ReportRequest) -> dict:
    """Build and send the report email, mapping failures to HTTPException.

    Returns the success payload on success.
    """
    if not SMTP_MAILTO:
        logger.error("SMTP configuration missing - no mailto address configured")
        raise HTTPException(status_code=500, detail="Email configuration error")

    # Format the report type for the subject line
    report_type_display = request.report_type.replace('_', ' ').title()

    # Create email message
    msg = MIMEMultipart()
    msg['From'] = SMTP_USER
    msg['To'] = SMTP_MAILTO
    msg['Subject'] = f"[{report_type_display}] Report from {request.reporter_name}"

    # Build email body
    body = f"""
Site Content Report

Report Type: {report_type_display}
Reporter Name/Company: {request.reporter_name}
Reporter Email: {request.reporter_email}
Reporter Website: {request.reporter_website or 'N/A'}

Reported Links:
{request.reported_links}

Description / Additional Details:
{request.description}

---
This request was submitted via the {SITE_NAME} Report form at {datetime.datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}
"""

    msg.attach(MIMEText(body, 'plain'))

    # Send email off the event loop (smtplib is blocking).
    try:
        await asyncio.to_thread(_send_report_email_sync, msg)
        logger.info(f"Report request sent from {request.reporter_email}")
        return {"status": "success", "message": "Your report has been submitted successfully."}
    except ValueError as e:
        logger.error(f"SMTP configuration error: {e}")
        raise HTTPException(status_code=500, detail="SMTP configuration error: unsupported port for secure email.")
    except smtplib.SMTPAuthenticationError as e:
        logger.error(f"SMTP Authentication failed: {e}. Check SMTP_USER and SMTP_PASS.")
        raise HTTPException(status_code=500, detail="Email server authentication failed.")
    except smtplib.SMTPConnectError as e:
        logger.error(f"Failed to connect to SMTP server {SMTP_HOST}:{SMTP_PORT}: {e}")
        raise HTTPException(status_code=500, detail="Could not connect to email server.")
    except smtplib.SMTPServerDisconnected as e:
        logger.error(f"SMTP server disconnected: {e}")
        raise HTTPException(status_code=500, detail="Email server disconnected unexpectedly.")
    except ssl.SSLError as e:
        logger.error(f"SSL Error during SMTP communication: {e}. Check port ({SMTP_PORT}), SSL/TLS settings, and server certificates.")
        raise HTTPException(status_code=500, detail=f"SSL error with email server: {e}")
    except Exception as e:
        logger.error(f"Failed to send report email: {e} (Host: {SMTP_HOST}, Port: {SMTP_PORT}, User: {SMTP_USER})")
        raise HTTPException(status_code=500, detail="Failed to send email due to an unexpected error.")
