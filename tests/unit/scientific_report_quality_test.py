from __future__ import annotations

from pathlib import Path
from typing import Any

from scagent_sdk.capabilities.registry import CapabilityRegistry


def _globals(skill_id: str, tool_name: str) -> dict[str, Any]:
    skills_root = Path(__file__).parents[2] / ".claude" / "skills"
    package = next(
        package
        for package in CapabilityRegistry(skills_root).discover()
        if package.manifest.skill_id == skill_id
    )
    tool = next(tool for tool in package.manifest.tools if tool.name == tool_name)
    return package.load_handler(tool).__globals__


def test_dataset_report_explains_findings_and_next_decisions() -> None:
    render = _globals("inspect-dataset", "describe_dataset")["_render_markdown"]
    sheet = {
        "path": "/data/example.h5ad",
        "backed": True,
        "shape": {"n_obs": 1000, "n_vars": 20000},
        "X": {
            "all_integer_sample": True,
            "has_negative_sample": False,
            "dtype": "float32",
        },
        "layers": [],
        "raw": {"present": False, "n_vars": 0},
        "obsm_keys": [],
        "uns_keys": [],
        "obs_columns": {
            "donor": {
                "dtype": "category",
                "n_unique": 4,
                "top_values": [
                    {"value": "D1", "count": 250},
                    {"value": "D2", "count": 250},
                ],
            }
        },
        "gene_symbols": {
            "symbol_source": "var_names",
            "mt_genes": 13,
            "ribo_genes": 90,
        },
        "var_names_examples": ["MALAT1", "CD3D"],
        "matrix_signals": {
            "x_all_integer_sample": True,
            "x_has_negative_sample": False,
            "log1p_in_uns": False,
        },
    }

    report = render(sheet)

    assert "## Summary" in report
    assert "`X` looks like raw counts" in report
    assert "`donor` (4 groups)" in report
    assert "## What this means for the analysis" in report
    assert "whether those samples should be investigated" in report
    assert "dataset-contents.json" in report


def test_doublet_reports_separate_evidence_from_the_decision(tmp_path: Path) -> None:
    globals_ = _globals("doublet-evidence", "evaluate_doublet_evidence")
    evidence_path = tmp_path / "doublet-evidence.md"
    globals_["_write_report"](
        evidence_path,
        batch_key="donor",
        count_source="layers/counts",
        parameters={
            "expected_doublet_rate": 0.06,
            "random_seed": 0,
            "n_prin_comps_used": 30,
        },
        rows=[
            {
                "library": "D1",
                "n_cells": 600,
                "predicted_doublets": 12,
                "predicted_rate": 0.02,
                "threshold": 0.2,
            },
            {
                "library": "D2",
                "n_cells": 400,
                "predicted_doublets": 20,
                "predicted_rate": 0.05,
                "threshold": 0.25,
            },
        ],
        warnings=[],
        has_umap=False,
    )
    evidence = evidence_path.read_text(encoding="utf-8")
    assert "32 of 1,000 cells (3.20%)" in evidence
    assert "candidates for review, not confirmed cells to remove" in evidence
    assert "## Interpretation and recommendation" in evidence
    assert "Do not remove cells from these calls alone" in evidence
    assert "| `D1` | 600 | 12 | 2.00% | 0.2 |" in evidence
    assert "doublet-score-distribution.png" in evidence

    review_path = tmp_path / "doublet-review.md"
    globals_["_write_review_report"](
        review_path,
        decision="retain_for_cluster_review",
        rationale="The calls need cluster and marker context.",
        n_cells=1000,
        n_predicted=32,
        output_path=None,
    )
    review = review_path.read_text(encoding="utf-8")
    assert "Retain the cells and review the calls by cluster" in review
    assert "No cells were removed" in review
    assert "expected rate is method context, not a target" in review
