"""A skill and the floor that judges its evidence must agree on the evidence version.

These constants are deliberately duplicated: the skill script runs inside a compute environment and
cannot import the runtime package, and the runtime package holds no biology. But nothing links the
two copies, and disagreement is silent in the worst possible direction — a floor expecting a newer
schema rejects every piece of evidence the skill can produce, so the capability becomes
unreachable with a message about staleness that no rerun can fix. Pin them together.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from scagent_sdk.capabilities.registry import CapabilityRegistry
from scagent_sdk.floors import evaluator as floor_evaluator

SKILLS_ROOT = Path(__file__).parents[2] / ".claude" / "skills"


def _skill_globals(skill_id: str, tool_name: str) -> dict[str, Any]:
    package = next(
        item
        for item in CapabilityRegistry(SKILLS_ROOT).discover()
        if item.manifest.skill_id == skill_id
    )
    tool = next(item for item in package.manifest.tools if item.name == tool_name)
    return package.load_handler(tool).__globals__


def test_batch_evidence_schema_matches_the_floor() -> None:
    skill = _skill_globals("batch-investigation", "investigate_batch")
    assert skill["BATCH_EVIDENCE_SCHEMA"] == floor_evaluator.BATCH_EVIDENCE_SCHEMA, (
        "batch-investigation writes a different evidence schema than floors/evaluator.py accepts; "
        "every investigation would fail the current-batch-evidence floor"
    )


def test_batch_decision_policy_matches_the_floor() -> None:
    skill = _skill_globals("batch-investigation", "decide_batch_handling")
    assert skill["DECISION_POLICY_VERSION"] == floor_evaluator.BATCH_DECISION_POLICY_VERSION, (
        "decide_batch_handling stamps a different policy version than the integration floor "
        "requires; no decision it records could authorize integration"
    )


def test_cluster_qc_evidence_schema_matches_the_floor() -> None:
    skill = _skill_globals("cluster-qc", "evaluate_cluster_qc")
    assert skill["CLUSTER_QC_EVIDENCE_SCHEMA"] == floor_evaluator.CLUSTER_QC_EVIDENCE_SCHEMA, (
        "cluster-qc writes a different evidence schema than floors/evaluator.py accepts; "
        "annotation and finalization would be permanently blocked"
    )
