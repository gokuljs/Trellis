import json
import os
import unittest
from uuid import uuid4

from app.domain.web import WebUsage
from tests import test_research_postgres as database_tests


@unittest.skipUnless(
    os.environ.get("TRELLIS_TEST_POSTGRES_PORT"), "isolated PostgreSQL is required"
)
class ResearchCrawlPostgresTests(unittest.TestCase):
    def finalize_crawl(self, budget_preset: str, previous_usage: WebUsage) -> None:
        db = database_tests.ResearchPostgresTests()
        db.budget_preset = budget_preset
        db.setUp()
        self.addCleanup(db.tearDown)
        tool = db.completed_task(tool_call=True, tool_name="web_crawl")["tool_calls"][0]
        dispatch_id = str(uuid4())
        db.rpc(
            "begin_tool_execution",
            {
                "run_id": db.run_id,
                "tool_call_id": tool["id"],
                "dispatch_id": dispatch_id,
                "expected_attempt": 0,
                "recovery_policy": "checkpointed",
                "input_snapshot": None,
            },
            research=False,
        )
        scope = {
            "user_id": db.owner,
            "session_id": db.session_id,
            "run_id": db.run_id,
            "tool_call_id": tool["id"],
            "dispatch_id": dispatch_id,
            "dispatch_attempt": 1,
            "generation": 0,
        }

        def web(action: str, payload: dict) -> dict:
            result = db.sql(
                f"SELECT trellis.web_mutate({database_tests.literal(action)},"
                + database_tests.literal(json.dumps(payload))
                + "::jsonb,"
                + database_tests.literal(db.capability)
                + ");"
            )
            return json.loads(result.stdout)

        scope["evidence_epoch"] = web("open_run", scope)["evidence_epoch"]
        db.sql(
            "UPDATE trellis.web_runs SET usage="
            + database_tests.literal(json.dumps(previous_usage.to_dict()))
            + f"::jsonb WHERE run_id={database_tests.literal(db.run_id)};",
            authenticated=False,
        )
        crawl_id = str(uuid4())
        url = "https://example.com/pricing"
        web(
            "create_crawl",
            {
                **scope,
                "crawl_id": crawl_id,
                "seed_url": url,
                "origin": "https://example.com",
                "path_prefix": "/",
                "max_pages": 1,
                "max_depth": 0,
            },
        )
        web("enqueue_pages", {**scope, "crawl_id": crawl_id, "pages": [{"url": url, "depth": 0}]})
        page = web("claim_page", {**scope, "crawl_id": crawl_id})
        reservation_id = str(uuid4())
        text = "The standard plan costs $10."
        usage = WebUsage(
            page_attempts=1,
            network_requests=1,
            response_bytes=len(text.encode()),
            extracted_bytes=len(text.encode()),
        ).to_dict()
        web(
            "reserve",
            {
                **scope,
                "reservation_id": reservation_id,
                "operation_key": "crawl-page",
                "usage": usage,
            },
        )
        source_id = str(uuid4())
        finalized = web(
            "finalize_crawl_page",
            {
                **scope,
                "crawl_id": crawl_id,
                "url": url,
                "page_attempt": page["attempt"],
                "reservation_id": reservation_id,
                "source": {
                    "id": source_id,
                    "requested_url": url,
                    "final_url": url,
                    "fetched_at": "2026-10-31T00:00:00Z",
                    "extractor_version": "test-v1",
                    "text": text,
                    "status": "success",
                },
                "usage": usage,
                "discovered_links": [],
            },
        )
        self.assertEqual(finalized["status"], "completed")
        self.assertEqual(finalized["source_id"], source_id)
        state = json.loads(
            db.sql(
                "SELECT jsonb_build_object('usage',w.usage,'reservation',r.status) "
                "FROM trellis.web_runs w JOIN trellis.web_reservations r ON r.run_id=w.run_id "
                f"WHERE w.run_id={database_tests.literal(db.run_id)};"
            ).stdout
        )
        self.assertEqual(state["reservation"], "settled")
        self.assertEqual(state["usage"]["search_calls"], previous_usage.search_calls)
        self.assertEqual(state["usage"]["page_attempts"], previous_usage.page_attempts + 1)

    def test_standard_research_finalizes_crawl_after_six_searches(self) -> None:
        self.finalize_crawl("conservative", WebUsage(search_calls=6))

    def test_extended_research_finalizes_twenty_first_page(self) -> None:
        self.finalize_crawl("longer", WebUsage(page_attempts=20))


if __name__ == "__main__":
    unittest.main()
