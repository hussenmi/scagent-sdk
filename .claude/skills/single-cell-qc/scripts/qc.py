"""Flag-first single-cell QC and separately confirmed filtering."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

_SYMBOL_COLUMNS = (
    "feature_name",
    "gene_symbols",
    "gene_symbol",
    "gene_name",
    "gene_names",
    "mgi_symbol",
    "hgnc_symbol",
    "symbol",
)


def _identity(kind: str, value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return f"{kind}:sha256:{hashlib.sha256(encoded).hexdigest()}"


def _count_matrix_identity(matrix: Any, obs_names: Any, var_names: Any) -> str:
    import numpy as np
    from scipy import sparse

    digest = hashlib.sha256()
    digest.update(b"scagent-count-matrix-v1\0")
    digest.update(str(tuple(map(int, matrix.shape))).encode() + b"\0")
    if sparse.issparse(matrix):
        value = matrix.tocsr()
        arrays = (value.data, value.indices, value.indptr)
    else:
        arrays = (np.asarray(matrix),)
    for array in arrays:
        contiguous = np.ascontiguousarray(array)
        digest.update(str(contiguous.dtype).encode() + b"\0")
        digest.update(memoryview(contiguous).cast("B"))
    for names in (obs_names, var_names):
        for name in names:
            digest.update(str(name).encode("utf-8", errors="replace") + b"\0")
    return f"counts:sha256:{digest.hexdigest()}"


def _resolve_layer(adata: Any, layer: str | None) -> str | None:
    if layer == "auto":
        return "counts" if "counts" in adata.layers else None
    return layer


def _matrix(adata: Any, layer: str | None) -> Any:
    import numpy as np

    layer = _resolve_layer(adata, layer)
    if layer is None:
        matrix = adata.X
        label = "X"
    elif layer in adata.layers:
        matrix = adata.layers[layer]
        label = f"layer:{layer}"
    else:
        raise ValueError(f"count layer {layer!r} is absent; use counts_layer=null for X")
    values = np.asarray(matrix.tocsr().data if hasattr(matrix, "tocsr") else matrix).ravel()
    if values.size and (
        not bool(np.all(np.isfinite(values)))
        or not bool(np.all(values >= 0))
        or not bool(np.all(values == np.round(values)))
    ):
        raise ValueError(f"{label} is not finite nonnegative integer counts")
    return matrix


def _symbols(adata: Any) -> list[str]:
    original = [str(value) for value in adata.var_names]
    for column in _SYMBOL_COLUMNS:
        if column in adata.var:
            values = [str(value) for value in adata.var[column]]
            if sum(bool(value) and value.lower() != "nan" for value in values) > len(values) / 2:
                return values
    return original


def _add_metrics(
    adata: Any,
    *,
    layer: str | None,
    organism: str,
    min_genes: int | None,
    max_genes: int | None,
    max_pct_mito: float | None,
) -> tuple[Any, dict[str, int]]:
    import numpy as np
    import scanpy as sc

    layer = _resolve_layer(adata, layer)
    counts = _matrix(adata, layer)
    work = adata.copy()
    work.X = counts.copy()
    names = _symbols(work)
    if organism == "mouse":
        mt = [name.startswith(("mt-", "Mt-", "MT-")) for name in names]
    else:
        mt = [name.upper().startswith("MT-") for name in names]
    ribo = [name.upper().startswith(("RPS", "RPL")) for name in names]
    work.var["mt"] = np.asarray(mt, dtype=bool)
    work.var["ribo"] = np.asarray(ribo, dtype=bool)
    sc.pp.calculate_qc_metrics(
        work, qc_vars=["mt", "ribo"], percent_top=None, log1p=True, inplace=True
    )
    keep = np.ones(work.n_obs, dtype=bool)
    flags: dict[str, int] = {}
    if min_genes is not None:
        flag = np.asarray(work.obs["n_genes_by_counts"] < min_genes)
        work.obs["qc_flag_low_genes"] = flag
        flags["low_genes"] = int(flag.sum())
        keep &= ~flag
    if max_genes is not None:
        flag = np.asarray(work.obs["n_genes_by_counts"] > max_genes)
        work.obs["qc_flag_high_genes"] = flag
        flags["high_genes"] = int(flag.sum())
        keep &= ~flag
    if max_pct_mito is not None:
        flag = np.asarray(work.obs["pct_counts_mt"] > max_pct_mito)
        work.obs["qc_flag_high_mito"] = flag
        flags["high_mito"] = int(flag.sum())
        keep &= ~flag
    work.obs["qc_pass_requested_thresholds"] = keep
    flags["any_requested_flag"] = int((~keep).sum())
    return work, flags


def _render_qc_figures(
    adata: Any,
    context: Any,
    *,
    thresholds: dict[str, int | float | None],
) -> list[dict[str, str]]:
    import matplotlib.pyplot as plt
    import numpy as np

    output_dir = context.staging_dir / "qc"
    output_dir.mkdir(parents=True, exist_ok=True)

    def values(column: str) -> Any:
        result = np.asarray(adata.obs[column], dtype=float)
        return result[np.isfinite(result)]

    def save(fig: Any, name: str) -> dict[str, str]:
        relative = f"qc/{name}"
        fig.savefig(context.staging_dir / relative, dpi=160, bbox_inches="tight")
        plt.close(fig)
        return {
            "name": Path(name).stem.replace("_", "-"),
            "relative_path": relative,
            "media_type": "image/png",
        }

    figures: list[dict[str, str]] = []
    total = values("total_counts")
    genes = values("n_genes_by_counts")
    mito = values("pct_counts_mt")
    ribo = values("pct_counts_ribo")

    fig, axes = plt.subplots(1, 4, figsize=(20, 4.5))
    ranked = np.sort(total)[::-1]
    axes[0].plot(np.arange(1, ranked.size + 1), ranked)
    axes[0].set(xscale="log", yscale="log", xlabel="Cell rank", ylabel="Total counts")
    axes[0].set_title("UMI rank (knee)")
    axes[1].hist(total, bins=60)
    axes[1].set(xlabel="Total counts", ylabel="Cells", title="Library size")
    axes[2].hist(genes, bins=60)
    axes[2].set(xlabel="Genes detected", ylabel="Cells", title="Genes per cell")
    axes[3].hist(mito, bins=60)
    if thresholds["max_pct_mito"] is not None:
        axes[3].axvline(
            float(thresholds["max_pct_mito"]),
            color="crimson",
            linestyle="--",
            label=f"threshold {float(thresholds['max_pct_mito']):g}%",
        )
        axes[3].legend()
    axes[3].set(xlabel="Mitochondrial percent", ylabel="Cells", title="Mitochondrial content")
    fig.suptitle(f"Single-cell QC distributions ({adata.n_obs:,} cells)")
    fig.tight_layout()
    figures.append(save(fig, "qc_distributions.png"))

    violin_columns = [
        ("log1p_total_counts", "log1p total counts"),
        ("log1p_n_genes_by_counts", "log1p genes"),
        ("pct_counts_mt", "mitochondrial %"),
        ("pct_counts_ribo", "ribosomal %"),
    ]
    fig, axes = plt.subplots(1, len(violin_columns), figsize=(16, 4.5))
    for axis, (column, label) in zip(axes, violin_columns, strict=True):
        axis.violinplot(values(column), showmeans=True, showextrema=True)
        axis.set_xticks([])
        axis.set_ylabel(label)
    fig.suptitle("QC metric distributions")
    fig.tight_layout()
    figures.append(save(fig, "qc_violin_metrics.png"))

    fig, axes = plt.subplots(1, 2, figsize=(12, 4.5))
    axes[0].hist(total, bins=100)
    axes[0].set_xscale("log")
    axes[0].set(xlabel="Total counts (log scale)", ylabel="Cells", title="Library size")
    axes[1].hist(genes, bins=100)
    axes[1].set_xscale("log")
    axes[1].set(xlabel="Genes detected (log scale)", ylabel="Cells", title="Genes per cell")
    fig.tight_layout()
    figures.append(save(fig, "qc_histograms.png"))

    fig, axis = plt.subplots(figsize=(8, 4.5))
    axis.hist(mito, bins=100)
    if thresholds["max_pct_mito"] is not None:
        axis.axvline(
            float(thresholds["max_pct_mito"]),
            color="crimson",
            linestyle="--",
            label=f"threshold {float(thresholds['max_pct_mito']):g}%",
        )
        axis.legend()
    axis.set(xlabel="Mitochondrial percent", ylabel="Cells", title="Mitochondrial content")
    fig.tight_layout()
    figures.append(save(fig, "qc_mt_histogram.png"))

    fig, axes = plt.subplots(1, 2, figsize=(12, 5))
    axes[0].scatter(total, genes, s=8, alpha=0.55)
    axes[0].set(xscale="log", yscale="log", xlabel="Total counts", ylabel="Genes detected")
    axes[0].set_title("Genes versus counts")
    axes[1].scatter(total, mito, s=8, alpha=0.55)
    axes[1].set(xscale="log", xlabel="Total counts", ylabel="Mitochondrial percent")
    if thresholds["max_pct_mito"] is not None:
        axes[1].axhline(float(thresholds["max_pct_mito"]), color="crimson", linestyle="--")
    axes[1].set_title("Mitochondrial percent versus counts")
    fig.tight_layout()
    figures.append(save(fig, "qc_scatter.png"))

    fig, axis = plt.subplots(figsize=(6, 5))
    axis.scatter(mito, ribo, s=8, alpha=0.55)
    if thresholds["max_pct_mito"] is not None:
        axis.axvline(float(thresholds["max_pct_mito"]), color="crimson", linestyle="--")
    axis.set(
        xlabel="Mitochondrial percent",
        ylabel="Ribosomal percent",
        title="Ribosomal versus mitochondrial content",
    )
    fig.tight_layout()
    figures.append(save(fig, "qc_ribo_vs_mt.png"))
    return figures


def _base_metadata(adata: Any) -> dict[str, Any]:
    return dict(adata.uns.get("scagent_sdk", {}))


def _write_qc_artifacts(
    adata: Any, context: Any, *, output_name: str, report: dict[str, Any]
) -> list[dict[str, str]]:
    adata.write_h5ad(context.staging_dir / output_name, compression="gzip")
    adata.obs[
        [
            column
            for column in (
                "total_counts",
                "n_genes_by_counts",
                "pct_counts_mt",
                "pct_counts_ribo",
                "qc_flag_low_genes",
                "qc_flag_high_genes",
                "qc_flag_high_mito",
                "qc_pass_requested_thresholds",
            )
            if column in adata.obs
        ]
    ].to_csv(context.staging_dir / "cell-qc-metrics.csv")
    (context.staging_dir / "cell-qc-summary.json").write_text(
        json.dumps(report, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )
    return [
        {
            "name": "qc-anndata",
            "relative_path": output_name,
            "media_type": "application/x-hdf5",
        },
        {
            "name": "cell-qc-metrics",
            "relative_path": "cell-qc-metrics.csv",
            "media_type": "text/csv",
        },
        {
            "name": "cell-qc-summary",
            "relative_path": "cell-qc-summary.json",
            "media_type": "application/json",
        },
    ]


def calculate_qc(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    import scanpy as sc

    path = Path(str(arguments["path"])).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    layer_arg = arguments.get("counts_layer", "auto")
    layer = str(layer_arg) if layer_arg is not None else None
    organism = str(arguments.get("organism", "human"))
    min_arg = arguments.get("min_genes", 200)
    max_arg = arguments.get("max_genes")
    mito_arg = arguments.get("max_pct_mito", 20)
    thresholds = {
        "min_genes": int(min_arg) if min_arg is not None else None,
        "max_genes": int(max_arg) if max_arg is not None else None,
        "max_pct_mito": float(mito_arg) if mito_arg is not None else None,
    }
    source = sc.read_h5ad(path)
    layer = _resolve_layer(source, layer)
    adata, flags = _add_metrics(source, layer=layer, organism=organism, **thresholds)
    metadata = _base_metadata(adata)
    metadata["qc_assessment_id"] = _identity(
        "cell-qc",
        {
            "path": str(path),
            "cells": sorted(map(str, adata.obs_names)),
            "thresholds": thresholds,
            "flags": flags,
        },
    )
    adata.uns["scagent_sdk"] = metadata
    output_name = "qc-assessed.h5ad"
    final_path = f"artifacts/capabilities/{context.execution_id}/{output_name}"
    report = {
        "operation": "calculate_only",
        "organism": organism,
        "counts_source": "X" if layer is None else f"layer:{layer}",
        "n_cells": int(adata.n_obs),
        "n_genes": int(adata.n_vars),
        "thresholds": thresholds,
        "flag_counts": flags,
        "metric_summary": {
            "median_total_counts": float(adata.obs["total_counts"].median()),
            "median_genes_detected": float(adata.obs["n_genes_by_counts"].median()),
            "median_pct_mito": float(adata.obs["pct_counts_mt"].median()),
            "median_pct_ribo": float(adata.obs["pct_counts_ribo"].median()),
            "n_mitochondrial_genes": int(adata.var["mt"].sum()),
            "n_ribosomal_genes": int(adata.var["ribo"].sum()),
        },
        "mitochondrial_threshold_options": {
            str(value): {
                "threshold": value,
                "cells_flagged": int((adata.obs["pct_counts_mt"] > value).sum()),
                "fraction_flagged": float((adata.obs["pct_counts_mt"] > value).mean()),
            }
            for value in (5, 10, 15, 20, 25, 30)
        },
        "qc_assessment_id": metadata["qc_assessment_id"],
    }
    artifacts = _write_qc_artifacts(adata, context, output_name=output_name, report=report)
    figures = _render_qc_figures(adata, context, thresholds=thresholds)
    artifacts.extend(figures)
    provenance = dict(adata.uns.get("scagent_sdk", {}))
    required_visual_artifacts = [
        f"artifacts/capabilities/{context.execution_id}/{item['relative_path']}"
        for item in figures
    ]
    return {
        "summary": (
            f"Calculated QC for {adata.n_obs:,} cells; "
            f"{flags['any_requested_flag']:,} cells meet at least one requested flag."
        ),
        "details": report,
        "facts_patch": {
            "cell_qc": {
                "status": "assessed",
                "assessment_id": metadata["qc_assessment_id"],
                "artifact_path": final_path,
                "thresholds": thresholds,
                "flag_counts": flags,
                "cell_set_id": provenance.get("cell_set_id"),
                "count_representation_id": provenance.get("count_representation_id"),
                "review_status": "pending",
                "required_visual_artifacts": required_visual_artifacts,
            }
        },
        "artifacts": artifacts,
        "model_media": figures,
    }


def review_qc(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    facts = context.state_facts
    evidence = facts.get("cell_qc")
    if not isinstance(evidence, dict) or evidence.get("status") != "assessed":
        raise ValueError("current cell-QC assessment is absent; run calculate_single_cell_qc")
    assessment_id = str(arguments["assessment_id"])
    if assessment_id != evidence.get("assessment_id"):
        raise ValueError("assessment_id does not match the current cell-QC assessment")
    rationale = str(arguments["rationale"]).strip()
    if not rationale:
        raise ValueError("rationale must not be empty")
    reviewed = {str(value) for value in arguments.get("reviewed_artifacts", [])}
    required = {str(value) for value in evidence.get("required_visual_artifacts", [])}
    missing = sorted(required - reviewed)
    if missing:
        raise ValueError(
            "visual review is incomplete; inspect and include every required artifact: "
            + ", ".join(missing)
        )
    decision = str(arguments["decision"])
    resolved = decision in {"keep_all", "not_applicable"}
    review = {
        "status": "resolved" if resolved else "action_required",
        "assessment_id": assessment_id,
        "decision": decision,
        "rationale": rationale,
        "visual_findings": [str(value) for value in arguments.get("visual_findings", [])],
        "reviewed_artifacts": sorted(reviewed),
        "cell_set_id": evidence.get("cell_set_id"),
        "count_representation_id": evidence.get("count_representation_id"),
    }
    return {
        "summary": (
            f"Recorded cell-QC decision {decision!r}; "
            + ("QC review is resolved." if resolved else "follow-up action is required.")
        ),
        "details": review,
        "facts_patch": {"cell_qc": {"review": review}},
        "decisions_patch": {"cell_qc_handling": review},
    }


def filter_cells(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    import scanpy as sc

    if arguments.get("confirm_filtering") is not True:
        raise ValueError("confirm_filtering must be true before changing the cell set")
    path = Path(str(arguments["path"])).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    layer_arg = arguments.get("counts_layer", "auto")
    layer = str(layer_arg) if layer_arg is not None else None
    organism = str(arguments.get("organism", "human"))
    min_arg = arguments.get("min_genes", 200)
    max_arg = arguments.get("max_genes")
    mito_arg = arguments.get("max_pct_mito", 20)
    thresholds = {
        "min_genes": int(min_arg) if min_arg is not None else None,
        "max_genes": int(max_arg) if max_arg is not None else None,
        "max_pct_mito": float(mito_arg) if mito_arg is not None else None,
    }
    source = sc.read_h5ad(path)
    layer = _resolve_layer(source, layer)
    assessed, flags = _add_metrics(source, layer=layer, organism=organism, **thresholds)
    before = int(assessed.n_obs)
    filtered = assessed[assessed.obs["qc_pass_requested_thresholds"].to_numpy()].copy()
    if filtered.n_obs == 0:
        raise ValueError("requested thresholds would remove every cell")
    cell_set_id = _identity("cells", sorted(map(str, filtered.obs_names)))
    filtered_counts = _matrix(filtered, layer)
    matrix_id = _count_matrix_identity(
        filtered_counts, filtered.obs_names, filtered.var_names
    )
    count_id = _identity(
        "count-representation",
        {
            "matrix_id": matrix_id,
            "cell_set_id": cell_set_id,
            "operation": "filter-cells",
        },
    )
    metadata = _base_metadata(filtered)
    metadata.update(
        {
            "cell_set_id": cell_set_id,
            "count_representation_id": count_id,
            "count_matrix_id": matrix_id,
            "dataset_revision_id": _identity(
                "dataset-revision",
                {
                    "parent_path": str(path),
                    "cell_set_id": cell_set_id,
                    "count_representation_id": count_id,
                    "operation": "filter-cells",
                },
            ),
        }
    )
    for key in ("representation_id", "clustering_id", "qc_assessment_id"):
        metadata.pop(key, None)
    filtered.uns["scagent_sdk"] = metadata
    output_name = "cells-filtered.h5ad"
    final_path = f"artifacts/capabilities/{context.execution_id}/{output_name}"
    report = {
        "operation": "filter_cells",
        "before_cells": before,
        "after_cells": int(filtered.n_obs),
        "removed_cells": before - int(filtered.n_obs),
        "thresholds": thresholds,
        "flag_counts": flags,
        "cell_set_id": cell_set_id,
    }
    artifacts = _write_qc_artifacts(filtered, context, output_name=output_name, report=report)
    return {
        "summary": (
            f"Filtered {before - filtered.n_obs:,} of {before:,} cells; "
            f"{filtered.n_obs:,} remain."
        ),
        "details": report,
        "facts_patch": {
            "analysis": {
                "dataset_revision": {
                    "id": metadata["dataset_revision_id"],
                    "prepared_path": final_path,
                    "n_cells": int(filtered.n_obs),
                    "n_genes": int(filtered.n_vars),
                },
                "cell_set": {"id": cell_set_id, "n_cells": int(filtered.n_obs)},
                "count_representation": {
                    "id": count_id,
                    "matrix_id": matrix_id,
                    "method": "counts-after-cell-filter",
                    "source_layer": layer or "X",
                },
                "representation": None,
                "clustering": None,
            },
            "cell_qc": None,
            "doublets": None,
            "batch": None,
            "cluster_qc": None,
            "annotation": None,
            "finalization": None,
        },
        "artifacts": artifacts,
    }


def filter_genes(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    import numpy as np
    import scanpy as sc

    if arguments.get("confirm_filtering") is not True:
        raise ValueError("confirm_filtering must be true before changing the feature set")
    path = Path(str(arguments["path"])).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    layer_arg = arguments.get("counts_layer", "auto")
    layer = str(layer_arg) if layer_arg is not None else None
    min_cells = int(arguments.get("min_cells", 3))
    adata = sc.read_h5ad(path)
    layer = _resolve_layer(adata, layer)
    counts = _matrix(adata, layer)
    detected = np.asarray((counts > 0).sum(axis=0)).ravel()
    before = int(adata.n_vars)
    filtered = adata[:, detected >= min_cells].copy()
    if filtered.n_vars == 0:
        raise ValueError("requested threshold would remove every gene")
    metadata = _base_metadata(filtered)
    cell_set_id = metadata.get("cell_set_id") or _identity(
        "cells", sorted(map(str, filtered.obs_names))
    )
    filtered_counts = _matrix(filtered, layer)
    matrix_id = _count_matrix_identity(
        filtered_counts, filtered.obs_names, filtered.var_names
    )
    count_id = _identity(
        "count-representation",
        {
            "matrix_id": matrix_id,
            "cell_set_id": cell_set_id,
            "operation": "filter-genes",
            "min_cells": min_cells,
        },
    )
    revision_id = _identity(
        "dataset-revision",
        {
            "parent_path": str(path),
            "genes": sorted(map(str, filtered.var_names)),
            "count_representation_id": count_id,
            "operation": "filter-genes",
            "min_cells": min_cells,
        },
    )
    metadata.update(
        {
            "dataset_revision_id": revision_id,
            "cell_set_id": cell_set_id,
            "count_representation_id": count_id,
            "count_matrix_id": matrix_id,
        }
    )
    for key in (
        "representation_id",
        "clustering_id",
        "qc_assessment_id",
    ):
        metadata.pop(key, None)
    filtered.uns["scagent_sdk"] = metadata
    output_name = "genes-filtered.h5ad"
    final_path = f"artifacts/capabilities/{context.execution_id}/{output_name}"
    filtered.write_h5ad(context.staging_dir / output_name, compression="gzip")
    report = {
        "operation": "filter_genes",
        "before_genes": before,
        "after_genes": int(filtered.n_vars),
        "removed_genes": before - int(filtered.n_vars),
        "min_cells": min_cells,
    }
    (context.staging_dir / "gene-filter-summary.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return {
        "summary": (
            f"Filtered {before - filtered.n_vars:,} of {before:,} genes; "
            f"{filtered.n_vars:,} remain."
        ),
        "details": report,
        "facts_patch": {
            "analysis": {
                "dataset_revision": {
                    "id": revision_id,
                    "prepared_path": final_path,
                    "n_cells": int(filtered.n_obs),
                    "n_genes": int(filtered.n_vars),
                },
                "cell_set": {"id": cell_set_id, "n_cells": int(filtered.n_obs)},
                "count_representation": {
                    "id": count_id,
                    "matrix_id": matrix_id,
                    "method": "counts-after-gene-filter",
                    "source_layer": layer or "X",
                },
                "representation": None,
                "clustering": None,
            },
            "cell_qc": None,
            "batch": None,
            "cluster_qc": None,
            "annotation": None,
            "finalization": None,
        },
        "artifacts": [
            {
                "name": "gene-filtered-anndata",
                "relative_path": output_name,
                "media_type": "application/x-hdf5",
            },
            {
                "name": "gene-filter-summary",
                "relative_path": "gene-filter-summary.json",
                "media_type": "application/json",
            },
        ],
    }
