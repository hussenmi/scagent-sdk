from __future__ import annotations

from pathlib import Path

from scagent_sdk.floors import FloorEvaluator, FloorFailure
from scagent_sdk.session import AnalysisSession


def _attested_cluster_qc(clustering_id: str) -> dict[str, object]:
    return {
        "status": "attested",
        "evidence_schema_version": 3,
        "evidence_id": "cluster-qc-evidence:sha256:abc",
        "clustering_id": clustering_id,
        "cell_set_id": "cells-a",
        "representation_id": "rep-a",
        "count_representation_id": "counts-a",
        "review": {
            "status": "resolved",
            "evidence_id": "cluster-qc-evidence:sha256:abc",
            "clustering_id": clustering_id,
            "unresolved_clusters": [],
        },
    }


def _seed_cluster_qc_analysis() -> dict[str, object]:
    return {
        "cell_set": {"id": "cells-a"},
        "count_representation": {"id": "counts-a"},
        "representation": {"id": "rep-a"},
        "clustering": {"id": "cluster-a"},
    }


def test_current_cluster_qc_refires_after_reclustering(tmp_path: Path) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="floor")
    session.store.record(
        "test.seed",
        state_patch={
            "facts": {
                "analysis": _seed_cluster_qc_analysis(),
                "cluster_qc": _attested_cluster_qc("cluster-a"),
            }
        },
    )
    evaluator = FloorEvaluator()

    assert evaluator.evaluate(session.store.state, "current_cluster_qc") is None

    session.store.record(
        "test.recluster",
        state_patch={"facts": {"analysis": {"clustering": {"id": "cluster-b"}}}},
    )

    failure = evaluator.evaluate(session.store.state, "current_cluster_qc")
    assert failure is not None
    assert "stale" in failure.reason


def test_current_cluster_qc_stales_when_count_representation_changes(tmp_path: Path) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="count-rep floor")
    session.store.record(
        "test.seed",
        state_patch={
            "facts": {
                "analysis": _seed_cluster_qc_analysis(),
                "cluster_qc": _attested_cluster_qc("cluster-a"),
            }
        },
    )
    evaluator = FloorEvaluator()
    assert evaluator.evaluate(session.store.state, "current_cluster_qc") is None

    session.store.record(
        "test.recount",
        state_patch={"facts": {"analysis": {"count_representation": {"id": "counts-b"}}}},
    )
    assert evaluator.evaluate(session.store.state, "current_cluster_qc") is not None


def test_current_cluster_qc_rejects_cleanup_applied_status(tmp_path: Path) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="cleanup-applied floor")
    session.store.record(
        "test.seed",
        state_patch={
            "facts": {
                "analysis": _seed_cluster_qc_analysis(),
                # After a cleanup the fact is cleanup_applied (not attested): the floor must not
                # be satisfied — the model has to re-prepare, recluster, and re-run cluster QC.
                "cluster_qc": {
                    "status": "cleanup_applied",
                    "evidence_schema_version": 2,
                    "evidence_id": "cluster-qc-evidence:sha256:abc",
                },
            }
        },
    )
    assert FloorEvaluator().evaluate(session.store.state, "current_cluster_qc") is not None


def test_current_cluster_qc_rejects_pre_restoration_attestation(tmp_path: Path) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="legacy attestation")
    session.store.record(
        "test.legacy",
        state_patch={
            "facts": {
                "analysis": {
                    "cell_set": {"id": "cells-a"},
                    "representation": {"id": "rep-a"},
                    "clustering": {"id": "cluster-a"},
                },
                # Old shape: no evidence_schema_version, representation binding, or evidence_id.
                "cluster_qc": {"status": "attested", "clustering_id": "cluster-a"},
            }
        },
    )
    assert FloorEvaluator().evaluate(session.store.state, "current_cluster_qc") is not None


def test_cellbender_suitability_is_bound_to_current_dataset_identity(tmp_path: Path) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="cellbender floor")
    evaluator = FloorEvaluator()
    session.store.record(
        "test.validation",
        state_patch={
            "facts": {
                "dataset": {"path": "/data/raw.h5", "fingerprint": "sha256:raw-a"},
                "ambient_background": {
                    "input_validation": {
                        "status": "suitable",
                        "input_path": "/data/raw.h5",
                        "dataset_fingerprint": "sha256:raw-a",
                    }
                },
            }
        },
    )

    assert evaluator.evaluate(session.store.state, "cellbender_input_suitable") is None

    session.store.record(
        "test.dataset_changed",
        state_patch={"facts": {"dataset": {"fingerprint": "sha256:raw-b"}}},
    )

    failure = evaluator.evaluate(session.store.state, "cellbender_input_suitable")
    assert failure is not None
    assert "stale" in failure.reason


def test_cellbender_suitability_rejects_different_path_and_unsuitable_status(
    tmp_path: Path,
) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="cellbender refusal")
    evaluator = FloorEvaluator()
    session.store.record(
        "test.validation",
        state_patch={
            "facts": {
                "dataset": {"path": "/data/raw.h5", "fingerprint": "sha256:raw"},
                "ambient_background": {
                    "input_validation": {
                        "status": "unsuitable",
                        "input_path": "/data/filtered.h5",
                        "dataset_fingerprint": "sha256:raw",
                    }
                },
            }
        },
    )

    assert evaluator.evaluate(session.store.state, "cellbender_input_suitable") is not None


def test_doublet_evidence_is_bound_to_current_cells_and_counts(tmp_path: Path) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="doublet floor")
    evaluator = FloorEvaluator()
    session.store.record(
        "test.doublets",
        state_patch={
            "facts": {
                "analysis": {
                    "cell_set": {"id": "cells-a"},
                    "count_representation": {"id": "counts-a"},
                },
                "doublets": {
                    "evidence": {
                        "status": "complete",
                        "evidence_id": "doublets-a",
                        "cell_set_id": "cells-a",
                        "count_representation_id": "counts-a",
                        "annotated_path": "/data/doublets.h5ad",
                    }
                },
            }
        },
    )

    assert evaluator.evaluate(session.store.state, "current_doublet_evidence") is None
    session.store.record(
        "test.new_cells",
        state_patch={"facts": {"analysis": {"cell_set": {"id": "cells-b"}}}},
    )
    failure = evaluator.evaluate(session.store.state, "current_doublet_evidence")
    assert failure is not None
    assert "stale" in failure.reason


def test_annotation_requires_markers_and_independent_current_evidence(tmp_path: Path) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="annotation floor")
    evaluator = FloorEvaluator()
    session.store.record(
        "test.markers",
        state_patch={
            "facts": {
                "analysis": {"clustering": {"id": "cluster-a"}},
                "annotation": {
                    "evidence": {
                        "markers": {
                            "status": "complete",
                            "clustering_id": "cluster-a",
                            "evidence_id": "markers-a",
                        }
                    }
                },
            }
        },
    )
    assert evaluator.evaluate(session.store.state, "current_annotation_evidence") is not None

    session.store.record(
        "test.reference",
        state_patch={
            "facts": {
                "annotation": {
                    "evidence": {
                        "celltypist": {
                            "status": "complete",
                            "clustering_id": "cluster-a",
                            "evidence_id": "celltypist-a",
                        }
                    }
                }
            }
        },
    )
    assert evaluator.evaluate(session.store.state, "current_annotation_evidence") is not None

    session.store.record(
        "test.annotation_review",
        state_patch={
            "facts": {
                "annotation": {
                    "review": {
                        "status": "resolved",
                        "clustering_id": "cluster-a",
                        "deg_primary": True,
                        "methods_reviewed": ["markers", "celltypist"],
                        "evidence_ids": {
                            "markers": "markers-a",
                            "celltypist": "celltypist-a",
                        },
                        "unresolved_clusters": [],
                    }
                }
            }
        },
    )
    assert evaluator.evaluate(session.store.state, "current_annotation_evidence") is None


def test_cell_qc_review_is_bound_to_current_cells_and_counts(tmp_path: Path) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="cell qc review")
    session.store.record(
        "test.qc",
        state_patch={
            "facts": {
                "analysis": {
                    "cell_set": {"id": "cells-a"},
                    "count_representation": {"id": "counts-a"},
                },
                "cell_qc": {
                    "status": "assessed",
                    "assessment_id": "qc-a",
                    "cell_set_id": "cells-a",
                    "count_representation_id": "counts-a",
                    "review": {
                        "status": "resolved",
                        "assessment_id": "qc-a",
                        "decision": "keep_all",
                        "cell_set_id": "cells-a",
                        "count_representation_id": "counts-a",
                    },
                },
            }
        },
    )
    evaluator = FloorEvaluator()
    assert evaluator.evaluate(session.store.state, "current_cell_qc_review") is None
    session.store.record(
        "test.recount",
        state_patch={
            "facts": {"analysis": {"count_representation": {"id": "counts-b"}}}
        },
    )
    assert evaluator.evaluate(session.store.state, "current_cell_qc_review") is not None


def _seed_analysis_identities(session: AnalysisSession, *, clustering_id: str) -> None:
    session.store.record(
        "test.analysis",
        state_patch={
            "facts": {
                "analysis": {
                    "cell_set": {"id": "cells-a"},
                    "count_representation": {"id": "counts-a"},
                    "representation": {"id": "rep-a"},
                    "clustering": {"id": clustering_id},
                }
            }
        },
    )


def _batch_ids(clustering_id: str) -> dict[str, str]:
    return {
        "cell_set_id": "cells-a",
        "count_representation_id": "counts-a",
        "representation_id": "rep-a",
        "clustering_id": clustering_id,
    }


def _record_batch(
    session: AnalysisSession,
    *,
    decision: str | None,
    clustering_id: str,
    evidence_id: str = "batch-evidence:e1",
    decision_evidence_id: str | None = None,
    status: str = "complete",
    integration_basis: str | None = None,
    recommendation: str = "do_not_integrate_based_on_current_evidence",
    validated: bool = True,
    policy_version: int = 4,
    override_warning: str | None = None,
    consulted: bool = True,
) -> None:
    ids = _batch_ids(clustering_id)
    evidence = {
        "schema_version": 2,
        "status": status,
        "evidence_id": evidence_id,
        "recommendation": recommendation,
        **ids,
    }
    decision_fact = (
        {
            "decision": decision,
            "evidence_id": decision_evidence_id or evidence_id,
            "integration_basis": integration_basis,
            "recommendation": recommendation,
            "validated": validated,
            "decision_policy_version": policy_version,
            "override_warning": override_warning,
            "user_consultation": (
                {
                    "turn_id": "turn-user-choice",
                    "question_asked": "How should these samples be handled?",
                    "user_answer": f"Please {decision}.",
                }
                if consulted and decision in ("keep_uncorrected", "integrate", "separate")
                else None
            ),
            **ids,
        }
        if decision is not None
        else None
    )
    session.store.record(
        "test.batch",
        state_patch={"facts": {"batch": {"evidence": evidence, "decision": decision_fact}}},
    )


def test_current_batch_evidence_is_bound_and_stales_after_reclustering(tmp_path: Path) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="batch evidence floor")
    evaluator = FloorEvaluator()
    _seed_analysis_identities(session, clustering_id="cluster-a")
    _record_batch(session, decision=None, clustering_id="cluster-a")
    assert evaluator.evaluate(session.store.state, "current_batch_evidence") is None

    session.store.record(
        "test.recluster",
        state_patch={"facts": {"analysis": {"clustering": {"id": "cluster-b"}}}},
    )
    assert evaluator.evaluate(session.store.state, "current_batch_evidence") is not None


def test_batch_decision_survives_the_integration_and_reclustering_it_authorizes(
    tmp_path: Path,
) -> None:
    # The decision governs the cells and counts, not the representation/clustering that acting on
    # it produces. Honoring an integrate decision changes the representation and clustering; that
    # must NOT invalidate the decision, or finalization would demand a circular re-investigation of
    # batch on the corrected data. Only cell/count changes stale it (covered separately).
    session = AnalysisSession.create(tmp_path / "sessions", title="batch decision floor")
    evaluator = FloorEvaluator()
    _seed_analysis_identities(session, clustering_id="cluster-a")
    _record_batch(
        session,
        decision="integrate",
        clustering_id="cluster-a",
        integration_basis="documented_technical_batch",
        recommendation="integration_supported",
    )

    assert evaluator.evaluate(session.store.state, "batch_decision") is None

    # Integration produces a new latent representation and a new clustering on it.
    session.store.record(
        "test.integrate_and_recluster",
        state_patch={
            "facts": {
                "analysis": {
                    "representation": {"id": "rep-scvi"},
                    "clustering": {"id": "cluster-b"},
                }
            }
        },
    )
    assert evaluator.evaluate(session.store.state, "batch_decision") is None


def test_request_guidance_is_pending_and_does_not_satisfy_the_decision_floor(
    tmp_path: Path,
) -> None:
    # "I need to ask the user" must not become the cheapest route to finalization: the question is
    # recorded as open, and the floor keeps it open until a real answer replaces it.
    session = AnalysisSession.create(tmp_path / "sessions", title="pending batch question")
    evaluator = FloorEvaluator()
    _seed_analysis_identities(session, clustering_id="cluster-a")
    _record_batch(
        session,
        decision="request_guidance",
        clustering_id="cluster-a",
        recommendation="cannot_determine_technical_vs_biological",
    )
    pending = evaluator.evaluate(session.store.state, "batch_decision")
    assert pending is not None
    assert "still unanswered" in pending.reason
    assert "user_consultation" in pending.remediation

    _record_batch(
        session,
        decision="keep_uncorrected",
        clustering_id="cluster-a",
        recommendation="cannot_determine_technical_vs_biological",
    )
    assert evaluator.evaluate(session.store.state, "batch_decision") is None


def test_batch_evidence_schema_one_no_longer_carries_a_consultation_anchor(tmp_path: Path) -> None:
    # Schema 1 evidence cannot prove the user was consulted about *this* evidence, so it fails
    # closed and has to be re-investigated rather than decided against.
    session = AnalysisSession.create(tmp_path / "sessions", title="pre-consultation evidence")
    evaluator = FloorEvaluator()
    _seed_analysis_identities(session, clustering_id="cluster-a")
    session.store.record(
        "test.schema_one_batch",
        state_patch={
            "facts": {
                "batch": {
                    "evidence": {
                        "schema_version": 1,
                        "status": "complete",
                        "evidence_id": "batch-evidence:e1",
                        **_batch_ids("cluster-a"),
                    },
                    "decision": None,
                }
            }
        },
    )
    assert evaluator.evaluate(session.store.state, "current_batch_evidence") is not None


def test_current_batch_evidence_rejects_legacy_schema(tmp_path: Path) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="legacy batch evidence")
    evaluator = FloorEvaluator()
    _seed_analysis_identities(session, clustering_id="cluster-a")
    session.store.record(
        "test.legacy_batch",
        state_patch={
            "facts": {
                "batch": {
                    # Pre-restoration shape: no schema_version.
                    "evidence": {
                        "status": "complete",
                        "evidence_id": "batch-evidence:e1",
                        **_batch_ids("cluster-a"),
                    },
                    "decision": None,
                }
            }
        },
    )
    assert evaluator.evaluate(session.store.state, "current_batch_evidence") is not None


def test_batch_decision_requires_matching_evidence_id(tmp_path: Path) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="batch evidence-id floor")
    evaluator = FloorEvaluator()
    _seed_analysis_identities(session, clustering_id="cluster-a")
    # Decision references a different evidence id than the current evidence.
    _record_batch(
        session,
        decision="keep_uncorrected",
        clustering_id="cluster-a",
        evidence_id="batch-evidence:e1",
        decision_evidence_id="batch-evidence:OLD",
    )
    assert evaluator.evaluate(session.store.state, "batch_decision") is not None


def test_batch_decision_stales_when_cell_set_changes(tmp_path: Path) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="batch cell-set floor")
    evaluator = FloorEvaluator()
    _seed_analysis_identities(session, clustering_id="cluster-a")
    _record_batch(session, decision="separate", clustering_id="cluster-a")
    assert evaluator.evaluate(session.store.state, "batch_decision") is None

    session.store.record(
        "test.filter",
        state_patch={"facts": {"analysis": {"cell_set": {"id": "cells-b"}}}},
    )
    assert evaluator.evaluate(session.store.state, "batch_decision") is not None


def test_not_applicable_now_requires_current_identity_bound_evidence(tmp_path: Path) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="batch not-applicable floor")
    evaluator = FloorEvaluator()
    # The legacy identity-free not-applicable shortcut is gone.
    session.store.record(
        "test.na_bare",
        state_patch={"facts": {"batch": {"decision": {"decision": "not_applicable"}}}},
    )
    assert evaluator.evaluate(session.store.state, "batch_decision") is not None

    _seed_analysis_identities(session, clustering_id="cluster-a")
    _record_batch(
        session, decision="not_applicable", clustering_id="cluster-a", status="not_applicable"
    )
    assert evaluator.evaluate(session.store.state, "batch_decision") is None


def test_integration_authorized_requires_basis_and_current_evidence(tmp_path: Path) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="integration floor")
    evaluator = FloorEvaluator()
    _seed_analysis_identities(session, clustering_id="cluster-a")

    _record_batch(session, decision="keep_uncorrected", clustering_id="cluster-a")
    assert evaluator.evaluate(session.store.state, "integration_authorized") is not None

    # integrate without an explicit basis does not authorize.
    _record_batch(session, decision="integrate", clustering_id="cluster-a")
    assert evaluator.evaluate(session.store.state, "integration_authorized") is not None

    _record_batch(
        session,
        decision="integrate",
        clustering_id="cluster-a",
        integration_basis="documented_technical_batch",
        recommendation="integration_supported",
    )
    assert evaluator.evaluate(session.store.state, "integration_authorized") is None

    # Reclustering the uncorrected data (same cells and counts) does not revoke the authorization:
    # the user's approval is over which cells/counts get integrated, not over a clustering.
    session.store.record(
        "test.recluster",
        state_patch={"facts": {"analysis": {"clustering": {"id": "cluster-b"}}}},
    )
    assert evaluator.evaluate(session.store.state, "integration_authorized") is None

    # Removing cells does revoke it: the substrate the user authorized has changed.
    session.store.record(
        "test.filter",
        state_patch={"facts": {"analysis": {"cell_set": {"id": "cells-b"}}}},
    )
    assert evaluator.evaluate(session.store.state, "integration_authorized") is not None


def test_integration_requires_validated_decision_at_current_policy(tmp_path: Path) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="policy floor")
    evaluator = FloorEvaluator()
    _seed_analysis_identities(session, clustering_id="cluster-a")
    kwargs = {
        "decision": "integrate",
        "clustering_id": "cluster-a",
        "integration_basis": "documented_technical_batch",
        "recommendation": "integration_supported",
    }

    # Unvalidated decision does not authorize.
    _record_batch(session, validated=False, **kwargs)
    assert evaluator.evaluate(session.store.state, "integration_authorized") is not None

    # A decision validated under an older policy version does not authorize.
    _record_batch(session, policy_version=0, **kwargs)
    assert evaluator.evaluate(session.store.state, "integration_authorized") is not None

    _record_batch(session, **kwargs)
    assert evaluator.evaluate(session.store.state, "integration_authorized") is None


def test_user_authorized_integration_does_not_need_recommendation_override(tmp_path: Path) -> None:
    session = AnalysisSession.create(tmp_path / "sessions", title="user-owned integration")
    evaluator = FloorEvaluator()
    _seed_analysis_identities(session, clustering_id="cluster-a")
    kwargs = {
        "decision": "integrate",
        "clustering_id": "cluster-a",
        "integration_basis": "user_authorized_integration",
        "recommendation": "cannot_determine_technical_vs_biological",
    }

    # The scientific recommendation is advisory; the current user-owned decision is sufficient.
    _record_batch(session, **kwargs)
    assert evaluator.evaluate(session.store.state, "integration_authorized") is None

    # A forged decision without the recorded user consultation still fails closed.
    _record_batch(session, consulted=False, **kwargs)
    assert evaluator.evaluate(session.store.state, "integration_authorized") is not None


# --- outstanding-requirement visibility ---------------------------------------


def test_outstanding_block_states_every_unmet_floor_without_prescribing_an_order() -> None:
    from scagent_sdk.floors import render_outstanding_block

    failures = (
        FloorFailure("dataset_identity", "No byte-level identity.", "Run inspect_dataset."),
        FloorFailure("current_cluster_qc", "Cluster QC is absent.", "Run evaluate_cluster_qc."),
    )

    block = render_outstanding_block(failures)

    assert "dataset_identity" in block and "current_cluster_qc" in block
    assert "Run inspect_dataset." in block and "Run evaluate_cluster_qc." in block
    # The whole point is that these are predicates, not a pipeline to march through.
    assert "not a required order" in block
    assert "Nothing here is a checklist" in block
    # Nothing outstanding renders nothing at all, rather than an encouraging empty section.
    assert render_outstanding_block(()) == ""


def test_floor_change_reports_new_staleness_when_it_is_caused_not_when_it_blocks() -> None:
    """Filtering cells made the QC review stale two hours before anything said so.

    The real session learned at `finalize_analysis` that `current_cell_qc_review` had gone stale
    back at the filtering step. Reporting the transition at the step that caused it is what lets a
    model fold the re-review into the work it is already doing.
    """

    from scagent_sdk.floors import render_floor_change

    stale = FloorFailure(
        "current_cell_qc_review",
        "Cell-level QC is stale for the current cells.",
        "Recalculate and review QC on the retained cells.",
    )

    note = render_floor_change(frozenset(), (stale,))

    assert "current_cell_qc_review" in note
    assert "Recalculate and review QC on the retained cells." in note
    assert "made 1 scientific requirement(s) outstanding" in note


def test_floor_change_is_silent_when_nothing_changed_and_reports_what_was_satisfied() -> None:
    from scagent_sdk.floors import render_floor_change

    stale = FloorFailure("current_cluster_qc", "Absent.", "Run evaluate_cluster_qc.")

    # Same set before and after: no note at all, so this never reads as a checklist recital.
    assert render_floor_change(frozenset({"current_cluster_qc"}), (stale,)) == ""
    assert render_floor_change(frozenset(), ()) == ""

    satisfied = render_floor_change(frozenset({"current_cluster_qc", "batch_decision"}), ())
    assert satisfied == "No longer outstanding: batch_decision, current_cluster_qc."
