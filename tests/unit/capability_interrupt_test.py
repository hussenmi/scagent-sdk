"""A running scientific capability must be stoppable, and stopping it must be recorded."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from pathlib import Path

import pytest

from scagent_sdk.capabilities.executor import CapabilityExecutor
from scagent_sdk.capabilities.registry import CapabilityRegistry
from scagent_sdk.execution import (
    EnvironmentBroker,
    EnvironmentProfile,
    EnvironmentRegistry,
    ExecutionRuntime,
)
from scagent_sdk.session import AnalysisSession

_SLOW_SKILL = """\
schema_version: 1
skill: {id: slow-compute, version: "1", description: test}
tools:
  - name: run_slow
    description: sleep like a long compute
    entrypoint: scripts/run.py:run
    environment: test-science
    input_schema: {type: object}
"""


def _slow_package(tmp_path: Path, *, marker: Path):
    skill = tmp_path / "skills" / "slow-compute"
    (skill / "scripts").mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: slow-compute\ndescription: a slow test capability\n---\n", encoding="utf-8"
    )
    (skill / "capability.yaml").write_text(_SLOW_SKILL, encoding="utf-8")
    (skill / "scripts" / "run.py").write_text(
        "import pathlib, time\n"
        "\n"
        "def run(arguments, context):\n"
        f"    pathlib.Path({str(marker)!r}).write_text(str(__import__('os').getpid()))\n"
        "    time.sleep(60)\n"
        "    return {'summary': 'finished', 'facts_patch': {'slow': True}}\n",
        encoding="utf-8",
    )
    return CapabilityRegistry(skill.parent).discover()[0]


def _broker() -> EnvironmentBroker:
    runtime = ExecutionRuntime(name="test-runtime", python=Path(sys.executable))
    profile = EnvironmentProfile(name="test-science", runtime=runtime)
    return EnvironmentBroker(EnvironmentRegistry({"test-science": profile}))


def _wait_for(path: Path, timeout: float = 30.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file() and path.read_text().strip():
            return int(path.read_text().strip())
        time.sleep(0.05)
    raise AssertionError(f"worker never started: {path}")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def test_running_compute_does_not_block_the_event_loop_and_can_be_stopped(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "worker.pid"
    package = _slow_package(tmp_path, marker=marker)
    broker = _broker()
    session = AnalysisSession.create(tmp_path / "sessions", title="stoppable")
    executor = CapabilityExecutor(session, environment_broker=broker)

    async def scenario() -> dict:
        call = asyncio.ensure_future(executor.execute(package, package.manifest.tools[0], {}))
        pid = await asyncio.to_thread(_wait_for, marker)
        # The loop is still servicing callbacks while the worker runs: without this the
        # interrupt could never be delivered in the first place.
        ticks = 0
        while ticks < 3:
            await asyncio.sleep(0.01)
            ticks += 1
        assert broker.running_executions(), "the worker should be registered as running"
        assert broker.cancel_all(), "cancelling must report the stopped execution"
        response = await asyncio.wait_for(call, timeout=30)
        await asyncio.to_thread(_wait_for_exit, pid)
        return response

    response = asyncio.run(scenario())

    assert response["is_error"] is True
    assert "stopped" in response["error_summary"]
    assert session.store.state.facts == {}, "an unfinished capability commits nothing"
    kinds = [event.kind for event in session.store.events()]
    assert kinds[-1] == "capability.execution_interrupted"
    assert session.store.events()[-1].payload["forced"] is False


def _wait_for_exit(pid: int, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return
        time.sleep(0.05)
    os.kill(pid, signal.SIGKILL)
    raise AssertionError(f"worker {pid} outlived its cancellation")


def test_forced_cancellation_stops_the_worker_and_records_it(tmp_path: Path) -> None:
    marker = tmp_path / "worker.pid"
    package = _slow_package(tmp_path, marker=marker)
    broker = _broker()
    session = AnalysisSession.create(tmp_path / "sessions", title="forced")
    executor = CapabilityExecutor(session, environment_broker=broker)

    async def scenario() -> int:
        call = asyncio.ensure_future(executor.execute(package, package.manifest.tools[0], {}))
        pid = await asyncio.to_thread(_wait_for, marker)
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        return pid

    pid = asyncio.run(scenario())

    _wait_for_exit(pid)
    assert session.store.state.facts == {}
    kinds = [event.kind for event in session.store.events()]
    assert kinds[-1] == "capability.execution_interrupted"
    assert session.store.events()[-1].payload["forced"] is True
