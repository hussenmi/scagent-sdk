"""Storage accounting and dry-run prune proposals.

The point of these is that apparent size is not reclaimable size. Today's artifacts are independent
files so the two nearly coincide; the moment versions can share bytes they diverge completely, and a
report that promised apparent bytes would be wrong by everything. These pin the distinction now, in
both regimes, so the number a prune promises stays honest across a storage-format change.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from scagent_sdk.state.retention import account, propose_prune


def _artifact(root: Path, execution_id: str, *files: tuple[str, int]) -> Path:
    directory = root / "artifacts" / "capabilities" / execution_id
    directory.mkdir(parents=True, exist_ok=True)
    for name, size in files:
        (directory / name).write_bytes(b"x" * size)
    return directory


def _node(execution_id: str, *, parent: str | None, tool: str = "cluster_single_cells") -> dict:
    return {
        "parent_execution_id": parent,
        "head_path": f"artifacts/capabilities/{execution_id}/matrix.h5ad",
        "identity_signature": "identity:v1:sha256:aa",
        "created_by": {"skill_id": "s", "tool_name": tool},
        "fact_patches": [],
    }


def _lineage(active: str | None, nodes: dict[str, Any]) -> dict[str, Any]:
    return {"active_execution_id": active, "nodes": nodes}


# --- accounting ----------------------------------------------------------------------------


def test_independent_files_are_fully_reclaimable(tmp_path: Path) -> None:
    """Today's regime: separate H5AD files, so apparent and reclaimable coincide."""

    a = _artifact(tmp_path, "a", ("matrix.h5ad", 4000))
    b = _artifact(tmp_path, "b", ("matrix.h5ad", 1000))

    result = account([a], retained=[b])
    assert result.apparent_bytes == 4000
    assert result.unique_bytes == 4000
    assert result.shared_bytes == 0
    assert result.reclaimable_bytes == 4000
    assert result.files == 1


def test_hard_linked_bytes_are_shared_not_reclaimable(tmp_path: Path) -> None:
    """The case that motivates the whole distinction.

    A version whose content is hard-linked from a retained version has a full apparent size and a
    reclaimable size of zero. Measured on a real four-version Zarr fixture, pruning such a branch
    reported 6.5 MB apparent and freed 0.0 MB.
    """

    kept = _artifact(tmp_path, "kept", ("matrix.h5ad", 8000))
    branch = tmp_path / "artifacts" / "capabilities" / "branch"
    branch.mkdir(parents=True)
    os.link(kept / "matrix.h5ad", branch / "matrix.h5ad")
    (branch / "extra.h5ad").write_bytes(b"y" * 500)

    result = account([branch], retained=[kept])
    assert result.apparent_bytes == 8500
    assert result.unique_bytes == 8500
    assert result.shared_bytes == 8000  # the linked matrix survives deletion
    assert result.reclaimable_bytes == 500  # only what this version alone holds


def test_a_file_linked_twice_inside_the_target_counts_once(tmp_path: Path) -> None:
    directory = _artifact(tmp_path, "a", ("matrix.h5ad", 1000))
    os.link(directory / "matrix.h5ad", directory / "alias.h5ad")

    result = account([directory])
    assert result.files == 2
    assert result.apparent_bytes == 2000  # what du would report
    assert result.unique_bytes == 1000  # what it physically occupies
    assert result.reclaimable_bytes == 1000


def test_missing_and_empty_directories_account_to_zero(tmp_path: Path) -> None:
    assert account([tmp_path / "absent"]).unique_bytes == 0
    empty = tmp_path / "empty"
    empty.mkdir()
    assert account([empty]).files == 0


def test_symlinks_are_not_counted_as_storage(tmp_path: Path) -> None:
    """The session's browsable view is built from symlinks; it owns no bytes."""

    directory = _artifact(tmp_path, "a", ("matrix.h5ad", 1000))
    (directory / "link.h5ad").symlink_to(directory / "matrix.h5ad")

    assert account([directory]).unique_bytes == 1000


# --- prune proposals -----------------------------------------------------------------------


def test_versions_on_the_active_line_are_never_candidates(tmp_path: Path) -> None:
    for name in ("root", "mid", "head"):
        _artifact(tmp_path, name, ("matrix.h5ad", 1000))
    lineage = _lineage(
        "head",
        {
            "root": _node("root", parent=None),
            "mid": _node("mid", parent="root"),
            "head": _node("head", parent="mid"),
        },
    )

    proposal = propose_prune(tmp_path, session_id="s", lineage=lineage, artifacts={})
    assert proposal.candidates == ()
    assert set(proposal.retained) == {"root", "mid", "head"}
    assert proposal.notes["active_line_depth"] == 3


def test_an_abandoned_branch_is_a_candidate_with_its_reclaimable_size(tmp_path: Path) -> None:
    _artifact(tmp_path, "base", ("matrix.h5ad", 1000))
    _artifact(tmp_path, "chosen", ("matrix.h5ad", 1000))
    _artifact(tmp_path, "abandoned", ("matrix.h5ad", 3000))
    lineage = _lineage(
        "chosen",
        {
            "base": _node("base", parent=None),
            "chosen": _node("chosen", parent="base"),
            "abandoned": _node("abandoned", parent="base"),
        },
    )

    proposal = propose_prune(tmp_path, session_id="s", lineage=lineage, artifacts={})
    assert [item.execution_id for item in proposal.candidates] == ["abandoned"]
    assert proposal.candidate_total.reclaimable_bytes == 3000
    assert proposal.candidates[0].tool_name == "cluster_single_cells"
    assert proposal.candidates[0].exists is True


def test_a_proposal_never_promises_shared_bytes(tmp_path: Path) -> None:
    """A hard-linked branch must report zero reclaimable and say so."""

    kept = _artifact(tmp_path, "kept", ("matrix.h5ad", 5000))
    branch = tmp_path / "artifacts" / "capabilities" / "branch"
    branch.mkdir(parents=True)
    os.link(kept / "matrix.h5ad", branch / "matrix.h5ad")
    lineage = _lineage(
        "kept", {"kept": _node("kept", parent=None), "branch": _node("branch", parent="kept")}
    )

    proposal = propose_prune(tmp_path, session_id="s", lineage=lineage, artifacts={})
    assert proposal.candidate_total.apparent_bytes == 5000
    assert proposal.candidate_total.reclaimable_bytes == 0
    assert any("reclaim nothing" in warning for warning in proposal.warnings)


def test_candidates_always_carry_the_disposition_warning(tmp_path: Path) -> None:
    """Unreachable is not the same as spent; the report must not imply consent to delete."""

    _artifact(tmp_path, "base", ("matrix.h5ad", 100))
    _artifact(tmp_path, "head", ("matrix.h5ad", 100))
    _artifact(tmp_path, "other", ("matrix.h5ad", 100))
    lineage = _lineage(
        "head",
        {
            "base": _node("base", parent=None),
            "head": _node("head", parent="base"),
            "other": _node("other", parent="base"),
        },
    )

    proposal = propose_prune(tmp_path, session_id="s", lineage=lineage, artifacts={})
    assert any("retained / pinned / rejected" in warning for warning in proposal.warnings)


def test_a_forest_of_roots_is_refused_rather_than_reported_as_prunable(tmp_path: Path) -> None:
    """The real false positive this guards against.

    A session whose events predate argument recording reconstructs as isolated roots. Reachability
    then marks all but one as unreachable, so a naive report offers most of the analysis for
    deletion. Measured on ``run_20260727T033221Z_59a5ac``: 13 versions, active line depth 1, 12
    apparent candidates.
    """

    nodes = {}
    for index in range(6):
        name = f"v{index}"
        _artifact(tmp_path, name, ("matrix.h5ad", 1000))
        nodes[name] = _node(name, parent=None)  # every version parentless

    proposal = propose_prune(tmp_path, session_id="s", lineage=_lineage("v0", nodes), artifacts={})

    assert proposal.notes["topology_reliable"] is False
    assert proposal.notes["parentless_versions"] == 6
    assert any("prune nothing here" in warning for warning in proposal.warnings)
    # The permissive "merely unreachable" wording must not appear: it would imply the candidates
    # are real, which is the mistake.
    assert not any("not the same as spent" in warning for warning in proposal.warnings)


def test_a_single_root_with_branches_is_reliable(tmp_path: Path) -> None:
    for name in ("base", "head", "branch"):
        _artifact(tmp_path, name, ("matrix.h5ad", 1000))
    lineage = _lineage(
        "head",
        {
            "base": _node("base", parent=None),
            "head": _node("head", parent="base"),
            "branch": _node("branch", parent="base"),
        },
    )

    proposal = propose_prune(tmp_path, session_id="s", lineage=lineage, artifacts={})
    assert proposal.notes["topology_reliable"] is True
    assert [item.execution_id for item in proposal.candidates] == ["branch"]


def test_an_empty_forest_reports_that_nothing_can_be_judged(tmp_path: Path) -> None:
    proposal = propose_prune(tmp_path, session_id="s", lineage=_lineage(None, {}), artifacts={})
    assert proposal.candidates == ()
    assert any("no lineage versions recorded" in warning for warning in proposal.warnings)


def test_versions_without_a_head_are_not_silently_all_prunable(tmp_path: Path) -> None:
    _artifact(tmp_path, "orphan", ("matrix.h5ad", 100))
    proposal = propose_prune(
        tmp_path,
        session_id="s",
        lineage=_lineage(None, {"orphan": _node("orphan", parent=None)}),
        artifacts={},
    )
    assert any("no active head" in warning for warning in proposal.warnings)


def test_session_total_covers_every_committed_execution(tmp_path: Path) -> None:
    """Read-only evidence executions own artifacts too, so the total is not the forest's total."""

    _artifact(tmp_path, "matrix", ("matrix.h5ad", 1000))
    _artifact(tmp_path, "evidence", ("report.json", 400))
    lineage = _lineage("matrix", {"matrix": _node("matrix", parent=None)})

    proposal = propose_prune(
        tmp_path,
        session_id="s",
        lineage=lineage,
        artifacts={"matrix": {"kind": "capability-result"}, "evidence": {"kind": "x"}},
    )
    assert proposal.total.unique_bytes == 1400
    assert proposal.notes["committed_executions"] == 2


def test_a_proposal_deletes_nothing(tmp_path: Path) -> None:
    directory = _artifact(tmp_path, "abandoned", ("matrix.h5ad", 3000))
    _artifact(tmp_path, "head", ("matrix.h5ad", 100))
    lineage = _lineage(
        "head",
        {"head": _node("head", parent=None), "abandoned": _node("abandoned", parent=None)},
    )

    propose_prune(tmp_path, session_id="s", lineage=lineage, artifacts={})
    assert (directory / "matrix.h5ad").is_file()
