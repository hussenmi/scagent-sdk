"""Composable live-test fixture construction from focused single-cell capabilities.

This is test harness code, not a user-facing umbrella capability. Each operation remains a
separate executor call and produces a committed artifact that can be inspected independently.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from scagent_sdk.capabilities.registry import SkillPackage

Execute = Callable[
    [Any, SkillPackage, str, dict[str, Any]],
    Awaitable[dict[str, Any]],
]


def _package(packages: tuple[SkillPackage, ...], skill_id: str) -> SkillPackage:
    return next(package for package in packages if package.manifest.skill_id == skill_id)


def _artifact(session_dir: Path, envelope: dict[str, Any], filename: str) -> Path:
    return session_dir / str(envelope["artifact_path"]) / filename


async def build_standard_fixture(
    *,
    execute: Execute,
    executor: Any,
    packages: tuple[SkillPackage, ...],
    session_dir: Path,
    source: Path,
    counts_source: str = "auto",
    random_seed: int = 0,
) -> tuple[dict[str, Any], Path]:
    counts = await execute(
        executor,
        _package(packages, "single-cell-counts"),
        "materialize_count_matrix",
        {"path": str(source), "counts_source": counts_source},
    )
    current = _artifact(session_dir, counts, "counts-ready.h5ad")
    cells = await execute(
        executor,
        _package(packages, "single-cell-qc"),
        "filter_single_cells",
        {
            "path": str(current),
            "confirm_filtering": True,
            "counts_layer": "counts",
            "min_genes": 200,
            "max_pct_mito": 20,
        },
    )
    current = _artifact(session_dir, cells, "cells-filtered.h5ad")
    genes = await execute(
        executor,
        _package(packages, "single-cell-qc"),
        "filter_single_cell_genes",
        {
            "path": str(current),
            "confirm_filtering": True,
            "counts_layer": "counts",
            "min_cells": 3,
        },
    )
    current = _artifact(session_dir, genes, "genes-filtered.h5ad")
    normalized = await execute(
        executor,
        _package(packages, "expression-preprocessing"),
        "normalize_single_cell_expression",
        {"path": str(current), "counts_layer": "counts", "target_sum": 10000},
    )
    current = _artifact(session_dir, normalized, "log-normalized.h5ad")
    hvg = await execute(
        executor,
        _package(packages, "expression-preprocessing"),
        "select_highly_variable_genes",
        {"path": str(current), "n_top_genes": 3000, "flavor": "seurat"},
    )
    current = _artifact(session_dir, hvg, "hvg-selected.h5ad")
    pca = await execute(
        executor,
        _package(packages, "dimensionality-reduction"),
        "compute_single_cell_pca",
        {
            "path": str(current),
            "n_components": 50,
            "use_highly_variable": True,
            "random_seed": random_seed,
        },
    )
    current = _artifact(session_dir, pca, "pca.h5ad")
    neighbors = await execute(
        executor,
        _package(packages, "dimensionality-reduction"),
        "build_single_cell_neighbors",
        {
            "path": str(current),
            "representation_key": "X_pca",
            "neighbors_key": "neighbors",
            "n_neighbors": 15,
            "random_seed": random_seed,
        },
    )
    current = _artifact(session_dir, neighbors, "neighbors.h5ad")
    umap = await execute(
        executor,
        _package(packages, "dimensionality-reduction"),
        "compute_single_cell_umap",
        {
            "path": str(current),
            "neighbors_key": "neighbors",
            "umap_key": "X_umap",
            "random_seed": random_seed,
        },
    )
    current = _artifact(session_dir, umap, "umap.h5ad")
    clustered = await execute(
        executor,
        _package(packages, "single-cell-clustering"),
        "cluster_single_cells",
        {
            "path": str(current),
            "neighbors_key": "neighbors",
            "cluster_key": "leiden",
            "resolution": 0.8,
            "random_seed": random_seed,
        },
    )
    return clustered, _artifact(session_dir, clustered, "clustered.h5ad")
