from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from scagent_sdk.capabilities.documentation import (
    ArtifactDocumentation,
    render_readme,
)
from scagent_sdk.capabilities.executor import CapabilityExecutor
from scagent_sdk.capabilities.results import CapabilityContext, CapabilityResult
from scagent_sdk.errors import CapabilityExecutionError


def _doc(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": "cluster-qc-guide",
        "title": "leiden cluster check explained",
        "overview": "Whether each cluster looks like a real population.",
        "what_was_done": ["Measured quality per cluster.", "Ranked genes per cluster."],
        "findings": ["Clusters 4 and 9 were flagged.", "No cells were removed."],
        "parameters": {"min_cluster_cells": 30, "auto_remove_convergent": True},
        "files": [
            {
                "filename": "decisions.csv",
                "purpose": "One row per cluster.",
                "how_to_read": "Read `action` first.",
                "columns": {"cluster": "Cluster label.", "action": "keep, review, or remove."},
            }
        ],
        "caveats": ["Says nothing about which cell type a cluster is."],
    }
    payload.update(overrides)
    return payload


def _context(tmp_path: Path) -> CapabilityContext:
    staging = tmp_path / "staging"
    staging.mkdir()
    return CapabilityContext(
        scientific_session_id="session",
        session_dir=tmp_path,
        staging_dir=staging,
        skill_id="cluster-qc",
        tool_name="evaluate_cluster_qc",
        execution_id="abcd1234-0000-0000-0000-000000000000",
        state_revision=1,
        state_facts={},
    )


# --- rendering ---------------------------------------------------------------


def test_readme_states_what_was_done_and_what_was_found_in_order() -> None:
    rendered = render_readme(ArtifactDocumentation.from_dict(_doc()))

    assert rendered.startswith("# leiden cluster check explained\n")
    headings = [line for line in rendered.splitlines() if line.startswith("## ")]
    assert headings == [
        "## What was done",
        "## What this found",
        "## Settings used",
        "## Files here",
        "## What this does not tell you",
    ]
    # Steps are numbered because they happened in an order; findings are not, because they are
    # independent observations.
    assert "1. Measured quality per cluster." in rendered
    assert "- Clusters 4 and 9 were flagged." in rendered
    assert "| `cluster` | Cluster label. |" in rendered
    assert "| auto_remove_convergent | yes |" in rendered


def test_absent_sections_are_omitted_rather_than_left_empty() -> None:
    rendered = render_readme(
        ArtifactDocumentation.from_dict(
            {"name": "guide", "title": "Minimal", "overview": "Just this."}
        )
    )

    assert "## " not in rendered
    assert rendered.rstrip().endswith("Just this.")


def test_pipe_in_a_description_cannot_break_the_column_table() -> None:
    rendered = render_readme(
        ArtifactDocumentation.from_dict(
            _doc(
                files=[
                    {
                        "filename": "t.csv",
                        "purpose": "p",
                        "columns": {"verdict": "junk | identity"},
                    }
                ]
            )
        )
    )

    assert "| `verdict` | junk \\| identity |" in rendered


# --- validation --------------------------------------------------------------


def test_documentation_requires_a_name_and_a_title() -> None:
    for missing in ("name", "title"):
        payload = _doc()
        payload.pop(missing)
        with pytest.raises(CapabilityExecutionError, match=f"requires a {missing}"):
            ArtifactDocumentation.from_dict(payload)


def test_documented_file_requires_a_purpose_so_a_listing_is_never_bare() -> None:
    with pytest.raises(CapabilityExecutionError, match="requires a purpose"):
        ArtifactDocumentation.from_dict(_doc(files=[{"filename": "t.csv"}]))


def test_directory_cannot_climb_out_of_the_artifact_folder() -> None:
    with pytest.raises(CapabilityExecutionError, match="stay inside the artifact folder"):
        ArtifactDocumentation.from_dict(_doc(directory="../../etc"))


def test_directory_places_the_readme_beside_the_files_it_describes() -> None:
    doc = ArtifactDocumentation.from_dict(_doc(directory="cluster-qc/leiden-res-2-0"))

    assert doc.relative_path == "cluster-qc/leiden-res-2-0/README.md"
    assert ArtifactDocumentation.from_dict(_doc()).relative_path == "README.md"


# --- executor wiring ---------------------------------------------------------


def test_declared_documentation_is_written_and_registered_as_an_artifact(tmp_path: Path) -> None:
    context = _context(tmp_path)
    (context.staging_dir / "cluster-qc").mkdir()
    result = CapabilityResult.from_value(
        {"summary": "ran", "documentation": _doc(directory="cluster-qc")}
    )

    staged = CapabilityExecutor._write_documentation(context, result)

    written = context.staging_dir / "cluster-qc" / "README.md"
    assert "# leiden cluster check explained" in written.read_text(encoding="utf-8")
    # Registered like any other output, so it is committed, hashed, and projected on the same
    # terms as the figures it explains.
    assert [artifact.relative_path for artifact in staged.artifacts] == ["cluster-qc/README.md"]
    assert staged.artifacts[0].name == "cluster-qc-guide"
    assert staged.artifacts[0].media_type == "text/markdown"


def test_no_documentation_leaves_the_result_untouched(tmp_path: Path) -> None:
    result = CapabilityResult.from_value({"summary": "ran"})

    assert CapabilityExecutor._write_documentation(_context(tmp_path), result) is result


def test_artifact_output_visibility_requires_a_boolean() -> None:
    visible = CapabilityResult.from_value(
        {
            "summary": "ran",
            "artifacts": [
                {
                    "name": "guide",
                    "relative_path": "README.md",
                    "media_type": "text/markdown",
                    "include_in_output_view": False,
                }
            ],
        }
    )
    assert visible.artifacts[0].include_in_output_view is False

    with pytest.raises(
        CapabilityExecutionError,
        match="include_in_output_view must be true or false",
    ):
        CapabilityResult.from_value(
            {
                "summary": "ran",
                "artifacts": [
                    {
                        "name": "guide",
                        "relative_path": "README.md",
                        "include_in_output_view": "no",
                    }
                ],
            }
        )


def test_documentation_may_not_overwrite_a_declared_artifact(tmp_path: Path) -> None:
    context = _context(tmp_path)
    (context.staging_dir / "README.md").write_text("hand written", encoding="utf-8")
    result = CapabilityResult.from_value(
        {
            "summary": "ran",
            "artifacts": [
                {"name": "readme", "relative_path": "README.md", "media_type": "text/markdown"}
            ],
            "documentation": _doc(),
        }
    )

    with pytest.raises(CapabilityExecutionError, match="would overwrite"):
        CapabilityExecutor._write_documentation(context, result)
    assert (context.staging_dir / "README.md").read_text(encoding="utf-8") == "hand written"
