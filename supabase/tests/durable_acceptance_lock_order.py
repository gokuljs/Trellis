#!/usr/bin/env python3
"""Check acceptance against a concurrent completion on real PostgreSQL."""

import json
import os
import select
import subprocess
import time
import uuid

PSQL = ["psql", "-X", "-qAt", "-v", "ON_ERROR_STOP=1", "-v", "VERBOSITY=verbose"]
ENVIRONMENT = {**os.environ, "PGCONNECT_TIMEOUT": "5"}
USER = str(uuid.uuid4())
CAPABILITY = "AcceptanceCompletionLockOrderCapabilityWithEnoughCharacters123"
APPLICATION = "trellis-admission-lock-" + uuid.uuid4().hex[:12]


def command(sql: str) -> str:
    result = subprocess.run(
        PSQL,
        input=sql,
        text=True,
        capture_output=True,
        env=ENVIRONMENT,
        timeout=15,
        check=False,
    )
    if result.returncode:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


def authenticated(sql: str) -> str:
    return f"SET LOCAL ROLE authenticated;\nSET LOCAL request.jwt.claim.sub='{USER}';\n" + sql


def start(application: str) -> subprocess.Popen:
    return subprocess.Popen(
        PSQL,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**ENVIRONMENT, "PGAPPNAME": application},
        bufsize=0,
    )


def send(process: subprocess.Popen, sql: str) -> None:
    assert process.stdin is not None
    process.stdin.write(sql.encode())
    process.stdin.flush()


def wait_for_marker(process: subprocess.Popen, marker: str) -> str:
    output = ""
    deadline = time.monotonic() + 5
    assert process.stdout is not None
    while marker not in output:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([process.stdout], [], [], remaining)[0]:
            raise AssertionError("completion session did not acquire its chat lock")
        chunk = os.read(process.stdout.fileno(), 65536)
        if not chunk:
            assert process.stderr is not None
            raise AssertionError(process.stderr.read().decode())
        output += chunk.decode()
    return output


def wait_for_admission(completion_pid: int, admission: subprocess.Popen) -> None:
    deadline = time.monotonic() + 2
    while (
        command(
            "SELECT COALESCE(bool_or("
            f"{completion_pid}=ANY(pg_blocking_pids(pid))),false) FROM pg_stat_activity "
            f"WHERE application_name='{APPLICATION}-admission';"
        )
        != "t"
    ):
        if admission.poll() is not None or time.monotonic() >= deadline:
            raise AssertionError("admission did not wait on the completion session's chat lock")


completion = admission = None
try:
    fixture = json.loads(
        command(
            f"INSERT INTO auth.users(id,email) VALUES ('{USER}','admission-lock@example.test');\n"
            "BEGIN;\n"
            + authenticated(
                "SELECT trellis.ensure_account();\n"
                "DO $$\nDECLARE chat_id uuid; run jsonb;\nBEGIN\n"
                "chat_id := (trellis.mutate('create_session','{}')->>'id')::uuid;\n"
                "run := trellis.mutate('create_run',jsonb_build_object('session_id',chat_id,"
                "'turn_id','completing','client_request_id','completing','content','Hello',"
                f"'model_id','openai:gpt-5.5'),'{CAPABILITY}')->'run';\n"
                "PERFORM trellis.mutate('transition_run_record',jsonb_build_object("
                "'run_id',run->>'id','next_status','running','event_type','run.started'),"
                f"'{CAPABILITY}');\n"
                "PERFORM set_config('trellis.test_chat_id',chat_id::text,true);\n"
                "PERFORM set_config('trellis.test_run_id',run->>'id',true);\n"
                "END; $$;\n"
                "SELECT jsonb_build_object('chat_id',current_setting('trellis.test_chat_id'),"
                "'run_id',current_setting('trellis.test_run_id'));\n"
            )
            + "COMMIT;\n"
        ).splitlines()[-1]
    )
    chat_id, run_id = fixture["chat_id"], fixture["run_id"]

    # Pause at complete_run's run/chat lock boundary before its message insert.
    completion = start(APPLICATION + "-completion")
    send(
        completion,
        "BEGIN; SET LOCAL statement_timeout='10s'; "
        "SET LOCAL idle_in_transaction_session_timeout='10s';\n"
        + authenticated(
            "SELECT trellis.mutate('renew_run_lease',"
            f"jsonb_build_object('run_id','{run_id}'),'{CAPABILITY}');\n"
        )
        + "RESET ROLE;\n"
        + f"SELECT id FROM trellis.chats WHERE id='{chat_id}' AND user_id='{USER}' FOR UPDATE;\n"
        + authenticated("SELECT 'CHAT_LOCKED:'||pg_backend_pid();\n"),
    )
    output = wait_for_marker(completion, "CHAT_LOCKED:")
    completion_pid = int(output.split("CHAT_LOCKED:")[1].splitlines()[0])

    admission = start(APPLICATION + "-admission")
    payload = {
        "session_id": chat_id,
        "client_request_id": "next-request",
        "content": "Next question",
        "model_id": "openai:gpt-5.5",
        "budget_preset": "conservative",
        "submission": {"content": "Next question", "turnId": "next-request"},
    }
    send(
        admission,
        "BEGIN; SET LOCAL statement_timeout='10s';\n"
        + authenticated(f"SELECT trellis.accept_run('{json.dumps(payload)}'::jsonb)->>'id';\n")
        + "COMMIT;\n",
    )
    wait_for_admission(completion_pid, admission)
    print("Confirmed admission waits on the chat lock after acquiring its account lock.")

    _, completion_error = completion.communicate(
        (
            "SELECT trellis.mutate('complete_run',jsonb_build_object("
            f"'run_id','{run_id}','content','Saved answer'),'{CAPABILITY}');\nCOMMIT;\n"
        ).encode(),
        timeout=12,
    )
    admission_output, admission_error = admission.communicate(timeout=12)
    errors = completion_error.decode() + admission_error.decode()
    if "40P01" in errors:
        raise AssertionError("run completion deadlocked with durable acceptance:\n" + errors)
    if completion.returncode or admission.returncode:
        raise AssertionError("completion or admission failed:\n" + errors)
    accepted_run = admission_output.decode().strip()
    saved = json.loads(
        command(
            "SELECT jsonb_build_array("
            f"(SELECT status FROM trellis.runs WHERE id='{run_id}'),"
            f"(SELECT content FROM trellis.messages WHERE run_id='{run_id}' AND role='assistant'),"
            f"(SELECT status FROM trellis.runs WHERE id='{accepted_run}'),"
            f"(SELECT status FROM trellis_private.jobs WHERE run_id='{accepted_run}'),"
            f"(SELECT count(*) FROM trellis.messages WHERE run_id='{accepted_run}'),"
            f"(SELECT count(*) FROM trellis.run_events WHERE run_id='{accepted_run}' "
            "AND event_type='run.queued'));"
        )
    )
    assert saved == ["completed", "Saved answer", "queued", "ready", 1, 1], saved
    print("PASS: completion saved its answer and concurrent admission saved one ready job.")
finally:
    for process in (completion, admission):
        if process is not None and process.poll() is None:
            process.terminate()
            process.communicate(timeout=5)
    command(f"DELETE FROM auth.users WHERE id='{USER}';")
