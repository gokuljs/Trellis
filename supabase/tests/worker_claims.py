"""Real-login PostgreSQL authority checks. Run against a disposable migrated database."""

from __future__ import annotations

import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

PSQL = os.environ.get("PSQL", "/Library/PostgreSQL/15/bin/psql")
DATABASE = os.environ.get("PGDATABASE", "trellis_authority")


def query(sql: str, *, worker: bool = False, fails: bool = False) -> str:
    result = subprocess.run(
        [PSQL, "-XAt", "-v", "ON_ERROR_STOP=1", "-h", "127.0.0.1", "-p", "56379",
         "-U", "trellis_runtime_worker" if worker else "trellis_test_admin",
         "-d", DATABASE, "-c", sql],
        capture_output=True, text=True, timeout=10,
    )
    assert (result.returncode != 0) == fails, result.stderr
    return result.stdout.strip()


def literal(value: object) -> str:
    return "'" + json.dumps(value).replace("'", "''") + "'::jsonb"


def accept() -> str:
    owner = str(uuid4())
    query(f"INSERT INTO auth.users(id) VALUES('{owner}');")
    result = query(
        f"SET ROLE authenticated; SELECT set_config('request.jwt.claim.sub','{owner}',false);"
        "SELECT trellis.ensure_account(); SELECT trellis.mutate('create_session','{}'::jsonb);"
    )
    chat = json.loads(result.splitlines()[-1])["id"]
    payload = {"session_id": chat, "client_request_id": str(uuid4()), "content": "hello",
               "model_id": "openai:gpt-5.5", "budget_preset": "conservative",
               "submission": {"content": "hello"}}
    result = query(
        f"SET ROLE authenticated; SELECT set_config('request.jwt.claim.sub','{owner}',false);"
        f"SELECT trellis.accept_run({literal(payload)});"
    )
    return json.loads(result.splitlines()[-1])["id"]


def claim(worker_id: str = "consumer") -> dict[str, object] | None:
    attempt = str(uuid4())
    return json.loads(query(
        f"SELECT trellis_private.worker_claim('{worker_id}','{attempt}','{'x' * 40}');",
        worker=True,
    ) or "null")


def main() -> None:
    query("TRUNCATE trellis_private.jobs CASCADE;")
    run = accept()
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(claim, ["consumer-a", "consumer-b"]))
    assigned = [value for value in claims if value is not None]
    assert len(assigned) == 1, claims
    assignment = assigned[0]
    assert assignment["run_id"] == run
    repeated = json.loads(query(
        "SELECT trellis_private.worker_claim("
        f"'{assignment['worker_id']}','{assignment['attempt_id']}','{assignment['capability']}');",
        worker=True,
    ))
    assert repeated == assignment
    snapshot = json.loads(query(
        f"SELECT trellis_private.worker_begin({literal(assignment)});", worker=True,
    ))
    retry = json.loads(query(
        f"SELECT trellis_private.worker_begin({literal(assignment)});", worker=True,
    ))
    assert snapshot == retry
    assert snapshot["status"] == "running" and snapshot["deadline_at"]
    assert query(f"SELECT count(*) FROM trellis.run_events WHERE run_id='{run}' AND event_type='run.started';") == "1"
    altered = {**assignment, "worker_id": "intruder"}
    query(f"SELECT trellis_private.worker_begin({literal(altered)});", worker=True, fails=True)
    query(f"SELECT trellis_private.worker_begin({literal(assignment)});", fails=True)
    query("SELECT * FROM trellis.runs;", worker=True, fails=True)
    print("PASS real worker logins, concurrent claim, retry reconciliation, begin idempotence, and fencing")


if __name__ == "__main__":
    main()
