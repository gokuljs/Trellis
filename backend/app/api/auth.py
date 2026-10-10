import asyncio
import re
from dataclasses import dataclass
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from fastapi import HTTPException, status

from app.core.config import Settings

_HOSTED_PROJECT = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.supabase\.co\Z")
_PUBLISHABLE_KEY = re.compile(r"sb_publishable_[A-Za-z0-9_-]+\Z")
_AUTH_DEADLINE_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class VerifiedUser:
    id: UUID
    email: str | None


def _auth_endpoint(settings: Settings) -> str | None:
    raw_url = (settings.supabase_url or "").strip()
    key = (settings.supabase_publishable_key or "").strip()
    if not raw_url or _PUBLISHABLE_KEY.fullmatch(key) is None:
        return None
    if "?" in raw_url or "#" in raw_url or "\\" in raw_url:
        return None
    try:
        parts = urlsplit(raw_url)
        port = parts.port
    except ValueError:
        return None
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.path not in {"", "/"}
        or (port is not None and port < 1)
    ):
        return None
    if parts.scheme == "https" and (
        _HOSTED_PROJECT.fullmatch(parts.hostname) is None or port not in {None, 443}
    ):
        return None
    if parts.scheme == "http" and not (
        settings.environment == "development" and parts.hostname in {"localhost", "127.0.0.1"}
    ):
        return None
    return f"{parts.scheme}://{parts.netloc}/auth/v1/user"


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
        self._endpoint = _auth_endpoint(settings)
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
        return VerifiedUser(id=user_id, email=email)
