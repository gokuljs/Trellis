import json
from pathlib import Path

from app.domain.jobs import JobStatus


def test_queue_states_and_public_fixtures() -> None:
    assert tuple(JobStatus) == ("ready", "claimed", "running", "finished")
    fixtures = Path(__file__).parent / "fixtures" / "durable"
    lookup = json.loads((fixtures / "run-lookup.json").read_text())
    queued = json.loads((fixtures / "run-queued-event.json").read_text())
    assert lookup["request"]["method"] == "run.lookup"
    assert lookup["found"]["result"]["limits"]["deadlineAt"] is None
    assert lookup["missing"]["result"] is None
    assert queued["params"]["eventType"] == "run.queued"
    assert queued["params"]["runId"] == lookup["found"]["result"]["runId"]
