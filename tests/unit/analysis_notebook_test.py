"""Deterministic tests for the requested analysis-notebook capability.

The renderer is stdlib-only by design, so it is exercised here from the control plane rather than
through a compute runtime -- which is also why the capability declares `environment: current`.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_MODULE_PATH = (
    Path(__file__).parents[2]
    / ".claude"
    / "skills"
    / "analysis-notebook"
    / "scripts"
)


def _load(name: str, filename: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, _MODULE_PATH / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


notebook = _load("notebook", "notebook.py")
build = _load("analysis_notebook_build", "build.py")
recipe = _load(
    "finalize_recipe",
    "../../finalize-analysis/scripts/recipe.py",
)

_PNG = bytes.fromhex("89504e470d0a1a0a") + b"fake-png-body"
_CLUSTERING = "single-cell-clustering"
_FINALIZE = "finalize-analysis"


def _step(
    *,
    execution_id: str,
    tool_name: str,
    skill_id: str,
    summary: str = "",
    arguments: dict[str, Any] | None = None,
    files: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "execution_id": execution_id,
        "skill_id": skill_id,
        "skill_version": "1.0.0",
        "tool_name": tool_name,
        "arguments": arguments or {},
        "summary": summary,
        "files": files or [],
    }


def _write_figures(session_dir: Path, execution_id: str, relative_paths: list[str]) -> None:
    root = session_dir / "artifacts" / "capabilities" / execution_id
    for relative in relative_paths:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_PNG)


def _sources(document: dict[str, Any], cell_type: str | None = None) -> list[str]:
    return [
        "".join(cell["source"])
        for cell in document["cells"]
        if cell_type is None or cell["cell_type"] == cell_type
    ]


def _build(tmp_path: Path, history: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
    return notebook.build_analysis_notebook(
        title="Test analysis",
        dataset_path="/data/input.h5ad",
        history=history,
        session_dir=tmp_path,
        **kwargs,
    )


def test_notebook_conforms_to_the_nbformat_4_5_envelope(tmp_path: Path) -> None:
    """Cell ids are mandatory at minor 5; without them Jupyter warns and will later fail."""

    document = _build(tmp_path, [_step(execution_id="a" * 8, tool_name="t", skill_id="s")])

    assert document["nbformat"] == 4
    assert document["nbformat_minor"] == 5
    assert document["metadata"]["kernelspec"]["name"] == "python3"
    ids = [cell["id"] for cell in document["cells"]]
    assert len(ids) == len(set(ids))
    assert all(cell["cell_type"] in {"markdown", "code"} for cell in document["cells"])
    for cell in document["cells"]:
        assert isinstance(cell["source"], list)
        assert all(isinstance(line, str) for line in cell["source"])
        if cell["cell_type"] == "code":
            assert cell["outputs"] == []
            assert cell["execution_count"] is None


def test_cell_ids_are_positional_so_regeneration_diffs_cleanly(tmp_path: Path) -> None:
    history = [_step(execution_id="a" * 8, tool_name="t", skill_id="s")]

    first = _build(tmp_path, history)
    second = _build(tmp_path, history)

    assert [cell["id"] for cell in first["cells"]] == [cell["id"] for cell in second["cells"]]
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_an_input_path_is_resolved_to_the_step_that_produced_it(tmp_path: Path) -> None:
    """The core readability fix: an opaque uuid path becomes a named dependency."""

    producer = "bf7442a5-4709-4f1d-98b4-7c8993517011"
    history = [
        _step(
            execution_id=producer,
            tool_name="materialize_count_matrix",
            skill_id="single-cell-counts",
        ),
        _step(
            execution_id="c" * 8,
            tool_name="calculate_single_cell_qc",
            skill_id="single-cell-qc",
            arguments={
                "path": (
                    "/home/user/sessions/run_x/artifacts/capabilities/"
                    f"{producer}/counts-ready.h5ad"
                ),
                "organism": "human",
            },
        ),
    ]

    code = "\n".join(_sources(_build(tmp_path, history), "code"))
    assert "path='counts-ready.h5ad',  # output of step 1 · materialize_count_matrix" in code
    assert producer not in code
    assert "/home/user/sessions" not in code


def test_an_unrecognized_path_is_left_alone_rather_than_mislabeled(tmp_path: Path) -> None:
    history = [
        _step(
            execution_id="a" * 8,
            tool_name="describe_dataset",
            skill_id="inspect-dataset",
            arguments={"path": "/data/raw/input.h5ad"},
        )
    ]

    code = "\n".join(_sources(_build(tmp_path, history), "code"))
    assert "path='/data/raw/input.h5ad'" in code
    assert "output of step" not in code


def test_decisions_and_full_rationales_are_promoted_not_truncated(tmp_path: Path) -> None:
    rationale = "Because " + "x" * 900
    history = [
        _step(
            execution_id="a" * 8,
            tool_name="review_single_cell_qc",
            skill_id="single-cell-qc",
            summary="Recorded cell-QC decision.",
            arguments={"decision": "filter_cells", "rationale": rationale},
        )
    ]

    markdown = "\n".join(_sources(_build(tmp_path, history), "markdown"))
    assert "**Decision: `filter_cells`**" in markdown
    assert f"> {rationale}" in markdown
    # Prose is lifted out of the call signature rather than inlined into it.
    code = "\n".join(_sources(_build(tmp_path, history), "code"))
    assert "rationale=...,  # see above" in code
    assert "x" * 900 not in code


def test_phase_headings_follow_the_real_order_and_mark_recurrence(tmp_path: Path) -> None:
    """Regrouping steps into phase buckets would misrepresent an iterative analysis."""

    history = [
        _step(execution_id="a" * 8, tool_name="cluster_single_cells", skill_id=_CLUSTERING),
        _step(execution_id="b" * 8, tool_name="evaluate_cluster_qc", skill_id="cluster-qc"),
        _step(execution_id="c" * 8, tool_name="cluster_single_cells", skill_id=_CLUSTERING),
    ]

    headings = [
        line
        for source in _sources(_build(tmp_path, history), "markdown")
        for line in source.splitlines()
        if line.startswith("## ")
    ]
    assert headings[0] == "## Contents"
    assert "## Clustering" in headings
    assert "## Cluster quality control" in headings
    assert "## Clustering (continued)" in headings
    # Order preserved: the second clustering heading comes after the cluster-QC heading.
    assert headings.index("## Cluster quality control") < headings.index(
        "## Clustering (continued)"
    )


def test_an_unknown_skill_still_gets_a_phase(tmp_path: Path) -> None:
    document = _build(
        tmp_path, [_step(execution_id="a" * 8, tool_name="mystery", skill_id="not-a-known-skill")]
    )

    assert "## Other steps" in _sources(document, "markdown")


def test_overview_figures_embed_and_nested_series_is_linked(tmp_path: Path) -> None:
    """A per-cluster diagnostic series would dominate the file, so only the overview inlines."""

    execution_id = "a3e21a6e-e531-4b71-8992-e9c3651292e7"
    relatives = [
        "cluster-qc/leiden/cluster-qc-metrics.png",
        "cluster-qc/leiden/cluster-qc-umap.png",
        "cluster-qc/leiden/cluster-structure/cluster_0.png",
        "cluster-qc/leiden/cluster-structure/cluster_1.png",
    ]
    _write_figures(tmp_path, execution_id, relatives)
    history = [
        _step(
            execution_id=execution_id,
            tool_name="evaluate_cluster_qc",
            skill_id="cluster-qc",
            files=[
                {"name": Path(r).stem, "relative_path": r, "media_type": "image/png",
                 "size_bytes": len(_PNG)}
                for r in relatives
            ],
        )
    ]

    document = _build(tmp_path, history)
    attachments = {
        name for cell in document["cells"] for name in (cell.get("attachments") or {})
    }
    assert attachments == {
        "step-1-cluster-qc-metrics.png",
        "step-1-cluster-qc-umap.png",
    }
    markdown = "\n".join(_sources(document, "markdown"))
    assert "2 further diagnostic figure(s)" in markdown
    assert (
        f"../artifacts/capabilities/{execution_id}"
        "/cluster-qc/leiden/cluster-structure/cluster_0.png"
    ) in markdown
    assert "attachment:step-1-cluster-qc-metrics.png" in markdown


def test_the_embed_budget_links_the_overflow_instead_of_dropping_it(tmp_path: Path) -> None:
    execution_id = "d" * 8
    relatives = [f"figure-{index}.png" for index in range(3)]
    _write_figures(tmp_path, execution_id, relatives)
    oversized = notebook._EMBED_TOTAL_BYTES + 1
    history = [
        _step(
            execution_id=execution_id,
            tool_name="plot_qc_distributions",
            skill_id="visualize-single-cell",
            files=[
                {"name": Path(r).stem, "relative_path": r, "media_type": "image/png",
                 "size_bytes": oversized}
                for r in relatives
            ],
        )
    ]

    document = _build(tmp_path, history)
    assert not any(cell.get("attachments") for cell in document["cells"])
    markdown = "\n".join(_sources(document, "markdown"))
    assert "3 further diagnostic figure(s)" in markdown


def test_a_missing_figure_file_is_linked_rather_than_crashing(tmp_path: Path) -> None:
    history = [
        _step(
            execution_id="e" * 8,
            tool_name="plot_embedding",
            skill_id="visualize-single-cell",
            files=[
                {"name": "gone", "relative_path": "gone.png", "media_type": "image/png",
                 "size_bytes": 10}
            ],
        )
    ]

    document = _build(tmp_path, history)
    assert not any(cell.get("attachments") for cell in document["cells"])
    assert "1 further diagnostic figure(s)" in "\n".join(_sources(document, "markdown"))


def test_an_in_progress_notebook_does_not_read_like_a_finished_result(tmp_path: Path) -> None:
    """The notebook is requestable mid-analysis, so it must say so rather than imply an ending."""

    history = [_step(execution_id="a" * 8, tool_name="cluster_single_cells", skill_id=_CLUSTERING)]

    in_progress = _sources(_build(tmp_path, history), "markdown")[0]
    assert "still in progress" in in_progress
    assert "snapshot of the work committed so far" in in_progress
    # Deliverables finalization has not produced yet must not be advertised.
    assert "final-annotated.h5ad" not in in_progress

    finalized = _sources(_build(tmp_path, history, finalized=True), "markdown")[0]
    assert "The analysis is finalized." in finalized
    assert "still in progress" not in finalized
    assert "`data/final-annotated.h5ad`" in finalized


def test_a_later_build_covers_the_steps_added_since(tmp_path: Path) -> None:
    """Rebuilding from provenance is what makes an evolving analysis stay representable."""

    first = [_step(execution_id="a" * 8, tool_name="cluster_single_cells", skill_id=_CLUSTERING)]
    second = first + [
        _step(execution_id="b" * 8, tool_name="investigate_batch", skill_id="batch-investigation"),
        _step(
            execution_id="c" * 8,
            tool_name="evaluate_marker_evidence",
            skill_id="marker-annotation",
        ),
    ]

    early = _build(tmp_path, first)
    later = _build(tmp_path, second)

    assert "records the 1 capability calls" in _sources(early, "markdown")[0]
    assert "records the 3 capability calls" in _sources(later, "markdown")[0]
    later_markdown = "\n".join(_sources(later, "markdown"))
    assert "## Batch investigation" in later_markdown
    assert "## Annotation" in later_markdown
    # No duplication: regeneration replaces rather than appends.
    assert later_markdown.count("### Step 1 · `cluster_single_cells`") == 1


def test_a_reduced_embed_budget_links_more_figures(tmp_path: Path) -> None:
    execution_id = "d" * 8
    _write_figures(tmp_path, execution_id, ["a.png", "b.png"])
    history = [
        _step(
            execution_id=execution_id,
            tool_name="plot_embedding",
            skill_id="visualize-single-cell",
            files=[
                {"name": name, "relative_path": f"{name}.png", "media_type": "image/png",
                 "size_bytes": 1000}
                for name in ("a", "b")
            ],
        )
    ]

    generous = _build(tmp_path, history, embed_budget_bytes=10_000)
    stingy = _build(tmp_path, history, embed_budget_bytes=1000)

    assert sum(len(c.get("attachments") or {}) for c in generous["cells"]) == 2
    assert sum(len(c.get("attachments") or {}) for c in stingy["cells"]) == 1
    assert "1 further diagnostic figure(s)" in "\n".join(_sources(stingy, "markdown"))


def test_the_intro_states_the_shape_and_that_cells_are_not_runnable(tmp_path: Path) -> None:
    document = _build(
        tmp_path,
        [_step(execution_id="a" * 8, tool_name="t", skill_id="s")],
        shape=(43559, 33694),
        n_clusters=29,
    )

    intro = _sources(document, "markdown")[0]
    assert "43,559 cells × 33,694 genes, 29 clusters" in intro
    assert "not a runnable pipeline" in intro
    # The stub turns shift-enter into an explanation instead of a NameError.
    stub = _sources(document, "code")[0]
    assert "def call(tool, **arguments):" in stub
    assert "NotImplementedError" in stub
    compile(stub, "stub.py", "exec")


def test_final_labels_and_caveats_close_the_notebook(tmp_path: Path) -> None:
    document = _build(
        tmp_path,
        [_step(execution_id="a" * 8, tool_name="t", skill_id="s")],
        label_rows=[
            {"cluster": "0", "cell_type": "AT2", "n_cells": 900, "confidence": "high",
             "rationale": "SFTPC and SFTPB positive."},
            {"cluster": "1", "cell_type": "T cell", "n_cells": 4100, "confidence": "medium",
             "rationale": "CD3D positive."},
        ],
        caveats=["Donor effects left uncorrected."],
    )

    markdown = "\n".join(_sources(document, "markdown"))
    assert "## Final labels" in markdown
    # Ordered by descending cell count, so the dominant population reads first.
    assert markdown.index("T cell") < markdown.index("AT2")
    assert "| 1 | T cell | 4,100 | medium |" in markdown
    assert "> CD3D positive." in markdown
    assert "## Caveats and limitations" in markdown
    assert "- Donor effects left uncorrected." in markdown


def test_a_pipe_in_a_label_cannot_break_the_table(tmp_path: Path) -> None:
    document = _build(
        tmp_path,
        [_step(execution_id="a" * 8, tool_name="t", skill_id="s")],
        label_rows=[
            {"cluster": "0", "cell_type": "AT1 | AT2 doublet", "n_cells": 5, "confidence": "low"}
        ],
    )

    row = next(
        line
        for line in "\n".join(_sources(document, "markdown")).splitlines()
        if "AT1" in line and line.startswith("|")
    )
    # Four columns means five delimiters; the escaped pipe must not add a sixth.
    assert row.replace("\\|", "") .count("|") == 5
    assert "AT1 \\| AT2 doublet" in row


def test_the_recipe_renders_as_importable_commented_python() -> None:
    """The recipe stays a .py, so it must not be JSON: true/false/null do not parse."""

    calls = [
        {
            "tool": "describe_dataset",
            "skill": "inspect-dataset",
            "skill_version": "0.2.0",
            "arguments": {"path": "/data/x.h5ad", "backed": True, "layer": None},
        },
        {
            "tool": "finalize_analysis",
            "skill": "finalize-analysis",
            "skill_version": "0.4.0",
            "arguments": {"labels": {"0": "AT2"}},
        },
    ]

    source = recipe.render_capability_recipe(calls)

    assert "\n" in source and source.count("\n") > 10
    assert "# 1. describe_dataset (inspect-dataset 0.2.0)" in source
    assert "# 2. finalize_analysis (finalize-analysis 0.4.0)" in source
    assert "true" not in source and "null" not in source
    namespace: dict[str, Any] = {}
    exec(compile(source, "analysis-recipe.py", "exec"), namespace)
    assert namespace["CAPABILITY_CALLS"] == calls
    assert ast.parse(source)


def test_the_recipe_survives_a_value_python_cannot_round_trip() -> None:
    source = recipe.render_capability_recipe(
        [{"tool": "t", "skill": "s", "skill_version": "1", "arguments": {"n": float("inf")}}]
    )

    namespace: dict[str, Any] = {}
    exec(compile(source, "analysis-recipe.py", "exec"), namespace)
    assert namespace["CAPABILITY_CALLS"][0]["arguments"]["n"] == float("inf")


def test_an_empty_history_still_produces_a_valid_document(tmp_path: Path) -> None:
    document = _build(tmp_path, [])

    assert document["cells"]
    assert "records the 0 capability calls" in _sources(document, "markdown")[0]
    assert recipe.render_capability_recipe([]).rstrip().endswith("]")


def _session(
    tmp_path: Path,
    records: list[dict[str, Any]],
    facts: dict[str, Any] | None = None,
) -> tuple[Any, Path, Path]:
    """Lay out the minimum durable session the entrypoint reads: state, events, artifacts."""

    session_dir = tmp_path / "session"
    (session_dir / "artifacts" / "capabilities").mkdir(parents=True)
    staging = tmp_path / "staging"
    staging.mkdir()
    artifacts = {
        record["execution_id"]: {
            "skill_id": record["skill_id"],
            "skill_version": record["skill_version"],
            "tool_name": record["tool_name"],
            "arguments": record["arguments"],
            "summary": record["summary"],
            "files": record["files"],
        }
        for record in records
    }
    (session_dir / "state.json").write_text(json.dumps({"artifacts": artifacts}))
    (session_dir / "events.jsonl").write_text(
        "".join(
            json.dumps(
                {
                    "kind": "capability.result_committed",
                    "sequence": index,
                    "payload": {"execution_id": record["execution_id"]},
                }
            )
            + "\n"
            for index, record in enumerate(records, start=1)
        )
    )
    context = SimpleNamespace(
        session_dir=session_dir, staging_dir=staging, state_facts=facts or {}
    )
    return context, session_dir, staging


def test_the_capability_needs_no_arguments_and_no_finalization(tmp_path: Path) -> None:
    """Requestable at any point: the whole point of not binding this to finalization."""

    context, _, staging = _session(
        tmp_path,
        [_step(execution_id="a" * 8, tool_name="cluster_single_cells", skill_id=_CLUSTERING)],
        facts={
            "dataset": {"path": "/data/Reyfman_all_raw.h5ad"},
            "analysis": {
                "dataset_revision": {"n_cells": 43559, "n_genes": 33694},
                "clustering": {"n_clusters": 29},
            },
        },
    )

    result = build.build_analysis_notebook({}, context)

    assert result["details"]["finalized"] is False
    assert result["details"]["steps"] == 1
    assert "Built an in-progress analysis notebook" in result["summary"]
    assert result["artifacts"][0]["media_type"] == "application/x-ipynb+json"
    document = json.loads((staging / "analysis-notebook.ipynb").read_text())
    intro = "".join(document["cells"][0]["source"])
    # Shape and cluster count come from durable facts, so no compute runtime is needed.
    assert "43,559 cells × 33,694 genes, 29 clusters" in intro
    assert "Reyfman_all_raw.h5ad" in intro


def test_the_capability_refuses_a_session_with_nothing_committed(tmp_path: Path) -> None:
    context, _, _ = _session(tmp_path, [])

    with pytest.raises(ValueError, match="no committed capability results yet"):
        build.build_analysis_notebook({}, context)


def test_committed_labels_are_read_from_csv_without_pandas(tmp_path: Path) -> None:
    execution_id = "fe7aeed8"
    context, session_dir, staging = _session(
        tmp_path,
        [_step(execution_id=execution_id, tool_name="finalize_analysis", skill_id=_FINALIZE)],
        facts={
            "finalization": {
                "status": "complete",
                "report_path": f"artifacts/capabilities/{execution_id}/analysis-report.md",
            }
        },
    )
    labels = session_dir / "artifacts" / "capabilities" / execution_id / "final-labels.csv"
    labels.parent.mkdir(parents=True, exist_ok=True)
    labels.write_text(
        "cluster,cell_type,n_cells,confidence,rationale\n"
        "0,AT2,900,high,SFTPC positive\n"
        "1,T cell,4100,medium,CD3D positive\n"
    )

    result = build.build_analysis_notebook({}, context)

    assert result["details"]["finalized"] is True
    assert "Built a finalized analysis notebook" in result["summary"]
    markdown = "\n".join(
        "".join(cell["source"])
        for cell in json.loads((staging / "analysis-notebook.ipynb").read_text())["cells"]
        if cell["cell_type"] == "markdown"
    )
    assert "| 1 | T cell | 4,100 | medium |" in markdown
    assert "> CD3D positive" in markdown


def test_a_labels_file_outside_the_session_is_refused(tmp_path: Path) -> None:
    """The report path comes from state; it must not be able to reach out of the session."""

    context, _, staging = _session(
        tmp_path,
        [_step(execution_id="a" * 8, tool_name="finalize_analysis", skill_id=_FINALIZE)],
        facts={
            "finalization": {
                "status": "complete",
                "report_path": "../../etc/analysis-report.md",
            }
        },
    )

    result = build.build_analysis_notebook({}, context)

    assert result["details"]["finalized"] is True
    markdown = (staging / "analysis-notebook.ipynb").read_text()
    assert "## Final labels" not in markdown


def test_a_custom_title_and_caveats_are_honored(tmp_path: Path) -> None:
    context, _, staging = _session(
        tmp_path, [_step(execution_id="a" * 8, tool_name="t", skill_id="s")]
    )

    build.build_analysis_notebook(
        {"title": "Lung atlas draft", "caveats": ["Donor effects uncorrected.", "  "]}, context
    )

    cells = json.loads((staging / "analysis-notebook.ipynb").read_text())["cells"]
    markdown = "\n".join("".join(c["source"]) for c in cells if c["cell_type"] == "markdown")
    assert markdown.startswith("# Lung atlas draft")
    assert "- Donor effects uncorrected." in markdown
    # A blank caveat is dropped rather than rendered as an empty bullet.
    assert "- \n" not in markdown


def test_the_entrypoint_writes_only_beneath_its_staging_directory(tmp_path: Path) -> None:
    context, session_dir, staging = _session(
        tmp_path, [_step(execution_id="a" * 8, tool_name="t", skill_id="s")]
    )
    before = sorted(path.name for path in session_dir.rglob("*"))

    build.build_analysis_notebook({}, context)

    assert sorted(path.name for path in staging.iterdir()) == ["analysis-notebook.ipynb"]
    assert sorted(path.name for path in session_dir.rglob("*")) == before


@pytest.mark.parametrize(
    ("skill_id", "expected"),
    [
        ("single-cell-qc", "Cell-level quality control"),
        ("batch-investigation", "Batch investigation"),
        ("scimilarity-annotation", "Annotation"),
        ("analysis-workspace", "Direct inspection"),
        ("finalize-analysis", "Finalization"),
    ],
)
def test_known_skills_map_to_reader_facing_phases(skill_id: str, expected: str) -> None:
    assert notebook._phase(skill_id) == expected
