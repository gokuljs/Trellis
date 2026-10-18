"""Disposable PostgreSQL transport and deterministic providers for process/browser tests."""

import asyncio
import json
import os
import secrets
import sys
from collections.abc import AsyncGenerator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import psycopg
from fastapi import FastAPI, HTTPException
from psycopg import sql
from psycopg.types.json import Jsonb
from pydantic import SecretStr

from app.api.auth import VerifiedUser
from app.api.routes import runtime
from app.application.errors import ApplicationError
from app.core.config import Settings
from app.domain.models import Message, ProviderName
from app.domain.runtime import ModelRequest, ModelStreamEvent
from app.infrastructure.supabase_repository import SupabaseRepository, VerifiedTokenSource
from app.infrastructure.worker_repository import PostgresWorkerQueue
from app.main import create_app
from app.worker import run_worker

ALICE = UUID("a59673c1-78d0-4bc8-8c49-6bc2e7a01dd5")
BOB = UUID("d7a9cd14-6abd-42c4-b46d-2f5bb82e7231")


class HarnessVerifier:
    async def verify_bearer(self, authorization: str | None) -> VerifiedUser:
        return await self.verify_token((authorization or "").removeprefix("Bearer "))

    async def verify_token(self, token: str) -> VerifiedUser:
        name, _, raw_expiry = token.partition(":")
        if name not in {"alice", "bob"}:
            raise HTTPException(status_code=401)
        expires = (
            datetime.fromtimestamp(float(raw_expiry), UTC)
            if raw_expiry
            else (datetime.now(UTC) + timedelta(hours=1))
        )
        if expires <= datetime.now(UTC) + timedelta(seconds=5):
            raise HTTPException(status_code=401)
        return VerifiedUser(ALICE if name == "alice" else BOB, f"{name}@example.test", expires)


class DeterministicProvider:
    name: ProviderName = "openai"
    model = "gpt-5.5"

    async def complete(self, messages: Sequence[Message], api_key: str, user_id: str) -> str:
        raise AssertionError("The durable test provider only supports streaming")

    async def stream(
        self, request: ModelRequest, api_key: str, user_id: str
    ) -> AsyncGenerator[ModelStreamEvent]:
        assert api_key == "deterministic-provider-key"
        assert user_id in {str(ALICE), str(BOB)}
        assert request.tools == ()
        await asyncio.sleep(float(os.environ.get("TRELLIS_TEST_PROVIDER_DELAY", "5")))
        yield ModelStreamEvent(kind="text_delta", text="Durable worker ")
        await asyncio.sleep(float(os.environ.get("TRELLIS_TEST_PROVIDER_CHUNK_DELAY", "0")))
        yield ModelStreamEvent(kind="text_delta", text="answer.")
        yield ModelStreamEvent(kind="usage", input_tokens=12, output_tokens=5)
        yield ModelStreamEvent(kind="completed", finish_reason="stop", finish_kind="final")


def harness_settings() -> Settings:
    return Settings(
        _env_file=None,
        environment="development",
        supabase_url="http://127.0.0.1:54321",
        supabase_publishable_key="sb_publishable_disposable",
        openai_api_key=SecretStr("deterministic-provider-key"),
        durable_execution_enabled=True,
        durable_acceptance_enabled=True,
        worker_database_url=SecretStr(os.environ["TRELLIS_WORKER_DATABASE_URL"]),
        worker_capacity=1,
    )


class PostgresUserRepository(SupabaseRepository):
    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        payload: dict[str, object] | None = None,
    ) -> object:
        self._token_source.get_token()
        query_params = params or {}
        try:
            async with await psycopg.AsyncConnection.connect(
                os.environ["TRELLIS_TEST_DATABASE_URL"], connect_timeout=3
            ) as connection:
                await connection.execute("SET LOCAL ROLE authenticated")
                await connection.execute(
                    "SELECT set_config('request.jwt.claim.sub',%s,true)", (str(self._user_id),)
                )
                await connection.execute("SET LOCAL statement_timeout='4s'")
                values: list[object] = []
                if method == "POST" and path.startswith("rpc/"):
                    function = path.removeprefix("rpc/")
                    arguments = []
                    for name, value in (payload or {}).items():
                        arguments.append(sql.SQL("{} => %s").format(sql.Identifier(name)))
                        values.append(Jsonb(value) if isinstance(value, dict | list) else value)
                    query = sql.SQL("SELECT to_jsonb(result) FROM trellis.{}({}) AS result").format(
                        sql.Identifier(function), sql.SQL(",").join(arguments)
                    )
                    many = function == "list_chat_summaries"
                elif method == "GET":
                    selected = query_params.get("select", "*")
                    columns = (
                        sql.SQL("*")
                        if selected == "*"
                        else sql.SQL(",").join(
                            sql.Identifier(column) for column in selected.split(",")
                        )
                    )
                    query = sql.SQL(
                        "SELECT to_jsonb(result) FROM (SELECT {} FROM trellis.{}"
                    ).format(columns, sql.Identifier(path))
                    clauses = []
                    for name, value in query_params.items():
                        if name in {"select", "order", "limit", "offset"}:
                            continue
                        operator, _, operand = value.partition(".")
                        comparator = {"eq": sql.SQL("="), "gt": sql.SQL(">")}[operator]
                        clauses.append(sql.SQL("{} {} %s").format(sql.Identifier(name), comparator))
                        values.append(operand)
                    if clauses:
                        query += sql.SQL(" WHERE ") + sql.SQL(" AND ").join(clauses)
                    many = True
                else:
                    raise AssertionError("Unsupported test transport operation")
                if many:
                    if order := query_params.get("order"):
                        query += sql.SQL(" ORDER BY ") + sql.SQL(",").join(
                            sql.SQL("{} {}").format(
                                sql.Identifier(item.split(".")[0]),
                                sql.SQL("DESC" if item.endswith(".desc") else "ASC"),
                            )
                            for item in order.split(",")
                        )
                    for name in ("limit", "offset"):
                        if name in query_params:
                            query += sql.SQL(" {} %s").format(sql.SQL(name.upper()))
                            values.append(int(query_params[name]))
                if method == "GET":
                    query += sql.SQL(") AS result")
                cursor = await connection.execute(query, values)
                rows = await cursor.fetchall()
                return [row[0] for row in rows] if many else rows[0][0] if rows else None
        except psycopg.errors.InvalidParameterValue as error:
            raise ValueError(error.diag.message_primary) from None
        except psycopg.errors.InsufficientPrivilege:
            raise ApplicationError("cloud_access_denied", "Access denied.") from None


def _repository_factory(
    settings: Settings, user_id: UUID, source: VerifiedTokenSource, client: httpx.AsyncClient
) -> PostgresUserRepository:
    return PostgresUserRepository(settings, user_id, source, client)


def create_api() -> FastAPI:
    runtime._WEB_ORIGINS |= {"http://127.0.0.1:3039"}
    return create_app(
        harness_settings(),
        auth_verifier=HarnessVerifier(),
        repository_factory=_repository_factory,
        streaming_provider_adapters={"openai": DeterministicProvider()},
    )


def _migration_sources() -> list[str]:
    repository_root = Path(__file__).resolve().parents[2]
    files = [
        repository_root / "supabase/tests/local_bootstrap.sql",
        *sorted((repository_root / "supabase/migrations").glob("*.sql")),
    ]
    return [path.read_text() for path in files]


async def bootstrap() -> None:
    sources = await asyncio.to_thread(_migration_sources)
    async with await psycopg.AsyncConnection.connect(
        os.environ["TRELLIS_TEST_DATABASE_URL"], autocommit=True
    ) as db:
        row = await (await db.execute("SELECT to_regnamespace('trellis')")).fetchone()
        if row is None or row[0] is not None:
            raise ValueError("Process proof bootstrap requires an empty disposable database")
        for source in sources:
            await db.execute(source.encode())


async def seed() -> None:
    async with await psycopg.AsyncConnection.connect(os.environ["TRELLIS_TEST_DATABASE_URL"]) as db:
        for name, user_id in (("alice", ALICE), ("bob", BOB)):
            await db.execute(
                "INSERT INTO auth.users(id,email) VALUES(%s,%s) ON CONFLICT(id) DO NOTHING",
                (user_id, f"{name}@example.test"),
            )
            await db.execute("SET LOCAL ROLE authenticated")
            await db.execute("SELECT set_config('request.jwt.claim.sub',%s,true)", (str(user_id),))
            await db.execute("SELECT trellis.ensure_account()")
            await db.execute("RESET ROLE")
            await db.execute(
                "UPDATE trellis.onboarding_progress SET current_step='complete',"
                "completed_at=now() WHERE user_id=%s",
                (user_id,),
            )


async def worker() -> None:
    await run_worker(harness_settings(), providers={"openai": DeterministicProvider()})


async def hold_claim(*, begin: bool) -> None:
    queue = await PostgresWorkerQueue.open(os.environ["TRELLIS_WORKER_DATABASE_URL"])
    try:
        claim = await queue.claim(str(uuid4()), str(uuid4()), secrets.token_urlsafe(32))
        if claim is None:
            raise AssertionError("Expected an eligible job")
        if begin:
            await queue.begin(claim)
        print(json.dumps({"run_id": claim.run_id, "attempt_id": claim.attempt_id}), flush=True)
        await asyncio.Event().wait()
    finally:
        await queue.close()


if __name__ == "__main__":
    mode = sys.argv[1]
    if mode in {"hold-claim", "hold-started"}:
        asyncio.run(hold_claim(begin=mode == "hold-started"))
    else:
        asyncio.run({"bootstrap": bootstrap, "seed": seed, "worker": worker}[mode]())
