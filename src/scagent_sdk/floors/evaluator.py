"""Evaluate independent scientific floors against durable session facts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from scagent_sdk.contracts.state import SessionState

# The restored three-axis cluster-QC evidence schema. Pre-restoration attestations lack this
# version (and the representation/evidence binding) and therefore fail the current-QC floor,
# forcing a rerun of the strengthened cluster QC before annotation or finalization.
CLUSTER_QC_EVIDENCE_SCHEMA = 3

# The gene-first batch-evidence schema. Legacy-shaped batch facts lack it and fail closed. Schema 2
# added the consultation anchor, without which a decision cannot prove the user was consulted about
# this evidence, so earlier evidence must be re-investigated.
BATCH_EVIDENCE_SCHEMA = 2

# Authorization policy version applied when a batch decision was validated. Decisions validated
# under an older policy no longer authorize integration.
BATCH_DECISION_POLICY_VERSION = 4

# A decision value that records the batch question as put to the user and still unanswered. It is
# a legitimate decision to record but not a resolution, so it must not satisfy the decision floor —
# otherwise "I need to ask the user" silently becomes the cheapest way to finalize.
_PENDING_BATCH_DECISION = "request_guidance"

@dataclass(frozen=True)
class FloorFailure:
    floor: str
    reason: str
    remediation: str


class FloorEvaluator:
    def evaluate(self, state: SessionState, floor: str) -> FloorFailure | None:
        facts = state.facts
        if floor == "dataset_identity":
            dataset = facts.get("dataset")
            if isinstance(dataset, dict) and dataset.get("fingerprint"):
                return None
            return FloorFailure(
                floor,
                "The input dataset has no durable byte-level identity.",
                "Run inspect_dataset on the active input first.",
            )
        if floor == "cellbender_input_suitable":
            dataset = facts.get("dataset")
            ambient = facts.get("ambient_background")
            validation = (
                ambient.get("input_validation") if isinstance(ambient, dict) else None
            )
            if (
                isinstance(dataset, dict)
                and isinstance(validation, dict)
                and validation.get("status") == "suitable"
                and validation.get("dataset_fingerprint") == dataset.get("fingerprint")
                and validation.get("input_path") == dataset.get("path")
            ):
                return None
            return FloorFailure(
                floor,
                "CellBender input suitability is absent or stale for the active dataset.",
                "Run validate_cellbender_input on the current raw droplet matrix.",
            )
        if floor == "current_doublet_evidence":
            analysis = facts.get("analysis")
            cell_set = analysis.get("cell_set") if isinstance(analysis, dict) else None
            count_representation = (
                analysis.get("count_representation") if isinstance(analysis, dict) else None
            )
            doublets = facts.get("doublets")
            evidence = doublets.get("evidence") if isinstance(doublets, dict) else None
            if (
                isinstance(cell_set, dict)
                and isinstance(count_representation, dict)
                and isinstance(evidence, dict)
                and evidence.get("status") == "complete"
                and evidence.get("cell_set_id") == cell_set.get("id")
                and evidence.get("count_representation_id") == count_representation.get("id")
                and evidence.get("evidence_id")
                and evidence.get("annotated_path")
            ):
                return None
            return FloorFailure(
                floor,
                "Doublet evidence is absent or stale for the current cells/counts.",
                "Run evaluate_doublet_evidence on the current prepared raw-count artifact.",
            )
        if floor == "current_cell_qc_review":
            cell_set_id = self._analysis_id(facts, "cell_set")
            count_representation_id = self._analysis_id(facts, "count_representation")
            evidence = facts.get("cell_qc")
            review = evidence.get("review") if isinstance(evidence, dict) else None
            if (
                cell_set_id
                and count_representation_id
                and isinstance(evidence, dict)
                and evidence.get("status") == "assessed"
                and evidence.get("assessment_id")
                and evidence.get("cell_set_id") == cell_set_id
                and evidence.get("count_representation_id") == count_representation_id
                and isinstance(review, dict)
                and review.get("status") == "resolved"
                and review.get("assessment_id") == evidence.get("assessment_id")
                and review.get("cell_set_id") == cell_set_id
                and review.get("count_representation_id") == count_representation_id
                and review.get("decision") in {"keep_all", "not_applicable"}
            ):
                return None
            return FloorFailure(
                floor,
                "Cell-level QC is absent, visually unreviewed, unresolved, or stale for the "
                "current cells and count representation.",
                "Run calculate_single_cell_qc, inspect every returned QC figure, then call "
                "review_single_cell_qc. If filtering is selected, filter, recalculate, and "
                "review QC on the retained cells.",
            )
        if floor == "current_cluster_qc":
            clustering_id = self._clustering_id(facts)
            cell_set_id = self._analysis_id(facts, "cell_set")
            representation_id = self._analysis_id(facts, "representation")
            count_representation_id = self._analysis_id(facts, "count_representation")
            evidence = facts.get("cluster_qc")
            review = evidence.get("review") if isinstance(evidence, dict) else None
            if (
                clustering_id
                and isinstance(evidence, dict)
                and evidence.get("status") == "attested"
                and evidence.get("evidence_schema_version") == CLUSTER_QC_EVIDENCE_SCHEMA
                and evidence.get("evidence_id")
                and evidence.get("clustering_id") == clustering_id
                and evidence.get("cell_set_id") == cell_set_id
                and evidence.get("representation_id") == representation_id
                and evidence.get("count_representation_id") == count_representation_id
                and isinstance(review, dict)
                and review.get("status") == "resolved"
                and review.get("evidence_id") == evidence.get("evidence_id")
                and review.get("clustering_id") == clustering_id
                and not review.get("unresolved_clusters")
            ):
                return None
            return FloorFailure(
                floor,
                "Three-axis cluster QC is absent, stale, visually unreviewed, or has unresolved "
                "cluster actions for the current analysis identities.",
                "Run evaluate_cluster_qc, inspect every required figure, then call "
                "review_cluster_qc. Apply any requested cleanup/reclustering before finalization.",
            )
        if floor == "current_batch_evidence":
            evidence = self._batch_evidence(facts)
            if (
                isinstance(evidence, dict)
                and evidence.get("status") in ("complete", "not_applicable")
                and evidence.get("schema_version") == BATCH_EVIDENCE_SCHEMA
                and evidence.get("evidence_id")
                and self._batch_identities_current(facts, evidence)
            ):
                return None
            return FloorFailure(
                floor,
                "Batch evidence is absent or stale for the current cells, counts, "
                "representation, and clustering.",
                "Run investigate_batch on the current clustering before deciding batch handling.",
            )
        if floor == "batch_decision":
            evidence = self._batch_evidence(facts)
            decision = self._batch_decision(facts)
            bound = (
                isinstance(decision, dict)
                and isinstance(evidence, dict)
                and bool(decision.get("decision"))
                and decision.get("evidence_id") == evidence.get("evidence_id")
                and self._batch_decision_current(facts, decision)
            )
            if bound and decision.get("decision") == _PENDING_BATCH_DECISION:
                return FloorFailure(
                    floor,
                    "The batch question was put to the user and is recorded as still unanswered "
                    "(request_guidance), so batch handling is pending rather than decided.",
                    "Ask the user how to handle the batch structure, then record their answer with "
                    "decide_batch_handling as keep_uncorrected, integrate, or separate, passing "
                        "user_consultation with the turn that carried their reply.",
                )
            resolved = bool(
                bound
                and (
                    decision.get("decision") == "not_applicable"
                    or (
                        decision.get("decision") in ("keep_uncorrected", "integrate", "separate")
                        and decision.get("validated") is True
                        and decision.get("decision_policy_version")
                        == BATCH_DECISION_POLICY_VERSION
                        and self._user_consultation_recorded(decision)
                    )
                )
            )
            if resolved:
                return None
            return FloorFailure(
                floor,
                "No explicit batch-handling decision is recorded against current batch evidence "
                "for the active cells, counts, representation, and clustering.",
                "Run investigate_batch, then decide_batch_handling with keep, integrate, "
                "separate, or not-applicable.",
            )
        if floor == "integration_authorized":
            evidence = self._batch_evidence(facts)
            decision = self._batch_decision(facts)
            if (
                isinstance(decision, dict)
                and isinstance(evidence, dict)
                and decision.get("decision") == "integrate"
                # The decision passed the capability's authorization policy, at the current version.
                and decision.get("validated") is True
                and decision.get("decision_policy_version") == BATCH_DECISION_POLICY_VERSION
                # It was taken against exactly this evidence, for the current analysis identities.
                and decision.get("evidence_id") == evidence.get("evidence_id")
                and decision.get("integration_basis")
                in (
                    "documented_technical_batch",
                    "user_authorized_integration",
                )
                and self._user_consultation_recorded(decision)
                and self._batch_decision_current(facts, decision)
            ):
                return None
            return FloorFailure(
                floor,
                "No current batch decision with an explicit integration basis authorizes "
                "integration for the active cells, counts, representation, and clustering.",
                "Investigate batch structure, then decide_batch_handling with "
                "decision='integrate' and an integration_basis.",
            )
        if floor == "current_annotation_evidence":
            clustering_id = self._clustering_id(facts)
            annotation = facts.get("annotation")
            evidence = annotation.get("evidence") if isinstance(annotation, dict) else None
            review = annotation.get("review") if isinstance(annotation, dict) else None
            valid: dict[str, dict[str, Any]] = {}
            if isinstance(evidence, dict):
                valid = {
                    str(name): value
                    for name, value in evidence.items()
                    if isinstance(value, dict)
                    and value.get("clustering_id") == clustering_id
                    and value.get("status") == "complete"
                }
            independent = set(valid) - {"markers"}
            if (
                clustering_id
                and "markers" in valid
                and independent
                and isinstance(review, dict)
                and review.get("status") == "resolved"
                and review.get("clustering_id") == clustering_id
                and review.get("deg_primary") is True
                and set(review.get("methods_reviewed", [])) <= set(valid)
                and all(
                    review.get("evidence_ids", {}).get(method)
                    == valid[method].get("evidence_id")
                    for method in review.get("methods_reviewed", [])
                )
                and not review.get("unresolved_clusters")
            ):
                return None
            return FloorFailure(
                floor,
                "Current annotation needs DEG-primary evidence review, current independent "
                "reference evidence, and no unresolved clusters.",
                "Run marker evidence and suitable reference methods, inspect their agreement, "
                "then call review_annotation_evidence. Use two references when available or "
                "record a specific waiver.",
            )
        return FloorFailure(
            floor,
            f"Unknown scientific floor {floor!r}; failing closed.",
            "Correct the capability manifest or add a tested floor evaluator.",
        )

    def failures(self, state: SessionState, floors: tuple[str, ...]) -> tuple[FloorFailure, ...]:
        return tuple(
            failure for floor in floors if (failure := self.evaluate(state, floor)) is not None
        )


    @staticmethod
    def _clustering_id(facts: dict[str, Any]) -> str | None:
        return FloorEvaluator._analysis_id(facts, "clustering")

    @staticmethod
    def _analysis_id(facts: dict[str, Any], node: str) -> str | None:
        analysis = facts.get("analysis")
        entry = analysis.get(node) if isinstance(analysis, dict) else None
        value = entry.get("id") if isinstance(entry, dict) else None
        return str(value) if value else None

    @staticmethod
    def _batch_evidence(facts: dict[str, Any]) -> Any:
        batch = facts.get("batch")
        return batch.get("evidence") if isinstance(batch, dict) else None

    @staticmethod
    def _batch_decision(facts: dict[str, Any]) -> Any:
        batch = facts.get("batch")
        return batch.get("decision") if isinstance(batch, dict) else None

    @staticmethod
    def _user_consultation_recorded(decision: dict[str, Any]) -> bool:
        consultation = decision.get("user_consultation")
        return bool(
            isinstance(consultation, dict)
            and consultation.get("turn_id")
            and consultation.get("question_asked")
            and consultation.get("user_answer")
        )

    @staticmethod
    def _identities_current(
        facts: dict[str, Any], node: dict[str, Any], keys: tuple[tuple[str, str], ...]
    ) -> bool:
        analysis = facts.get("analysis")
        if not isinstance(analysis, dict):
            return False
        for fact_key, id_key in keys:
            entry = analysis.get(fact_key)
            current = entry.get("id") if isinstance(entry, dict) else None
            recorded = node.get(id_key)
            if not current or not recorded or current != recorded:
                return False
        return True

    @classmethod
    def _batch_identities_current(cls, facts: dict[str, Any], node: dict[str, Any]) -> bool:
        """Batch *evidence* is a measurement of a specific representation and clustering, so it is
        current only if all four identities — cells, counts, representation, clustering — still
        match the active analysis."""

        return cls._identities_current(
            facts,
            node,
            (
                ("cell_set", "cell_set_id"),
                ("count_representation", "count_representation_id"),
                ("representation", "representation_id"),
                ("clustering", "clustering_id"),
            ),
        )

    @classmethod
    def _batch_decision_current(cls, facts: dict[str, Any], node: dict[str, Any]) -> bool:
        """A batch *decision* governs how the cells and their counts are handled. Integration, and
        the reclustering that follows it, are the *execution* of that decision — not new inputs to
        it — so the decision is current whenever the cells and count representation it was taken
        against are unchanged. Binding it to representation/clustering (as the evidence is bound)
        would make honoring an 'integrate' decision immediately invalidate the decision that
        authorized it, forcing a circular re-investigation of batch on the corrected data at
        finalize time. Removing cells (e.g. cluster cleanup) still changes cell_set and correctly
        stales the decision the moment it happens."""

        return cls._identities_current(
            facts,
            node,
            (
                ("cell_set", "cell_set_id"),
                ("count_representation", "count_representation_id"),
            ),
        )


def render_outstanding_block(failures: tuple[FloorFailure, ...]) -> str:
    """Render the currently unmet floors as a system-prompt section.

    A floor is a state predicate, not a step in a pipeline, and there is more than one legitimate
    order in which to satisfy one. What makes them feel like a fixed pipeline is discovering them
    only on denial: a real session ran for two and a half hours and learned at `finalize_analysis`
    that four separate predicates were unmet, one of them stale since a filtering step near the
    beginning. Stating them up front costs a few lines and lets the model choose its own route.
    """

    if not failures:
        return ""
    lines = [
        "## Outstanding scientific requirements",
        "",
        "Unmet right now, in this session. These are state predicates, not a required order: "
        "satisfy them by whatever route the analysis actually takes, interleaved with the work "
        "the user asked for. Nothing here is a checklist to run through before starting, and an "
        "empty list is not permission to skip evidence. Recomputing something upstream can make "
        "one of these stale again, and you will be told when that happens.",
        "",
    ]
    for failure in failures:
        lines.append(f"- **{failure.floor}** — {failure.reason} {failure.remediation}")
    return "\n".join(lines)


def render_floor_change(
    before: frozenset[str],
    after: tuple[FloorFailure, ...],
) -> str:
    """Describe what just became required or stopped being required, or return ``""``.

    Reported per tool call so staleness surfaces when it is caused rather than when it blocks. The
    silence when nothing changed is the point: this must not read as a checklist being ticked off.
    """

    now = {failure.floor: failure for failure in after}
    newly_unmet = [floor for floor in now if floor not in before]
    newly_met = sorted(floor for floor in before if floor not in now)
    if not newly_unmet and not newly_met:
        return ""
    parts: list[str] = []
    if newly_unmet:
        detail = " ".join(
            f"[{floor}] {now[floor].reason} {now[floor].remediation}" for floor in newly_unmet
        )
        parts.append(
            f"This step made {len(newly_unmet)} scientific requirement(s) outstanding: {detail}"
        )
    if newly_met:
        parts.append(f"No longer outstanding: {', '.join(newly_met)}.")
    return " ".join(parts)
