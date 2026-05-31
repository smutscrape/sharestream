"""Admin authentication: the login endpoint."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import OAuth2PasswordRequestForm

from sharestream.config import ADMIN_USERNAME
from sharestream.core.security import (
    HASHED_ADMIN_PASSWORD,
    client_ip,
    create_access_token,
    login_record_failure,
    login_record_success,
    login_seconds_remaining,
    pwd_context,
)
from sharestream.schemas.auth import Token

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/login", response_model=Token)
async def login(request: Request, form_data: OAuth2PasswordRequestForm = Depends()):
    ip = client_ip(request)
    logger.debug(f"Login attempt: username={form_data.username} ip={ip}")
    try:
        # Brute-force lockout: after LOGIN_MAX_FAILS failures, ban the IP.
        remaining = login_seconds_remaining(ip)
        if remaining:
            raise HTTPException(
                status_code=429,
                detail=f"Too many failed attempts. Try again in about {max(1, remaining // 60)} minute(s).",
                headers={"Retry-After": str(remaining)},
            )

        if not form_data.username or not form_data.password:
            raise HTTPException(status_code=422, detail="Username and password are required")
        if form_data.username != ADMIN_USERNAME or not pwd_context.verify(form_data.password, HASHED_ADMIN_PASSWORD):
            login_record_failure(ip)
            logger.warning(f"Failed admin login from ip={ip}")
            raise HTTPException(status_code=401, detail="Incorrect username or password")

        login_record_success(ip)
        access_token = create_access_token(data={"sub": form_data.username})
        logger.info(f"Login successful for username={form_data.username} ip={ip}")
        return {"access_token": access_token, "token_type": "bearer"}
    except HTTPException as http_exc:
        raise http_exc
    except Exception as e:
        logger.error(f"Login error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
