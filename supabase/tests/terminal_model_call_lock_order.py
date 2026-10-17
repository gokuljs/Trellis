#!/usr/bin/env python3
"""Prove terminal reconciliation never deadlocks with an in-flight usage RPC.

Run with PostgreSQL connection variables against a disposable database after
migration 010. Uses psql and Python's standard library only. Run once without
the run-first update wrapper for RED, then with it for GREEN.
"""

import json
import os
import select
import subprocess
import time
import uuid


USER = str(uuid.uuid4())
CAPABILITY = "TerminalModelCallLockOrderCapabilityWithEnoughCharacters123"
APPLICATION = "trellis-terminal-lock-" + uuid.uuid4().hex[:12]
PSQL = ["psql", "-X", "-qAt", "-v", "ON_ERROR_STOP=1", "-v", "VERBOSITY=verbose"]
ENVIRONMENT = {**os.environ, "PGCONNECT_TIMEOUT": "5"}


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
    return (
        "SET LOCAL ROLE authenticated;\n"
        f"SET LOCAL request.jwt.claim.sub='{USER}';\n" + sql
    )


def start(application: str) -> subprocess.Popen:
    return subprocess.Popen(
        PSQL,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**ENVIRONMENT, "PGAPPNAME": application},
        bufsize=0,
    )


def wait_for_marker(process: subprocess.Popen, marker: str) -> str:
    output = ""
    deadline = time.monotonic() + 10
    assert process.stdout is not None
    while marker not in output:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([process.stdout], [], [], remaining)[0]:
            raise AssertionError("terminal session did not acquire its run lock")
        chunk = os.read(process.stdout.fileno(), 65536)
        if not chunk:
            assert process.stderr is not None
            raise AssertionError(process.stderr.read().decode())
        output += chunk.decode()
    return output


terminal = writer = None
try:
    result = command(
        f"INSERT INTO auth.users(id,email) VALUES ('{USER}','terminal-lock@example.test');\n"
        "BEGIN;\n"
        + authenticated(
            "DO $$\nDECLARE session_id uuid; run_id uuid; call_id uuid; r jsonb;\nBEGIN\n"
            "PERFORM trellis.ensure_account();\n"
            "r:=trellis.mutate('create_session','{}'::jsonb); session_id:=(r->>'id')::uuid;\n"
            "r:=trellis.mutate('create_run',jsonb_build_object('session_id',session_id,"
            "'turn_id','lock-order','client_request_id','lock-order','content','Hello',"
            f"'model_id','openai:gpt-5.5'),'{CAPABILITY}');\n"
            "run_id:=(r->'run'->>'id')::uuid;\n"
            "PERFORM trellis.mutate('transition_run_record',jsonb_build_object('run_id',run_id,"
            f"'next_status','running','event_type','run.started'),'{CAPABILITY}');\n"
            "r:=trellis.mutate('create_model_call',jsonb_build_object('run_id',run_id,"
            f"'step_index',1,'model_id','openai:gpt-5.5','request_snapshot','{{}}'::jsonb),'{CAPABILITY}');\n"
            "call_id:=(r->>'id')::uuid;\n"
            "PERFORM trellis.mutate('update_model_call',jsonb_build_object('call_id',call_id,"
            "'next_status','streaming','input_tokens',20,'output_tokens',3,'estimated_cost',0.01),"
            f"'{CAPABILITY}');\n"
            "PERFORM set_config('trellis.test_run_id',run_id::text,true);\n"
            "PERFORM set_config('trellis.test_call_id',call_id::text,true);\n"
            "END; $$;\n"
            "SELECT jsonb_build_object('run_id',current_setting('trellis.test_run_id'),"
            "'call_id',current_setting('trellis.test_call_id'));\n"
        )
        + "COMMIT;\n"
    )
    fixture = json.loads(result)
    run_id, call_id = fixture["run_id"], fixture["call_id"]

    # A acquires exactly the run lock held by terminal RPCs. Keeping this valid
    # authenticated RPC in an open transaction makes the interleaving explicit.
    terminal = start(APPLICATION + "-terminal")
    assert terminal.stdin is not None
    terminal.stdin.write(
        (
            "BEGIN; SET LOCAL statement_timeout='8s';\n"
            + authenticated(
                "SELECT trellis.mutate('renew_run_lease',"
                f"jsonb_build_object('run_id','{run_id}'),'{CAPABILITY}');\n"
                "SELECT 'RUN_LOCKED:'||pg_backend_pid();\n"
            )
        ).encode()
    )
    terminal.stdin.flush()
    output = wait_for_marker(terminal, "RUN_LOCKED:")
    terminal_pid = int(output.split("RUN_LOCKED:")[1].splitlines()[0])

    # B runs the real call-update RPC and must block on A's run lock. Before the
    # fix it already owns the call lock; after the fix it owns neither row lock.
    writer = start(APPLICATION + "-writer")
    assert writer.stdin is not None
    writer.stdin.write(
        (
            "BEGIN; SET LOCAL statement_timeout='8s';\n"
            + authenticated(
                "SELECT trellis.mutate('update_model_call',"
                f"jsonb_build_object('call_id','{call_id}','next_status','streaming',"
                f"'input_tokens',21),'{CAPABILITY}');\nCOMMIT;\n"
            )
        ).encode()
    )
    writer.stdin.flush()
    deadline = time.monotonic() + 5
    while command(
        "SELECT COALESCE(bool_or("
        f"{terminal_pid}=ANY(pg_blocking_pids(pid))),false) FROM pg_stat_activity "
        f"WHERE application_name='{APPLICATION}-writer';"
    ) != "t":
        if writer.poll() is not None or time.monotonic() >= deadline:
            raise AssertionError("usage writer did not block on the terminal session's run lock")
        time.sleep(0.02)
    print("Confirmed usage RPC waits on the run lock held by the terminal session.")

    terminal_output, terminal_error = terminal.communicate(
        (
            "SELECT trellis.mutate('transition_run_record',"
            f"jsonb_build_object('run_id','{run_id}','next_status','cancelled',"
            f"'event_type','run.cancelled'),'{CAPABILITY}');\nCOMMIT;\n"
        ).encode(),
        timeout=12,
    )
    writer_output, writer_error = writer.communicate(timeout=12)
    errors = terminal_error.decode() + writer_error.decode()
    if "40P01" in errors:
        raise AssertionError("terminal reconciliation deadlocked with usage RPC:\n" + errors)
    if terminal.returncode:
        raise AssertionError("terminal RPC failed:\n" + terminal_error.decode())
    if writer.returncode == 0 or "42501" not in writer_error.decode():
        raise AssertionError("late usage RPC did not reject the revoked capability:\n" + errors)
    saved = json.loads(
        command(
            "SELECT jsonb_build_array(r.status,c.status,c.input_tokens,c.output_tokens,c.estimated_cost) "
            "FROM trellis.runs r JOIN trellis.model_calls c ON c.run_id=r.id AND c.user_id=r.user_id "
            f"WHERE r.id='{run_id}' AND c.id='{call_id}';"
        )
    )
    assert saved == ["cancelled", "cancelled", 20, 3, 0.01], saved
    print("PASS: terminal RPC committed; late usage was rejected; saved usage was preserved.")
finally:
    for process in (terminal, writer):
        if process is not None and process.poll() is None:
            process.terminate()
            process.communicate(timeout=5)
    command(f"DELETE FROM auth.users WHERE id='{USER}';")
