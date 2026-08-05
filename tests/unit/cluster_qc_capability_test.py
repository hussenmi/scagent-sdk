from __future__ import annotations

from pathlib import Path
from typing import Any

from scagent_sdk.capabilities.registry import CapabilityRegistry


def _package() -> Any:
    skills_root = Path(__file__).parents[2] / ".claude" / "skills"
    return next(
        package
        for package in CapabilityRegistry(skills_root).discover()
        if package.manifest.skill_id == "cluster-qc"
    )


def _handler() -> Any:
    package = _package()
    tool = next(tool for tool in package.manifest.tools if tool.name == "evaluate_cluster_qc")
    return package.load_handler(tool)


def _g(name: str) -> Any:
    return _handler().__globals__[name]


# --- manifest ----------------------------------------------------------------


def test_manifest_bumped_and_declares_three_axis_parameters() -> None:
    package = _package()
    assert package.manifest.version == "0.7.1"
    assert {tool.name for tool in package.manifest.tools} == {
        "evaluate_cluster_qc",
        "review_cluster_qc",
    }
    tool = next(tool for tool in package.manifest.tools if tool.name == "evaluate_cluster_qc")
    assert tool.environment == "gpu-singlecell"
    props = tool.input_schema["properties"]
    for key in (
        "n_structure_genes",
        "corr_threshold",
        "moran_min_cells",
        "max_heatmaps",
        "metric_warning_z",
        "metric_extreme_z",
        "auto_remove_convergent",
        "auto_remove_max_fraction",
    ):
        assert key in props, key
    assert tool.input_schema["additionalProperties"] is False


# --- gene classification -----------------------------------------------------


def test_gene_class_partitions_nuisance_broad_discriminating() -> None:
    gene_class = _g("gene_class")
    assert gene_class("MT-CO1") == "nuisance"
    assert gene_class("RPL13") == "nuisance"
    assert gene_class("MALAT1") == "nuisance"
    assert gene_class("HBB") == "nuisance"
    assert gene_class("HLA-DRA") == "broad"
    assert gene_class("GAPDH") == "broad"
    assert gene_class("MKI67") == "broad"
    assert gene_class("CD3D") == "discriminating"
    assert gene_class("LILRA4") == "discriminating"


# --- Axis A ------------------------------------------------------------------


def test_metric_single_signal_is_ambiguous_not_obvious() -> None:
    classify = _g("classify_metric_severity")
    result = classify({"mt_z": 2.5}, warning_z=2.0, extreme_z=3.0)
    assert result["severity"] == "ambiguous"


def test_metric_two_signals_is_obvious() -> None:
    classify = _g("classify_metric_severity")
    result = classify({"mt_z": 2.5, "lib_z": -2.4}, warning_z=2.0, extreme_z=3.0)
    assert result["severity"] == "obvious"
    assert "high_mitochondrial_fraction" in result["adverse_signals"]
    assert "low_library_size" in result["adverse_signals"]


def test_metric_one_extreme_signal_is_obvious() -> None:
    classify = _g("classify_metric_severity")
    result = classify({"lib_z": -3.5}, warning_z=2.0, extreme_z=3.0)
    assert result["severity"] == "obvious"
    assert "low_library_size" in result["extreme_signals"]


def test_metric_no_signal_is_clean() -> None:
    classify = _g("classify_metric_severity")
    assert (
        classify({"mt_z": 0.5, "lib_z": -0.2}, warning_z=2.0, extreme_z=3.0)["severity"] == "clean"
    )


# --- Axis B ------------------------------------------------------------------


def test_deg_identity_supported_with_discriminating_genes() -> None:
    classify = _g("classify_deg_identity")
    genes = [
        {"name": "CD3D", "frac_diff": 0.4, "significant": True},
        {"name": "TRAC", "frac_diff": 0.3, "significant": True},
    ]
    assert classify(genes)["verdict"] == "identity_supported"


def test_deg_junk_when_only_nuisance_and_broad() -> None:
    classify = _g("classify_deg_identity")
    genes = [
        {"name": "MT-CO1", "frac_diff": 0.5, "significant": True},
        {"name": "RPL13", "frac_diff": 0.5, "significant": True},
        {"name": "GAPDH", "frac_diff": 0.5, "significant": True},
    ]
    assert classify(genes)["verdict"] == "junk_markers"


def test_deg_inconclusive_with_one_discriminating() -> None:
    classify = _g("classify_deg_identity")
    genes = [{"name": "CD3D", "frac_diff": 0.4, "significant": True}]
    assert classify(genes)["verdict"] == "inconclusive"


def test_deg_empty_is_junk() -> None:
    assert _g("classify_deg_identity")([])["verdict"] == "junk_markers"


# --- Axis C ------------------------------------------------------------------


def test_structure_unstructured_below_thresholds() -> None:
    classify = _g("classify_structure")
    assert classify(0.05, 0.02, 500, 100, min_cells=15)["label"] == "unstructured"


def test_structure_strong_above_thresholds() -> None:
    classify = _g("classify_structure")
    assert classify(0.25, 0.4, 500, 100, min_cells=15)["label"] == "strong"


def test_structure_inconclusive_when_insufficient() -> None:
    classify = _g("classify_structure")
    assert classify(None, None, 5, 3, min_cells=15)["label"] == "inconclusive"
    assert classify(0.3, 0.5, 10, 100, min_cells=15)["label"] == "inconclusive"


# --- synthesis ---------------------------------------------------------------


def test_synthesis_all_three_agree_removes() -> None:
    synth = _g("synthesize_decision")
    assert synth("obvious", "junk_markers", "unstructured") == {
        "synthesis": "confirmed_junk",
        "action": "remove",
    }


def test_synthesis_metric_clean_junk_is_reviewed_not_removed() -> None:
    synth = _g("synthesize_decision")
    result = synth("clean", "junk_markers", "unstructured")
    assert result["synthesis"] == "unstructured_junk_markers"
    assert result["action"] == "review"


def test_synthesis_structured_identity_is_kept() -> None:
    synth = _g("synthesize_decision")
    assert synth("clean", "identity_supported", "strong")["action"] == "keep"


def test_synthesis_junk_but_structured_is_reviewed() -> None:
    synth = _g("synthesize_decision")
    result = synth("obvious", "junk_markers", "strong")
    assert result["synthesis"] == "junk_markers_but_structured"
    assert result["action"] == "review"


def test_synthesis_missing_axis_never_removes() -> None:
    synth = _g("synthesize_decision")
    # Adverse metric + junk DEG, but covariance could not be computed.
    assert synth("obvious", "junk_markers", "inconclusive")["action"] == "review"
    # Adverse metric + unstructured, but DEG axis inconclusive.
    assert synth("obvious", "inconclusive", "unstructured")["action"] == "review"


# --- cleanup selection -------------------------------------------------------


def test_cleanup_applies_below_fraction() -> None:
    select = _g("select_cleanup_set")
    decisions = [
        {"cluster": "3", "synthesis": "confirmed_junk"},
        {"cluster": "1", "synthesis": "structured_identity"},
    ]
    result = select(decisions, {"3": 100, "1": 900}, 1000, auto_remove=True, max_fraction=0.2)
    assert result["applied"] is True
    assert result["confirmed_junk"] == ["3"]
    assert result["removed_cells"] == 100


def test_cleanup_holds_at_or_above_fraction_boundary() -> None:
    select = _g("select_cleanup_set")
    decisions = [{"cluster": "3", "synthesis": "confirmed_junk"}]
    # Exactly at the bound is held, not applied (strict <).
    result = select(decisions, {"3": 200}, 1000, auto_remove=True, max_fraction=0.2)
    assert result["applied"] is False
    assert result["held_reason"]


def test_cleanup_disabled_holds_everything() -> None:
    select = _g("select_cleanup_set")
    decisions = [{"cluster": "3", "synthesis": "confirmed_junk"}]
    result = select(decisions, {"3": 10}, 1000, auto_remove=False, max_fraction=0.2)
    assert result["applied"] is False
    assert result["held_reason"] == "auto-removal disabled"


def test_cleanup_empty_when_nothing_confirmed() -> None:
    select = _g("select_cleanup_set")
    decisions = [{"cluster": "1", "synthesis": "structured_identity"}]
    result = select(decisions, {"1": 1000}, 1000, auto_remove=True, max_fraction=0.2)
    assert result["applied"] is False
    assert result["confirmed_junk"] == []
    assert result["held_reason"] is None


# --- cleanup lineage + invalidation (pure, no AnnData) -----------------------


def _ident() -> dict[str, str]:
    return {
        "clustering_id": "clustering:old",
        "cell_set_id": "cells:old",
        "representation_id": "rep:old",
        "count_representation_id": "counts:old",
    }


def _evidence() -> dict[str, Any]:
    return {"evidence_id": "cluster-qc-evidence:abc", "cleanup": {"removal_fraction": 0.1}}


def test_cleanup_identities_are_fresh_and_deterministic() -> None:
    make = _g("_cleanup_identities")
    ids = make(
        confirmed=["3"],
        n_removed=100,
        remaining_cell_names=["c1", "c2", "c3"],
        remaining_gene_names=["g1", "g2"],
        evidence=_evidence(),
        ident=_ident(),
    )
    # Fresh identities differ from every parent identity.
    assert ids["cell_set_id"] != _ident()["cell_set_id"]
    assert ids["count_representation_id"] != _ident()["count_representation_id"]
    assert ids["dataset_revision_id"] != _ident()["cell_set_id"]
    # Deterministic for the same inputs.
    again = make(
        confirmed=["3"],
        n_removed=100,
        remaining_cell_names=["c3", "c1", "c2"],  # order-insensitive
        remaining_gene_names=["g2", "g1"],
        evidence=_evidence(),
        ident=_ident(),
    )
    assert ids == again


def test_cleanup_facts_patch_invalidates_all_downstream() -> None:
    make_ids = _g("_cleanup_identities")
    make_patch = _g("_cleanup_facts_patch")
    ids = make_ids(
        confirmed=["3"],
        n_removed=100,
        remaining_cell_names=["c1", "c2"],
        remaining_gene_names=["g1", "g2"],
        evidence=_evidence(),
        ident=_ident(),
    )
    result = make_patch(
        ids=ids,
        ident=_ident(),
        evidence=_evidence(),
        source_path="/s/prepared.h5ad",
        final_path="artifacts/capabilities/x/cluster-qc-filtered-raw-counts.h5ad",
        dataset_abs_path="/s/artifacts/capabilities/x/cluster-qc-filtered-raw-counts.h5ad",
        n_obs=2,
        n_vars=2,
        size_bytes=123,
        modified_time_ns=456,
        fingerprint="sha256:def",
        confirmed=["3"],
        n_removed=100,
        removal_fraction=0.1,
    )
    facts = result["facts_patch"]
    analysis = facts["analysis"]
    # Fresh identities on the new revision; representation and clustering cleared.
    assert analysis["cell_set"]["id"] == ids["cell_set_id"]
    assert analysis["count_representation"]["id"] == ids["count_representation_id"]
    assert analysis["representation"] is None
    assert analysis["clustering"] is None
    # Every downstream fact is explicitly invalidated (merge-patch would otherwise keep them).
    for key in ("cell_qc", "doublets", "batch", "annotation", "finalization"):
        assert facts[key] is None, key
    # QC fact is cleanup_applied (NOT attested), so the current-QC floor stays unsatisfied.
    assert facts["cluster_qc"]["status"] == "cleanup_applied"
    # Downstream decisions cleared too.
    decisions = result["decisions_patch"]
    for key in ("doublet_handling", "batch_handling", "integration", "final_labels"):
        assert decisions[key] is None, key
    assert decisions["cluster_cleanup"]["removed_clusters"] == ["3"]


# --- cluster highlight grid --------------------------------------------------


def test_cluster_grid_widens_as_the_partition_gets_finer() -> None:
    layout = _g("_cluster_grid_layout")

    assert layout(4) == (1, 4)
    assert layout(20) == (4, 5)
    # The resolutions this skill adjudicates routinely exceed thirty clusters; a five-wide
    # grid would become a very tall strip.
    assert layout(60) == (10, 6)
    for count in range(1, 151):
        rows, columns = layout(count)
        assert rows * columns >= count


def test_cluster_grid_points_stay_visible_at_every_dataset_size() -> None:
    sizes = _g("_cluster_grid_point_sizes")

    small_background, small_foreground = sizes(1_000)
    large_background, large_foreground = sizes(1_000_000)

    assert small_foreground > small_background
    assert large_foreground > large_background
    assert large_background >= 0.4 and large_foreground >= 0.8


# --- artifact documentation --------------------------------------------------


def _documentation_for(
    decisions: list[dict[str, Any]], **evidence_overrides: Any
) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "n_clusters": len(decisions),
        "cleanup": {
            "confirmed_junk": [],
            "removed_cells": 0,
            "removal_fraction": 0.0,
            "applied": False,
            "held_reason": None,
        },
        "review_clusters": [],
        "doublet_signal_missing": False,
        "doublet_enriched_clusters": [],
        "effective_parameters": {"cluster_key": "leiden", "min_cluster_cells": 30},
    }
    evidence.update(evidence_overrides)
    return _g("_documentation")(
        output_prefix="cluster-qc/leiden",
        cluster_key="leiden",
        evidence=evidence,
        decisions=decisions,
        n_heatmaps=len(decisions),
        has_metric_figure=True,
        umap_names=["cluster-qc-umap", "cluster-qc-umap-grid"],
    )


def _decision(cluster: str, metric: str, deg: str, structure: str) -> dict[str, Any]:
    return {
        "cluster": cluster,
        "metric_severity": metric,
        "deg_verdict": deg,
        "structure": structure,
        "action": "keep",
    }


def test_documentation_names_the_clusters_each_axis_flagged() -> None:
    doc = _documentation_for(
        [
            _decision("0", "clean", "identity_supported", "strong"),
            _decision("4", "obvious", "identity_supported", "moderate"),
            _decision("11", "ambiguous", "junk_markers", "unstructured"),
            _decision("2", "clean", "junk_markers", "strong"),
        ]
    )
    findings = "\n".join(doc["findings"])

    # Findings must be about THIS dataset. A reader should not have to open four CSVs to learn
    # which cluster was flagged, and cluster order must read numerically, not "11" before "2".
    assert "4 clusters were checked in `leiden`." in findings
    assert "singled out: clusters 4 and 11." in findings
    assert "distinguishing genes — for: clusters 2 and 11." in findings
    assert "for: cluster 11." in findings
    assert doc["directory"] == "cluster-qc/leiden"
    assert doc["name"] == "cluster-qc-guide"


def test_axis_membership_uses_the_same_vocabulary_the_decision_rule_does() -> None:
    """A README that summarises the decision table must not disagree with it.

    `synthesize_decision` treats `weak` covariance as adverse and spells the DEG verdict
    `junk_markers`; matching on `junk`/`unstructured` alone silently reported "none" for clusters
    the same pass had just removed.
    """

    doc = _documentation_for(
        [
            _decision("1", "obvious", "junk_markers", "weak"),
            _decision("5", "clean", "identity_supported", "strong"),
        ]
    )
    findings = "\n".join(doc["findings"])

    assert "distinguishing genes — for: cluster 1." in findings
    assert "for: cluster 1." in findings


def test_a_cluster_list_cannot_be_misread_as_a_count() -> None:
    """Clusters are labelled from 0, so "decide: 0" must not read as "zero clusters"."""

    doc = _documentation_for(
        [_decision("0", "clean", "identity_supported", "weak")], review_clusters=["0"]
    )

    assert any("decide: cluster 0." in item for item in doc["findings"])


def test_documentation_says_plainly_when_nothing_was_removed() -> None:
    doc = _documentation_for([_decision("0", "clean", "identity_supported", "strong")])

    assert any("cell set is unchanged" in item for item in doc["findings"])
    assert not any("were removed here" in item for item in doc["findings"])


def test_documentation_separates_an_applied_removal_from_one_held_for_review() -> None:
    applied = _documentation_for(
        [_decision("3", "obvious", "junk_markers", "unstructured")],
        cleanup={
            "confirmed_junk": ["3"],
            "removed_cells": 412,
            "removal_fraction": 0.041,
            "applied": True,
            "held_reason": None,
        },
    )
    held = _documentation_for(
        [_decision("3", "obvious", "junk_markers", "unstructured")],
        cleanup={
            "confirmed_junk": ["3"],
            "removed_cells": 0,
            "removal_fraction": 0.62,
            "applied": False,
            "held_reason": "removal fraction 0.62 exceeds the 0.20 ceiling",
        },
    )

    assert any("412 cells, 4.1% of cells" in item for item in applied["findings"])
    assert any("but nothing was removed" in item for item in held["findings"])
    assert any("exceeds the 0.20 ceiling" in item for item in held["findings"])


def test_documentation_flags_a_missing_doublet_signal_as_reduced_evidence() -> None:
    doc = _documentation_for(
        [_decision("0", "clean", "identity_supported", "strong")], doublet_signal_missing=True
    )

    assert any("No doublet scores were available" in item for item in doc["findings"])


def test_documented_files_cover_every_artifact_the_pass_writes() -> None:
    doc = _documentation_for([_decision("0", "clean", "identity_supported", "strong")])
    documented = {entry["filename"] for entry in doc["files"]}

    assert documented == {
        "cluster-qc-decision-table.csv",
        "cluster-metric-evidence.csv",
        "cluster-deg-identity.csv",
        "cluster-structure-evidence.csv",
        "cluster-qc-evidence.json",
        "cluster-qc-metrics.png",
        "cluster-qc-umap.png",
        "cluster-qc-umap-grid.png",
        "cluster-structure/cluster_<id>_correlation.png",
    }
    # Every entry earns its place by explaining something, not by being listed.
    assert all(entry["purpose"].strip() for entry in doc["files"])


def test_figures_absent_from_a_pass_are_not_documented_as_present() -> None:
    doc = _g("_documentation")(
        output_prefix="cluster-qc/leiden",
        cluster_key="leiden",
        evidence={
            "n_clusters": 1,
            "cleanup": {
                "confirmed_junk": [],
                "removed_cells": 0,
                "removal_fraction": 0.0,
                "applied": False,
                "held_reason": None,
            },
            "review_clusters": [],
            "doublet_signal_missing": False,
            "doublet_enriched_clusters": [],
            "effective_parameters": {},
        },
        decisions=[_decision("0", "clean", "identity_supported", "strong")],
        n_heatmaps=0,
        has_metric_figure=False,
        umap_names=[],
    )
    documented = {entry["filename"] for entry in doc["files"]}

    assert not any(name.endswith(".png") for name in documented)


def test_documentation_renders_through_the_runtime_contract() -> None:
    from scagent_sdk.capabilities.documentation import ArtifactDocumentation, render_readme

    doc = _documentation_for([_decision("0", "clean", "identity_supported", "strong")])
    rendered = render_readme(ArtifactDocumentation.from_dict(doc))

    assert rendered.startswith("# leiden cluster check explained")
    assert "## What was done" in rendered
    assert "## What this found" in rendered
    assert "| min_cluster_cells | 30 |" in rendered
