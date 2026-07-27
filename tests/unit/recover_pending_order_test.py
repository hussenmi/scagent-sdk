"""Crash recovery must replay staged results in the order they were produced.

Staging directories are named with UUID4s, so sorting by name replays recovery in an arbitrary
order. Commits apply state patches in sequence, so recovering two executions backwards leaves the
earlier patch overwriting the later one.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from scagent_sdk.capabilities.executor import CapabilityExecutor
from scagent_sdk.capabilities.registry import CapabilityRegistry
from scagent_sdk.session import AnalysisSession


def _counter_skill(root: Path) -> Path:
    skill = root / "skills" / "counter"
    (skill / "scripts").mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: counter\ndescription: test skill\n---\n\nCounter.\n", encoding="utf-8"
    )
    (skill / "capability.yaml").write_text(
        """\
schema_version: 1
skill: {id: counter, version: "1", description: test}
tools:
  - name: bump
    description: record a step value
    entrypoint: scripts/run.py:run
    input_schema:
      type: object
      properties:
        step: {type: integer}
      required: [step]
""",
        encoding="utf-8",
    )
    (skill / "scripts" / "run.py").write_text(
        "def run(arguments, context):\n"
        "    step = arguments['step']\n"
        "    return {'summary': f'step {step}', 'details': {'step': step},\n"
        "            'facts_patch': {'counter': {'last': step}}}\n",
        encoding="utf-8",
    )
    return skill.parent


def test_recover_pending_replays_in_staging_order(tmp_path: Path) -> None:
    skills_root = _counter_skill(tmp_path)
    package = CapabilityRegistry(skills_root).discover()[0]
    tool = package.manifest.tools[0]
    session = AnalysisSession.create(tmp_path / "sessions", title="recovery")
    executor = CapabilityExecutor(session)

    staged: list[str] = []
    for step in (1, 2, 3):
        response = asyncio.run(executor.execute(package, tool, {"step": step}))
        staged.append(response["structuredContent"]["scagent_execution_id"])

    # Nothing was committed: PostToolUse never ran, so all three sit in pending.
    assert session.store.state.facts.get("counter") is None
    assert executor.recover_pending() == staged

    # The last-staged execution wins, which is only true if recovery replayed in order.
    assert session.store.state.facts["counter"]["last"] == 3


def test_unsequenced_staging_directories_are_committed_last(tmp_path: Path) -> None:
    """A crash between writing result.json and recording its event leaves no sequence.

    Such a directory has no defensible position in the ordering, so it is committed after
    everything that does have one, deterministically by name rather than at random.
    """

    skills_root = _counter_skill(tmp_path)
    package = CapabilityRegistry(skills_root).discover()[0]
    tool = package.manifest.tools[0]
    session = AnalysisSession.create(tmp_path / "sessions", title="unsequenced")
    executor = CapabilityExecutor(session)

    response = asyncio.run(executor.execute(package, tool, {"step": 1}))
    sequenced = response["structuredContent"]["scagent_execution_id"]

    orphan = executor.pending_root / "00000000-0000-4000-8000-000000000000"
    orphan.mkdir()
    (orphan / "result.json").write_text(
        (executor.pending_root / sequenced / "result.json").read_text(encoding="utf-8").replace(
            sequenced, orphan.name
        ),
        encoding="utf-8",
    )

    # Sorted by name the orphan is first; ordered by staging sequence it is last.
    assert orphan.name < sequenced
    assert executor.recover_pending() == [sequenced, orphan.name]
