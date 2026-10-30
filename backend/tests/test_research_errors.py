import unittest
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx

from app.application.errors import ApplicationError
from app.core.config import Settings
from app.infrastructure.supabase_repository import SupabaseRepository, VerifiedTokenSource


class ResearchErrorTests(unittest.IsolatedAsyncioTestCase):
    async def test_database_research_errors_keep_their_typed_boundary(self) -> None:
        owner = uuid4()
        tokens = VerifiedTokenSource(owner)
        tokens.record_verified("verified-test-token", owner, datetime.now(UTC) + timedelta(hours=1))
        settings = Settings(
            environment="development",
            supabase_url="http://127.0.0.1:6543",
            supabase_publishable_key="sb_publishable_test",
        )
        for code in (
            "research_finalization_reserve",
            "research_usage_unknown",
            "research_source_changed",
        ):
            with self.subTest(code=code):
                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(
                        lambda request, error_code=code: httpx.Response(
                            400, json={"code": "22023", "message": error_code}
                        )
                    )
                ) as client:
                    repository = SupabaseRepository(settings, owner, tokens, client)
                    with self.assertRaises(ApplicationError) as raised:
                        await repository.read_research(str(uuid4()))
                    self.assertEqual(raised.exception.code, code)
                    self.assertNotIn("verified-test-token", raised.exception.message)


if __name__ == "__main__":
    unittest.main()
