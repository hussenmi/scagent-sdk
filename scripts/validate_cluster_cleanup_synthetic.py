"""Synthetic convergent-junk acceptance for three-axis cluster QC cleanup.

Runs UNDER the compute (rapids) environment. Builds a dataset with one clean, structured,
identity-bearing cluster and one injected junk cluster (high mitochondrial fraction, only
MT/ribosomal markers, unstructured covariance), then runs the real ``evaluate.run`` and asserts the
end-to-end cleanup mutation: raw counts restored, junk cells filtered, fresh identities, embeddings
and clustering removed, and full downstream invalidation.

Launched by ``scripts/launch_cluster_cleanup_synthetic.py`` (agent venv), which resolves the
gpu-singlecell runtime and environment and subprocesses this file with the compute interpreter.
"""

from __future__ import annotations

import importlib.util
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import anndata as ad
import numpy as np
import scanpy as sc


def _load_run(skill_root: Path):
    source = skill_root / "cluster-qc" / "scripts" / "evaluate.py"
    spec = importlib.util.spec_from_file_location("cluster_qc_evaluate", source)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _build(seed: int = 0) -> ad.AnnData:
    rng = np.random.default_rng(seed)
    n_clean, n_junk = 1000, 150
    identity = ["CD3D", "TRAC", "IL7R", "LTB", "CD2", "CD7", "CD8A", "GZMK"]
    filler = [f"IDG{i}" for i in range(20)]
    mt = [f"MT-{g}" for g in ("CO1", "CO2", "ND1", "ND2", "ATP6", "CYB", "ND4", "ND5")]
    ribo = [f"RPL{i}" for i in range(8)] + [f"RPS{i}" for i in range(8)]
    genes = identity + filler + mt + ribo
    gi = {g: i for i, g in enumerate(genes)}
    counts = np.zeros((n_clean + n_junk, len(genes)), dtype=np.int64)

    # Clean cluster: correlated identity program (shared latent factor) + moderate housekeeping.
    factor = rng.gamma(2.0, 1.0, size=n_clean)
    for g in identity:
        counts[:n_clean, gi[g]] = rng.poisson(3 + 6 * factor)
    for g in filler:
        counts[:n_clean, gi[g]] = rng.poisson(1 + 2 * factor)
    for g in mt:
        counts[:n_clean, gi[g]] = rng.poisson(0.4)  # low MT
    for g in ribo:
        counts[:n_clean, gi[g]] = rng.poisson(2.0)

    # Junk cluster: high MT, high ribo, and identity/filler genes drawn as INDEPENDENT moderate
    # noise (no shared factor) so their gene-gene covariance is unstructured.
    for g in mt:
        counts[n_clean:, gi[g]] = rng.poisson(20)  # dominant MT -> high MT fraction
    for g in ribo:
        counts[n_clean:, gi[g]] = rng.poisson(10)
    for g in identity + filler:
        counts[n_clean:, gi[g]] = rng.poisson(2.0)  # independent -> unstructured covariance

    adata = ad.AnnData(X=counts.astype(np.float32))
    adata.obs_names = [f"cell{i}" for i in range(adata.n_obs)]
    adata.var_names = genes
    adata.obs["leiden"] = np.array(["0"] * n_clean + ["1"] * n_junk)
    adata.obs["leiden"] = adata.obs["leiden"].astype("category")
    adata.var["mt"] = [g.startswith("MT-") for g in genes]
    sc.pp.calculate_qc_metrics(adata, qc_vars=["mt"], percent_top=None, log1p=False, inplace=True)
    adata.layers["counts"] = adata.X.copy()
    sc.pp.normalize_total(adata, target_sum=1e4)
    sc.pp.log1p(adata)
    adata.var["highly_variable"] = [g in set(identity + filler) for g in genes]
    sc.pp.pca(adata, n_comps=10, random_state=seed)
    sc.pp.neighbors(adata, n_neighbors=15, random_state=seed)
    sc.tl.rank_genes_groups(adata, "leiden", method="wilcoxon", pts=True)
    adata.uns["scagent_sdk"] = {
        "schema_version": 1,
        "source_path": "synthetic",
        "dataset_revision_id": "dataset-revision:syn",
        "cell_set_id": "cells:syn",
        "count_representation_id": "counts:syn",
        "count_matrix_id": "count-matrix:syn",
        "representation_id": "representation:syn",
        "clustering_id": "clustering:syn",
    }
    return adata


def main() -> int:
    skill_root = Path(__file__).resolve().parents[1] / ".claude" / "skills"
    evaluate = _load_run(skill_root)
    workdir = Path(tempfile.mkdtemp(prefix="cluster-cleanup-syn-"))
    staging = workdir / "staging"
    staging.mkdir(parents=True)
    prepared = workdir / "prepared.h5ad"
    adata = _build()
    adata.write_h5ad(prepared)

    context = SimpleNamespace(
        scientific_session_id="syn",
        session_dir=workdir,
        staging_dir=staging,
        skill_id="cluster-qc",
        tool_name="evaluate_cluster_qc",
        execution_id="synexec",
        state_revision=1,
        state_facts={
            "analysis": {
                "dataset_revision": {"id": "dataset-revision:syn", "prepared_path": str(prepared)},
                "cell_set": {"id": "cells:syn"},
                "count_representation": {"id": "counts:syn"},
                "representation": {"id": "representation:syn"},
                "clustering": {"id": "clustering:syn", "key": "leiden"},
            }
        },
    )
    result = evaluate.run(
        {"path": str(prepared), "auto_remove_convergent": True, "auto_remove_max_fraction": 0.2},
        context,
    )

    facts = result["facts_patch"]
    details = result["details"]
    analysis = facts.get("analysis", {})
    diagnostic = {
        "summary": result["summary"],
        "cleanup": details["cleanup"],
        "moran_global": details["moran_global"],
        "moran_skipped": details.get("moran_skipped"),
        "decision_table": [
            {
                k: r[k]
                for k in ("cluster", "metric_severity", "deg_verdict", "structure", "synthesis")
            }
            for r in details["decision_table"]
        ],
        "structure_evidence": [
            {k: r.get(k) for k in ("cluster", "n_structure_genes", "mean_abs_corr", "structure")}
            for r in details.get("structure_evidence", [])
        ],
    }

    checks: dict[str, bool] = {}
    checks["cleanup_applied"] = facts.get("cluster_qc", {}).get("status") == "cleanup_applied"
    checks["junk_cluster_confirmed"] = details["cleanup"]["confirmed_junk"] == ["1"]
    checks["representation_cleared"] = analysis.get("representation", "x") is None
    checks["clustering_cleared"] = analysis.get("clustering", "x") is None
    checks["doublets_invalidated"] = facts.get("doublets", "x") is None
    checks["downstream_invalidated"] = all(
        facts.get(k, "x") is None for k in ("cell_qc", "batch", "annotation", "finalization")
    )
    checks["fresh_cell_set_id"] = analysis.get("cell_set", {}).get("id") not in (None, "cells:syn")

    filtered_file = staging / "cluster-qc-filtered-raw-counts.h5ad"
    if filtered_file.is_file():
        filtered = ad.read_h5ad(filtered_file)
        xmat = filtered.X.toarray() if hasattr(filtered.X, "toarray") else np.asarray(filtered.X)
        checks["junk_cells_removed"] = int(filtered.n_obs) == 1000
        checks["raw_counts_restored"] = (
            bool(np.all(xmat == np.round(xmat))) and float(xmat.max()) > 1.5
        )
        checks["counts_layer_present"] = "counts" in filtered.layers
        checks["embeddings_removed"] = len(filtered.obsm) == 0 and len(filtered.obsp) == 0
        checks["clustering_removed"] = "leiden" not in filtered.obs
        checks["new_identity_on_disk"] = filtered.uns["scagent_sdk"]["cell_set_id"] != "cells:syn"
    else:
        checks["filtered_artifact_written"] = False

    passed = all(checks.values())
    print(
        json.dumps(
            {"passed": passed, "checks": checks, "diagnostic": diagnostic}, indent=2, default=str
        )
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
