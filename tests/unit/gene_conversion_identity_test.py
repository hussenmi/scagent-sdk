"""Gene relabelling must re-mint the count identities it invalidates.

``count_matrix_id`` hashes ``var_names`` and ``dataset_revision.id`` derives from the count
representation, so converting Ensembl IDs to symbols after counts were materialized leaves both
identities describing a vocabulary the file no longer uses. These tests pin the re-mint, the
invalidation patch, and the recipe parity between the two skills that own the recipe.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import numpy as np

_SKILLS = Path(__file__).parents[2] / ".claude" / "skills"
_IDENTITY_PATH = _SKILLS / "inspect-dataset" / "scripts" / "identity.py"
_COUNTS_PATH = _SKILLS / "single-cell-counts" / "scripts" / "counts.py"


def _load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _identity_module() -> Any:
    return _load(_IDENTITY_PATH, "scagent_identity_under_test")


def _counts_module() -> Any:
    return _load(_COUNTS_PATH, "scagent_counts_under_test")


def _convert_module() -> Any:
    return _load(_SKILLS / "inspect-dataset" / "scripts" / "convert.py", "scagent_convert_test")


def _matrix() -> Any:
    return np.arange(12, dtype="int32").reshape(3, 4)


# --- recipe parity -------------------------------------------------------------------


def test_count_matrix_identity_matches_the_counts_skill_recipe() -> None:
    """Drift guard: the two copies of the recipe must agree byte for byte.

    ``convert_gene_ids`` cannot import ``single-cell-counts`` -- skill entrypoints only put their
    own scripts directory on ``sys.path`` -- so the recipe is duplicated. If either copy changes,
    identities minted by conversion stop matching identities minted by counts, and this fails.
    """

    identity = _identity_module()
    counts = _counts_module()
    matrix, obs, var = _matrix(), ["c1", "c2", "c3"], ["ENSG1", "ENSG2", "ENSG3", "ENSG4"]

    assert identity.count_matrix_identity(matrix, obs, var) == counts._count_matrix_identity(
        matrix, obs, var
    )


def test_derived_identity_recipes_match_the_counts_skill() -> None:
    identity = _identity_module()
    counts = _counts_module()

    count_id = identity.count_representation_identity("counts:sha256:aa", "cells:sha256:bb", "X")
    assert count_id == counts._identity(
        "count-representation",
        {"matrix_id": "counts:sha256:aa", "cell_set_id": "cells:sha256:bb", "selected_source": "X"},
    )
    assert identity.dataset_revision_identity("/in.h5ad", "cells:sha256:bb", count_id) == (
        counts._identity(
            "dataset-revision",
            {"input_path": "/in.h5ad", "cell_set_id": "cells:sha256:bb", "count_id": count_id},
        )
    )


def test_relabelling_genes_changes_the_count_matrix_identity() -> None:
    """The premise of the whole fix: var_names participate in the hash."""

    identity = _identity_module()
    matrix, obs = _matrix(), ["c1", "c2", "c3"]

    ensembl = identity.count_matrix_identity(matrix, obs, ["ENSG1", "ENSG2", "ENSG3", "ENSG4"])
    symbols = identity.count_matrix_identity(matrix, obs, ["CD3D", "CD8A", "MS4A1", "LYZ"])
    assert ensembl != symbols


# --- re-mint behaviour ---------------------------------------------------------------


class _FakeAnnData:
    def __init__(self, matrix: Any, obs_names: list[str], var_names: list[str]):
        self.layers = {"counts": matrix}
        self.obs_names = obs_names
        self.var_names = var_names
        self.uns: dict[str, Any] = {}


def _facts(**overrides: Any) -> dict[str, Any]:
    analysis: dict[str, Any] = {
        "cell_set": {"id": "cells:sha256:bb", "n_cells": 3},
        "count_representation": {"id": "count-representation:sha256:old", "count_source": "X"},
        "dataset_revision": {"id": "dataset-revision:sha256:old", "source_path": "/in.h5ad"},
    }
    analysis.update(overrides)
    return {"analysis": analysis}


def test_remint_is_skipped_when_no_count_representation_exists() -> None:
    """The common ordering: conversion runs before counts are materialized."""

    convert = _convert_module()
    adata = _FakeAnnData(_matrix(), ["c1", "c2", "c3"], ["CD3D", "CD8A", "MS4A1", "LYZ"])

    assert convert._remint_after_relabel(adata, Path("/in.h5ad"), {}) is None
    assert convert._remint_after_relabel(adata, Path("/in.h5ad"), {"analysis": {}}) is None


def test_remint_produces_new_identities_and_the_invalidation_patch() -> None:
    convert = _convert_module()
    identity = _identity_module()
    matrix, obs, var = _matrix(), ["c1", "c2", "c3"], ["CD3D", "CD8A", "MS4A1", "LYZ"]
    adata = _FakeAnnData(matrix, obs, var)

    outcome = convert._remint_after_relabel(adata, Path("/other.h5ad"), _facts())
    assert outcome is not None
    fragment, uns_updates = outcome

    expected_matrix = identity.count_matrix_identity(matrix, obs, var)
    expected_count = identity.count_representation_identity(
        expected_matrix, "cells:sha256:bb", "X"
    )
    expected_revision = identity.dataset_revision_identity(
        "/in.h5ad", "cells:sha256:bb", expected_count
    )

    assert fragment["analysis"]["count_representation"] == {
        "id": expected_count,
        "matrix_id": expected_matrix,
    }
    assert fragment["analysis"]["dataset_revision"] == {"id": expected_revision}
    assert fragment["analysis"]["count_representation"]["id"] != "count-representation:sha256:old"

    # Annotation evidence is scored against gene symbols, but current_annotation_evidence keys only
    # on clustering_id -- which a relabel does not change. Without explicit clearing, stale
    # annotation evidence would remain "current" and permit finalization.
    assert fragment["annotation"] is None
    assert fragment["finalization"] is None
    assert fragment["reference_runs"] is None

    assert uns_updates == {
        "count_representation_id": expected_count,
        "count_matrix_id": expected_matrix,
        "dataset_revision_id": expected_revision,
    }


def test_remint_uses_the_recorded_source_path_not_the_conversion_input() -> None:
    """dataset_revision.id must stay derivable; the input path is the original source."""

    convert = _convert_module()
    adata = _FakeAnnData(_matrix(), ["c1", "c2", "c3"], ["CD3D", "CD8A", "MS4A1", "LYZ"])

    from_recorded = convert._remint_after_relabel(adata, Path("/unrelated.h5ad"), _facts())
    from_other = convert._remint_after_relabel(
        adata,
        Path("/unrelated.h5ad"),
        _facts(dataset_revision={"id": "x", "source_path": "/different.h5ad"}),
    )
    assert from_recorded is not None and from_other is not None
    assert from_recorded[1]["dataset_revision_id"] != from_other[1]["dataset_revision_id"]


def test_remint_requires_a_counts_layer() -> None:
    convert = _convert_module()
    adata = _FakeAnnData(_matrix(), ["c1"], ["CD3D"])
    adata.layers = {}

    assert convert._remint_after_relabel(adata, Path("/in.h5ad"), _facts()) is None
