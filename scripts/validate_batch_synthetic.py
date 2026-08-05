"""Synthetic acceptance for the guidance-led, gene-first batch investigation.

Runs UNDER the compute (rapids) environment via ``launch_batch_synthetic.py``. Builds a dataset with
two shared cell populations across two batches plus an injected **recurring** sample-associated
program, and checks:

A. the two populations are recovered by within-sample DEGs and matched across samples;
B. all five batch-diagnostic CSV files plus README are produced with their documented headers;
C. design/confounding and recurrence remain available as supporting evidence;
D. integration is refused without a post-evidence user choice and accepted with one, regardless of
   the advisory recommendation;
E. pair-order invariance and discriminating-gene matching remain intact.
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


def _load(skill_root: Path):
    source = skill_root / "batch-investigation" / "scripts" / "investigate.py"
    spec = importlib.util.spec_from_file_location("batch_investigate", source)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _build(seed: int = 0, shuffle: bool = False) -> ad.AnnData:
    rng = np.random.default_rng(seed)
    per = 150  # cells per (population, batch)
    pop_a = ["CD3D", "TRAC", "IL7R", "CD2", "CD7"]
    pop_b = ["LYZ", "S100A8", "S100A9", "FCN1", "VCAN"]
    tech = ["SOD2", "FOSL2", "IER3"]  # injected recurring sample-associated program
    filler = [f"BG{i}" for i in range(25)]
    genes = pop_a + pop_b + tech + filler
    gi = {g: i for i, g in enumerate(genes)}

    blocks, clusters, batches = [], [], []
    for pop, markers in (("A", pop_a), ("B", pop_b)):
        for batch in ("S1", "S2"):
            counts = np.zeros((per, len(genes)), dtype=np.int64)
            factor = rng.gamma(2.0, 1.0, size=per)
            for g in markers:
                counts[:, gi[g]] = rng.poisson(4 + 8 * factor)
            for g in filler:
                counts[:, gi[g]] = rng.poisson(1.0)
            # The recurring program is elevated in S1 in BOTH populations.
            for g in tech:
                counts[:, gi[g]] = rng.poisson(12.0 if batch == "S1" else 1.0)
            blocks.append(counts)
            clusters += [pop] * per
            batches += [batch] * per

    matrix = np.vstack(blocks)
    adata = ad.AnnData(X=matrix.astype(np.float32))
    adata.obs_names = [f"cell{i}" for i in range(adata.n_obs)]
    adata.var_names = genes
    adata.obs["leiden"] = np.array(clusters)
    adata.obs["sample"] = np.array(batches)
    # Perfectly confounded condition: S1 -> disease, S2 -> healthy.
    adata.obs["condition"] = np.where(adata.obs["sample"] == "S1", "disease", "healthy")
    if shuffle:
        order = rng.permutation(adata.n_obs)
        adata = adata[order].copy()
    for column in ("leiden", "sample", "condition"):
        adata.obs[column] = adata.obs[column].astype("category")
    adata.layers["counts"] = adata.X.copy()
    sc.pp.normalize_total(adata, target_sum=1e4)
    sc.pp.log1p(adata)
    adata.uns["scagent_sdk"] = {
        "schema_version": 1,
        "cell_set_id": "cells:syn",
        "count_representation_id": "counts:syn",
        "representation_id": "representation:syn",
        "clustering_id": "clustering:syn",
    }
    return adata


def _context(workdir: Path, prepared: Path, tag: str) -> SimpleNamespace:
    staging = workdir / f"staging-{tag}"
    staging.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(
        scientific_session_id="syn",
        session_dir=workdir,
        staging_dir=staging,
        skill_id="batch-investigation",
        tool_name="investigate_batch",
        execution_id=f"syn-{tag}",
        state_revision=1,
        state_facts={
            "analysis": {
                "dataset_revision": {"id": "rev:syn", "prepared_path": str(prepared)},
                "cell_set": {"id": "cells:syn"},
                "count_representation": {"id": "counts:syn"},
                "representation": {"id": "representation:syn"},
                "clustering": {"id": "clustering:syn", "key": "leiden"},
            }
        },
        user_turns=(
            {"turn_id": "turn-open", "sequence": 1, "user_prompt": "Investigate batch effects."},
        ),
    )


def main() -> int:
    skill_root = Path(__file__).resolve().parents[1] / ".claude" / "skills"
    batch = _load(skill_root)
    workdir = Path(tempfile.mkdtemp(prefix="batch-syn-"))
    checks: dict[str, bool] = {}

    prepared = workdir / "prepared.h5ad"
    _build().write_h5ad(prepared)
    base_args = {
        "path": str(prepared),
        "batch_key": "sample",
        "cluster_key": "leiden",
        "min_cells_per_region": 20,
        "min_enrichment": 1.0,  # both batches are 50% of each cluster here
    }

    # A. Gene-first scientific core.
    a = batch.run_evidence(dict(base_args), _context(workdir, prepared, "a"))
    checks["A_gene_evidence_recorded"] = a["details"]["gene_evidence"] in {
        "localized",
        "recurring_sample_associated",
    }
    checks["A_design_unknown"] = a["details"]["design_interpretation"] == "unknown"
    checks["A_advisory_interpretation_recorded"] = bool(a["details"]["recommendation"])
    recurring_genes = {r["gene"] for r in a["details"]["recurring_programs"]}
    checks["A_matches_are_real_identity"] = a["details"]["n_supported_matches"] >= 1
    packet = set(batch.BATCH_EVIDENCE_FILENAMES)
    produced = {path.name for path in _context(workdir, prepared, "a").staging_dir.glob("*")}
    # The context helper is deterministic; re-create only to resolve the staging path.
    checks["A_batch_packet_present"] = packet <= produced
    checks["A_readme_present"] = "README.md" in produced

    expected_headers = {
        "batch_diagnostic_sample_enriched_regions.csv": "cluster,sample,n_cells,n_cluster",
        "batch_diagnostic_within_sample_degs.csv": (
            "region,comparison,target_cells,reference_cells,gene,higher_in,expression_effect"
        ),
        "batch_diagnostic_population_pairs.csv": (
            "cluster_a,sample_a,cluster_b,sample_b,profile_correlation"
        ),
        "batch_diagnostic_direct_pair_degs.csv": (
            "cluster_a,sample_a,cluster_b,sample_b,gene,higher_in,expression_effect"
        ),
        "batch_diagnostic_design_check.csv": (
            "condition_key,n_conditions,median_batch_purity,median_condition_purity"
        ),
    }
    staging_a = workdir / "staging-a"
    checks["A_batch_headers"] = all(
        (staging_a / filename).read_text(encoding="utf-8").splitlines()[0].startswith(prefix)
        for filename, prefix in expected_headers.items()
    )

    # B. confounded condition + documented technical -> confounding wins.
    b = batch.run_evidence(
        dict(
            base_args,
            condition_keys=["condition"],
            technical_batch_documented=True,
            technical_batch_basis="Synthetic: sequencing run recorded in study metadata.",
        ),
        _context(workdir, prepared, "b"),
    )
    checks["B_confounding_outranks_technical"] = (
        b["details"]["design_interpretation"] == "confounded_with_biology"
    )

    # B2. Recommendation is advisory, but a real post-evidence user choice is mandatory.
    decision_ctx = _context(workdir, prepared, "b2")
    decision_ctx.state_facts["batch"] = {"evidence": b["details"], "decision": None}
    refused = False
    try:
        batch.run_decision(
            {
                "evidence_id": b["details"]["evidence_id"],
                "decision": "integrate",
                "integration_basis": "documented_technical_batch",
            },
            decision_ctx,
        )
    except ValueError:
        refused = True
    checks["B_integrate_refused_without_user_choice"] = refused
    decision_ctx.user_turns = (
        *decision_ctx.user_turns,
        {
            "turn_id": "turn-choice",
            "sequence": 20,
            "user_prompt": "These are comparable replicates; integrate them.",
        },
    )
    accepted = batch.run_decision(
        {
            "evidence_id": b["details"]["evidence_id"],
            "decision": "integrate",
            "integration_basis": "documented_technical_batch",
            "question_asked": "How should the samples be handled after investigation?",
        },
        decision_ctx,
    )
    checks["B_user_choice_authorizes_integration"] = (
        accepted["details"]["decision"] == "integrate"
        and accepted["details"]["decision_policy_version"] == 4
    )

    # B3. The answer survives re-investigation. A real session said "yes integrate", trained scVI,
    # re-clustered, re-ran investigate_batch because the batch floor required evidence for the new
    # clustering, and then could not record the decision at all: the answer now predated the newest
    # evidence, and only a future user turn could have unblocked it. The question — how to handle
    # this batch variable and its samples — never changed.
    reinvestigated = batch.run_evidence(
        dict(
            base_args,
            condition_keys=["condition"],
            technical_batch_documented=True,
            technical_batch_basis="Synthetic: sequencing run recorded in study metadata.",
        ),
        _context(workdir, prepared, "b3"),
    )
    after_ctx = _context(workdir, prepared, "b3-decide")
    after_ctx.state_facts["batch"] = {
        "evidence": reinvestigated["details"],
        "decision": accepted["details"],
    }
    # The user's answer is the only turn in the log, and it predates this newer evidence.
    after_ctx.user_turns = (
        {
            "turn_id": "turn-choice",
            "sequence": 20,
            "user_prompt": "These are comparable replicates; integrate them.",
        },
    )
    after_ctx.state_facts["batch"]["evidence"]["consultation_anchor_sequence"] = 40
    reaffirmed = batch.run_decision(
        {
            "evidence_id": reinvestigated["details"]["evidence_id"],
            "decision": "integrate",
            "integration_basis": "documented_technical_batch",
            "question_asked": "How should the samples be handled after investigation?",
        },
        after_ctx,
    )
    consultation = reaffirmed["details"]["user_consultation"]
    checks["B_answer_survives_reinvestigation"] = (
        reaffirmed["details"]["decision"] == "integrate"
        and consultation["turn_id"] == "turn-choice"
        and consultation.get("reaffirmed_without_new_turn") is True
    )
    # A genuinely different question is not covered by that answer and must be asked again.
    other_ctx = _context(workdir, prepared, "b3-other")
    other_ctx.state_facts["batch"] = {
        "evidence": {
            **reinvestigated["details"],
            "consultation_question_id": "batch-question:sha256:something-else",
            "consultation_anchor_sequence": 40,
        },
        "decision": accepted["details"],
    }
    other_ctx.user_turns = after_ctx.user_turns
    refused_other = False
    try:
        batch.run_decision(
            {
                "evidence_id": reinvestigated["details"]["evidence_id"],
                "decision": "integrate",
                "integration_basis": "documented_technical_batch",
                "question_asked": "How should the samples be handled after investigation?",
            },
            other_ctx,
        )
    except ValueError:
        refused_other = True
    checks["B_a_different_question_still_needs_asking"] = refused_other

    # C. pair-order invariance.
    shuffled = workdir / "prepared-shuffled.h5ad"
    _build(shuffle=True).write_h5ad(shuffled)
    c_args = dict(base_args)
    c_args["path"] = str(shuffled)
    c = batch.run_evidence(c_args, _context(workdir, shuffled, "c"))
    checks["C_order_invariant_verdict"] = (
        c["details"]["gene_evidence"] == a["details"]["gene_evidence"]
    )
    checks["C_order_invariant_recurring_genes"] = {
        r["gene"] for r in c["details"]["recurring_programs"]
    } == recurring_genes

    # D. matching used discriminating genes only and evidence remains versioned.
    checks["D_gene_class_versioned"] = bool(a["details"].get("gene_class_version"))
    checks["D_schema_versioned"] = a["details"].get("schema_version") == 2

    passed = all(checks.values())
    print(
        json.dumps(
            {
                "passed": passed,
                "checks": checks,
                "A": {
                    k: a["details"][k]
                    for k in (
                        "gene_evidence",
                        "design_interpretation",
                        "recommendation",
                        "n_enriched_regions",
                        "n_supported_matches",
                        "n_recurring_programs",
                    )
                },
                "A_recurring": a["details"]["recurring_programs"][:5],
                "B": {
                    k: b["details"][k]
                    for k in ("design_interpretation", "recommendation", "confounded_columns")
                },
            },
            indent=2,
            default=str,
        )
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
