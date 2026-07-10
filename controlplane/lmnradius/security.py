# SPDX-FileCopyrightText: Kevin Stenzel
# SPDX-License-Identifier: GPL-3.0-or-later
"""Bearer-token authentication dependency for the control plane API."""

from __future__ import annotations

import hmac
from typing import Callable, Coroutine, Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import Settings


def make_verify_token(
    settings: Settings,
) -> Callable[..., Coroutine[object, object, None]]:
    """Build a FastAPI dependency that enforces the configured API token.

    - No credentials supplied  -> HTTP 401.
    - Credentials with a wrong token -> HTTP 403 (constant-time compare).
    - Valid token -> returns ``None`` (request proceeds).
    """

    bearer = HTTPBearer(auto_error=False)

    async def verify(
        cred: Optional[HTTPAuthorizationCredentials] = Depends(bearer),
    ) -> None:
        if cred is None or not cred.credentials:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing authentication credentials",
                headers={"WWW-Authenticate": "Bearer"},
            )
        if not hmac.compare_digest(
            cred.credentials.encode("utf-8"), settings.api_token.encode("utf-8")
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Invalid authentication token",
            )
        return None

    return verify
