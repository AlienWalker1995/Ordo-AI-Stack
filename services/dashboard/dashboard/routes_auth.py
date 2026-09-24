"""The operator's sign-in state, and the local-mode sign-in (dashboard/auth.py holds the model)."""
from __future__ import annotations

import logging
import time

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from dashboard import auth, settings

logger = logging.getLogger(__name__)

router = APIRouter()


class LocalSignIn(BaseModel):
    token: str


@router.get("/api/auth/session")
async def session(request: Request) -> dict:
    """How the operator signs in here (`local` or `edge`), and whether this request is signed in."""
    return {"mode": "local" if auth.local_mode() else "edge",
            "signed_in": await auth.request_principal(request) is not None}


@router.post(auth.LOCAL_SIGN_IN_PATH, status_code=204)
async def local_sign_in(body: LocalSignIn, request: Request) -> Response:
    """Exchange the local sign-in token for a session cookie. Absent (404) with the edge on.

    JSON only (the body model), so a cross-site HTML form cannot post here; the cookie is
    SameSite=Strict, so a cross-site request never carries it back."""
    token = settings.DASHBOARD_LOCAL_LOGIN_TOKEN
    if not token:
        raise HTTPException(status_code=404, detail="Local sign-in is off: sign in through the SSO edge")
    if not auth.local_token_matches(body.token, token):
        peer_ip = request.client.host if request.client else "unknown"
        logger.warning("LOCAL_SIGN_IN_FAIL src=%s", peer_ip)
        raise HTTPException(status_code=401, detail="That is not this stack's sign-in token")
    response = Response(status_code=204)
    response.set_cookie(
        auth.LOCAL_SESSION_COOKIE, auth.local_session_cookie(token, now=time.time()),
        max_age=auth.LOCAL_SESSION_SECONDS, path="/", httponly=True, samesite="strict",
    )
    return response
