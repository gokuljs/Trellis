"""External operator catalog refresh, with no application account privileges.

Run from backend with `uv run python -m app.infrastructure.catalog_sync`.
Provider keys and the restricted PostgreSQL URL come only from its environment.
The worker never accepts provider URLs, arbitrary SQL, or trusted profile data.
"""

import argparse
import asyncio
import json
import os
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from urllib.parse import parse_qs, unquote, urlsplit

import httpx

MAX_MODELS = 2000
MAX_PAGES = 20
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
DISCOVERY_TIMEOUT_SECONDS = 30.0
DATABASE_TIMEOUT_SECONDS = 20.0
REFRESH_INTERVAL_SECONDS = 600
PSQL_ARGUMENTS = (
    "psql",
    "-X",
    "--no-password",
    "--set",
    "ON_ERROR_STOP=1",
    "--quiet",
    "--tuples-only",
    "--no-align",
)
_ENDPOINTS = {
    "openai": "https://api.openai.com/v1/models",
    "anthropic": "https://api.anthropic.com/v1/models",
}
_MODEL_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._:-]{0,199}\Z")
CatalogWriter = Callable[[str, tuple[str, ...]], Awaitable[None]]


class CatalogDiscoveryError(Exception):
    """Safe code only; never include provider bodies, headers or credentials."""


class CatalogDatabaseError(Exception):
    """Safe code only; psql stderr is discarded rather than forwarded."""


def _model_ids(provider: str, values: Sequence[object]) -> tuple[str, ...]:
    if provider not in _ENDPOINTS or len(values) > MAX_MODELS:
        raise ValueError("invalid catalog snapshot")
    ids: list[str] = []
    for value in values:
        if not isinstance(value, str) or not _MODEL_ID.fullmatch(value):
            raise ValueError("invalid catalog model ID")
        ids.append(value)
    return tuple(sorted(set(ids)))


async def discover_models(
    client: httpx.AsyncClient, provider: str, api_key: str
) -> tuple[str, ...]:
    if provider not in _ENDPOINTS:
        raise CatalogDiscoveryError("provider_unsupported")
    headers = (
        {"Authorization": f"Bearer {api_key}"}
        if provider == "openai"
        else {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    )
    ids: set[str] = set()
    seen_cursors: set[str] = set()
    params: dict[str, str] = {} if provider == "openai" else {"limit": "100"}
    try:
        async with asyncio.timeout(DISCOVERY_TIMEOUT_SECONDS):
            for _ in range(MAX_PAGES):
                async with client.stream(
                    "GET",
                    _ENDPOINTS[provider],
                    headers=headers,
                    params=params,
                    follow_redirects=False,
                    timeout=httpx.Timeout(10, connect=3),
                ) as response:
                    if response.status_code != 200:
                        raise CatalogDiscoveryError("discovery_failed")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                            raise CatalogDiscoveryError("catalog_response_too_large")
                        body.extend(chunk)
                payload = json.loads(body)
                if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                    raise CatalogDiscoveryError("catalog_response_invalid")
                data = payload["data"]
                if not all(isinstance(item, dict) for item in data):
                    raise CatalogDiscoveryError("catalog_response_invalid")
                ids.update(_model_ids(provider, [item.get("id") for item in data]))
                if len(ids) > MAX_MODELS:
                    raise CatalogDiscoveryError("catalog_response_too_large")
                if provider == "openai" or payload.get("has_more") is False:
                    return tuple(sorted(ids))
                if payload.get("has_more") is not True:
                    raise CatalogDiscoveryError("catalog_response_invalid")
                cursor = payload.get("last_id")
                if (
                    not isinstance(cursor, str)
                    or not _MODEL_ID.fullmatch(cursor)
                    or cursor in seen_cursors
                ):
                    raise CatalogDiscoveryError("catalog_cursor_invalid")
                seen_cursors.add(cursor)
                params["after_id"] = cursor
            raise CatalogDiscoveryError("catalog_page_limit")
    except httpx.HTTPError, TimeoutError, ValueError, TypeError:
        raise CatalogDiscoveryError("discovery_failed") from None


def database_environment(dsn: str, environment: Mapping[str, str]) -> dict[str, str]:
    try:
        url = urlsplit(dsn)
        options = parse_qs(url.query, strict_parsing=True)
        if (
            url.scheme not in {"postgres", "postgresql"}
            or unquote(url.username or "") != "trellis_catalog_sync"
            or not url.hostname
            or not url.password
            or not url.path.strip("/")
            or "/" in url.path.strip("/")
            or url.fragment
            or set(options) - {"sslmode"}
            or options.get("sslmode", ["verify-full"])
            not in (["require"], ["verify-ca"], ["verify-full"])
        ):
            raise ValueError
        # A clean libpq environment prevents inherited PGOPTIONS/PGSERVICE from
        # changing the role, connection target, or SQL startup behavior.
        result = {
            key: value
            for key, value in environment.items()
            if not key.startswith(("PG", "TRELLIS_"))
        }
        result.update(
            {
                "PGHOST": url.hostname,
                "PGPORT": str(url.port or 5432),
                "PGDATABASE": unquote(url.path.strip("/")),
                "PGUSER": "trellis_catalog_sync",
                "PGPASSWORD": unquote(url.password),
                "PGSSLMODE": options.get("sslmode", ["verify-full"])[0],
                "PGCONNECT_TIMEOUT": "5",
                "PGAPPNAME": "trellis-catalog-sync",
                "PGOPTIONS": "-c statement_timeout=15000 -c lock_timeout=3000",
            }
        )
        return result
    except ValueError:
        raise ValueError("restricted database connection is invalid") from None


def sync_statement(provider: str, models: tuple[str, ...]) -> str:
    ids = _model_ids(provider, models)
    return (
        f"SELECT trellis_private.sync_model_catalog('{provider}', "
        f"$catalog${json.dumps(ids)}$catalog$::jsonb);\n"
    )


class RestrictedCatalogWriter:
    def __init__(self, environment: Mapping[str, str]) -> None:
        self._environment = dict(environment)

    async def __call__(self, provider: str, models: tuple[str, ...]) -> None:
        statement = sync_statement(provider, models).encode()
        try:
            process = await asyncio.create_subprocess_exec(
                *PSQL_ARGUMENTS,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                env=self._environment,
            )
        except OSError:
            raise CatalogDatabaseError("database_failed") from None
        try:
            async with asyncio.timeout(DATABASE_TIMEOUT_SECONDS):
                await process.communicate(statement)
            if process.returncode != 0:
                raise CatalogDatabaseError("database_failed")
        except BaseException as error:
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.terminate()
                try:
                    async with asyncio.timeout(2):
                        await process.wait()
                except TimeoutError:
                    with suppress(ProcessLookupError):
                        process.kill()
                    async with asyncio.timeout(2):
                        await process.wait()
            if isinstance(error, TimeoutError):
                raise CatalogDatabaseError("database_failed") from None
            raise


async def sync_once(
    client: httpx.AsyncClient,
    write: CatalogWriter,
    credentials: Mapping[str, str],
) -> dict[str, str]:
    outcomes: dict[str, str] = {}
    for provider in _ENDPOINTS:
        key = credentials.get(provider)
        if not key:
            outcomes[provider] = "not_configured"
            continue
        try:
            ids = await discover_models(client, provider, key)
        except CatalogDiscoveryError:
            outcomes[provider] = "discovery_failed"
            continue
        try:
            await write(provider, ids)
        except CatalogDatabaseError:
            outcomes[provider] = "database_failed"
            continue
        outcomes[provider] = "updated"
    return outcomes


async def operator_loop(
    client: httpx.AsyncClient,
    write: CatalogWriter,
    credentials: Mapping[str, str],
    *,
    once: bool = False,
    report: Callable[[str], None] = print,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> int:
    while True:
        outcomes = await sync_once(client, write, credentials)
        for provider, outcome in outcomes.items():
            report(f"{provider}: {outcome}")
        if once:
            return int(any(outcome.endswith("failed") for outcome in outcomes.values()))
        await sleep(REFRESH_INTERVAL_SECONDS)


async def _run_operator(environment: Mapping[str, str], *, once: bool) -> int:
    try:
        connection = database_environment(
            environment.get("TRELLIS_CATALOG_DATABASE_URL", ""), environment
        )
    except ValueError:
        print("catalog_configuration_invalid")
        return 2
    credentials = {
        provider: environment.get(f"TRELLIS_{provider.upper()}_API_KEY", "")
        for provider in _ENDPOINTS
    }
    async with httpx.AsyncClient(trust_env=False) as client:
        return await operator_loop(
            client, RestrictedCatalogWriter(connection), credentials, once=once
        )


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Refresh model availability with a restricted role."
    )
    parser.add_argument("--once", action="store_true", help="Refresh once and exit.")
    options = parser.parse_args(arguments)
    try:
        return asyncio.run(_run_operator(os.environ, once=options.once))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
