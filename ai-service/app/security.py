"""
app/security.py — service-to-service authentication for the AI service.

This service performs no user authentication of its own: the ASP.NET API owns
identity, validates the user's JWT, and passes down an already-authorised
user_id. That is a reasonable split, but it means anything able to reach port
8001 could otherwise impersonate any user, read their vectors and spend their
model budget. Being on the network must not be the same as being authorised.

So every route except /health requires a shared secret that only the API knows.
The comparison is constant-time, because a naive == leaks the secret one byte
at a time to anyone who can measure response latency.

If SERVICE_TOKEN is unset the check is skipped and a loud warning is logged —
that keeps `docker compose up` working out of the box, while making it obvious
that the deployment is open.
"""
from __future__ import annotations

import hmac
import logging
import os

from fastapi import Request
from fastapi.responses import JSONResponse

log = logging.getLogger(__name__)

HEADER = "X-Service-Token"

# Routes reachable without the shared secret: liveness only.
PUBLIC_PATHS = {"/health"}

# Docs are useful locally but should not describe the surface to the internet.
_DEV_PATHS = {"/docs", "/redoc", "/openapi.json"}

_TOKEN = os.getenv("SERVICE_TOKEN", "").strip()
_ALLOW_DOCS = os.getenv("EXPOSE_DOCS", "").lower() in ("1", "true", "yes")

if not _TOKEN:
    log.warning(
        "SERVICE_TOKEN is not set — the AI service will accept unauthenticated "
        "requests. Set it (and Auth__ServiceToken on the API) before exposing "
        "this service anywhere untrusted."
    )


def is_public(path: str) -> bool:
    if path in PUBLIC_PATHS:
        return True
    if path in _DEV_PATHS and (_ALLOW_DOCS or not _TOKEN):
        return True
    return False


def token_valid(presented: str | None) -> bool:
    if not _TOKEN:
        return True  # unconfigured: open, and already warned about above
    if not presented:
        return False
    # compare_digest keeps the comparison independent of how many leading bytes
    # happen to match.
    return hmac.compare_digest(presented, _TOKEN)


async def service_token_middleware(request: Request, call_next):
    if is_public(request.url.path):
        return await call_next(request)

    if not token_valid(request.headers.get(HEADER)):
        # Deliberately terse: do not reveal whether the header was missing,
        # malformed, or simply wrong.
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)

    return await call_next(request)
