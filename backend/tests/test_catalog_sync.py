import asyncio
import importlib
import json
from types import ModuleType

import httpx
import pytest


def sync_module() -> ModuleType:
    try:
        return importlib.import_module("app.infrastructure.catalog_sync")
    except ModuleNotFoundError:
        pytest.fail("restricted provider catalog sync is not implemented")


def test_provider_discovery_reads_openai_ids_and_all_anthropic_pages() -> None:
    module = sync_module()
    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.host == "api.openai.com":
            assert request.headers["authorization"] == "Bearer openai-secret"
            return httpx.Response(200, json={"data": [{"id": "gpt-6.1-sol"}, {"id": "unknown"}]})
        assert request.headers["x-api-key"] == "anthropic-secret"
        assert request.headers["anthropic-version"] == "2023-06-01"
        if request.url.params.get("after_id") is None:
            return httpx.Response(
                200,
                json={
                    "data": [{"id": "claude-sonnet-5"}],
                    "has_more": True,
                    "last_id": "claude-sonnet-5",
                },
            )
        assert request.url.params["after_id"] == "claude-sonnet-5"
        return httpx.Response(200, json={"data": [{"id": "claude-haiku-5-5"}], "has_more": False})

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            assert await module.discover_models(client, "openai", "openai-secret") == (
                "gpt-6.1-sol",
                "unknown",
            )
            assert await module.discover_models(client, "anthropic", "anthropic-secret") == (
                "claude-haiku-5-5",
                "claude-sonnet-5",
            )

    asyncio.run(exercise())
    assert len(calls) == 3


def test_outage_keeps_successful_cache_and_recovers_on_next_sync() -> None:
    module = sync_module()
    available = True
    snapshots: list[tuple[str, tuple[str, ...]]] = []

    async def write(provider: str, models: tuple[str, ...]) -> None:
        snapshots.append((provider, models))

    def respond(request: httpx.Request) -> httpx.Response:
        if not available:
            return httpx.Response(503, text="provider secret must not escape")
        return httpx.Response(200, json={"data": [{"id": "gpt-6.1-sol"}]})

    async def exercise() -> None:
        nonlocal available
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            credentials = {"openai": "secret"}
            first = await module.sync_once(client, write, credentials)
            assert first == {"openai": "updated", "anthropic": "not_configured"}
            available = False
            assert (await module.sync_once(client, write, credentials))[
                "openai"
            ] == "discovery_failed"
            assert snapshots == [("openai", ("gpt-6.1-sol",))]
            available = True
            assert (await module.sync_once(client, write, credentials))["openai"] == "updated"
            assert len(snapshots) == 2

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "response",
    [
        {"data": "bad"},
        {"data": [{"id": "bad'$(secret)"}]},
        {"data": [{"id": "x"}], "has_more": True, "last_id": "x"},
    ],
)
def test_invalid_or_repeated_catalog_pages_fail_before_any_database_write(response: object) -> None:
    module = sync_module()

    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response))
        ) as client:
            with pytest.raises(module.CatalogDiscoveryError):
                await module.discover_models(client, "anthropic", "secret")

    asyncio.run(exercise())


def test_restricted_database_connection_never_puts_credentials_in_process_arguments() -> None:
    module = sync_module()
    connection = module.database_environment(
        "postgresql://trellis_catalog_sync:password%40marker@db.example.test:5432/postgres?sslmode=require",
        {
            "PATH": "/bin",
            "PGUSER": "privileged",
            "PGSERVICE": "untrusted",
            "PGOPTIONS": "-c role=admin",
        },
    )
    assert connection["PGUSER"] == "trellis_catalog_sync"
    assert connection["PGPASSWORD"] == "password@marker"
    assert connection["PGSSLMODE"] == "require"
    assert connection["PGOPTIONS"] == "-c statement_timeout=15000 -c lock_timeout=3000"
    assert "PGSERVICE" not in connection
    assert "db.example.test" not in module.PSQL_ARGUMENTS
    assert "password@marker" not in module.PSQL_ARGUMENTS
    sql = module.sync_statement("openai", ("gpt-6.1-sol", "unknown"))
    assert "trellis_private.sync_model_catalog" in sql
    assert json.loads(sql.split("$catalog$")[1]) == ["gpt-6.1-sol", "unknown"]


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://postgres:secret@db.example.test/postgres",
        "postgresql://trellis_catalog_sync:secret@db.example.test/postgres?options=unsafe",
        "https://trellis_catalog_sync:secret@db.example.test/postgres",
    ],
)
def test_operator_rejects_privileged_or_arbitrary_database_options(dsn: str) -> None:
    module = sync_module()
    with pytest.raises(ValueError, match="restricted database connection"):
        module.database_environment(dsn, {})


def test_discovery_body_and_pagination_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    module = sync_module()
    monkeypatch.setattr(module, "MAX_RESPONSE_BYTES", 8)

    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"x" * 9))
        ) as client:
            with pytest.raises(module.CatalogDiscoveryError):
                await module.discover_models(client, "openai", "secret")

    asyncio.run(exercise())


def test_database_timeout_terminates_subprocess_and_hides_connection_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = sync_module()
    arguments: list[object] = []
    stopped = False
    release: asyncio.Event

    class Process:
        returncode = None

        async def communicate(self, input: bytes) -> tuple[bytes, bytes]:
            assert b"sync_model_catalog" in input
            await release.wait()
            return b"", b"password must not escape"

        def terminate(self) -> None:
            nonlocal stopped
            stopped = True
            release.set()

        async def wait(self) -> int:
            return 0

    async def spawn(*args: str, **kwargs: object) -> Process:
        arguments.extend(args)
        assert kwargs["stderr"] == asyncio.subprocess.DEVNULL
        assert "restricted-secret" not in args
        return Process()

    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(module, "DATABASE_TIMEOUT_SECONDS", 0.01)

    async def exercise() -> None:
        nonlocal release
        release = asyncio.Event()
        writer = module.RestrictedCatalogWriter({"PGPASSWORD": "restricted-secret"})
        with pytest.raises(module.CatalogDatabaseError, match="database_failed"):
            await writer("openai", ("gpt-6.1-sol",))
        assert stopped and "restricted-secret" not in arguments

    asyncio.run(exercise())


def test_operator_waits_ten_minutes_between_completed_cycles() -> None:
    module = sync_module()
    reports: list[str] = []
    intervals: list[float] = []
    writes = 0

    async def write(provider: str, models: tuple[str, ...]) -> None:
        nonlocal writes
        writes += 1

    async def sleep(seconds: float) -> None:
        intervals.append(seconds)
        raise asyncio.CancelledError

    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"data": []}))
        ) as client:
            with pytest.raises(asyncio.CancelledError):
                await module.operator_loop(
                    client, write, {"openai": "secret"}, report=reports.append, sleep=sleep
                )

    asyncio.run(exercise())
    assert writes == 1 and intervals == [600]
    assert reports == ["openai: updated", "anthropic: not_configured"]


def test_cli_missing_connection_reports_only_safe_configuration_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = sync_module()
    monkeypatch.delenv("TRELLIS_CATALOG_DATABASE_URL", raising=False)
    monkeypatch.setenv("TRELLIS_OPENAI_API_KEY", "provider-secret")
    assert module.main(["--once"]) == 2
    captured = capsys.readouterr()
    assert "provider-secret" not in captured.out + captured.err
    assert "catalog_configuration_invalid" in captured.out


def test_database_failure_is_isolated_from_the_other_provider() -> None:
    module = sync_module()

    async def write(provider: str, models: tuple[str, ...]) -> None:
        if provider == "openai":
            raise module.CatalogDatabaseError("database_failed")

    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"data": [], "has_more": False})
            )
        ) as client:
            outcomes = await module.sync_once(
                client, write, {"openai": "secret", "anthropic": "secret"}
            )
            assert outcomes == {"openai": "database_failed", "anthropic": "updated"}
            assert (
                await module.operator_loop(
                    client, write, {"openai": "secret"}, once=True, report=lambda message: None
                )
                == 1
            )

    asyncio.run(exercise())


def test_discovery_timeout_and_redirect_do_not_publish_partial_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = sync_module()

    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1)
        return httpx.Response(200, json={"data": []})

    async def exercise() -> None:
        monkeypatch.setattr(module, "DISCOVERY_TIMEOUT_SECONDS", 0.01)
        async with httpx.AsyncClient(transport=httpx.MockTransport(slow)) as client:
            with pytest.raises(module.CatalogDiscoveryError, match="discovery_failed"):
                await module.discover_models(client, "openai", "secret")
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    302, headers={"location": "https://other.example.test"}
                )
            ),
            follow_redirects=True,
        ) as client:
            with pytest.raises(module.CatalogDiscoveryError, match="discovery_failed"):
                await module.discover_models(client, "openai", "secret")

    asyncio.run(exercise())


def test_database_cancellation_terminates_child_and_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = sync_module()
    started: asyncio.Event
    stopped = False

    class Process:
        returncode = None

        async def communicate(self, input: bytes) -> tuple[bytes, bytes]:
            started.set()
            await asyncio.Event().wait()
            return b"", b""

        def terminate(self) -> None:
            nonlocal stopped
            stopped = True

        async def wait(self) -> int:
            return 0

    async def spawn(*args: str, **kwargs: object) -> Process:
        return Process()

    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", spawn)

    async def exercise() -> None:
        nonlocal started
        started = asyncio.Event()
        task = asyncio.create_task(module.RestrictedCatalogWriter({})("openai", ("gpt-5.5",)))
        await started.wait()
        task.cancel("operator-stopped")
        with pytest.raises(asyncio.CancelledError, match="operator-stopped"):
            await task
        assert stopped

    asyncio.run(exercise())


def test_once_operator_success_does_not_wait_and_drops_provider_secrets_from_psql_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = sync_module()

    async def write(provider: str, models: tuple[str, ...]) -> None:
        pass

    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"data": []}))
        ) as client:
            assert (
                await module.operator_loop(
                    client, write, {"openai": "secret"}, once=True, report=lambda message: None
                )
                == 0
            )

    asyncio.run(exercise())
    env = module.database_environment(
        "postgresql://trellis_catalog_sync:secret@db.example.test/postgres",
        {
            "TRELLIS_OPENAI_API_KEY": "provider-secret",
            "TRELLIS_CATALOG_DATABASE_URL": "connection-secret",
        },
    )
    assert not any(key.startswith("TRELLIS_") for key in env)


@pytest.mark.parametrize("provider,models", [("other", ()), ("openai", ("bad'",))])
def test_sync_sql_accepts_only_known_providers_and_valid_identifiers(
    provider: str, models: tuple[str, ...]
) -> None:
    module = sync_module()
    with pytest.raises(ValueError):
        module.sync_statement(provider, models)


def test_discovery_page_limit_rejects_incomplete_result(monkeypatch: pytest.MonkeyPatch) -> None:
    module = sync_module()
    monkeypatch.setattr(module, "MAX_PAGES", 1)

    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={
                        "data": [{"id": "claude-sonnet-5"}],
                        "has_more": True,
                        "last_id": "claude-sonnet-5",
                    },
                )
            )
        ) as client:
            with pytest.raises(module.CatalogDiscoveryError, match="catalog_page_limit"):
                await module.discover_models(client, "anthropic", "secret")

    asyncio.run(exercise())
