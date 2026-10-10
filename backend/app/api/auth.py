import asyncio
import base64
import binascii
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
from fastapi import HTTPException, status

from app.core.config import Settings
from app.core.supabase import project_base_url

_AUTH_DEADLINE_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class VerifiedUser:
    id: UUID
    email: str | None
    expires_at: datetime | None = None


def _verified_jwt_expiry(token: str, user_id: UUID) -> datetime:
    """Read claims only after Supabase Auth has verified this exact access token."""
    parts = token.split(".")
    if len(parts) != 3 or not all(parts):
        raise _unauthorized()
    try:
        claims_raw = base64.b64decode(
            parts[1] + "=" * (-len(parts[1]) % 4), altchars=b"-_", validate=True
        )
        claims = json.loads(claims_raw)
        if not isinstance(claims, dict):
            raise ValueError("Invalid claims")
        subject = claims.get("sub")
        expiry = claims.get("exp")
        if (
            not isinstance(subject, str)
            or UUID(subject) != user_id
            or claims.get("role") != "authenticated"
            or isinstance(expiry, bool)
            or not isinstance(expiry, int)
        ):
            raise ValueError("Invalid access claims")
        expires_at = datetime.fromtimestamp(expiry, UTC)
    except binascii.Error, ValueError, TypeError, OverflowError:
        raise _unauthorized() from None
    if expires_at <= datetime.now(UTC) + timedelta(seconds=5):
        raise _unauthorized()
    return expires_at


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Sign in to continue.",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _unavailable() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Authentication is temporarily unavailable.",
    )


class SupabaseAuthVerifier:
    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        project_url = project_base_url(settings)
        self._endpoint = None if project_url is None else f"{project_url}/auth/v1/user"
        self._publishable_key = (settings.supabase_publishable_key or "").strip()
        self._client = client

    async def verify_bearer(self, authorization: str | None) -> VerifiedUser:
        if not authorization:
            raise _unauthorized()
        scheme, separator, token = authorization.partition(" ")
        if not separator or scheme.casefold() != "bearer":
            raise _unauthorized()
        return await self.verify_token(token)

    async def verify_token(self, token: str) -> VerifiedUser:
        if (
            not token
            or len(token) > 16_384
            or not token.isascii()
            or any(character.isspace() for character in token)
        ):
            raise _unauthorized()
        if self._endpoint is None:
            raise _unavailable()

        try:
            async with asyncio.timeout(_AUTH_DEADLINE_SECONDS):
                response = await self._client.get(
                    self._endpoint,
                    headers={
                        "apikey": self._publishable_key,
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/json",
                    },
                    timeout=httpx.Timeout(5.0, connect=3.0),
                    follow_redirects=False,
                )
        except httpx.RequestError, TimeoutError:
            raise _unavailable() from None

        if response.status_code in {400, 401, 403}:
            raise _unauthorized()
        if response.status_code != 200:
            raise _unavailable()

        try:
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("Auth user must be an object")
            raw_id = payload.get("id")
            email = payload.get("email")
            if not isinstance(raw_id, str) or (email is not None and not isinstance(email, str)):
                raise ValueError("Auth user has invalid fields")
            user_id = UUID(raw_id)
        except ValueError, TypeError:
            raise _unavailable() from None
        return VerifiedUser(
            id=user_id, email=email, expires_at=_verified_jwt_expiry(token, user_id)
        )
