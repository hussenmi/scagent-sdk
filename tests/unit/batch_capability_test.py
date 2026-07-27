from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scagent_sdk.capabilities.registry import CapabilityRegistry


def _package() -> Any:
    skills_root = Path(__file__).parents[2] / ".claude" / "skills"
    return next(
        package
        for package in CapabilityRegistry(skills_root).discover()
        if package.manifest.skill_id == "batch-investigation"
    )


def _handler(name: str) -> Any:
    package = _package()
    tool = next(tool for tool in package.manifest.tools if tool.name == name)
    return package.load_handler(tool)


def _g(name: str) -> Any:
    return _handler("investigate_batch").__globals__[name]


# --- manifest ----------------------------------------------------------------


def test_manifest_splits_evidence_and_decision_tools() -> None:
    package = _package()
    assert package.manifest.version == "0.5.0"
    names = {tool.name for tool in package.manifest.tools}
    assert names == {"investigate_batch", "decide_batch_handling"}
    evidence = next(t for t in package.manifest.tools if t.name == "investigate_batch")
    decision = next(t for t in package.manifest.tools if t.name == "decide_batch_handling")
    # Evidence tool records no decision.
    assert "decision" not in evidence.input_schema["properties"]
    assert evidence.entrypoint.endswith(":run_evidence")
    assert set(evidence.input_schema["required"]) == {"path", "batch_key"}
    props = evidence.input_schema["properties"]
    # The misleading diffxpy no-op is gone; Wilcoxon is the declared primary method.
    assert "prefer_diffxpy" not in props
    # Cost and match quality are explicitly bounded.
    assert props["max_candidate_pairs"]["maximum"] == 500
    assert "min_match_jaccard" in props
    # Decision tool consumes an evidence id and is floored on current evidence.
    assert decision.floors == ("current_batch_evidence",)
    assert decision.entrypoint.endswith(":run_decision")
    assert set(decision.input_schema["properties"]["decision"]["enum"]) == {
        "keep_uncorrected",
        "integrate",
        "separate",
        "request_guidance",
        "not_applicable",
    }


# --- gene-first pure helpers -------------------------------------------------


def test_region_enrichment_catches_modest_purity_high_enrichment() -> None:
    enrich = _g("region_enrichment")
    # 42% of a cluster is one sample that is only 9% of the dataset.
    assert enrich(42, 100, 0.09) == pytest.approx(4.666, abs=1e-2)
    assert enrich(0, 0, 0.5) == 0.0
    assert enrich(10, 100, 0.0) == 0.0


def test_gene_class_separates_broad_stress_from_discriminating() -> None:
    gene_class = _g("gene_class")
    # The live false-match culprits are broad ER/stress, not identity genes.
    assert gene_class("DERL3") == "broad"
    assert gene_class("HSP90B1") == "broad"
    assert gene_class("H13") == "broad"
    assert gene_class("HLA-DRA") == "broad"
    assert gene_class("MT-CO1") == "nuisance"
    assert gene_class("RPL13") == "nuisance"
    assert gene_class("CD3D") == "discriminating"


def test_match_rejects_broad_stress_only_overlap() -> None:
    """The live cluster-19/cluster-9 false match (DERL3/H13/HSP90B1) must not be supported."""
    match = _g("match_regions")
    result = match(
        ["DERL3", "H13", "HSP90B1", "IGKC"],
        ["DERL3", "H13", "HSP90B1", "COL1A1"],
        min_shared=3,
        min_jaccard=0.15,
    )
    assert result["supported"] is False
    assert result["shared"] == 0
    assert "discriminating" in result["reason"]


def test_match_supported_on_real_shared_identity() -> None:
    match = _g("match_regions")
    result = match(
        ["CD3D", "TRAC", "IL7R", "CD2"],
        ["CD3D", "TRAC", "IL7R", "CD7"],
        min_shared=3,
        min_jaccard=0.15,
    )
    assert result["supported"] is True
    assert result["shared"] == 3
    assert result["jaccard"] > 0.15


def test_match_rejected_with_reason_when_jaccard_too_low() -> None:
    match = _g("match_regions")
    disc_a = ["CD3D", "TRAC", "IL7R"] + [f"GENE{i}" for i in range(40)]
    result = match(disc_a, ["CD3D", "TRAC", "IL7R"], min_shared=3, min_jaccard=0.5)
    assert result["supported"] is False
    assert "jaccard" in result["reason"]


def test_recurrence_is_order_invariant_and_needs_two_populations() -> None:
    summarize = _g("summarize_recurrence")
    rows = [
        {"gene": "SOD2", "higher_in_batch": "S1", "population": 1},
        {"gene": "SOD2", "higher_in_batch": "S1", "population": 2},
        {"gene": "ONCE", "higher_in_batch": "S1", "population": 1},
    ]
    forward = summarize(rows)
    reverse = summarize(list(reversed(rows)))
    assert forward == reverse
    assert [r["gene"] for r in forward] == ["SOD2"]
    assert forward[0]["n_populations"] == 2


def test_recurrence_same_gene_opposite_batches_does_not_recur() -> None:
    summarize = _g("summarize_recurrence")
    rows = [
        {"gene": "SOD2", "higher_in_batch": "S1", "population": 1},
        {"gene": "SOD2", "higher_in_batch": "S2", "population": 2},
    ]
    assert summarize(rows) == []


def test_classify_gene_evidence_axis() -> None:
    classify = _g("classify_gene_evidence")
    assert classify(0, 0) == "none"
    assert classify(3, 1) == "localized"
    assert classify(3, 2) == "recurring_sample_associated"


def test_confounding_outranks_documented_technical() -> None:
    """Perfect biological confounding is never silently reclassified as technical."""
    classify = _g("classify_design")
    assert (
        classify(
            confounded_columns=["disease"], technical_documented=True, has_orthogonal_condition=True
        )
        == "confounded_with_biology"
    )


def test_classify_design_axis_priority() -> None:
    classify = _g("classify_design")
    assert (
        classify(confounded_columns=[], technical_documented=True, has_orthogonal_condition=True)
        == "documented_technical_batch"
    )
    assert (
        classify(
            confounded_columns=["disease"],
            technical_documented=False,
            has_orthogonal_condition=False,
        )
        == "confounded_with_biology"
    )
    assert (
        classify(confounded_columns=[], technical_documented=False, has_orthogonal_condition=True)
        == "orthogonal_but_not_known_technical"
    )
    assert (
        classify(confounded_columns=[], technical_documented=False, has_orthogonal_condition=False)
        == "unknown"
    )


def test_recommendation_matrix() -> None:
    recommend = _g("recommend")
    assert (
        recommend("none", "documented_technical_batch")
        == "do_not_integrate_based_on_current_evidence"
    )
    assert (
        recommend("localized", "documented_technical_batch")
        == "do_not_integrate_based_on_current_evidence"
    )
    assert (
        recommend("recurring_sample_associated", "unknown")
        == "cannot_determine_technical_vs_biological"
    )
    assert (
        recommend("recurring_sample_associated", "confounded_with_biology")
        == "cannot_determine_technical_vs_biological"
    )
    assert (
        recommend("recurring_sample_associated", "orthogonal_but_not_known_technical")
        == "integration_optional_for_confirmed_replicates"
    )
    assert (
        recommend("recurring_sample_associated", "documented_technical_batch")
        == "integration_supported"
    )


# --- decision gating ---------------------------------------------------------


def test_non_integration_decisions_always_allowed() -> None:
    validate = _g("validate_decision")
    for decision in ("keep_uncorrected", "separate", "request_guidance", "not_applicable"):
        assert (
            validate(decision, "cannot_determine_technical_vs_biological", None, None)["ok"] is True
        )


def test_integrate_requires_explicit_basis() -> None:
    validate = _g("validate_decision")
    assert validate("integrate", "integration_supported", None, None)["ok"] is False


def test_integrate_allowed_when_recommendation_supports_it() -> None:
    validate = _g("validate_decision")
    assert (
        validate("integrate", "integration_supported", "documented_technical_batch", None)["ok"]
        is True
    )


def test_integrate_against_evidence_requires_override_warning() -> None:
    validate = _g("validate_decision")
    blocked = validate(
        "integrate",
        "cannot_determine_technical_vs_biological",
        "user_authorized_comparable_replicates",
        None,
    )
    assert blocked["ok"] is False
    allowed = validate(
        "integrate",
        "cannot_determine_technical_vs_biological",
        "user_authorized_comparable_replicates",
        "User confirmed the samples are technical replicates of one biological condition.",
    )
    assert allowed["ok"] is True


def test_integration_optional_needs_replicate_basis() -> None:
    validate = _g("validate_decision")
    # A documented-technical basis does not by itself clear the optional-replicates recommendation
    # without an override; the replicate basis does.
    assert (
        validate(
            "integrate",
            "integration_optional_for_confirmed_replicates",
            "user_authorized_comparable_replicates",
            None,
        )["ok"]
        is True
    )


# --- integration basis must be backed by the evidence ------------------------


def test_documented_technical_basis_requires_documented_evidence() -> None:
    check = _g("validate_integration_basis")
    # Evidence never recorded a documented technical batch.
    assert check("documented_technical_batch", {})["ok"] is False
    assert (
        check("documented_technical_batch", {"technical_batch_documented": True})["ok"] is False
    )  # missing basis
    assert (
        check(
            "documented_technical_batch",
            {"technical_batch_documented": True, "technical_batch_basis": "   "},
        )["ok"]
        is False
    )  # blank basis
    assert (
        check(
            "documented_technical_batch",
            {
                "technical_batch_documented": True,
                "technical_batch_basis": "10x run recorded in metadata",
            },
        )["ok"]
        is True
    )


def test_replicate_basis_needs_no_evidence_documentation() -> None:
    check = _g("validate_integration_basis")
    assert check("user_authorized_comparable_replicates", {})["ok"] is True
    assert check(None, {})["ok"] is True


def test_decision_policy_version_is_declared() -> None:
    assert _g("DECISION_POLICY_VERSION") == 1


# --- identity resolution -----------------------------------------------------


def _provenance() -> dict[str, str]:
    return {
        "cell_set_id": "cells-a",
        "count_representation_id": "counts-a",
        "representation_id": "rep-a",
        "clustering_id": "cluster-a",
    }


def test_resolve_identities_preserves_all_four_from_artifact() -> None:
    adata = SimpleNamespace()
    resolved = _g("_resolve_input_identities")(_provenance(), adata, "leiden")
    assert resolved == _provenance()


def test_resolve_identities_derives_missing_values_from_artifact() -> None:
    adata = SimpleNamespace(
        obs_names=["cell-1", "cell-2"],
        var_names=["GeneA", "GeneB"],
        obs={"leiden": ["0", "1"]},
        n_obs=2,
        n_vars=2,
    )
    resolved = _g("_resolve_input_identities")({}, adata, "leiden")
    assert set(resolved) == {
        "cell_set_id",
        "count_representation_id",
        "representation_id",
        "clustering_id",
    }
    assert all(":sha256:" in value for value in resolved.values())


# --- advisory figure layout (retained) --------------------------------------


def test_low_cardinality_uses_external_legend_bar() -> None:
    layout = _g("_figure_layout")(7, 12)
    assert layout["mode"] == "bar"
    assert layout["figsize"][0] <= 26.0


def test_high_cardinality_switches_to_heatmap() -> None:
    layout = _g("_figure_layout")(26, 17)
    assert layout["mode"] == "heatmap"
    assert layout["figsize"][0] <= 26.0
