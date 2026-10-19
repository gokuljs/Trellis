from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
from run_fixtures import MODEL

from app.application.budgets import BudgetPreset
from app.application.errors import ApplicationError
from app.core.config import Settings
from app.infrastructure.supabase_repository import SupabaseRepository, VerifiedTokenSource


@pytest.mark.parametrize("preset,required", [("conservative", 630), ("longer", 1230)])
def test_short_token_rejects_new_acceptance_without_expiring_sign_in(
    preset: BudgetPreset, required: int, monkeypatch
) -> None:
    async def exercise() -> None:
        user = UUID("19000000-0000-0000-0000-000000000001")
        source = VerifiedTokenSource(user)
        source.record_verified(
            "still-valid", user, datetime.now(UTC) + timedelta(seconds=required - 1)
        )
        async with httpx.AsyncClient() as client:
            repo = SupabaseRepository(
                Settings(
                    _env_file=None,
                    supabase_url="https://test.supabase.co",
                    supabase_publishable_key="sb_publishable_test",
                ),
                user,
                source,
                client,
            )
            rpc = AsyncMock(side_effect=AssertionError("Must reject before database acceptance"))
            monkeypatch.setattr(repo, "_rpc", rpc)
            with pytest.raises(ApplicationError) as failure:
                await repo.create_run(
                    "chat", "turn", "request", "hello", MODEL, budget_preset=preset
                )
            assert failure.value.code == "run_token_too_short"
            assert source.get_token() == "still-valid"
            rpc.assert_not_awaited()
            source.record_verified(
                "refreshed", user, datetime.now(UTC) + timedelta(seconds=required + 10)
            )
            assert source.valid_for(required)

    asyncio.run(exercise())
