"""Lineage recorded at commit: node creation, fact routing, staleness, and recovery.

Stage 2 of ``docs/artifact-lineage-and-head-spec.md``. These drive the real executor rather than
the pure helpers, so they pin the wiring: what a dispatch records, what a commit writes into the
forest, and which facts become globally visible.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from scagent_sdk.capabilities.executor import CapabilityExecutor
from scagent_sdk.capabilities.registry import CapabilityRegistry
from scagent_sdk.errors import CapabilityExecutionError
from scagent_sdk.session import AnalysisSession
from scagent_sdk.state.lineage import (
    active_head,
    ancestry,
    classify_input,
    node_scoped_roots,
    resolve_node_facts,
)
from scagent_sdk.state.store import apply_merge_patch


def _skill(root: Path) -> Path:
    """A skill that can write a matrix, write only evidence, or clone its input identities."""

    skill = root / "skills" / "matrix"
    (skill / "scripts").mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: matrix\ndescription: test skill\n---\n\nMatrix.\n", encoding="utf-8"
    )
    (skill / "capability.yaml").write_text(
        """\
schema_version: 1
skill: {id: matrix, version: "1", description: test}
tools:
  - name: make_matrix
    description: write a matrix artifact
    entrypoint: scripts/run.py:make_matrix
    input_schema:
      type: object
      properties:
        path: {type: string}
        clustering: {type: string}
        facts: {type: object}
  - name: write_evidence
    description: write node-scoped evidence without a matrix
    entrypoint: scripts/run.py:write_evidence
    input_schema:
      type: object
      properties:
        path: {type: string}
        facts: {type: object}
""",
        encoding="utf-8",
    )
    (skill / "scripts" / "run.py").write_text(
        '''
def make_matrix(arguments, context):
    (context.staging_dir / "matrix.h5ad").write_bytes(b"H5AD")
    facts = dict(arguments.get("facts") or {})
    clustering = arguments.get("clustering")
    if clustering:
        analysis = dict(facts.get("analysis") or {})
        analysis["clustering"] = {"id": clustering}
        facts["analysis"] = analysis
    return {
        "summary": "wrote a matrix",
        "details": {},
        "facts_patch": facts,
        "artifacts": [
            {
                "name": "matrix",
                "relative_path": "matrix.h5ad",
                "media_type": "application/x-hdf5",
            }
        ],
    }


def write_evidence(arguments, context):
    (context.staging_dir / "evidence.json").write_text("{}\\n")
    return {
        "summary": "wrote evidence",
        "details": {},
        "facts_patch": dict(arguments.get("facts") or {}),
        "artifacts": [
            {
                "name": "evidence",
                "relative_path": "evidence.json",
                "media_type": "application/json",
            }
        ],
    }
'''.lstrip(),
        encoding="utf-8",
    )
    return skill.parent


class _Harness:
    def __init__(self, tmp_path: Path, title: str):
        self.package = CapabilityRegistry(_skill(tmp_path)).discover()[0]
        self.tools = {tool.name: tool for tool in self.package.manifest.tools}
        self.session = AnalysisSession.create(tmp_path / "sessions", title=title)
        self.executor = CapabilityExecutor(self.session)

    def run(self, tool: str, commit: bool = True, **arguments: Any) -> str:
        response = asyncio.run(
            self.executor.execute(self.package, self.tools[tool], dict(arguments))
        )
        assert response.get("is_error") is not True, response
        execution_id = response["structuredContent"]["scagent_execution_id"]
        if commit:
            assert self.executor.commit(execution_id)
        return execution_id

    def matrix_path(self, execution_id: str) -> str:
        return str(
            self.session.directory
            / "artifacts"
            / "capabilities"
            / execution_id
            / "matrix.h5ad"
        )

    @property
    def lineage(self) -> dict[str, Any]:
        return self.session.store.state.lineage

    @property
    def facts(self) -> dict[str, Any]:
        return self.session.store.state.facts


# --- node creation -------------------------------------------------------------------------


def test_first_matrix_creates_a_root_node_and_becomes_the_head(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, "root")
    first = harness.run("make_matrix")

    node = harness.lineage["nodes"][first]
    assert active_head(harness.lineage) == first
    # Optional fields are omitted rather than stored as null: a merge patch deletes null keys.
    assert node.get("parent_execution_id") is None
    assert node["head_path"] == f"artifacts/capabilities/{first}/matrix.h5ad"
    assert node["identity_signature"].startswith("identity:v1:sha256:")


def test_parent_is_the_artifact_actually_consumed(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, "chain")
    first = harness.run("make_matrix")
    second = harness.run("make_matrix", path=harness.matrix_path(first))

    assert harness.lineage["nodes"][second]["parent_execution_id"] == first
    assert harness.lineage["nodes"][second]["resolved_input_execution_id"] == first
    assert ancestry(harness.lineage, second) == [second, first]


def test_three_runs_from_one_parent_are_siblings(tmp_path: Path) -> None:
    """Acceptance case 1, end to end.

    Each run explicitly reads the same parent. Parentage must come from that path, not from the
    head, which advances after every commit.
    """

    harness = _Harness(tmp_path, "sweep")
    base = harness.run("make_matrix")
    children = [
        harness.run("make_matrix", path=harness.matrix_path(base), clustering=f"clust:{index}")
        for index in range(3)
    ]

    for child in children:
        assert harness.lineage["nodes"][child]["parent_execution_id"] == base
        assert ancestry(harness.lineage, child) == [child, base]
    assert children[0] not in ancestry(harness.lineage, children[2])


def test_untracked_input_starts_a_root_rather_than_inventing_a_parent(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, "untracked")
    external = tmp_path / "external.h5ad"
    external.write_bytes(b"H5AD")
    execution = harness.run("make_matrix", path=str(external))

    assert harness.lineage["nodes"][execution].get("parent_execution_id") is None
    assert harness.lineage["nodes"][execution]["requested_input"] == str(external)


def test_identity_signature_tracks_a_changed_clustering(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, "signature")
    first = harness.run("make_matrix", clustering="clust:1")
    second = harness.run("make_matrix", path=harness.matrix_path(first), clustering="clust:2")
    third = harness.run("make_matrix", path=harness.matrix_path(second))

    nodes = harness.lineage["nodes"]
    assert nodes[first]["identity_signature"] != nodes[second]["identity_signature"]
    # An execution that changes no identity keeps the signature it inherited.
    assert nodes[second]["identity_signature"] == nodes[third]["identity_signature"]


# --- fact routing --------------------------------------------------------------------------


def test_node_scoped_facts_reach_global_state_and_the_node(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, "routing")
    execution = harness.run("make_matrix", facts={"cluster_qc": {"status": "complete"}})

    assert harness.facts["cluster_qc"] == {"status": "complete"}
    resolved = resolve_node_facts(harness.lineage, execution, merge=apply_merge_patch)
    assert resolved["cluster_qc"] == {"status": "complete"}


def test_session_scoped_facts_are_not_snapshotted(tmp_path: Path) -> None:
    """``gene_conversion`` describes the input file, not one matrix, so it stays global only."""

    harness = _Harness(tmp_path, "session-scope")
    execution = harness.run("make_matrix", facts={"gene_conversion": {"changed": True}})

    assert harness.facts["gene_conversion"] == {"changed": True}
    resolved = resolve_node_facts(harness.lineage, execution, merge=apply_merge_patch)
    assert "gene_conversion" not in resolved


def test_read_only_evidence_attaches_to_the_head_without_moving_it(tmp_path: Path) -> None:
    """Acceptance case 8. Most session evidence comes from tools that write no matrix."""

    harness = _Harness(tmp_path, "read-only")
    matrix = harness.run("make_matrix")
    before = json.dumps(harness.lineage["nodes"][matrix], sort_keys=True)

    harness.run(
        "write_evidence",
        path=harness.matrix_path(matrix),
        facts={"annotation": {"evidence": {"markers": "complete"}}},
    )

    assert active_head(harness.lineage) == matrix
    assert set(harness.lineage["nodes"]) == {matrix}
    assert harness.facts["annotation"] == {"evidence": {"markers": "complete"}}

    resolved = resolve_node_facts(harness.lineage, matrix, merge=apply_merge_patch)
    assert resolved["annotation"] == {"evidence": {"markers": "complete"}}
    # The node gained a patch but nothing structural changed.
    after = json.loads(json.dumps(harness.lineage["nodes"][matrix], sort_keys=True))
    structural = {k: v for k, v in after.items() if k != "fact_patches"}
    assert structural == {
        k: v for k, v in json.loads(before).items() if k != "fact_patches"
    }


def test_read_only_evidence_without_an_input_targets_the_head(tmp_path: Path) -> None:
    """A pure review over facts carries no matrix path; rule 4 sends it to the active node."""

    harness = _Harness(tmp_path, "review")
    matrix = harness.run("make_matrix")
    harness.run("write_evidence", facts={"annotation": {"review": "resolved"}})

    resolved = resolve_node_facts(harness.lineage, matrix, merge=apply_merge_patch)
    assert resolved["annotation"] == {"review": "resolved"}


def test_evidence_on_an_ancestor_is_inherited_by_the_head(tmp_path: Path) -> None:
    """An ancestor's facts are part of the head's resolved view, so they stay globally visible.

    Suppressing them would make global facts disagree with ``resolve_node_facts(active)``, which is
    the invariant that lets a checkout reconstruct state.
    """

    harness = _Harness(tmp_path, "ancestor")
    first = harness.run("make_matrix")
    second = harness.run("make_matrix", path=harness.matrix_path(first))
    assert classify_input(harness.lineage, first) == "ancestor"

    harness.run(
        "write_evidence",
        path=harness.matrix_path(first),
        facts={"cluster_qc": {"status": "on-ancestor"}},
    )

    assert harness.facts["cluster_qc"] == {"status": "on-ancestor"}
    resolved = resolve_node_facts(harness.lineage, second, merge=apply_merge_patch)
    assert resolved["cluster_qc"] == {"status": "on-ancestor"}


def test_evidence_on_a_sibling_branch_stays_out_of_global_facts(tmp_path: Path) -> None:
    """The isolation that matters: a branch the head does not descend from is not visible.

    Reachable without explicit branch intent -- deriving twice from the same parent leaves the
    first derivation as a sibling of the new head.
    """

    harness = _Harness(tmp_path, "sibling")
    base = harness.run("make_matrix")
    sibling = harness.run("make_matrix", path=harness.matrix_path(base), clustering="clust:a")
    head = harness.run("make_matrix", path=harness.matrix_path(base), clustering="clust:b")

    assert active_head(harness.lineage) == head
    assert classify_input(harness.lineage, sibling) == "sibling"

    harness.run(
        "write_evidence",
        path=harness.matrix_path(sibling),
        facts={"cluster_qc": {"status": "on-sibling"}},
    )

    assert "cluster_qc" not in harness.facts
    assert resolve_node_facts(harness.lineage, sibling, merge=apply_merge_patch)["cluster_qc"] == {
        "status": "on-sibling"
    }
    assert "cluster_qc" not in resolve_node_facts(harness.lineage, head, merge=apply_merge_patch)


def test_global_facts_match_the_resolved_view_of_the_active_head(tmp_path: Path) -> None:
    """The invariant the routing rules exist to maintain, exercised over a mixed history."""

    harness = _Harness(tmp_path, "invariant")
    first = harness.run("make_matrix", facts={"cell_qc": {"step": 1}})
    harness.run("write_evidence", facts={"annotation": {"step": 2}})
    second = harness.run(
        "make_matrix", path=harness.matrix_path(first), facts={"cluster_qc": {"step": 3}}
    )
    harness.run("write_evidence", path=harness.matrix_path(second), facts={"batch": {"step": 4}})
    harness.run("make_matrix", path=harness.matrix_path(first), clustering="clust:x")

    active = active_head(harness.lineage)
    assert active is not None
    resolved = resolve_node_facts(harness.lineage, active, merge=apply_merge_patch)
    node_scoped = {
        key: value for key, value in harness.facts.items() if key in node_scoped_roots()
    }
    assert node_scoped == resolved


def test_unregistered_fact_root_fails_as_a_tool_error_not_a_hook_error(tmp_path: Path) -> None:
    """Fail closed, but early: the model should see an ordinary tool error it can act on.

    Validating only at commit would report the capability as successful and then fail inside
    PostToolUse, after the compute had already been paid for.
    """

    harness = _Harness(tmp_path, "unregistered")
    response = asyncio.run(
        harness.executor.execute(
            harness.package, harness.tools["make_matrix"], {"facts": {"pathways": {"a": 1}}}
        )
    )

    assert response["is_error"] is True
    assert "unregistered fact root: 'pathways'" in response["error_summary"]
    # Nothing was staged into state, so the session is untouched.
    assert harness.session.store.state.artifacts == {}
    assert harness.lineage["nodes"] == {}


# --- staleness and recovery ----------------------------------------------------------------


def test_commit_is_refused_when_the_head_moved_underneath_it(tmp_path: Path) -> None:
    """D6. A long compute must not silently rebase onto a head that appeared while it ran."""

    harness = _Harness(tmp_path, "stale-base")
    base = harness.run("make_matrix")
    slow = harness.run("make_matrix", commit=False, path=harness.matrix_path(base))
    fast = harness.run("make_matrix", path=harness.matrix_path(base))

    assert active_head(harness.lineage) == fast
    with pytest.raises(CapabilityExecutionError, match="active head is now"):
        harness.executor.commit(slow)

    # Rejected before the staging directory moved, so nothing looks committed.
    assert (harness.executor.pending_root / slow).is_dir()
    assert not (harness.executor.artifact_root / slow).exists()
    assert slow not in harness.session.store.state.artifacts


def test_read_only_commit_is_not_blocked_by_a_moved_head(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, "read-only-stale")
    base = harness.run("make_matrix")
    evidence = harness.run("write_evidence", commit=False, path=harness.matrix_path(base))
    harness.run("make_matrix", path=harness.matrix_path(base))

    assert harness.executor.commit(evidence)


def test_unsequenced_staging_directory_is_quarantined_not_adopted(tmp_path: Path) -> None:
    """D9. A crash between writing result.json and recording its event leaves no order."""

    harness = _Harness(tmp_path, "quarantine")
    sequenced = harness.run("make_matrix", commit=False)

    orphan = harness.executor.pending_root / "00000000-0000-4000-8000-000000000000"
    orphan.mkdir()
    (orphan / "result.json").write_text(
        (harness.executor.pending_root / sequenced / "result.json")
        .read_text(encoding="utf-8")
        .replace(sequenced, orphan.name),
        encoding="utf-8",
    )
    (orphan / "matrix.h5ad").write_bytes(b"H5AD")

    assert orphan.name < sequenced  # name order would have taken it first
    assert harness.executor.recover_pending() == [sequenced]

    # Moved aside intact rather than deleted, and recorded.
    quarantined = harness.executor.quarantine_root / orphan.name
    assert (quarantined / "result.json").is_file()
    assert not orphan.exists()
    assert orphan.name not in harness.session.store.state.artifacts
    kinds = [event.kind for event in harness.session.store.events()]
    assert "capability.result_quarantined" in kinds


def test_two_annotators_chained_forward_compose_into_one_head(tmp_path: Path) -> None:
    """The reported incident, done correctly: the second reads the first's output.

    Acceptance case 2's shape. Once D3 resolves omitted paths from the head this becomes the only
    reachable behaviour; today it records that chaining forward produces a single line of descent
    carrying both contributions.
    """

    harness = _Harness(tmp_path, "chained-annotators")
    clustered = harness.run("make_matrix")
    first = harness.run(
        "make_matrix",
        path=harness.matrix_path(clustered),
        facts={"annotation": {"evidence": {"scimilarity": "complete"}}},
    )
    second = harness.run(
        "make_matrix",
        path=harness.matrix_path(first),
        facts={"annotation": {"evidence": {"celltypist": "complete"}}},
    )

    assert active_head(harness.lineage) == second
    assert ancestry(harness.lineage, second) == [second, first, clustered]
    # Both contributions survive on the head's resolved view and in global facts.
    resolved = resolve_node_facts(harness.lineage, second, merge=apply_merge_patch)
    assert resolved["annotation"]["evidence"] == {
        "scimilarity": "complete",
        "celltypist": "complete",
    }
    assert harness.facts["annotation"]["evidence"] == {
        "scimilarity": "complete",
        "celltypist": "complete",
    }


def test_two_annotators_from_one_parent_are_recorded_as_divergent(tmp_path: Path) -> None:
    """The defect itself, now *visible* instead of silent.

    Both annotators read ``clustered``, so the second's output does not descend from the first and
    cannot carry its column. Before lineage there was nothing in state that recorded this; the
    forest now shows two children of one parent, and the head's resolved view is missing the
    abandoned branch's evidence rather than appearing to include it.
    """

    harness = _Harness(tmp_path, "divergent-annotators")
    clustered = harness.run("make_matrix")
    first = harness.run(
        "make_matrix",
        path=harness.matrix_path(clustered),
        facts={"annotation": {"evidence": {"scimilarity": "complete"}}},
    )
    second = harness.run(
        "make_matrix",
        path=harness.matrix_path(clustered),
        facts={"annotation": {"evidence": {"celltypist": "complete"}}},
    )

    nodes = harness.lineage["nodes"]
    assert nodes[first]["parent_execution_id"] == clustered
    assert nodes[second]["parent_execution_id"] == clustered
    assert classify_input(harness.lineage, first) == "sibling"

    # The head is the second annotator, and it genuinely does not have the first's evidence --
    # which is the truth about the delivered artifact, now recorded rather than papered over.
    resolved = resolve_node_facts(harness.lineage, second, merge=apply_merge_patch)
    assert resolved["annotation"]["evidence"] == {"celltypist": "complete"}
    assert harness.facts["annotation"]["evidence"] == {"celltypist": "complete"}
    assert resolve_node_facts(harness.lineage, first, merge=apply_merge_patch)["annotation"][
        "evidence"
    ] == {"scimilarity": "complete"}


def test_recovery_replays_sequenced_results_in_staging_order(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, "recovery-order")
    first = harness.run("make_matrix", commit=False)
    second = harness.run("make_matrix", commit=False, path=harness.matrix_path(first))

    assert harness.executor.recover_pending() == [first, second]
    assert active_head(harness.lineage) == second
    assert harness.lineage["nodes"][second]["parent_execution_id"] == first
