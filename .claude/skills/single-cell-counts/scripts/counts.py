"""Resolve one raw-count representation without running downstream analysis."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


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


def _read(path: Path, sc: Any) -> Any:
    if path.is_dir():
        return sc.read_10x_mtx(path, var_names="gene_symbols", cache=False)
    if path.suffix.lower() == ".h5ad":
        return sc.read_h5ad(path)
    if path.suffix.lower() in {".h5", ".hdf5"}:
        return sc.read_10x_h5(path)
    raise ValueError("supported inputs are H5AD, 10x H5, or a 10x Matrix Market directory")


def _stored_values(matrix: Any) -> Any:
    import numpy as np

    if hasattr(matrix, "tocsr"):
        return np.asarray(matrix.tocsr().data)
    return np.asarray(matrix).ravel()


def _inspect_matrix(matrix: Any) -> dict[str, Any]:
    import numpy as np

    values = _stored_values(matrix)
    if values.size == 0:
        return {
            "count_like": True,
            "min": 0.0,
            "max": 0.0,
            "all_integer": True,
            "finite": True,
            "n_stored": 0,
        }
    finite = bool(np.all(np.isfinite(values)))
    nonnegative = bool(finite and np.all(values >= 0))
    all_integer = bool(finite and np.all(values == np.round(values)))
    return {
        "count_like": bool(finite and nonnegative and all_integer),
        "min": float(np.min(values)) if finite else None,
        "max": float(np.max(values)) if finite else None,
        "all_integer": all_integer,
        "finite": finite,
        "n_stored": int(values.size),
    }


def _choose_count_source(
    inspections: dict[str, dict[str, Any]],
    *,
    counts_source: str,
    counts_layer: str | None,
) -> tuple[str, str]:
    def require(label: str, absent: str) -> None:
        stats = inspections.get(label)
        if stats is None:
            raise ValueError(absent)
        if not stats["count_like"]:
            raise ValueError(
                f"{label} is not finite nonnegative integer counts "
                f"(min={stats['min']}, max={stats['max']}, all_integer={stats['all_integer']})"
            )

    if counts_source == "X":
        require("X", "X is unavailable")
        return "X", "explicit X selection validated as count-like"
    if counts_source == "raw":
        require("raw", "counts_source='raw' but no aligned count-like .raw is available")
        return "raw", "explicit .raw selection validated as count-like"
    if counts_source == "layer":
        if not counts_layer:
            raise ValueError("counts_source='layer' requires counts_layer")
        label = f"layer:{counts_layer}"
        require(label, f"counts layer {counts_layer!r} is absent")
        return label, f"explicit layer {counts_layer!r} selection validated as count-like"
    if counts_source != "auto":
        raise ValueError(f"unknown counts_source {counts_source!r}")
    if inspections.get("X", {}).get("count_like"):
        return "X", "X already holds count-like values"
    alternatives = sorted(
        label for label, stats in inspections.items() if label != "X" and stats["count_like"]
    )
    if len(alternatives) == 1:
        return alternatives[0], f"selected sole count-like alternative {alternatives[0]!r}"
    if not alternatives:
        raise ValueError(
            "no finite nonnegative integer count source was found in X, aligned .raw, or layers"
        )
    raise ValueError(
        "multiple count-like alternatives found ("
        + ", ".join(alternatives)
        + "); choose counts_source/counts_layer explicitly"
    )


def _candidate_sources(adata: Any) -> tuple[dict[str, Any], bool, bool]:
    candidates: dict[str, Any] = {"X": adata.X}
    for name in list(adata.layers.keys()):
        candidates[f"layer:{name}"] = adata.layers[name]
    raw_present = adata.raw is not None
    raw_usable = False
    if raw_present:
        raw_vars = set(map(str, adata.raw.var_names))
        if set(map(str, adata.var_names)).issubset(raw_vars):
            candidates["raw"] = adata.raw[:, adata.var_names].X
            raw_usable = True
    return candidates, raw_present, raw_usable


def run(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    import scanpy as sc

    path = Path(str(arguments["path"])).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(path)
    source = str(arguments.get("counts_source", "auto"))
    layer_arg = arguments.get("counts_layer")
    layer = str(layer_arg) if layer_arg is not None else None

    adata = _read(path, sc)
    adata.var_names_make_unique()
    if adata.n_obs == 0 or adata.n_vars == 0:
        raise ValueError("the input matrix has zero cells or zero genes")
    candidates, raw_present, raw_usable = _candidate_sources(adata)
    inspections = {name: _inspect_matrix(matrix) for name, matrix in candidates.items()}
    selected, reason = _choose_count_source(
        inspections, counts_source=source, counts_layer=layer
    )
    counts = candidates[selected].copy()
    adata.X = counts.copy()
    adata.layers["counts"] = counts.copy()

    cell_set_id = _identity("cells", sorted(map(str, adata.obs_names)))
    matrix_id = _count_matrix_identity(counts, adata.obs_names, adata.var_names)
    count_id = _identity(
        "count-representation",
        {"matrix_id": matrix_id, "cell_set_id": cell_set_id, "selected_source": selected},
    )
    revision_id = _identity(
        "dataset-revision",
        {"input_path": str(path), "cell_set_id": cell_set_id, "count_id": count_id},
    )
    metadata = dict(adata.uns.get("scagent_sdk", {}))
    metadata.update(
        {
            "schema_version": 1,
            "source_path": str(path),
            "dataset_revision_id": revision_id,
            "cell_set_id": cell_set_id,
            "count_representation_id": count_id,
            "count_matrix_id": matrix_id,
            "count_source": selected,
        }
    )
    adata.uns["scagent_sdk"] = metadata
    output_name = "counts-ready.h5ad"
    final_path = f"artifacts/capabilities/{context.execution_id}/{output_name}"
    adata.write_h5ad(context.staging_dir / output_name, compression="gzip")

    report = {
        "requested_source": source,
        "requested_layer": layer,
        "selected_source": selected,
        "selection_reason": reason,
        "raw_present": raw_present,
        "raw_usable": raw_usable,
        "inspections": inspections,
        "n_cells": int(adata.n_obs),
        "n_genes": int(adata.n_vars),
        "identities": {
            "dataset_revision_id": revision_id,
            "cell_set_id": cell_set_id,
            "count_representation_id": count_id,
            "count_matrix_id": matrix_id,
        },
    }
    (context.staging_dir / "count-source-selection.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return {
        "summary": (
            f"Materialized {selected} as raw counts for "
            f"{adata.n_obs:,} cells × {adata.n_vars:,} genes."
        ),
        "details": report,
        "facts_patch": {
            "analysis": {
                "dataset_revision": {
                    "id": revision_id,
                    "source_path": str(path),
                    "prepared_path": final_path,
                    "n_cells": int(adata.n_obs),
                    "n_genes": int(adata.n_vars),
                },
                "cell_set": {"id": cell_set_id, "n_cells": int(adata.n_obs)},
                "count_representation": {
                    "id": count_id,
                    "matrix_id": matrix_id,
                    "method": "validated-source-counts",
                    "count_source": selected,
                    "source_layer": "counts",
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
                "name": "count-ready-anndata",
                "relative_path": output_name,
                "media_type": "application/x-hdf5",
            },
            {
                "name": "count-source-selection",
                "relative_path": "count-source-selection.json",
                "media_type": "application/json",
            },
        ],
    }
