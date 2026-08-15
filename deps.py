"""Shared FastAPI dependencies.

Same shape as conduct's `deps.py`: pull the long-lived objects off `app.state`,
where the lifespan put them, rather than reaching for module globals. That is
what lets a test build its own engine on a NullDevice and override one dep.
"""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from config.settings import Settings, get_settings
from core.engine import Engine
from library.library import Library

_bearer = HTTPBearer(auto_error=False)


def get_engine(request: Request) -> Engine:
    engine = getattr(request.app.state, "engine", None)
    if engine is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "engine is not running"
        )
    return engine


def get_library(request: Request) -> Library:
    library = getattr(request.app.state, "library", None)
    if library is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "library is not initialized"
        )
    return library


async def require_token(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    """No-op unless PERFORM_TOKEN is set.

    SPEC §7: *"Loopback-only with no auth is fine to start. Anything exposed
    over a tunnel needs a shared secret first."* So the default really is no
    auth, and the knob exists so that turning it on is an edit rather than a
    refactor. `compare_digest` because a token check that leaks timing is worse
    than no check at all pretending otherwise.
    """
    if not settings.token:
        return
    if credentials is None or not hmac.compare_digest(credentials.credentials, settings.token):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "invalid or missing bearer token")


EngineDep = Annotated[Engine, Depends(get_engine)]
LibraryDep = Annotated[Library, Depends(get_library)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
Authed = Depends(require_token)
