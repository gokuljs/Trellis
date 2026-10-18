#!/usr/bin/env python3
"""Check durable acceptance with independent PostgreSQL client processes."""

import json
import os
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor

PSQL = ["psql", "-X", "-qAt", "-v", "ON_ERROR_STOP=1"]
ENVIRONMENT = {**os.environ, "PGCONNECT_TIMEOUT": "5"}
USER = str(uuid.uuid4())


def command(sql: str, *, required: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        PSQL,
        input=sql,
        text=True,
        capture_output=True,
        env=ENVIRONMENT,
        timeout=20,
        check=False,
    )
    if required and result.returncode:
        raise AssertionError(result.stderr)
    return result


def literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def owned(sql: str) -> str:
    return (
        "BEGIN; SET LOCAL ROLE authenticated; SET LOCAL statement_timeout='12s';\n"
        f"SET LOCAL request.jwt.claim.sub={literal(USER)};\n" + sql + "\nCOMMIT;"
    )


def acceptance(chat: str, request: str) -> str:
    payload = {
        "session_id": chat,
        "client_request_id": request,
        "content": "Hello",
        "model_id": "openai:gpt-5.5",
        "budget_preset": "conservative",
        "submission": {"content": "Hello", "turnId": request},
    }
    return owned(f"SELECT trellis.accept_run({literal(json.dumps(payload))}::jsonb)->>'id';")


try:
    command(f"INSERT INTO auth.users(id,email) VALUES ({literal(USER)},'durable-race@example.test');")
    initial = command(
        owned("SELECT trellis.ensure_account(); SELECT trellis.mutate('create_session','{}')->>'id';")
    ).stdout.splitlines()
    chat = initial[-1]
    with ThreadPoolExecutor(max_workers=8) as pool:
        duplicates = list(pool.map(lambda _: command(acceptance(chat, "same-request")), range(8)))
    run_ids = {item.stdout.strip() for item in duplicates}
    assert len(run_ids) == 1, run_ids
    run = next(iter(run_ids))
    records = json.loads(
        command(
            "SELECT jsonb_build_array("
            f"(SELECT count(*) FROM trellis.runs WHERE user_id={literal(USER)}),"
            f"(SELECT count(*) FROM trellis.messages WHERE user_id={literal(USER)}),"
            f"(SELECT count(*) FROM trellis_private.jobs WHERE user_id={literal(USER)} AND status='ready'),"
            f"(SELECT count(*) FROM trellis.run_events WHERE run_id={literal(run)}));"
        ).stdout
    )
    assert records == [1, 1, 1, 1], records
    print("PASS: eight independent concurrent clients received one saved ready run.")

    chats = [
        command(owned("SELECT trellis.mutate('create_session','{}')->>'id';")).stdout.strip()
        for _ in range(16)
    ]
    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(
            pool.map(
                lambda entry: command(acceptance(entry[1], f"capacity-{entry[0]}"), required=False),
                enumerate(chats),
            )
        )
    assert sum(item.returncode == 0 for item in results) == 15
    failures = [item.stderr for item in results if item.returncode]
    assert len(failures) == 1 and "account run capacity reached" in failures[0], failures
    records = json.loads(
        command(
            "SELECT jsonb_build_array("
            f"(SELECT count(*) FROM trellis.runs WHERE user_id={literal(USER)}),"
            f"(SELECT count(*) FROM trellis.messages WHERE user_id={literal(USER)}),"
            f"(SELECT count(*) FROM trellis_private.jobs WHERE user_id={literal(USER)}),"
            f"(SELECT count(*) FROM trellis.run_events WHERE user_id={literal(USER)}));"
        ).stdout
    )
    assert records == [16, 16, 16, 16], records
    retry = command(acceptance(chat, "same-request")).stdout.strip()
    assert retry == run
    print("PASS: concurrent chats honored the account limit without partial acceptance.")
finally:
    command(f"DELETE FROM auth.users WHERE id={literal(USER)};")
