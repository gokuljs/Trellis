import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = """
import asyncio, json, os, sys
from dataclasses import asdict, replace
from pathlib import Path
from run_fixtures import MODEL
from test_compaction_recovery import FileLedger, Provider, context, intent_for, native_plan, resume
from app.application.execution_snapshots import request_snapshot
from app.domain.runtime import RunSnapshot, RunStatus

directory, mode = Path(sys.argv[1]), sys.argv[2]
ledger = FileLedger(directory / 'ledger.json')
run_file = directory / 'run.json'
if run_file.exists():
    row = json.loads(run_file.read_text())
    row['status'] = RunStatus(row['status'])
    ledger.run = RunSnapshot(**row)
else:
    ledger.run = replace(ledger.run, status=RunStatus.RUNNING)
    run_file.write_text(json.dumps(asdict(ledger.run)))

def invoked(purpose):
    with (directory / 'dispatches.txt').open('a') as target:
        target.write(purpose + '\\n')

class ProviderSpy(Provider):
    async def compact(self, *args):
        invoked('native')
        return await super().compact(*args)

    async def stream(self, *args):
        invoked('summary')
        async for event in super().stream(*args):
            yield event

async def execute():
    runtime, provider = context(ledger), ProviderSpy()
    if mode == 'recover':
        checkpoint = await resume(runtime, ledger, provider, ledger.calls[-1])
        (directory / 'result.json').write_text(json.dumps(asdict(checkpoint)))
        return
    intent = intent_for(ledger)
    if mode == 'native_saved':
        await runtime._compact(
            ledger.run, MODEL, provider, native_plan(intent), 'key', 'user', 1, intent=intent
        )
    elif mode == 'summary_saved':
        await runtime._summarize(
            ledger.run, MODEL, provider, intent.summary_plan, 'key', 'user', 1, intent=intent
        )
    else:
        await ledger.create_model_call(ledger.run.id, 1, MODEL, {
            **request_snapshot(intent.summary_plan.request), 'purpose': 'summary',
            'context': intent.summary_plan.snapshot(), 'compaction_intent': intent.snapshot(),
        })
    os._exit(73)

asyncio.run(execute())
"""


@pytest.mark.parametrize("boundary", ["native_saved", "summary_prepared", "summary_saved"])
def test_compaction_recovers_in_a_fresh_process_without_repeating_saved_calls(tmp_path, boundary):
    backend = Path(__file__).resolve().parents[1]
    environment = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join((str(backend), str(backend / "tests"))),
    }

    def child(mode, expected):
        result = subprocess.run(
            [sys.executable, "-c", SCRIPT, str(tmp_path), mode],
            cwd=backend,
            env=environment,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        assert result.returncode == expected, result.stdout + result.stderr

    child(boundary, 73)
    before = json.loads((tmp_path / "ledger.json").read_text())
    original = before["calls"][0]
    intent = original["request_snapshot"]["compaction_intent"]
    child("recover", 0)
    after = json.loads((tmp_path / "ledger.json").read_text())
    checkpoint = json.loads((tmp_path / "result.json").read_text())
    assert checkpoint["draft"]["id"] == intent["target_checkpoint_id"]
    assert after["calls"][0]["id"] == original["id"]
    assert after["calls"][0]["request_snapshot"] == original["request_snapshot"]
    assert (tmp_path / "dispatches.txt").read_text().splitlines() == (
        ["native", "summary"] if boundary == "native_saved" else ["summary"]
    )
    assert all(call["status"] == "completed" for call in after["calls"])
