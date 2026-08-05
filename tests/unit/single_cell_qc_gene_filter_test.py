"""Deterministic tests for the dataset-size-aware gene filter thresholds and class masks.

These exercise the pure decision logic only; the module imports numpy/scanpy lazily inside the
capability functions, so it loads in the control-plane venv without a compute runtime. Whether the
resulting gene set is scientifically right on a given dataset is a separate (live) question.
"""

from __future__ import annotations

import importlib.util
import math
from pathlib import Path
from typing import Any

import pytest

_MODULE_PATH = (
    Path(__file__).parents[2] / ".claude" / "skills" / "single-cell-qc" / "scripts" / "qc.py"
)


def _module() -> Any:
    spec = importlib.util.spec_from_file_location("single_cell_qc_under_test", _MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


Q = _module()


@pytest.mark.parametrize(
    "n_cells,expected,basis,capped",
    [
        # Small datasets: 2% of cells, rounded up, below the cap.
        (500, math.ceil(0.02 * 500), "fraction_of_cells", False),  # 10
        (1000, 20, "fraction_of_cells", False),
        (4999, math.ceil(0.02 * 4999), "fraction_of_cells", False),  # 100 (ceil of 99.98)
        # Large datasets: the 100-cell cap takes over, shrinking the effective fraction.
        (43559, 100, "fraction_of_cells_capped", True),
        (1_000_000, 100, "fraction_of_cells_capped", True),
    ],
)
def test_fraction_threshold_scales_then_caps(
    n_cells: int, expected: int, basis: str, capped: bool
) -> None:
    threshold, resolved_basis, was_capped = Q.low_detection_threshold(
        n_cells, min_cells=None, min_cell_fraction=0.02, max_cells=100
    )
    assert threshold == expected
    assert resolved_basis == basis
    assert was_capped is capped


def test_absolute_override_wins_over_fraction() -> None:
    threshold, basis, capped = Q.low_detection_threshold(
        50_000, min_cells=3, min_cell_fraction=0.02, max_cells=100
    )
    assert threshold == 3
    assert basis == "absolute_min_cells"
    assert capped is False


def test_zero_fraction_disables_the_filter() -> None:
    threshold, basis, _ = Q.low_detection_threshold(
        10_000, min_cells=None, min_cell_fraction=0.0, max_cells=100
    )
    assert threshold == 0
    assert basis == "disabled"


def test_uncapped_when_cap_is_zero() -> None:
    # max_cells=0 means "no cap": the raw fraction governs even a large dataset.
    threshold, basis, capped = Q.low_detection_threshold(
        43559, min_cells=None, min_cell_fraction=0.02, max_cells=0
    )
    assert threshold == math.ceil(0.02 * 43559)
    assert basis == "fraction_of_cells"
    assert capped is False


@pytest.mark.parametrize(
    "symbol,ribo",
    [
        ("RPS6", True),
        ("RPL13A", True),
        ("MRPS12", True),  # mitochondrial-ribosomal is in the removal set
        ("MRPL44", True),
        ("rps6", True),  # case-insensitive (mouse-style symbols)
        ("ACTB", False),
        ("MT-CO1", False),  # mitochondrial, not ribosomal
        ("RPTOR", False),  # not a ribosomal-protein gene despite the RP prefix
    ],
)
def test_ribosomal_classification(symbol: str, ribo: bool) -> None:
    assert Q.is_ribosomal_gene(symbol) is ribo


@pytest.mark.parametrize(
    "symbol,mito",
    [
        ("MT-CO1", True),
        ("MT-ND2", True),
        ("mt-Co1", True),  # mouse casing
        ("MTATP6", False),  # no hyphen: not the MT- mitochondrial-encoded prefix
        ("RPS6", False),
        ("MRPL44", False),  # mitochondrial-ribosomal is ribosomal, not counted as MT here
    ],
)
def test_mitochondrial_classification(symbol: str, mito: bool) -> None:
    assert Q.is_mitochondrial_gene(symbol) is mito


# --------------------------------------------------------------------------------------------------
# Carry-forward across a gene-only change: gene filtering preserves the cell set, so a current
# cell-QC review and complete doublet evidence stay valid and are re-stamped, not invalidated.
# --------------------------------------------------------------------------------------------------


def _cell_qc(cell_set: str, count_rep: str, decision: str = "keep_all") -> dict[str, Any]:
    return {
        "status": "assessed",
        "assessment_id": "qc-1",
        "cell_set_id": cell_set,
        "count_representation_id": count_rep,
        "review": {
            "status": "resolved",
            "assessment_id": "qc-1",
            "cell_set_id": cell_set,
            "count_representation_id": count_rep,
            "decision": decision,
        },
    }


def test_cell_qc_carried_restamps_both_levels_and_marks_it() -> None:
    carried = Q.carry_cell_qc_through_gene_filter(
        _cell_qc("cells-A", "count-OLD"),
        cell_set_id="cells-A",
        parent_count_representation_id="count-OLD",
        new_count_representation_id="count-NEW",
    )
    assert carried is not None
    # Both the evidence and the nested review point at the filtered count representation, so the
    # floor (which checks both) passes; the cell set is untouched.
    assert carried["count_representation_id"] == "count-NEW"
    assert carried["review"]["count_representation_id"] == "count-NEW"
    assert carried["cell_set_id"] == "cells-A"
    assert carried["review"]["decision"] == "keep_all"
    assert carried["carried_through"] == ["gene_filter"]


def test_cell_qc_not_carried_for_different_cells() -> None:
    assert (
        Q.carry_cell_qc_through_gene_filter(
            _cell_qc("cells-A", "count-OLD"),
            cell_set_id="cells-DIFFERENT",
            parent_count_representation_id="count-OLD",
            new_count_representation_id="count-NEW",
        )
        is None
    )


def test_cell_qc_not_carried_when_stale_for_the_input_artifact() -> None:
    # The review was current for count-OLD, but the artifact being filtered is on a different count
    # representation — the review was already stale, so it must not be silently re-validated.
    assert (
        Q.carry_cell_qc_through_gene_filter(
            _cell_qc("cells-A", "count-OLD"),
            cell_set_id="cells-A",
            parent_count_representation_id="count-SOMETHING-ELSE",
            new_count_representation_id="count-NEW",
        )
        is None
    )


def test_cell_qc_carries_on_cell_match_when_parent_id_unavailable() -> None:
    carried = Q.carry_cell_qc_through_gene_filter(
        _cell_qc("cells-A", "count-OLD"),
        cell_set_id="cells-A",
        parent_count_representation_id=None,
        new_count_representation_id="count-NEW",
    )
    assert carried is not None and carried["count_representation_id"] == "count-NEW"


def test_cell_qc_none_input_returns_none() -> None:
    assert (
        Q.carry_cell_qc_through_gene_filter(
            None, cell_set_id="cells-A", parent_count_representation_id=None,
            new_count_representation_id="count-NEW",
        )
        is None
    )


def test_carry_does_not_mutate_the_input_fact() -> None:
    original = _cell_qc("cells-A", "count-OLD")
    Q.carry_cell_qc_through_gene_filter(
        original, cell_set_id="cells-A", parent_count_representation_id="count-OLD",
        new_count_representation_id="count-NEW",
    )
    assert original["count_representation_id"] == "count-OLD"
    assert original["review"]["count_representation_id"] == "count-OLD"
    assert "carried_through" not in original


def _doublets(cell_set: str, count_rep: str) -> dict[str, Any]:
    return {
        "evidence": {
            "status": "complete",
            "evidence_id": "dbl-1",
            "cell_set_id": cell_set,
            "count_representation_id": count_rep,
            "annotated_path": "/x/doublet-annotated.h5ad",
        },
        "review": None,
    }


def test_doublets_carried_restamps_evidence_and_marks_it() -> None:
    carried = Q.carry_doublets_through_gene_filter(
        _doublets("cells-A", "count-OLD"),
        cell_set_id="cells-A",
        parent_count_representation_id="count-OLD",
        new_count_representation_id="count-NEW",
    )
    assert carried is not None
    assert carried["evidence"]["count_representation_id"] == "count-NEW"
    # The annotated-path provenance is preserved; the scores are the same per-cell calls.
    assert carried["evidence"]["annotated_path"] == "/x/doublet-annotated.h5ad"
    assert carried["evidence"]["carried_through"] == ["gene_filter"]


def test_doublets_not_carried_for_different_cells_or_missing_evidence() -> None:
    assert (
        Q.carry_doublets_through_gene_filter(
            _doublets("cells-A", "count-OLD"),
            cell_set_id="cells-DIFFERENT",
            parent_count_representation_id="count-OLD",
            new_count_representation_id="count-NEW",
        )
        is None
    )
    assert (
        Q.carry_doublets_through_gene_filter(
            {"review": None}, cell_set_id="cells-A", parent_count_representation_id=None,
            new_count_representation_id="count-NEW",
        )
        is None
    )
