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
    assert package.manifest.version == "0.9.0"
    names = {tool.name for tool in package.manifest.tools}
    assert names == {"investigate_batch", "decide_batch_handling"}
    evidence = next(t for t in package.manifest.tools if t.name == "investigate_batch")
    decision = next(t for t in package.manifest.tools if t.name == "decide_batch_handling")
    # Evidence tool records no decision.
    assert "decision" not in evidence.input_schema["properties"]
    assert evidence.entrypoint.endswith(":run_evidence")
    # ``path`` is the declared matrix input, so it is deliberately optional: the executor resolves
    # an omitted value to the active lineage artifact.
    assert set(evidence.input_schema["required"]) == {"batch_key"}
    assert evidence.primary_matrix_input == "path"
    assert evidence.primary_matrix_output is None  # reads a matrix, writes none
    props = evidence.input_schema["properties"]
    # The misleading diffxpy no-op is gone; Wilcoxon is the declared primary method.
    assert "prefer_diffxpy" not in props
    # Cost and match quality are explicitly bounded.
    assert props["max_candidate_pairs"]["default"] == 3
    assert props["max_candidate_pairs"]["maximum"] == 20
    assert props["max_pair_attempts"]["default"] == 10
    assert props["min_profile_correlation"]["default"] == 0.4
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
    assert "user_consultation" not in decision.input_schema["properties"]
    assert "rationale" not in decision.input_schema["properties"]
    assert "question_asked" in decision.input_schema["properties"]


def test_batch_artifact_packet_is_preserved() -> None:
    assert _g("BATCH_EVIDENCE_FILENAMES") == (
        "batch_diagnostic_sample_enriched_regions.csv",
        "batch_diagnostic_within_sample_degs.csv",
        "batch_diagnostic_population_pairs.csv",
        "batch_diagnostic_direct_pair_degs.csv",
        "batch_diagnostic_design_check.csv",
    )


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
        assert validate(decision, None)["ok"] is True


def test_integrate_requires_explicit_basis() -> None:
    validate = _g("validate_decision")
    assert validate("integrate", None)["ok"] is False


def test_integrate_allowed_with_explicit_basis() -> None:
    validate = _g("validate_decision")
    assert validate("integrate", "documented_technical_batch")["ok"] is True


def test_user_authorized_integration_is_not_blocked_by_an_advisory_recommendation() -> None:
    validate = _g("validate_decision")
    assert validate("integrate", "user_authorized_integration")["ok"] is True


def test_integration_optional_accepts_the_users_choice() -> None:
    validate = _g("validate_decision")
    assert validate("integrate", "user_authorized_integration")["ok"] is True


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


def test_user_authorized_basis_needs_no_evidence_documentation() -> None:
    check = _g("validate_integration_basis")
    assert check("user_authorized_integration", {})["ok"] is True
    assert check(None, {})["ok"] is True


def test_decision_policy_version_is_declared() -> None:
    assert _g("DECISION_POLICY_VERSION") == 4
    assert _g("BATCH_EVIDENCE_SCHEMA") == 2


# --- user consultation on decisions the evidence cannot settle ---------------

_UNSETTLED = "cannot_determine_technical_vs_biological"


def _turns() -> tuple[dict[str, Any], ...]:
    return (
        {"turn_id": "t-open", "sequence": 4, "user_prompt": "analyze this dataset"},
        {"turn_id": "t-answer", "sequence": 40, "user_prompt": "they are 8 unrelated donors"},
    )


_QUESTION = "Keep the 8 donors combined uncorrected, integrate, or separate?"


def _check(decision: str, recommendation: str, question: Any, *, anchor: int = 20) -> Any:
    return _g("validate_consultation")(
        decision,
        question,
        anchor_sequence=anchor,
        user_turns=_turns(),
    )


def test_latest_user_turn_sequence_ignores_malformed_entries() -> None:
    latest = _g("latest_user_turn_sequence")
    assert latest(_turns()) == 40
    assert latest(()) == 0
    assert latest([{"sequence": "nope"}, "junk", {"turn_id": "x"}]) == 0


def test_anchor_is_carried_forward_for_unchanged_science() -> None:
    # Re-running the investigation on unchanged science must not move the anchor forward: a user who
    # already answered in the current turn would otherwise be told their answer predates the
    # evidence and be made to repeat themselves.
    carry = _g("carry_forward_anchor")
    facts = {
        "batch": {"evidence": {"evidence_id": "e1", "consultation_anchor_sequence": 5}},
    }
    assert carry(facts, "e1", 40) == 5
    # Changed science is a different question and correctly starts over.
    assert carry(facts, "e2", 40) == 40
    # Nothing recorded yet, or a malformed/absent anchor, falls back to the current turn.
    assert carry({}, "e1", 40) == 40
    assert carry({"batch": {"evidence": {"evidence_id": "e1"}}}, "e1", 40) == 40
    assert carry(
        {"batch": {"evidence": {"evidence_id": "e1", "consultation_anchor_sequence": True}}},
        "e1",
        40,
    ) == 40


def test_every_substantive_post_investigation_choice_needs_consultation() -> None:
    for recommendation in (
        "do_not_integrate_based_on_current_evidence",
        "integration_supported",
        "not_applicable",
    ):
        verdict = _check("keep_uncorrected", recommendation, None)
        assert verdict["ok"] is False
        assert "question_asked" in verdict["violation"]


def test_unsettled_recommendation_blocks_substantive_decision_without_consultation() -> None:
    # The exact defect this gate exists for: keep_uncorrected was the frictionless path, needing
    # neither a basis nor an override, so an unresolved question could be recorded as resolved.
    for decision in ("keep_uncorrected", "integrate", "separate"):
        verdict = _check(decision, _UNSETTLED, None)
        assert verdict["ok"] is False
        assert "question_asked" in verdict["violation"]
        assert "request_guidance" in verdict["violation"]


def test_integration_optional_recommendation_also_requires_consultation() -> None:
    verdict = _check("integrate", "integration_optional_for_confirmed_replicates", None)
    assert verdict["ok"] is False


def test_deferring_decisions_never_require_consultation() -> None:
    for decision in ("request_guidance", "not_applicable"):
        assert _check(decision, _UNSETTLED, None)["ok"] is True


def test_consultation_requires_the_question() -> None:
    assert _check("keep_uncorrected", _UNSETTLED, None)["ok"] is False
    assert _check("keep_uncorrected", _UNSETTLED, "   ")["ok"] is False


def test_consultation_requires_a_real_later_user_turn() -> None:
    # The opening "analyze this dataset" prompt cannot authorize an answer to a question that
    # only the later evidence raised.
    verdict = _check("keep_uncorrected", _UNSETTLED, _QUESTION, anchor=40)
    assert verdict["ok"] is False
    assert "no user answer after" in verdict["violation"]


def test_consultation_rejects_the_turn_the_evidence_itself_was_produced_in() -> None:
    # Evidence and decision inside one turn is exactly what the bad session did: the newest turn
    # at evidence time is the anchor, so it cannot also be the consultation.
    verdict = _check("keep_uncorrected", _UNSETTLED, _QUESTION, anchor=40)
    assert verdict["ok"] is False


def test_accepted_consultation_resolves_exact_answer_from_the_first_turn_after_the_question(
) -> None:
    verdict = _check("keep_uncorrected", _UNSETTLED, _QUESTION)
    assert verdict["ok"] is True
    assert verdict["consultation"] == {
        "turn_id": "t-answer",
        "turn_sequence": 40,
        "question_asked": _QUESTION,
        "user_answer": "they are 8 unrelated donors",
        "anchor_sequence": 20,
        "turns_after_anchor": 1,
        "question_id": None,
    }


def test_an_unrelated_later_instruction_is_not_harvested_as_the_batch_answer() -> None:
    """The answer to a question is the first thing the user says after it, not the newest thing.

    Taking the newest post-anchor turn let a real session record "can you finalize" — typed two
    hours later, about something else entirely — as the user's authorization to integrate.
    """

    turns = (
        {"turn_id": "t-open", "sequence": 4, "user_prompt": "analyze this dataset"},
        {"turn_id": "t-answer", "sequence": 40, "user_prompt": "yes integrate"},
        {"turn_id": "t-unrelated", "sequence": 92, "user_prompt": "can you finalize"},
    )

    verdict = _g("validate_consultation")(
        "integrate", _QUESTION, anchor_sequence=20, user_turns=turns
    )

    assert verdict["consultation"]["turn_id"] == "t-answer"
    assert verdict["consultation"]["user_answer"] == "yes integrate"
    # A longer exchange stays visible instead of being hidden behind the one quoted line.
    assert verdict["consultation"]["turns_after_anchor"] == 2


def test_a_recorded_answer_survives_re_investigation_of_the_same_question() -> None:
    """The deadlock this fixes: integrate → re-cluster → re-investigate → decision impossible.

    A real session asked, was told "yes integrate", trained scVI, re-clustered, re-ran
    investigate_batch because the batch floor required evidence for the new clustering, and then
    could not record the decision at all: the answer now predated the newest evidence, and only a
    future user turn could unblock it. The question — how to handle this batch variable and its
    samples — had not changed, so the recorded answer still applies.
    """

    question_id = _g("consultation_question_id")("sample", ["A", "B"])
    prior = {
        "turn_id": "t-answer",
        "turn_sequence": 46,
        "user_answer": "yes integrate",
        "question_asked": _QUESTION,
        "anchor_sequence": 42,
        "question_id": question_id,
    }
    # No user turn exists after the *new* anchor, which is what made this unsatisfiable.
    verdict = _g("validate_consultation")(
        "integrate",
        _QUESTION,
        anchor_sequence=58,
        user_turns=({"turn_id": "t-answer", "sequence": 46, "user_prompt": "yes integrate"},),
        question_id=question_id,
        prior_consultation=prior,
    )

    assert verdict["ok"] is True
    assert verdict["consultation"]["turn_id"] == "t-answer"
    assert verdict["consultation"]["user_answer"] == "yes integrate"
    assert verdict["consultation"]["reaffirmed_without_new_turn"] is True

    # A different batch variable is a different question and must be asked.
    other = _g("validate_consultation")(
        "integrate",
        _QUESTION,
        anchor_sequence=58,
        user_turns=({"turn_id": "t-answer", "sequence": 46, "user_prompt": "yes integrate"},),
        question_id=_g("consultation_question_id")("donor", ["A", "B"]),
        prior_consultation=prior,
    )
    assert other["ok"] is False


def test_carried_forward_consultation_requires_a_real_answer_to_the_same_question() -> None:
    carry = _g("carry_forward_consultation")
    question_id = _g("consultation_question_id")("sample", ["A", "B"])
    answered = {
        "batch": {
            "decision": {
                "decision": "integrate",
                "user_consultation": {"turn_id": "t-answer", "question_id": question_id},
            }
        }
    }
    assert carry(answered, question_id)["turn_id"] == "t-answer"
    # A different question, a deferral, and a missing consultation all carry nothing forward.
    assert carry(answered, _g("consultation_question_id")("donor", ["A", "B"])) is None
    assert (
        carry(
            {"batch": {"decision": {"decision": "request_guidance", "user_consultation": None}}},
            question_id,
        )
        is None
    )
    assert carry({}, question_id) is None
    assert carry(answered, None) is None


def test_the_question_identity_ignores_representation_and_clustering() -> None:
    same = _g("consultation_question_id")
    # Same batch variable and samples, in any order: the same question.
    assert same("sample", ["B", "A", "A"]) == same("sample", ["A", "B"])
    assert same("sample", ["A", "B"]) != same("sample", ["A", "B", "C"])
    assert same("sample", ["A", "B"]) != same("donor", ["A", "B"])


# --- decision entrypoint end to end ------------------------------------------


def _evidence_facts(recommendation: str = _UNSETTLED, anchor: int = 20) -> dict[str, Any]:
    return {
        "batch": {
            "evidence": {
                "schema_version": _g("BATCH_EVIDENCE_SCHEMA"),
                "status": "complete",
                "evidence_id": "batch-evidence:e1",
                "recommendation": recommendation,
                "consultation_anchor_sequence": anchor,
                "technical_batch_documented": False,
                "technical_batch_basis": None,
                "matched_populations": [
                    {
                        "cluster_a": "1",
                        "sample_a": "Donor_01",
                        "cluster_b": "5",
                        "sample_b": "Donor_02",
                        "shared_top25_count": 12,
                        "shared_identity_genes": ["C1QA", "C1QB", "APOC1"],
                    }
                ],
                **_provenance(),
            },
            "decision": None,
        }
    }


def _decision_context(tmp_path: Path, facts: dict[str, Any]) -> Any:
    return SimpleNamespace(
        staging_dir=tmp_path,
        state_facts=facts,
        user_turns=_turns(),
        execution_id="exec-1",
    )


def test_run_decision_rejects_unconsulted_keep_and_accepts_the_consulted_one(
    tmp_path: Path,
) -> None:
    run_decision = _handler("decide_batch_handling")
    facts = _evidence_facts()
    with pytest.raises(ValueError, match="question_asked"):
        run_decision(
            {
                "evidence_id": "batch-evidence:e1",
                "decision": "keep_uncorrected",
            },
            _decision_context(tmp_path, facts),
        )
    result = run_decision(
        {
            "evidence_id": "batch-evidence:e1",
            "decision": "keep_uncorrected",
            "question_asked": _QUESTION,
        },
        _decision_context(tmp_path, facts),
    )
    recorded = result["facts_patch"]["batch"]["decision"]
    assert recorded["decision"] == "keep_uncorrected"
    assert recorded["user_consultation"]["turn_id"] == "t-answer"
    assert recorded["decision_policy_version"] == 4
    assert "t-answer" in result["summary"]
    report = (tmp_path / "batch-decision.md").read_text(encoding="utf-8")
    assert "## What the user said" in report
    assert "they are 8 unrelated donors" in report
    assert "Keep the samples combined without integration" in report
    assert "## Evidence considered" in report
    assert "within-sample identity DEGs" in report
    assert "## Effect on the analysis" in report
    assert "cannot_determine_technical_vs_biological" not in report


def test_run_decision_records_request_guidance_as_pending(tmp_path: Path) -> None:
    run_decision = _handler("decide_batch_handling")
    result = run_decision(
        {
            "evidence_id": "batch-evidence:e1",
            "decision": "request_guidance",
        },
        _decision_context(tmp_path, _evidence_facts()),
    )
    recorded = result["facts_patch"]["batch"]["decision"]
    assert recorded["decision"] == "request_guidance"
    assert recorded["user_consultation"] is None
    assert "finalization stays blocked" in result["summary"]
    assert "Pending." in (tmp_path / "batch-decision.md").read_text(encoding="utf-8")


def test_run_decision_records_user_authorized_integration_without_inventing_cause(
    tmp_path: Path,
) -> None:
    run_decision = _handler("decide_batch_handling")
    result = run_decision(
        {
            "evidence_id": "batch-evidence:e1",
            "decision": "integrate",
            "integration_basis": "user_authorized_integration",
            "question_asked": "Would you like to integrate these samples?",
        },
        _decision_context(tmp_path, _evidence_facts()),
    )
    recorded = result["facts_patch"]["batch"]["decision"]
    assert recorded["user_consultation"]["user_answer"] == "they are 8 unrelated donors"
    assert "does not by itself establish" in recorded["rationale"]
    assert "technical replicates" not in recorded["rationale"]
    assert "technical variation" not in recorded["rationale"]


def test_run_decision_still_needs_user_choice_when_advisory_looks_settled(
    tmp_path: Path,
) -> None:
    run_decision = _handler("decide_batch_handling")
    with pytest.raises(ValueError, match="question_asked"):
        run_decision(
            {
                "evidence_id": "batch-evidence:e1",
                "decision": "keep_uncorrected",
            },
            _decision_context(
                tmp_path, _evidence_facts("do_not_integrate_based_on_current_evidence")
            ),
        )


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


def test_evidence_report_translates_internal_labels_to_plain_language(tmp_path: Path) -> None:
    report = tmp_path / "batch-investigation.md"
    _g("_write_evidence_report")(
        report,
        {
            "batch_key": "donor",
            "gene_evidence": "recurring_sample_associated",
            "design_interpretation": "unknown",
            "recommendation": "cannot_determine_technical_vs_biological",
            "n_enriched_regions": 30,
            "n_supported_matches": 3,
            "n_direct_compared_pairs": 3,
            "n_recurring_programs": 10,
            "de_engine": "scanpy_wilcoxon",
            "gene_class_version": "batch-gene-class-v1",
            "matched_populations": [
                {
                    "cluster_a": "7",
                    "sample_a": "Donor_08",
                    "within_sample_comparison_a": (
                        "cluster 7 vs all other cells in Donor_08"
                    ),
                    "cluster_b": "26",
                    "sample_b": "Donor_05",
                    "within_sample_comparison_b": (
                        "cluster 26 vs all other cells in Donor_05"
                    ),
                    "shared_top25_count": 20,
                    "shared_identity_genes": ["APOC1", "C1QA", "C1QB", "TYROBP"],
                    "direct_comparison_higher_in_a": ["GENEA"],
                    "direct_comparison_higher_in_b": ["GENEB"],
                }
            ],
        },
    )
    text = report.read_text(encoding="utf-8")
    assert "## Conclusion" in text
    assert "## Recommendation" in text
    assert "The samples clearly organize the data" in text
    assert "cluster 7 vs all other cells in Donor_08" in text
    assert "share 20 of their top 25 genes" in text
    assert "APOC1, C1QA, C1QB, TYROBP" in text
    assert "does not prove that the cause is technical" in text
    assert "The matched-population DEGs are the main evidence" in text
    assert "batch_diagnostic_population_pairs.csv" in text
    assert "Ask the user" not in text
    assert "recurring_sample_associated" not in text
    assert "cannot_determine_technical_vs_biological" not in text
    assert "legacy" not in text.casefold()
