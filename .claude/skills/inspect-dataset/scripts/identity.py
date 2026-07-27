"""Count-representation identity recipes, shared by gene conversion.

These reproduce the identities minted by ``single-cell-counts``. They live here because
``convert_gene_ids`` must re-mint them when it relabels the var axis: ``count_matrix_id`` hashes
``var_names``, so a relabel that leaves the recorded identity untouched makes the identity describe
a gene vocabulary the file no longer uses.

Skill packages cannot import each other -- each entrypoint only puts its own ``scripts`` directory
on ``sys.path`` -- so the recipes are duplicated here rather than imported. That duplication is
guarded by a deterministic drift test asserting byte-identical digests against
``single-cell-counts/scripts/counts.py``. Promoting both copies into one shared platform helper
needs a cross-skill sharing mechanism that does not exist yet; see
``docs/artifact-lineage-and-head-spec.md`` (D8).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def identity(kind: str, value: Any) -> str:
    """Hash a JSON-encodable descriptor into a ``<kind>:sha256:<hex>`` identity."""

    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return f"{kind}:sha256:{hashlib.sha256(encoded).hexdigest()}"


def count_matrix_identity(matrix: Any, obs_names: Any, var_names: Any) -> str:
    """Hash matrix values together with both axis labels.

    ``var_names`` participates, which is precisely why a gene relabel invalidates this identity.
    """

    import numpy as np

    try:
        from scipy import sparse
    except ImportError:  # control plane has numpy but not scipy; dense arrays still hash
        sparse = None  # type: ignore[assignment]

    digest = hashlib.sha256()
    digest.update(b"scagent-count-matrix-v1\0")
    digest.update(str(tuple(map(int, matrix.shape))).encode() + b"\0")
    if sparse is not None and sparse.issparse(matrix):
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


def count_representation_identity(
    matrix_id: str, cell_set_id: str, selected_source: str
) -> str:
    """Re-mint ``count_representation.id`` from its three inputs."""

    return identity(
        "count-representation",
        {"matrix_id": matrix_id, "cell_set_id": cell_set_id, "selected_source": selected_source},
    )


def dataset_revision_identity(input_path: str, cell_set_id: str, count_id: str) -> str:
    """Re-mint ``dataset_revision.id``, which derives from the count representation."""

    return identity(
        "dataset-revision",
        {"input_path": input_path, "cell_set_id": cell_set_id, "count_id": count_id},
    )
