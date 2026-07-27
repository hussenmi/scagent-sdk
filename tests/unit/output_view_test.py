from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scagent_sdk.output_view import OUTPUT_DIRECTORIES, refresh_output_view
from scagent_sdk.session import AnalysisSession


def _commit_artifact(
    session: AnalysisSession,
    *,
    execution_id: str,
    tool_name: str,
    summary: str,
    files: list[tuple[str, str, str, bytes]],
) -> Path:
    artifact_path = session.directory / "artifacts" / "capabilities" / execution_id
    artifact_path.mkdir(parents=True)
    records: list[dict[str, Any]] = []
    for name, relative_path, media_type, content in files:
        path = artifact_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        records.append(
            {
                "name": name,
                "relative_path": relative_path,
                "media_type": media_type,
                "size_bytes": len(content),
            }
        )
    artifact_record = {
        "kind": "capability-result",
        "skill_id": "test-skill",
        "skill_version": "1",
        "skill_fingerprint": "sha256:test",
        "tool_name": tool_name,
        "environment": {"name": "current"},
        "input_state_revision": session.store.state.revision,
        "summary": summary,
        "path": f"artifacts/capabilities/{execution_id}",
        "files": records,
        "model_media": [],
    }
    session.store.record(
        "capability.result_committed",
        payload={"execution_id": execution_id, **artifact_record},
        state_patch={"artifacts": {execution_id: artifact_record}},
    )
    session.refresh_outputs()
    return artifact_path


def test_new_session_has_empty_browsable_output_surface(tmp_path: Path) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="new")

    for directory in OUTPUT_DIRECTORIES:
        assert (session.directory / directory).is_dir()
    assert (session.directory / "data" / "intermediates").is_dir()
    index = json.loads((session.directory / "outputs.json").read_text())
    assert index["generator"] == "scagent-sdk"
    assert all(not entries for entries in index["categories"].values())
    assert "# Session outputs" in (session.directory / "outputs.md").read_text()


def test_committed_files_are_projected_by_kind_without_copying_data(tmp_path: Path) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="projection")
    execution_id = "aaaaaaaa-1111-2222-3333-444444444444"
    artifact_path = _commit_artifact(
        session,
        execution_id=execution_id,
        tool_name="finalize_analysis",
        summary="Custom analysis code completed for: Inspect the dataset",
        files=[
            ("analysis-code", "analysis.py", "text/x-python", b"print('ok')\n"),
            (
                "analysis-recipe",
                "analysis-recipe.py",
                "text/x-python",
                b"CAPABILITY_CALLS = []\n",
            ),
            ("umap", "embedding.png", "image/png", b"PNG"),
            ("analysis-report", "analysis-report.md", "text/markdown", b"# Report\n"),
            ("run-facts", "run.json", "application/json", b"{}\n"),
            ("final-labels", "final-labels.csv", "text/csv", b"cluster,label\n0,T\n"),
            ("final-anndata", "final-annotated.h5ad", "application/x-hdf5", b"H5AD"),
            ("analysis-stdout", "analysis.stdout.txt", "text/plain", b"not projected\n"),
        ],
    )

    code = next((session.directory / "code").glob("inspect-the-dataset-*.py"))
    figure = next((session.directory / "figures").glob("*.png"))
    reports = sorted((session.directory / "reports").glob("*--aaaaaaaa.*"))
    table = next((session.directory / "tables").glob("*--aaaaaaaa.csv"))
    data = next((session.directory / "data").glob("*--aaaaaaaa.h5ad"))
    assert code.name == "inspect-the-dataset-aaaaaaaa.py"
    assert code.is_symlink()
    assert figure.is_symlink()
    assert len(reports) == 1
    assert all(path.is_symlink() for path in reports)
    assert table.is_symlink()
    assert data.is_symlink()
    assert data.resolve() == artifact_path / "final-annotated.h5ad"
    assert data.stat().st_ino == (artifact_path / "final-annotated.h5ad").stat().st_ino
    assert not list((session.directory / "reports").glob("*stdout*"))

    assert (session.directory / "data" / "final-annotated.h5ad").resolve() == data.resolve()
    assert (
        session.directory / "reports" / "final-analysis-report.md"
    ).resolve() == artifact_path / "analysis-report.md"
    assert (
        session.directory / "tables" / "final-labels.csv"
    ).resolve() == artifact_path / "final-labels.csv"
    assert (
        session.directory / "code" / "analysis-recipe.py"
    ).resolve() == artifact_path / "analysis-recipe.py"

    index = json.loads((session.directory / "outputs.json").read_text())
    assert index["authoritative_root"] == "artifacts/capabilities"
    assert len(index["aliases"]) == 4
    assert index["categories"]["figures"][0]["canonical_path"].endswith("/embedding.png")
    markdown = (session.directory / "outputs.md").read_text()
    assert "## Primary results" in markdown
    assert "final-annotated.h5ad" in markdown
    assert "`finalize_analysis`" in markdown


def test_nested_artifact_groups_remain_meaningfully_grouped(tmp_path: Path) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="nested")
    artifact_path = _commit_artifact(
        session,
        execution_id="a0a0a0a0-1111-2222-3333-444444444444",
        tool_name="evaluate_cluster_qc",
        summary="Evaluated cluster structure",
        files=[
            (
                "cluster-structure-10",
                "cluster-structure/cluster_10_correlation.png",
                "image/png",
                b"ten",
            ),
            (
                "cluster-structure-2",
                "cluster-structure/cluster_2_correlation.png",
                "image/png",
                b"two",
            ),
        ],
    )

    group = session.directory / "figures" / "cluster-structure"
    projected = sorted(group.glob("*.png"))
    assert len(projected) == 2
    assert {path.resolve() for path in projected} == {
        artifact_path / "cluster-structure" / "cluster_2_correlation.png",
        artifact_path / "cluster-structure" / "cluster_10_correlation.png",
    }
    index = json.loads((session.directory / "outputs.json").read_text())
    view_paths = [entry["view_path"] for entry in index["categories"]["figures"]]
    assert "cluster-structure-2" in view_paths[0]
    assert "cluster-structure-10" in view_paths[1]


def test_intermediate_data_is_separated_from_final_data(tmp_path: Path) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="data")
    artifact_path = _commit_artifact(
        session,
        execution_id="bbbbbbbb-1111-2222-3333-444444444444",
        tool_name="compute_single_cell_umap",
        summary="Computed UMAP",
        files=[("umap-anndata", "umap.h5ad", "application/x-hdf5", b"intermediate")],
    )

    projected = next((session.directory / "data" / "intermediates").glob("*.h5ad"))
    assert projected.is_symlink()
    assert projected.resolve() == artifact_path / "umap.h5ad"
    assert not (session.directory / "data" / "final-annotated.h5ad").exists()


def test_repeated_names_remain_distinct_and_traceable(tmp_path: Path) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="repeated")
    for execution_id, content in (
        ("11111111-aaaa-bbbb-cccc-000000000000", b"first"),
        ("22222222-aaaa-bbbb-cccc-000000000000", b"second"),
    ):
        _commit_artifact(
            session,
            execution_id=execution_id,
            tool_name="visualize_embedding",
            summary="Visualized UMAP",
            files=[("embedding", "embedding.png", "image/png", content)],
        )

    figures = sorted((session.directory / "figures").glob("*.png"))
    assert [path.name for path in figures] == [
        "embedding--visualize-embedding--11111111.png",
        "embedding--visualize-embedding--22222222.png",
    ]
    assert [path.read_bytes() for path in figures] == [b"first", b"second"]


def test_resume_rebuilds_a_deleted_projection_from_committed_state(tmp_path: Path) -> None:
    sessions_root = tmp_path / "sessions"
    session = AnalysisSession.create(sessions_root, title="resume", session_id="resume-view")
    artifact_path = _commit_artifact(
        session,
        execution_id="cccccccc-1111-2222-3333-444444444444",
        tool_name="visualize_qc",
        summary="Visualized QC",
        files=[("qc-distributions", "qc.png", "image/png", b"PNG")],
    )
    projected = next((session.directory / "figures").glob("*.png"))
    projected.unlink()
    assert not projected.exists()

    resumed = AnalysisSession.resume(sessions_root, "resume-view")

    rebuilt = next((resumed.directory / "figures").glob("*.png"))
    assert rebuilt.is_symlink()
    assert rebuilt.resolve() == artifact_path / "qc.png"


def test_relative_links_survive_moving_the_complete_session(tmp_path: Path) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="portable")
    _commit_artifact(
        session,
        execution_id="dddddddd-1111-2222-3333-444444444444",
        tool_name="visualize_embedding",
        summary="Visualized UMAP",
        files=[("embedding", "embedding.png", "image/png", b"PNG")],
    )
    original_link = next((session.directory / "figures").glob("*.png"))
    assert not original_link.readlink().is_absolute()

    destination_root = tmp_path / "moved"
    destination_root.mkdir()
    moved = session.directory.rename(destination_root / session.directory.name)

    moved_link = next((moved / "figures").glob("*.png"))
    assert moved_link.read_bytes() == b"PNG"
    assert moved_link.resolve().is_relative_to(moved)


def test_staged_or_failed_outputs_never_enter_the_review_view(tmp_path: Path) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="uncommitted")
    pending = (
        session.directory
        / "runtime"
        / "capabilities"
        / "pending"
        / "eeeeeeee-1111-2222-3333-444444444444"
    )
    pending.mkdir(parents=True)
    (pending / "uncommitted.png").write_bytes(b"PNG")
    session.store.record(
        "capability.execution_failed",
        payload={"execution_id": pending.name, "error": "failed"},
    )

    session.refresh_outputs()

    assert not list((session.directory / "figures").iterdir())
    index = json.loads((session.directory / "outputs.json").read_text())
    assert index["categories"]["figures"] == []


def test_output_view_io_failure_does_not_invalidate_session_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="view failure")
    session.checkpoint_facts({"dataset": {"path": "/data/input.h5ad"}}, reason="test")

    def fail() -> dict[str, Any]:
        raise OSError("symlinks unavailable")

    monkeypatch.setattr(session, "refresh_outputs", fail)

    assert session.refresh_outputs_best_effort() is None
    assert session.store.state.facts["dataset"]["path"] == "/data/input.h5ad"
    assert session.store.events()[-1].kind == "session.output_view_refresh_failed"


def test_user_files_and_aliases_are_not_overwritten(tmp_path: Path) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="preserve")
    user_report = session.directory / "reports" / "notes.md"
    user_report.write_text("mine\n")
    user_alias = session.directory / "data" / "final-annotated.h5ad"
    user_alias.write_bytes(b"user-owned")

    _commit_artifact(
        session,
        execution_id="ffffffff-1111-2222-3333-444444444444",
        tool_name="finalize_analysis",
        summary="Finalized analysis",
        files=[
            ("analysis-report", "notes.md", "text/markdown", b"generated\n"),
            ("final-anndata", "final-annotated.h5ad", "application/x-hdf5", b"canonical"),
        ],
    )

    assert user_report.read_text() == "mine\n"
    assert user_alias.read_bytes() == b"user-owned"
    generated_report = next((session.directory / "reports").glob("*--ffffffff.md"))
    assert generated_report.read_bytes() == b"generated\n"
    assert next((session.directory / "data").glob("*--ffffffff.h5ad")).read_bytes() == b"canonical"
    index = json.loads((session.directory / "outputs.json").read_text())
    assert "data/final-annotated.h5ad" not in {
        entry["view_path"] for entry in index["aliases"]
    }


def test_byte_identical_legacy_code_copy_is_migrated_but_edited_code_is_preserved(
    tmp_path: Path,
) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="code migration")
    _commit_artifact(
        session,
        execution_id="abababab-1111-2222-3333-444444444444",
        tool_name="run_analysis_code",
        summary="Custom analysis code completed for: Inspect the dataset",
        files=[("analysis-code", "analysis.py", "text/x-python", b"print('canonical')\n")],
    )
    projected = next((session.directory / "code").glob("*.py"))
    projected.unlink()
    (session.directory / "outputs.json").unlink()
    projected.write_bytes(b"print('canonical')\n")

    session.refresh_outputs()

    assert projected.is_symlink()
    assert projected.read_bytes() == b"print('canonical')\n"

    projected.unlink()
    (session.directory / "outputs.json").unlink()
    projected.write_bytes(b"print('user edit')\n")
    session.refresh_outputs()

    assert not projected.is_symlink()
    assert projected.read_bytes() == b"print('user edit')\n"
    alternate = next((session.directory / "code").glob("*--view-2.py"))
    assert alternate.is_symlink()
    assert alternate.read_bytes() == b"print('canonical')\n"


def test_refresh_removes_only_stale_generated_links(tmp_path: Path) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="cleanup")
    _commit_artifact(
        session,
        execution_id="99999999-1111-2222-3333-444444444444",
        tool_name="visualize_embedding",
        summary="Visualized UMAP",
        files=[("embedding", "embedding.png", "image/png", b"PNG")],
    )
    generated = next((session.directory / "figures").glob("*.png"))
    user_file = session.directory / "figures" / "README.md"
    user_file.write_text("keep\n")

    refresh_output_view(session.directory, {})

    assert not generated.exists()
    assert user_file.read_text() == "keep\n"
