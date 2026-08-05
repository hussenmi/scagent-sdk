"""Gene-first batch evidence, followed by a user-owned handling decision.

`investigate_batch` (``run_evidence``) produces evidence and records no decision. It is gene-first:
it finds sample-enriched cluster regions, characterizes each with a within-sample identity DEG
(that cluster versus the rest of its OWN batch, holding batch constant), matches the same population
across batches by shared **discriminating** identity genes, and compares a small number of confirmed
pairs directly. It writes five compact scientific CSVs plus SDK state and provenance artifacts.
Recurrence, composition, Cramer's V, neighborhood mixing, and design checks are supporting context;
they do not authorize or block a user choice. The DE test is Scanpy's in-environment Wilcoxon rank
test.

`decide_batch_handling` (``run_decision``) consumes a current evidence id after the model has
inspected the evidence and records the handling decision. Every substantive post-investigation
choice is taken directly from the newest real user turn after the evidence; integration
additionally needs an explicit basis. The alternative is ``request_guidance``, a pending state
that does not finalize.

Pure classifiers live at module scope for unit testing without Scanpy.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

# 2 adds ``consultation_anchor_sequence``: without it a decision cannot prove the user spoke
# *after* the evidence existed, so schema-1 evidence fails the current-evidence floor closed and
# must be re-investigated before a decision can be recorded against it.
BATCH_EVIDENCE_SCHEMA = 2
DE_ENGINE = "scanpy_wilcoxon"
# Version 4 resolves the user's answer from the session log itself. The model no longer has to
# discover or reproduce opaque turn ids, and it cannot paraphrase the user's answer inaccurately.
DECISION_POLICY_VERSION = 4
# Decisions that defer rather than resolve. They are always allowed without a consultation because
# they *are* the act of not deciding; the finalization floor treats ``request_guidance`` as pending.
DEFERRING_DECISIONS = frozenset({"request_guidance", "not_applicable"})
BATCH_EVIDENCE_FILENAMES = (
    "batch_diagnostic_sample_enriched_regions.csv",
    "batch_diagnostic_within_sample_degs.csv",
    "batch_diagnostic_population_pairs.csv",
    "batch_diagnostic_direct_pair_degs.csv",
    "batch_diagnostic_design_check.csv",
)
GENE_EVIDENCE_PLAIN = {
    "none": "No convincing cross-sample match with expression differences was established.",
    "localized": (
        "The same biological population appears in different samples and shows sample-associated "
        "differences, but the pattern is limited to the matched examples."
    ),
    "recurring_sample_associated": (
        "The same sample-associated expression pattern appears in more than one matched population."
    ),
}
DESIGN_INTERPRETATION_PLAIN = {
    "unknown": (
        "The available metadata does not say whether the samples are comparable replicates, "
        "different biological individuals or conditions, or known technical batches."
    ),
    "confounded_with_biology": (
        "Sample and biological condition overlap, so technical and biological differences cannot "
        "be separated from these data."
    ),
    "orthogonal_but_not_known_technical": (
        "The supplied condition is not confounded with sample, but sample differences may still "
        "include real donor or sample biology."
    ),
    "documented_technical_batch": (
        "The experimental information identifies a technical batch that is separate from the "
        "biological condition."
    ),
}
RECOMMENDATION_PLAIN = {
    "not_applicable": "There is no meaningful multi-sample batch question for this analysis.",
    "do_not_integrate_based_on_current_evidence": (
        "The current evidence does not by itself justify integration."
    ),
    "cannot_determine_technical_vs_biological": (
        "The samples clearly organize the data, but the genes alone cannot tell whether that "
        "structure is unwanted technical variation or real sample biology."
    ),
    "integration_optional_for_confirmed_replicates": (
        "Integration may be useful if the samples are intended to be comparable replicates."
    ),
    "integration_supported": (
        "The documented technical design and the observed sample-associated structure support "
        "integration."
    ),
}
DECISION_PLAIN = {
    "keep_uncorrected": "Keep the samples combined without integration",
    "integrate": "Integrate the samples",
    "separate": "Analyze the samples separately",
    "request_guidance": "Wait for the user's guidance",
    "not_applicable": "No batch-handling action is needed",
}

# --- versioned gene classification (shared vocabulary with cluster QC) -------
GENE_CLASS_VERSION = "batch-gene-class-v1"

_NUISANCE_PATTERNS = tuple(
    re.compile(p)
    for p in (
        r"^MT-",
        r"^MRP[LS]\d",
        r"^RP[LS]\d",
        r"^MALAT1$",
        r"^NEAT1$",
        r"^HB[ABDEGMQZ]$",
        r"^HBA\d$",
        r"^LINC\d",
        r"^A[CL]\d{6}\.",
        r"\.\d+$",
    )
)
# Broad/context: antigen presentation, heat-shock/ER stress, activation/IEG, housekeeping, cell
# cycle. Real programs, but shared across lineages, so they must not decide a population match.
_BROAD_PATTERNS = tuple(re.compile(p) for p in (r"^HLA-", r"^HSP", r"^HIST\d", r"^DNAJ"))
_BROAD_CONTEXT_GENES = frozenset(
    {
        "B2M",
        "ACTB",
        "ACTG1",
        "GAPDH",
        "TMSB4X",
        "TMSB10",
        "FTL",
        "FTH1",
        "FOS",
        "FOSB",
        "JUN",
        "JUNB",
        "JUND",
        "EGR1",
        "DUSP1",
        "NFKBIA",
        "IER2",
        "CD74",
        "XIST",
        "TPT1",
        "EEF1A1",
        "EEF2",
        "UBC",
        "UBB",
        "MKI67",
        "TOP2A",
        "STMN1",
        "TUBB",
        "TUBA1B",
        "PCNA",
        "CENPF",
        "HMGB1",
        "HMGB2",
        "S100A4",
        # ER/secretory stress program (the live false-match culprits: DERL3, HSP90B1, ...).
        "HSP90B1",
        "HSPA5",
        "DERL3",
        "XBP1",
        "SSR4",
        "SEC61B",
        "SDF2L1",
        "PDIA4",
        "PDIA6",
        "CALR",
        "CANX",
        "MANF",
        "DNAJB9",
        "SEL1L",
        "H13",
        "HM13",
    }
)


def gene_class(gene: str) -> str:
    """Classify a gene symbol as ``nuisance``, ``broad``, or ``discriminating``."""
    symbol = str(gene).upper()
    for pattern in _NUISANCE_PATTERNS:
        if pattern.search(symbol):
            return "nuisance"
    if symbol in _BROAD_CONTEXT_GENES:
        return "broad"
    for pattern in _BROAD_PATTERNS:
        if pattern.search(symbol):
            return "broad"
    return "discriminating"


def _identity(kind: str, value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return f"{kind}:sha256:{hashlib.sha256(encoded).hexdigest()}"


# --- pure classifiers --------------------------------------------------------
def region_enrichment(n_in_region: int, n_in_cluster: int, batch_global_fraction: float) -> float:
    """Enrichment of a batch within a cluster over that batch's dataset-wide frequency.

    A region that is 42% one sample when that sample is 9% of the data returns ~4.6 — caught even
    though raw purity (0.42) is modest.
    """
    if n_in_cluster <= 0 or batch_global_fraction <= 0:
        return 0.0
    return (n_in_region / n_in_cluster) / batch_global_fraction


def match_regions(
    disc_a: list[str], disc_b: list[str], *, min_shared: int, min_jaccard: float
) -> dict[str, Any]:
    """Decide whether two regions are the same population using DISCRIMINATING genes only.

    Broad/context and nuisance genes are excluded upstream, so an ER/stress or housekeeping overlap
    can no longer manufacture a match. A match needs both a minimum count of shared discriminating
    genes and a minimum Jaccard, and records an explicit rejection reason otherwise.
    """
    ranked_a = list(
        dict.fromkeys(str(g).upper() for g in disc_a if gene_class(g) == "discriminating")
    )
    ranked_b = list(
        dict.fromkeys(str(g).upper() for g in disc_b if gene_class(g) == "discriminating")
    )
    a25, b25 = ranked_a[:25], ranked_b[:25]
    a50, b50 = ranked_a[:50], ranked_b[:50]
    shared25 = [gene for gene in a25 if gene in set(b25)]
    shared50 = [gene for gene in a50 if gene in set(b50)]
    union50 = set(a50) | set(b50)
    jaccard = len(shared50) / len(union50) if union50 else 0.0
    if len(shared25) < min_shared:
        supported, reason = (
            False,
            f"only {len(shared25)} shared top-25 discriminating genes (<{min_shared})",
        )
    elif jaccard < min_jaccard:
        supported, reason = False, f"jaccard {jaccard:.2f} below {min_jaccard:.2f}"
    else:
        supported, reason = True, "shared discriminating identity above thresholds"
    return {
        "shared": len(shared25),
        "shared_top50": len(shared50),
        "jaccard": jaccard,
        "shared_genes": shared25,
        "shared_top50_genes": shared50,
        "supported": supported,
        "reason": reason,
    }


def summarize_recurrence(
    direct_rows: list[dict[str, Any]], *, min_populations: int = 2
) -> list[dict[str, Any]]:
    """Programs where the same batch is higher for a gene across >=2 distinct populations.

    Order-invariant: the result depends only on the set of (gene, batch, population) facts.
    """
    seen: dict[tuple[str, str], set[Any]] = {}
    for row in direct_rows:
        seen.setdefault((str(row["gene"]), str(row["higher_in_batch"])), set()).add(
            row["population"]
        )
    recurring = [
        {
            "gene": gene,
            "higher_in_batch": batch,
            "n_populations": len(pops),
            "populations": sorted(map(str, pops)),
        }
        for (gene, batch), pops in seen.items()
        if len(pops) >= min_populations
    ]
    recurring.sort(key=lambda r: (-r["n_populations"], r["gene"], r["higher_in_batch"]))
    return recurring


def classify_gene_evidence(n_matched_with_diffs: int, n_recurring_populations: int) -> str:
    """gene_evidence axis from cross-sample matches and program recurrence."""
    if n_recurring_populations >= 2:
        return "recurring_sample_associated"
    if n_matched_with_diffs >= 1:
        return "localized"
    return "none"


def classify_design(
    *, confounded_columns: list[str], technical_documented: bool, has_orthogonal_condition: bool
) -> str:
    """design_interpretation axis. Confounding takes priority over documented-technical status.

    Perfect biological confounding is never silently reclassified as technical: even when a batch is
    documented technical, a confounded design stays ``confounded_with_biology`` (so the
    recommendation is cannot-determine and integration requires an explicit override).
    """
    if confounded_columns:
        return "confounded_with_biology"
    if technical_documented:
        return "documented_technical_batch"
    if has_orthogonal_condition:
        return "orthogonal_but_not_known_technical"
    return "unknown"


def recommend(gene_evidence: str, design_interpretation: str) -> str:
    """Deterministic, non-binding recommendation.

    Only a recurring program together with a documented technical batch yields support.
    """
    if gene_evidence in ("none", "localized"):
        return "do_not_integrate_based_on_current_evidence"
    if design_interpretation == "documented_technical_batch":
        return "integration_supported"
    if design_interpretation == "orthogonal_but_not_known_technical":
        return "integration_optional_for_confirmed_replicates"
    return "cannot_determine_technical_vs_biological"


def validate_decision(decision: str, integration_basis: str | None) -> dict[str, Any]:
    """Keep only the consequential invariant: integration needs an explicit basis.

    Scientific evidence informs the conversation but does not become a hidden decision engine.
    User authorization is checked separately by :func:`validate_consultation`.
    """
    if decision != "integrate":
        return {"ok": True, "violation": None}
    if integration_basis not in (
        "documented_technical_batch",
        "user_authorized_integration",
    ):
        return {
            "ok": False,
            "violation": "integrate requires integration_basis (documented_technical_batch or "
            "user_authorized_integration)",
        }
    return {"ok": True, "violation": None}


def validate_integration_basis(
    integration_basis: str | None, evidence: dict[str, Any]
) -> dict[str, Any]:
    """A ``documented_technical_batch`` basis must be backed by the evidence itself.

    The model cannot assert a documented technical batch at decision time: the claim has to be
    present in the evidence object (``technical_batch_documented`` with a non-empty basis), which
    in turn required a real basis string when the evidence was produced.
    """
    if integration_basis != "documented_technical_batch":
        return {"ok": True, "violation": None}
    documented = bool(evidence.get("technical_batch_documented"))
    basis = evidence.get("technical_batch_basis")
    if documented and isinstance(basis, str) and basis.strip():
        return {"ok": True, "violation": None}
    return {
        "ok": False,
        "violation": "integration_basis='documented_technical_batch' requires evidence recorded "
        "with technical_batch_documented=true and a non-empty technical_batch_basis",
    }


def latest_user_turn_sequence(user_turns: Any) -> int:
    """Event sequence of the newest user turn, or 0 when the session records none.

    Written into the evidence as its consultation anchor, so a later decision can require that the
    user spoke *after* this evidence was produced rather than at any point in the session.
    """
    best = 0
    for turn in user_turns or ():
        if not isinstance(turn, dict):
            continue
        sequence = turn.get("sequence")
        if isinstance(sequence, int) and sequence > best:
            best = sequence
    return best


def consultation_question_id(batch_key: Any, sample_labels: Any) -> str:
    """Identity of the *question* put to the user, not of the evidence that raised it.

    "How should this sample-associated structure be handled?" is asked about a batch variable and
    the samples in it. It is not asked about a clustering or a representation, so re-measuring the
    same structure after integrating or re-clustering does not re-ask it, and must not invalidate
    the answer already given. Anchoring on ``evidence_id`` did exactly that: a session where the
    user said "yes integrate" trained scVI, re-clustered, re-investigated as the batch floor
    required, and then could not record the decision at all — every attempt was told the answer
    predated the evidence, while the only turn that could unblock it was a *future* one the user
    had no reason to type. Four consecutive failures, and the gate finally passed two hours later
    by harvesting an unrelated "can you finalize" as the batch answer.
    """

    labels = sorted({str(value) for value in sample_labels or ()})
    return _identity("batch-question", {"batch_key": None if batch_key is None else str(batch_key),
                                        "samples": labels})


def carry_forward_anchor(
    state_facts: Any,
    evidence_id: str,
    anchor_sequence: int,
    question_id: str | None = None,
) -> int:
    """Keep the earliest anchor recorded for this same question.

    The anchor answers "when did this question first arise", not "when was the tool last run". Any
    prior evidence for the same question keeps its anchor, so a user who already answered is never
    told to answer again. A genuinely different question (another batch variable, another sample
    set) starts over, which is correct: it has not been put to the user yet.
    """
    batch = state_facts.get("batch") if isinstance(state_facts, dict) else None
    previous = batch.get("evidence") if isinstance(batch, dict) else None
    if not isinstance(previous, dict):
        return anchor_sequence
    same_question = (
        question_id is not None
        and previous.get("consultation_question_id") == question_id
    ) or previous.get("evidence_id") == evidence_id
    if not same_question:
        return anchor_sequence
    recorded = previous.get("consultation_anchor_sequence")
    if not isinstance(recorded, int) or isinstance(recorded, bool):
        return anchor_sequence
    return min(anchor_sequence, recorded)


def carry_forward_consultation(state_facts: Any, question_id: str | None) -> dict[str, Any] | None:
    """Return the answer the user already gave to this same question, if there is one.

    A recorded human answer is durable evidence about the analysis, not about the artifact that was
    in front of the model when it was given. Once the user has said how to handle this batch
    variable, re-running the investigation may change the numbers but does not un-ask the question,
    so the recorded answer stays valid until the question itself changes.
    """

    batch = state_facts.get("batch") if isinstance(state_facts, dict) else None
    if not isinstance(batch, dict) or question_id is None:
        return None
    decision = batch.get("decision")
    if not isinstance(decision, dict) or decision.get("decision") in DEFERRING_DECISIONS:
        return None
    consultation = decision.get("user_consultation")
    if not isinstance(consultation, dict) or not consultation.get("turn_id"):
        return None
    if consultation.get("question_id") != question_id:
        return None
    return dict(consultation)


def validate_consultation(
    decision: str,
    question_asked: Any,
    *,
    anchor_sequence: int,
    user_turns: Any,
    question_id: str | None = None,
    prior_consultation: Any = None,
) -> dict[str, Any]:
    """Capture the real post-evidence user answer for keep/integrate/separate.

    The runtime resolves the turn id, sequence, and verbatim answer from the session event log. The
    model supplies only the question it asked. A turn that predates the question cannot answer it,
    which is what stops the opening analysis request from being read as authorization.

    The *first* turn after the question is taken as the answer, because that is what an answer is.
    Taking the newest instead let an unrelated later instruction be recorded as the user's batch
    decision. ``turns_after_anchor`` records how many turns followed, so a longer exchange is
    visible to an auditor rather than hidden behind one quoted line.

    An answer already recorded for this same question is honoured without a new turn: the user is
    never asked to repeat a decision they have made.

    ``request_guidance`` and ``not_applicable`` never need a consultation: they are the act of
    deferring. ``request_guidance`` does not satisfy the finalization floor, so the question stays
    open and the model is re-prompted until a real answer resolves it.
    """
    if decision in DEFERRING_DECISIONS:
        return {"ok": True, "violation": None, "consultation": None}

    if not isinstance(question_asked, str) or not question_asked.strip():
        return {
            "ok": False,
            "violation": (
                f"post-investigation decision {decision!r} requires question_asked and a later "
                "user turn; ask the user, or record 'request_guidance' until they reply"
            ),
            "consultation": None,
        }
    candidates: list[dict[str, Any]] = []
    for turn in user_turns or ():
        if not isinstance(turn, dict):
            continue
        sequence = turn.get("sequence")
        turn_id = turn.get("turn_id")
        answer = turn.get("user_prompt")
        if (
            isinstance(sequence, int)
            and not isinstance(sequence, bool)
            and sequence > anchor_sequence
            and isinstance(turn_id, str)
            and turn_id.strip()
            and isinstance(answer, str)
            and answer.strip()
        ):
            candidates.append(turn)
    if not candidates:
        carried = (
            dict(prior_consultation) if isinstance(prior_consultation, dict) else None
        )
        # An answer carries forward only to the question it answered. Checked here as well as at
        # the call site so the rule holds wherever this is called from.
        if carried and carried.get("question_id") != question_id:
            carried = None
        if carried and carried.get("turn_id"):
            carried["reaffirmed_without_new_turn"] = True
            carried["question_asked"] = question_asked.strip()
            return {"ok": True, "violation": None, "consultation": carried}
        return {
            "ok": False,
            "violation": (
                f"post-investigation decision {decision!r} has no user answer after the batch "
                "evidence; ask the user, or record 'request_guidance' until they reply"
            ),
            "consultation": None,
        }
    matched = min(candidates, key=lambda turn: int(turn["sequence"]))
    return {
        "ok": True,
        "violation": None,
        "consultation": {
            "turn_id": str(matched["turn_id"]),
            "turn_sequence": int(matched["sequence"]),
            "question_asked": question_asked.strip(),
            "user_answer": str(matched["user_prompt"]).strip(),
            "anchor_sequence": int(anchor_sequence),
            "turns_after_anchor": len(candidates),
            "question_id": question_id,
        },
    }


LEGEND_CARDINALITY_LIMIT = 12


def _figure_layout(n_batches: int, n_clusters: int) -> dict[str, Any]:
    """Choose a readable composition figure for the batch cardinality (advisory only)."""
    if n_batches > LEGEND_CARDINALITY_LIMIT:
        width = float(max(6.0, min(0.35 * n_batches + 3.0, 26.0)))
        height = float(max(4.0, min(0.30 * n_clusters + 2.0, 22.0)))
        return {
            "mode": "heatmap",
            "figsize": (width, height),
            "legend": "colorbar",
            "annotate": n_batches <= 30 and n_clusters <= 30,
            "tick_fontsize": 7 if max(n_batches, n_clusters) > 20 else 8,
        }
    width = float(max(7.0, min(0.5 * n_clusters + 2.0, 26.0)))
    return {
        "mode": "bar",
        "figsize": (width, 5.0),
        "legend": "outside",
        "legend_ncol": 1 if n_batches <= 8 else 2,
        "rotate_xticks": n_clusters > 12,
    }


def _cramers_v(table: Any) -> float:
    import numpy as np
    from scipy.stats import chi2_contingency

    values = np.asarray(table, dtype=float)
    n = values.sum()
    if n <= 0 or min(values.shape) <= 1:
        return 0.0
    chi2 = chi2_contingency(values, correction=False)[0]
    phi2 = chi2 / n
    rows, cols = values.shape
    corrected = max(0.0, phi2 - ((cols - 1) * (rows - 1)) / max(n - 1, 1))
    rows_corrected = rows - ((rows - 1) ** 2) / max(n - 1, 1)
    cols_corrected = cols - ((cols - 1) ** 2) / max(n - 1, 1)
    denominator = min(cols_corrected - 1, rows_corrected - 1)
    return float(math.sqrt(corrected / denominator)) if denominator > 0 else 0.0


def _resolve_input_identities(
    provenance: Any, adata: Any, cluster_key: str
) -> dict[str, str]:
    """Resolve portable identities from the artifact without consulting session state."""

    provenance = provenance if isinstance(provenance, dict) else {}
    cell_set_id = provenance.get("cell_set_id")
    if not isinstance(cell_set_id, str):
        cell_set_id = _identity("cells", sorted(map(str, adata.obs_names)))
    count_representation_id = provenance.get("count_representation_id")
    if not isinstance(count_representation_id, str):
        count_representation_id = _identity(
            "count-representation",
            {
                "cell_set_id": cell_set_id,
                "count_matrix_id": provenance.get("count_matrix_id"),
                "genes": list(map(str, adata.var_names)),
            },
        )
    representation_id = provenance.get("representation_id")
    if not isinstance(representation_id, str):
        representation_id = _identity(
            "representation",
            {
                "cell_set_id": cell_set_id,
                "shape": [int(adata.n_obs), int(adata.n_vars)],
                "source": "X",
            },
        )
    clustering_id = provenance.get("clustering_id")
    if not isinstance(clustering_id, str):
        clustering_id = _identity(
            "clustering",
            {
                "cell_set_id": cell_set_id,
                "key": cluster_key,
                "assignments": list(
                    zip(
                        map(str, adata.obs_names),
                        map(str, adata.obs[cluster_key]),
                        strict=True,
                    )
                ),
            },
        )
    return {
        "cell_set_id": cell_set_id,
        "count_representation_id": count_representation_id,
        "representation_id": representation_id,
        "clustering_id": clustering_id,
    }


def _analysis_patch(
    identities: dict[str, str], adata: Any, cluster_key: str
) -> dict[str, Any]:
    """Re-assert the identities this evidence is bound to.

    Deliberately does not touch ``dataset_revision.prepared_path``. Batch investigation reads a
    matrix and writes none, so asserting a head would let a read-only tool redirect the analysis to
    whatever file it happened to be handed -- including a stale one.
    """

    return {
        "cell_set": {"id": identities["cell_set_id"], "n_cells": int(adata.n_obs)},
        "count_representation": {"id": identities["count_representation_id"]},
        "representation": {"id": identities["representation_id"]},
        "clustering": {
            "id": identities["clustering_id"],
            "key": cluster_key,
            "n_clusters": int(adata.obs[cluster_key].astype(str).nunique()),
        },
    }


def _region_markers(
    sub: Any,
    target_mask: Any,
    *,
    n_top: int,
    min_lfc: float,
    max_padj: float,
    min_frac_diff: float,
    sc: Any,
    np: Any,
) -> dict[str, Any]:
    """One-vs-rest positive Wilcoxon markers for a region within its own batch subset.

    Returns full per-gene records (effect, q-value, target/reference fractions) plus the
    discriminating-only identity gene list used for cross-sample matching.
    """
    n_target = int(target_mask.sum())
    n_rest = int((~target_mask).sum())
    if n_target < 3 or n_rest < 3:
        return {"discriminating_genes": [], "records": [], "n_target": n_target, "n_rest": n_rest}
    sub = sub.copy()
    sub.obs["_grp"] = np.where(target_mask, "target", "rest")
    sc.tl.rank_genes_groups(
        sub, "_grp", groups=["target"], reference="rest", method="wilcoxon", pts=True
    )
    frame = sc.get.rank_genes_groups_df(sub, group="target")
    has_pts = "pct_nz_group" in frame.columns and "pct_nz_reference" in frame.columns
    matrix = sub.X
    mean_target = np.asarray(matrix[target_mask].mean(axis=0)).ravel()
    mean_reference = np.asarray(matrix[~target_mask].mean(axis=0)).ravel()
    gene_pos = {str(name): idx for idx, name in enumerate(sub.var_names)}
    positive = frame[(frame["logfoldchanges"] >= min_lfc) & (frame["pvals_adj"] <= max_padj)]
    positive = positive.sort_values("scores", ascending=False)
    records: list[dict[str, Any]] = []
    discriminating: list[str] = []
    for _, row in positive.head(max(n_top * 3, 60)).iterrows():
        gene = str(row["names"])
        position = gene_pos.get(gene)
        frac_diff = float(row["pct_nz_group"] - row["pct_nz_reference"]) if has_pts else None
        record = {
            "gene": gene,
            "gene_class": gene_class(gene),
            "expression_effect": (
                float(mean_target[position] - mean_reference[position])
                if position is not None
                else None
            ),
            "logfoldchange": float(row["logfoldchanges"]),
            "score": float(row["scores"]),
            "pvals_adj": float(row["pvals_adj"]),
            "pct_target": float(row["pct_nz_group"]) if has_pts else None,
            "pct_reference": float(row["pct_nz_reference"]) if has_pts else None,
        }
        records.append(record)
        if record["gene_class"] == "discriminating" and (
            frac_diff is None or frac_diff >= min_frac_diff
        ):
            discriminating.append(gene)
    return {
        "discriminating_genes": discriminating[:n_top],
        "records": records[: n_top * 2],
        "n_target": n_target,
        "n_rest": n_rest,
    }


def _union_find_components(
    region_keys: list[tuple[str, str]], edges: list[tuple[int, int]]
) -> dict[tuple[str, str], int]:
    parent = list(range(len(region_keys)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for a, b in edges:
        parent[find(a)] = find(b)
    return {region_keys[i]: find(i) for i in range(len(region_keys))}


def run_evidence(arguments: dict[str, Any], context: Any) -> dict[str, Any]:  # noqa: C901
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    import pandas as pd
    import scanpy as sc

    path = Path(str(arguments["path"])).expanduser().resolve()
    batch_key = arguments.get("batch_key")
    cluster_key = str(arguments.get("cluster_key", "leiden"))
    condition_keys = [str(k) for k in arguments.get("condition_keys", [])]
    technical_documented = bool(arguments.get("technical_batch_documented", False))
    technical_basis = arguments.get("technical_batch_basis")
    min_cells_region = int(arguments.get("min_cells_per_region", 30))
    min_enrichment = float(arguments.get("min_enrichment", 2.0))
    n_identity_genes = int(arguments.get("n_identity_genes", 50))
    max_regions = int(arguments.get("max_regions", 40))
    max_candidate_pairs = int(arguments.get("max_candidate_pairs", 3))
    max_pair_attempts = int(arguments.get("max_pair_attempts", 10))
    min_profile_correlation = float(arguments.get("min_profile_correlation", 0.4))
    min_shared = int(arguments.get("min_shared_identity_genes", 5))
    min_jaccard = float(arguments.get("min_match_jaccard", 0.15))
    n_neighbors = int(arguments.get("n_neighbors_for_mixing", 30))
    max_cells = int(arguments.get("max_cells_for_mixing", 20000))
    min_lfc = float(arguments.get("min_logfoldchange", 0.5))
    max_padj = float(arguments.get("max_adjusted_pvalue", 0.05))
    min_frac_diff = float(arguments.get("min_fraction_difference", 0.1))
    seed = int(arguments.get("random_seed", 0))
    if technical_documented and not (isinstance(technical_basis, str) and technical_basis.strip()):
        raise ValueError(
            "technical_batch_documented=true requires a non-empty technical_batch_basis"
        )

    effective_parameters = {
        "cluster_key": cluster_key,
        "condition_keys": sorted(condition_keys),
        "min_cells_per_region": min_cells_region,
        "min_enrichment": min_enrichment,
        "n_identity_genes": n_identity_genes,
        "max_regions": max_regions,
        "max_candidate_pairs": max_candidate_pairs,
        "max_pair_attempts": max_pair_attempts,
        "min_profile_correlation": min_profile_correlation,
        "min_shared_identity_genes": min_shared,
        "min_match_jaccard": min_jaccard,
        "min_logfoldchange": min_lfc,
        "max_adjusted_pvalue": max_padj,
        "min_fraction_difference": min_frac_diff,
        "random_seed": seed,
    }

    if not path.is_file():
        raise FileNotFoundError(path)
    adata = sc.read_h5ad(path)
    if cluster_key not in adata.obs:
        raise ValueError(f"observation column {cluster_key!r} is absent")
    ident = _resolve_input_identities(adata.uns.get("scagent_sdk", {}), adata, cluster_key)
    analysis_patch = _analysis_patch(ident, adata, cluster_key)

    # Newest user turn at evidence time. A consultation cited by a later decision must postdate it.
    # Deliberately excluded from every ``evidence_id``: it records *when* this evidence was made,
    # not what it found, so identical science in a different turn must keep the same identity or a
    # prior decision would go stale for a reason that is not scientific.
    anchor_sequence = latest_user_turn_sequence(getattr(context, "user_turns", ()))

    if batch_key is None:
        evidence = {
            "schema_version": BATCH_EVIDENCE_SCHEMA,
            "status": "not_applicable",
            "batch_key": None,
            "gene_evidence": "none",
            "design_interpretation": "unknown",
            "recommendation": "not_applicable",
            "de_engine": DE_ENGINE,
            "technical_batch_documented": technical_documented,
            "technical_batch_basis": technical_basis,
            "effective_parameters": effective_parameters,
            **ident,
        }
        # Hashed before the anchor is attached, so this identity stays purely scientific.
        evidence["evidence_id"] = _identity("batch-evidence", evidence)
        evidence["consultation_question_id"] = consultation_question_id(None, ())
        evidence["consultation_anchor_sequence"] = carry_forward_anchor(
            getattr(context, "state_facts", {}),
            evidence["evidence_id"],
            anchor_sequence,
            evidence["consultation_question_id"],
        )
        (context.staging_dir / "batch-investigation.md").write_text(
            "# Batch investigation\n\nNo meaningful batch key was selected; recorded "
            "`not_applicable` bound to the current analysis identities.\n",
            encoding="utf-8",
        )
        return {
            "summary": "Recorded not-applicable batch evidence for the input artifact identities.",
            "details": evidence,
            "facts_patch": {
                "analysis": analysis_patch,
                "batch": {"evidence": evidence, "decision": None},
            },
            "artifacts": [
                {
                    "name": "batch-report",
                    "relative_path": "batch-investigation.md",
                    "media_type": "text/markdown",
                }
            ],
        }

    batch_key = str(batch_key)
    if batch_key not in adata.obs:
        raise ValueError(f"observation column {batch_key!r} is absent")
    if bool(adata.obs[batch_key].isna().any()):
        raise ValueError(f"batch column {batch_key!r} contains missing values")
    batch = adata.obs[batch_key].astype(str)
    cluster = adata.obs[cluster_key].astype(str)
    if batch.nunique() < 2:
        raise ValueError("batch investigation requires at least two observed batch levels")

    # --- advisory context ---------------------------------------------------
    table = pd.crosstab(cluster, batch)
    proportions = table.div(table.sum(axis=1), axis=0)
    association = _cramers_v(table)
    table.to_csv(context.staging_dir / "batch-cluster-counts.csv")
    proportions.to_csv(context.staging_dir / "batch-cluster-proportions.csv")
    n_batches = int(proportions.shape[1])
    layout = _figure_layout(n_batches, int(proportions.shape[0]))
    if layout["mode"] == "heatmap":
        fig, ax = plt.subplots(figsize=layout["figsize"])
        image = ax.imshow(proportions.to_numpy(), aspect="auto", cmap="viridis", vmin=0.0, vmax=1.0)
        ax.set_xticks(range(n_batches))
        ax.set_xticklabels(
            list(map(str, proportions.columns)), rotation=90, fontsize=layout["tick_fontsize"]
        )
        ax.set_yticks(range(int(proportions.shape[0])))
        ax.set_yticklabels(list(map(str, proportions.index)), fontsize=layout["tick_fontsize"])
        ax.set_title(f"{batch_key} fraction within each {cluster_key} ({n_batches} batches)")
        fig.colorbar(image, ax=ax, label="fraction within cluster")
    else:
        ax = proportions.plot(kind="bar", stacked=True, figsize=layout["figsize"])
        ax.set_ylabel("fraction within cluster")
        ax.set_title(f"{batch_key} composition by {cluster_key}")
        ax.legend(
            title=str(batch_key),
            bbox_to_anchor=(1.02, 1.0),
            loc="upper left",
            ncol=layout["legend_ncol"],
            fontsize=8,
            frameon=False,
        )
        if layout["rotate_xticks"]:
            plt.setp(ax.get_xticklabels(), rotation=90, fontsize=8)
    plt.tight_layout()
    plt.savefig(context.staging_dir / "batch-composition.png", dpi=160, bbox_inches="tight")
    plt.close("all")

    mixing: dict[str, Any] = {"status": "unavailable", "reason": "X_pca is absent"}
    if "X_pca" in adata.obsm:
        from sklearn.neighbors import NearestNeighbors

        rng = np.random.default_rng(seed)
        indices = np.arange(adata.n_obs)
        if indices.size > max_cells:
            indices = np.sort(rng.choice(indices, size=max_cells, replace=False))
        embedding = np.asarray(adata.obsm["X_pca"])[indices]
        sampled_batch = batch.iloc[indices].to_numpy()
        used = min(n_neighbors, len(indices) - 1)
        nn = NearestNeighbors(n_neighbors=used + 1).fit(embedding)
        neighbor_idx = nn.kneighbors(return_distance=False)[:, 1:]
        neighbor_batches = sampled_batch[neighbor_idx]
        same = (neighbor_batches == sampled_batch[:, None]).mean(axis=1)
        levels = sorted(map(str, batch.unique()))
        entropies = [
            float(
                -(
                    pd.Series(v).value_counts(normalize=True)
                    * pd.Series(v).value_counts(normalize=True).map(math.log)
                ).sum()
            )
            / math.log(len(levels))
            for v in neighbor_batches
        ]
        freq = batch.value_counts(normalize=True)
        mixing = {
            "status": "complete",
            "representation": "X_pca",
            "mean_same_batch_neighbor_fraction": float(same.mean()),
            "random_composition_same_batch_fraction": float((freq**2).sum()),
            "mean_normalized_batch_entropy": float(np.mean(entropies)),
            "caution": "Descriptive mixing; not an integration objective.",
        }

    # --- stage 1: sample-enriched regions -----------------------------------
    global_freq = batch.value_counts(normalize=True).to_dict()
    cluster_sizes = cluster.value_counts().to_dict()
    regions: list[dict[str, Any]] = []
    for cl in table.index:
        for bt in table.columns:
            n_region = int(table.loc[cl, bt])
            if n_region < min_cells_region:
                continue
            enr = region_enrichment(
                n_region, int(cluster_sizes[str(cl)]), float(global_freq.get(str(bt), 0.0))
            )
            if enr >= min_enrichment:
                regions.append(
                    {"cluster": str(cl), "batch": str(bt), "n_cells": n_region, "enrichment": enr}
                )
    regions.sort(key=lambda r: r["enrichment"] * r["n_cells"], reverse=True)
    regions = regions[:max_regions]
    region_rows = [
        {
            "cluster": r["cluster"],
            "batch": r["batch"],
            "n_cells_in_region": r["n_cells"],
            "n_cells_in_cluster": int(cluster_sizes[r["cluster"]]),
            "batch_global_fraction": float(global_freq.get(r["batch"], 0.0)),
            "fraction_within_cluster": r["n_cells"] / int(cluster_sizes[r["cluster"]]),
            "enrichment_over_baseline": r["enrichment"],
        }
        for r in regions
    ]
    pd.DataFrame(region_rows).to_csv(
        context.staging_dir / "sample-enriched-regions.csv", index=False
    )
    diagnostic_region_rows = [
        {
            "cluster": row["cluster"],
            "sample": row["batch"],
            "n_cells": row["n_cells_in_region"],
            "n_cluster": row["n_cells_in_cluster"],
            "frac_of_cluster": round(row["fraction_within_cluster"], 4),
            "sample_baseline_frac": round(row["batch_global_fraction"], 4),
            "enrichment": round(row["enrichment_over_baseline"], 3),
        }
        for row in region_rows
    ]
    pd.DataFrame(
        diagnostic_region_rows,
        columns=[
            "cluster",
            "sample",
            "n_cells",
            "n_cluster",
            "frac_of_cluster",
            "sample_baseline_frac",
            "enrichment",
        ],
    ).to_csv(
        context.staging_dir / "batch_diagnostic_sample_enriched_regions.csv", index=False
    )

    # --- stage 2: within-sample identity DEGs (batch held constant) ---------
    within_rows: list[dict[str, Any]] = []
    for region in regions:
        sub = adata[batch.to_numpy() == region["batch"]]
        target = cluster[batch == region["batch"]].to_numpy() == region["cluster"]
        markers = _region_markers(
            sub,
            target,
            n_top=n_identity_genes,
            min_lfc=min_lfc,
            max_padj=max_padj,
            min_frac_diff=min_frac_diff,
            sc=sc,
            np=np,
        )
        region["discriminating_genes"] = markers["discriminating_genes"]
        for record in markers["records"]:
            within_rows.append(
                {
                    "cluster": region["cluster"],
                    "batch": region["batch"],
                    "target_cells": markers["n_target"],
                    "reference_cells": markers["n_rest"],
                    **record,
                }
            )
    pd.DataFrame(within_rows).to_csv(
        context.staging_dir / "within-sample-identity-degs.csv", index=False
    )
    diagnostic_within_rows = [
        {
            "_key": (row["cluster"], row["batch"]),
            "region": f"{row['cluster']}/{row['batch']}",
            "comparison": (
                f"{row['cluster']}/{row['batch']} vs rest of {row['batch']}"
            ),
            "target_cells": row["target_cells"],
            "reference_cells": row["reference_cells"],
            "gene": row["gene"],
            "higher_in": f"{row['cluster']}/{row['batch']}",
            "expression_effect": (
                round(float(row["expression_effect"]), 4)
                if row["expression_effect"] is not None
                else None
            ),
            "q_value": row["pvals_adj"],
            "engine_log2fc": round(float(row["logfoldchange"]), 4),
            "pct_expressed_target": row["pct_target"],
            "pct_expressed_reference": row["pct_reference"],
            "de_engine": "scanpy",
            "de_test": "wilcoxon",
            "matrix_source": "X",
        }
        for row in within_rows
    ]

    # --- stage 3: profile nomination, then identity-DEG confirmation --------
    region_keys = [(r["cluster"], r["batch"]) for r in regions]
    region_profiles: list[Any] = []
    for region in regions:
        mask = (cluster == region["cluster"]) & (batch == region["batch"])
        region_profiles.append(np.asarray(adata[mask.to_numpy()].X.mean(axis=0)).ravel())
    candidates: list[tuple[float, int, int]] = []
    for i in range(len(regions)):
        for j in range(i + 1, len(regions)):
            if regions[i]["batch"] == regions[j]["batch"]:
                continue
            left, right = region_profiles[i], region_profiles[j]
            corr = float(np.corrcoef(left, right)[0, 1])
            if np.isfinite(corr) and corr >= min_profile_correlation:
                candidates.append((corr, i, j))
    candidates.sort(key=lambda item: item[0], reverse=True)

    match_rows: list[dict[str, Any]] = []
    supported_edges: list[tuple[int, int, int, float]] = []
    attempts = 0
    for corr, i, j in candidates:
        if len(supported_edges) >= max_candidate_pairs or attempts >= max_pair_attempts:
            break
        attempts += 1
        m = match_regions(
            regions[i]["discriminating_genes"],
            regions[j]["discriminating_genes"],
            min_shared=min_shared,
            min_jaccard=min_jaccard,
        )
        match_rows.append(
            {
                "cluster_a": regions[i]["cluster"],
                "batch_a": regions[i]["batch"],
                "cluster_b": regions[j]["cluster"],
                "batch_b": regions[j]["batch"],
                "profile_correlation": round(corr, 3),
                "shared_discriminating": m["shared"],
                "shared_discriminating_top50": m["shared_top50"],
                "jaccard": round(m["jaccard"], 3),
                "shared_genes": ";".join(m["shared_genes"][:25]),
                "identity_match_supported": m["supported"],
                "rejection_reason": "" if m["supported"] else m["reason"],
            }
        )
        if m["supported"]:
            supported_edges.append((i, j, m["shared"], corr))
    pd.DataFrame(match_rows).to_csv(context.staging_dir / "population-matches.csv", index=False)
    diagnostic_match_rows = [
        {
            "cluster_a": row["cluster_a"],
            "sample_a": row["batch_a"],
            "cluster_b": row["cluster_b"],
            "sample_b": row["batch_b"],
            "profile_correlation": row["profile_correlation"],
            "n_shared_top25": row["shared_discriminating"],
            "n_shared_top50": row["shared_discriminating_top50"],
            "jaccard_top50": row["jaccard"],
            "identity_match_supported": row["identity_match_supported"],
            "shared_top25_genes": row["shared_genes"].replace(";", ", "),
        }
        for row in match_rows
    ]
    pd.DataFrame(
        diagnostic_match_rows,
        columns=[
            "cluster_a",
            "sample_a",
            "cluster_b",
            "sample_b",
            "profile_correlation",
            "n_shared_top25",
            "n_shared_top50",
            "jaccard_top50",
            "identity_match_supported",
            "shared_top25_genes",
        ],
    ).to_csv(
        context.staging_dir / "batch_diagnostic_population_pairs.csv", index=False
    )
    attempted_region_keys = {
        (row[cluster_side], row[batch_side])
        for row in match_rows
        for cluster_side, batch_side in (
            ("cluster_a", "batch_a"),
            ("cluster_b", "batch_b"),
        )
    }
    compact_diagnostic_within: list[dict[str, Any]] = []
    per_region_count: dict[tuple[str, str], int] = {}
    for row in diagnostic_within_rows:
        key = row["_key"]
        if key not in attempted_region_keys or per_region_count.get(key, 0) >= 50:
            continue
        compact_diagnostic_within.append(
            {name: value for name, value in row.items() if name != "_key"}
        )
        per_region_count[key] = per_region_count.get(key, 0) + 1
    diagnostic_within_rows = compact_diagnostic_within
    pd.DataFrame(
        diagnostic_within_rows,
        columns=[
            "region",
            "comparison",
            "target_cells",
            "reference_cells",
            "gene",
            "higher_in",
            "expression_effect",
            "q_value",
            "engine_log2fc",
            "pct_expressed_target",
            "pct_expressed_reference",
            "de_engine",
            "de_test",
            "matrix_source",
        ],
    ).to_csv(
        context.staging_dir / "batch_diagnostic_within_sample_degs.csv", index=False
    )
    de_edges = [(i, j) for (i, j, _shared, _corr) in supported_edges]

    # --- stage 4: direct matched comparison; recurrence is secondary --------
    direct_rows: list[dict[str, Any]] = []
    for pair_number, (i, j) in enumerate(de_edges):
        ri, rj = regions[i], regions[j]
        mask = ((cluster == ri["cluster"]) & (batch == ri["batch"])) | (
            (cluster == rj["cluster"]) & (batch == rj["batch"])
        )
        pair = adata[mask.to_numpy()].copy()
        is_a = (cluster[mask] == ri["cluster"]).to_numpy() & (batch[mask] == ri["batch"]).to_numpy()
        pair.obs["_pair"] = np.where(is_a, "A", "B")
        if int(is_a.sum()) < 3 or int((~is_a).sum()) < 3:
            continue
        sc.tl.rank_genes_groups(
            pair, "_pair", groups=["A"], reference="B", method="wilcoxon", pts=True
        )
        frame = sc.get.rank_genes_groups_df(pair, group="A")
        # Compute detection fractions directly: scanpy omits ``pct_nz_reference`` when the
        # reference is a named group, and these fractions are the noise gate below.
        matrix = pair.X
        matrix = matrix.toarray() if hasattr(matrix, "toarray") else np.asarray(matrix)
        pct_a_all = (matrix[is_a] > 0).mean(axis=0)
        pct_b_all = (matrix[~is_a] > 0).mean(axis=0)
        gene_pos = {str(name): idx for idx, name in enumerate(pair.var_names)}
        mean_a_all = matrix[is_a].mean(axis=0)
        mean_b_all = matrix[~is_a].mean(axis=0)
        sig = frame[frame["pvals_adj"] <= max_padj]
        for _, row in sig.iterrows():
            lfc = float(row["logfoldchanges"])
            if lfc >= min_lfc:
                higher = ri["batch"]
            elif lfc <= -min_lfc:
                higher = rj["batch"]
            else:
                continue
            # Detection fractions are recorded so the model can judge effect magnitude. NOTE: a
            # naive detection-fraction gate over-filters real programs on this data, so recurrence
            # is currently NOT noise-gated -- cell-level q-values rank separation and are not
            # sample-level replication, so low-expression genes can recur by chance. Weigh
            # pct_a/pct_b and effect size when reading recurring-programs.csv.
            position = gene_pos.get(str(row["names"]))
            if position is None:
                continue
            pct_a = float(pct_a_all[position])
            pct_b = float(pct_b_all[position])
            direct_rows.append(
                {
                    "population": pair_number,
                    "gene": str(row["names"]),
                    "gene_class": gene_class(str(row["names"])),
                    "higher_in_batch": higher,
                    "expression_effect": float(mean_a_all[position] - mean_b_all[position]),
                    "logfoldchange": lfc,
                    "score": float(row["scores"]),
                    "pvals_adj": float(row["pvals_adj"]),
                    "pct_a": pct_a,
                    "pct_b": pct_b,
                    "cluster_a": ri["cluster"],
                    "batch_a": ri["batch"],
                    "cluster_b": rj["cluster"],
                    "batch_b": rj["batch"],
                }
            )
    pd.DataFrame(direct_rows).to_csv(
        context.staging_dir / "direct-matched-region-degs.csv", index=False
    )
    diagnostic_direct_rows: list[dict[str, Any]] = []
    pair_groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    for row in direct_rows:
        key = (row["cluster_a"], row["batch_a"], row["cluster_b"], row["batch_b"])
        pair_groups.setdefault(key, []).append(row)
    for rows in pair_groups.values():
        up_a = sorted(
            (row for row in rows if row["expression_effect"] > 0),
            key=lambda row: row["expression_effect"],
            reverse=True,
        )[:25]
        up_b = sorted(
            (row for row in rows if row["expression_effect"] < 0),
            key=lambda row: row["expression_effect"],
        )[:25]
        for row in [*up_a, *up_b]:
            diagnostic_direct_rows.append(
                {
                    "cluster_a": row["cluster_a"],
                    "sample_a": row["batch_a"],
                    "cluster_b": row["cluster_b"],
                    "sample_b": row["batch_b"],
                    "gene": row["gene"],
                    "higher_in": row["higher_in_batch"],
                    "expression_effect": round(row["expression_effect"], 4),
                    "q_value": row["pvals_adj"],
                    "pct_expressed_sample_a": round(row["pct_a"], 4),
                    "pct_expressed_sample_b": round(row["pct_b"], 4),
                    "de_engine": "scanpy",
                    "de_test": "wilcoxon",
                    "matrix_source": "X",
                }
            )
    pd.DataFrame(
        diagnostic_direct_rows,
        columns=[
            "cluster_a",
            "sample_a",
            "cluster_b",
            "sample_b",
            "gene",
            "higher_in",
            "expression_effect",
            "q_value",
            "pct_expressed_sample_a",
            "pct_expressed_sample_b",
            "de_engine",
            "de_test",
            "matrix_source",
        ],
    ).to_csv(
        context.staging_dir / "batch_diagnostic_direct_pair_degs.csv", index=False
    )
    recurring = summarize_recurrence(direct_rows)
    pd.DataFrame(recurring).to_csv(context.staging_dir / "recurring-programs.csv", index=False)

    # Put the decisive gene-level comparisons directly in the result. A reader should not have to
    # infer the science from scalar mixing statistics or reopen a CSV before explaining the result.
    matched_populations: list[dict[str, Any]] = []
    for row in diagnostic_match_rows:
        if not row["identity_match_supported"]:
            continue
        pair_direct = [
            item
            for item in diagnostic_direct_rows
            if item["cluster_a"] == row["cluster_a"]
            and item["sample_a"] == row["sample_a"]
            and item["cluster_b"] == row["cluster_b"]
            and item["sample_b"] == row["sample_b"]
        ]
        higher_a = [
            item["gene"] for item in pair_direct if item["higher_in"] == row["sample_a"]
        ][:6]
        higher_b = [
            item["gene"] for item in pair_direct if item["higher_in"] == row["sample_b"]
        ][:6]
        matched_populations.append(
            {
                "cluster_a": row["cluster_a"],
                "sample_a": row["sample_a"],
                "within_sample_comparison_a": (
                    f"cluster {row['cluster_a']} vs all other cells in {row['sample_a']}"
                ),
                "cluster_b": row["cluster_b"],
                "sample_b": row["sample_b"],
                "within_sample_comparison_b": (
                    f"cluster {row['cluster_b']} vs all other cells in {row['sample_b']}"
                ),
                "profile_correlation": row["profile_correlation"],
                "shared_top25_count": row["n_shared_top25"],
                "shared_top50_count": row["n_shared_top50"],
                "shared_identity_genes": [
                    gene.strip()
                    for gene in str(row["shared_top25_genes"]).split(",")
                    if gene.strip()
                ],
                "direct_comparison_higher_in_a": higher_a,
                "direct_comparison_higher_in_b": higher_b,
            }
        )

    # --- stage 6: design / confounding --------------------------------------
    confounding_rows: list[dict[str, Any]] = []
    confounded_columns: list[str] = []
    has_orthogonal = False
    for column in condition_keys:
        if column not in adata.obs:
            confounding_rows.append({"column": column, "status": "absent"})
            continue
        raw = adata.obs[column]
        n_missing = int(raw.isna().sum())
        values = raw.astype(str)
        ctab = pd.crosstab(batch, values)
        batch_purity = ctab.div(ctab.sum(axis=1), axis=0).max(axis=1)
        condition_purity = ctab.div(ctab.sum(axis=0), axis=1).max(axis=0)
        median_batch_purity = float(batch_purity.median())
        median_condition_purity = float(condition_purity.median())
        perfectly_confounded = bool(
            median_batch_purity >= 0.95
            and median_condition_purity >= 0.95
            and ctab.shape[0] > 1
            and ctab.shape[1] > 1
        )
        assoc = _cramers_v(ctab)
        # Record how batch levels map onto condition levels so a reader can see the design, not
        # just a scalar association.
        mapping = {
            str(level): sorted(map(str, row[row > 0].index)) for level, row in ctab.iterrows()
        }
        confounding_rows.append(
            {
                "column": column,
                "status": "present",
                "perfectly_confounded": perfectly_confounded,
                "cramers_v": assoc,
                "n_missing": n_missing,
                "n_condition_levels": int(values.nunique()),
                "median_batch_purity": median_batch_purity,
                "median_condition_purity": median_condition_purity,
                "batch_to_condition_levels": json.dumps(mapping, sort_keys=True),
            }
        )
        if perfectly_confounded:
            confounded_columns.append(column)
        elif assoc < 0.8:
            has_orthogonal = True
    pd.DataFrame(confounding_rows).to_csv(
        context.staging_dir / "design-confounding.csv", index=False
    )
    diagnostic_design_rows = (
        [
            {
                "condition_key": "(none available)",
                "n_conditions": 0,
                "median_batch_purity": None,
                "median_condition_purity": None,
                "confounded_with_batch": None,
                "status": "unknown",
            }
        ]
        if not condition_keys
        else [
            {
                "condition_key": row["column"],
                "n_conditions": row.get("n_condition_levels", 0),
                "median_batch_purity": row.get("median_batch_purity"),
                "median_condition_purity": row.get("median_condition_purity"),
                "confounded_with_batch": row.get("perfectly_confounded"),
                "status": (
                    "absent"
                    if row.get("status") == "absent"
                    else "confounded"
                    if row.get("perfectly_confounded")
                    else "orthogonal"
                ),
            }
            for row in confounding_rows
        ]
    )
    pd.DataFrame(
        diagnostic_design_rows,
        columns=[
            "condition_key",
            "n_conditions",
            "median_batch_purity",
            "median_condition_purity",
            "confounded_with_batch",
            "status",
        ],
    ).to_csv(
        context.staging_dir / "batch_diagnostic_design_check.csv", index=False
    )

    n_matched_with_diffs = len({r["population"] for r in direct_rows})
    n_recurring_populations = max((r["n_populations"] for r in recurring), default=0)
    gene_evidence = classify_gene_evidence(n_matched_with_diffs, n_recurring_populations)
    design_interpretation = classify_design(
        confounded_columns=confounded_columns,
        technical_documented=technical_documented,
        has_orthogonal_condition=has_orthogonal,
    )
    recommendation = recommend(gene_evidence, design_interpretation)

    match_summary = [
        {
            "a": list(region_keys[i]),
            "b": list(region_keys[j]),
            "shared": shared,
            "profile_correlation": corr,
        }
        for (i, j, shared, corr) in supported_edges
    ]
    evidence = {
        "schema_version": BATCH_EVIDENCE_SCHEMA,
        "status": "complete",
        "batch_key": batch_key,
        "cluster_key": cluster_key,
        "n_batches": int(batch.nunique()),
        "n_clusters": int(cluster.nunique()),
        "gene_evidence": gene_evidence,
        "design_interpretation": design_interpretation,
        "recommendation": recommendation,
        "n_enriched_regions": len(regions),
        "n_supported_matches": len(supported_edges),
        "n_direct_compared_pairs": len(de_edges),
        "n_recurring_programs": len(recurring),
        "matched_populations": matched_populations,
        "recurring_programs": recurring[:20],
        "confounding": confounding_rows,
        "confounded_columns": confounded_columns,
        "technical_batch_documented": technical_documented,
        "technical_batch_basis": technical_basis,
        "consultation_anchor_sequence": anchor_sequence,
        "de_engine": DE_ENGINE,
        "gene_class_version": GENE_CLASS_VERSION,
        "advisory": {"cramers_v": association, "mixing": mixing},
        "figure_mode": layout["mode"],
        "effective_parameters": effective_parameters,
        "artifact_path": f"artifacts/capabilities/{context.execution_id}/batch-evidence.json",
        **ident,
    }
    evidence["evidence_id"] = _identity(
        "batch-evidence",
        {
            "schema": BATCH_EVIDENCE_SCHEMA,
            "gene_evidence": gene_evidence,
            "design_interpretation": design_interpretation,
            "recommendation": recommendation,
            "batch_key": batch_key,
            "identities": {k: ident[k] for k in ident},
            "effective_parameters": effective_parameters,
            "technical_batch_documented": technical_documented,
            "technical_batch_basis": technical_basis,
            "regions": region_keys,
            "matches": match_summary,
            "recurring": [(r["gene"], r["higher_in_batch"], r["n_populations"]) for r in recurring],
            "gene_class_version": GENE_CLASS_VERSION,
        },
    )
    evidence["consultation_question_id"] = consultation_question_id(
        batch_key, batch.unique().tolist()
    )
    evidence["consultation_anchor_sequence"] = carry_forward_anchor(
        getattr(context, "state_facts", {}),
        evidence["evidence_id"],
        anchor_sequence,
        evidence["consultation_question_id"],
    )
    (context.staging_dir / "batch-evidence.json").write_text(
        json.dumps(evidence, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )
    _write_evidence_report(context.staging_dir / "batch-investigation.md", evidence)
    _write_batch_readme(
        context.staging_dir / "README.md",
        evidence,
        {
            "batch_diagnostic_sample_enriched_regions.csv": len(diagnostic_region_rows),
            "batch_diagnostic_within_sample_degs.csv": len(diagnostic_within_rows),
            "batch_diagnostic_population_pairs.csv": len(diagnostic_match_rows),
            "batch_diagnostic_direct_pair_degs.csv": len(diagnostic_direct_rows),
            "batch_diagnostic_design_check.csv": len(diagnostic_design_rows),
        },
    )

    artifacts = [
        {
            "name": "batch-report",
            "relative_path": "batch-investigation.md",
            "media_type": "text/markdown",
        },
        {
            "name": "batch-evidence",
            "relative_path": "batch-evidence.json",
            "media_type": "application/json",
        },
        {
            "name": "batch-diagnostic-readme",
            "relative_path": "README.md",
            "media_type": "text/markdown",
            "include_in_output_view": False,
        },
        {
            "name": "batch-cluster-counts",
            "relative_path": "batch-cluster-counts.csv",
            "media_type": "text/csv",
        },
        {
            "name": "batch-cluster-proportions",
            "relative_path": "batch-cluster-proportions.csv",
            "media_type": "text/csv",
        },
        {
            "name": "sample-enriched-regions",
            "relative_path": "sample-enriched-regions.csv",
            "media_type": "text/csv",
        },
        {
            "name": "within-sample-identity-degs",
            "relative_path": "within-sample-identity-degs.csv",
            "media_type": "text/csv",
        },
        {
            "name": "population-matches",
            "relative_path": "population-matches.csv",
            "media_type": "text/csv",
        },
        {
            "name": "direct-matched-region-degs",
            "relative_path": "direct-matched-region-degs.csv",
            "media_type": "text/csv",
        },
        {
            "name": "recurring-programs",
            "relative_path": "recurring-programs.csv",
            "media_type": "text/csv",
        },
        {
            "name": "design-confounding",
            "relative_path": "design-confounding.csv",
            "media_type": "text/csv",
        },
        {
            "name": "batch-sample-enriched-regions",
            "relative_path": "batch_diagnostic_sample_enriched_regions.csv",
            "media_type": "text/csv",
        },
        {
            "name": "batch-within-sample-degs",
            "relative_path": "batch_diagnostic_within_sample_degs.csv",
            "media_type": "text/csv",
        },
        {
            "name": "batch-population-pairs",
            "relative_path": "batch_diagnostic_population_pairs.csv",
            "media_type": "text/csv",
        },
        {
            "name": "batch-direct-pair-degs",
            "relative_path": "batch_diagnostic_direct_pair_degs.csv",
            "media_type": "text/csv",
        },
        {
            "name": "batch-design-check",
            "relative_path": "batch_diagnostic_design_check.csv",
            "media_type": "text/csv",
        },
        {
            "name": "batch-composition",
            "relative_path": "batch-composition.png",
            "media_type": "image/png",
        },
    ]
    return {
        "summary": _evidence_summary(evidence),
        "details": evidence,
        "facts_patch": {
            "analysis": analysis_patch,
            "batch": {"evidence": evidence, "decision": None},
        },
        "artifacts": artifacts,
        "model_media": [a for a in artifacts if a["media_type"].startswith("image/")],
    }


def _write_batch_readme(
    path: Path, evidence: dict[str, Any], row_counts: dict[str, int]
) -> None:
    """Document the compact gene-first evidence packet."""
    descriptions = {
        "batch_diagnostic_sample_enriched_regions.csv": (
            "Sample-enriched cluster regions, measured relative to each sample's dataset-wide "
            "frequency rather than by a raw-purity cutoff."
        ),
        "batch_diagnostic_within_sample_degs.csv": (
            "Primary evidence: each region versus all other cells in its own sample. These DEGs "
            "describe population identity while holding sample constant."
        ),
        "batch_diagnostic_population_pairs.csv": (
            "Cross-sample candidate populations nominated by mean-expression similarity and "
            "confirmed by overlap between the two within-sample identity DEG signatures."
        ),
        "batch_diagnostic_direct_pair_degs.csv": (
            "Secondary evidence: direct DE between confirmed cross-sample population pairs, "
            "describing how the samples differ without claiming the cause is technical."
        ),
        "batch_diagnostic_design_check.csv": (
            "Whether supplied condition metadata is confounded with sample; missing design "
            "metadata is recorded as unknown rather than treated as evidence of no confounding."
        ),
    }
    lines = [
        "# Batch-effect diagnostic — how to read these files",
        "",
        "These files ask whether several samples show sample-associated structure that may matter "
        "for the analysis. The test is gene-first: it does not assume that separation in a plot is "
        "technical, because real biology can separate samples too.",
        "",
        "The main evidence comes from finding a cell population in two samples. In each sample, "
        "the cluster is compared with all other cells in that SAME sample. This holds sample "
        "constant and gives an identity-gene list for the population. If the two lists share many "
        "genes, the regions likely represent the same population despite being separated by "
        "sample. Their direct comparison then describes how the two versions differ. It cannot, "
        "by itself, decide whether those differences are technical or biological.",
        "",
        f"- Batch key: `{evidence['batch_key']}`",
        f"- Cluster key: `{evidence['cluster_key']}`",
        f"- DE engine: `{evidence['de_engine']}`",
        "",
        "## Files",
        "",
    ]
    for filename, description in descriptions.items():
        lines.extend(
            [
                f"### `{filename}` ({row_counts.get(filename, 0)} rows)",
                "",
                description,
                "",
            ]
        )
    lines.extend(
        [
            "## Interpretation",
            "",
            "Review the named matched populations and their shared identity genes, then put the "
            "choice to the user: integrate, keep combined uncorrected, analyze separately, or "
            "provide more information about the experiment. Similar DEG signatures establish a "
            "shared population identity and sample-associated separation; experimental design "
            "determines whether correcting that separation is appropriate.",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def _write_evidence_report(path: Path, evidence: dict[str, Any]) -> None:
    gene_finding = GENE_EVIDENCE_PLAIN.get(
        str(evidence["gene_evidence"]), str(evidence["gene_evidence"])
    )
    design_finding = DESIGN_INTERPRETATION_PLAIN.get(
        str(evidence["design_interpretation"]), str(evidence["design_interpretation"])
    )
    practical_interpretation = RECOMMENDATION_PLAIN.get(
        str(evidence["recommendation"]), str(evidence["recommendation"])
    )
    next_step = {
        "not_applicable": "No batch-handling action is needed.",
        "do_not_integrate_based_on_current_evidence": (
            "Do not integrate on the current evidence alone. Keep the samples uncorrected or "
            "collect clearer experimental information first."
        ),
        "cannot_determine_technical_vs_biological": (
            "The expression evidence supports a real sample-associated pattern, but the "
            "experimental design must decide whether integration is appropriate. Present the "
            "evidence and let the user choose integration, one combined uncorrected analysis, "
            "separate analyses, or additional design context."
        ),
        "integration_optional_for_confirmed_replicates": (
            "If these samples are intended as comparable replicates, integration is reasonable; "
            "otherwise preserve the sample differences and analyze them explicitly."
        ),
        "integration_supported": (
            "Integration is recommended because the experimental record identifies a technical "
            "batch and the same populations show recurring sample-associated expression."
        ),
    }.get(str(evidence["recommendation"]), practical_interpretation)
    lines = [
        "# Batch-effect investigation",
        "",
        "## Conclusion",
        "",
        practical_interpretation,
        "",
        f"The gene-level result was: {gene_finding}",
        "",
        "## Recommendation",
        "",
        next_step,
        "",
        "## What was done",
        "",
        f"The analysis first found {evidence['n_enriched_regions']} cluster-and-sample regions "
        "where one sample was more common than expected. It then looked for the same cell "
        "population in different samples.",
        "",
        "For each candidate region, the cluster was compared with every other cell in the SAME "
        "sample. This holds sample constant and identifies genes that describe the population "
        "itself. Two regions from different samples were treated as a likely match only when "
        "their within-sample identity-gene lists shared enough informative genes.",
        "",
        "This is the key test: if two separated regions have the same identity genes after each "
        "is compared only within its own sample, the simplest interpretation is that they are "
        "versions of the same population that the uncorrected representation is organizing by "
        "sample.",
        "",
        "## Matched populations",
        "",
    ]
    matches = evidence.get("matched_populations") or []
    if not matches:
        lines.append(
            "No cross-sample population match passed the identity-gene check, so the gene-level "
            "evidence is currently limited."
        )
    for match in matches:
        shared = ", ".join(match.get("shared_identity_genes", [])[:8]) or "none listed"
        lines += [
            f"### Cluster {match['cluster_a']} in {match['sample_a']} and cluster "
            f"{match['cluster_b']} in {match['sample_b']}",
            "",
            f"- Compared within {match['sample_a']}: {match['within_sample_comparison_a']}.",
            f"- Compared within {match['sample_b']}: {match['within_sample_comparison_b']}.",
            f"- The two identity lists share {match['shared_top25_count']} of their top 25 genes "
            f"({shared}).",
        ]
        higher_a = ", ".join(match.get("direct_comparison_higher_in_a", []))
        higher_b = ", ".join(match.get("direct_comparison_higher_in_b", []))
        if higher_a or higher_b:
            lines += [
                f"- In the direct comparison, genes higher in {match['sample_a']} included "
                f"{higher_a or 'none listed'}; genes higher in {match['sample_b']} included "
                f"{higher_b or 'none listed'}.",
            ]
        lines += [
            "- This supports that a similar population is present in both samples and is being "
            "organized partly by sample. It does not prove that the cause is technical.",
            "",
        ]
    lines += [
        "## Evidence-to-interpretation",
        "",
        f"- Gene-level finding: {gene_finding}",
        f"- Experimental-design limit: {design_finding}",
        f"- Practical interpretation: {practical_interpretation}",
        "",
        "The matched-population DEGs are the main evidence. Cluster composition, Cramér's V, and "
        "neighborhood mixing only show where samples separate; they cannot say why. The direct "
        "pair DEGs describe how each matched population differs between samples, and recurrence "
        "across several populations strengthens the evidence that the pattern is sample-wide.",
        "",
        "## Supporting files",
        "",
        "- `batch_diagnostic_population_pairs.csv` — the matched cross-sample populations and "
        "their shared identity genes.",
        "- `batch_diagnostic_within_sample_degs.csv` — each candidate population compared with "
        "all other cells in the same sample.",
        "- `batch_diagnostic_direct_pair_degs.csv` — expression differences between the matched "
        "versions of a population.",
        "- `batch_diagnostic_sample_enriched_regions.csv` — where samples are over-represented.",
        "- `batch_diagnostic_design_check.csv` — whether available experimental metadata can "
        "separate technical batch from biology.",
        "- `batch-composition.png` — how the samples are distributed across clusters.",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _evidence_summary(evidence: dict[str, Any]) -> str:
    matches = evidence.get("matched_populations") or []
    opening = (
        f"Batch investigation for {evidence['batch_key']!r}: "
        f"{evidence['n_enriched_regions']} sample-enriched regions and "
        f"{len(matches)} cross-sample population match(es)."
    )
    if matches:
        example = matches[0]
        genes = ", ".join(example.get("shared_identity_genes", [])[:6])
        comparison = (
            f" Example: cluster {example['cluster_a']} in {example['sample_a']} and cluster "
            f"{example['cluster_b']} in {example['sample_b']} share "
            f"{example['shared_top25_count']}/25 within-sample identity genes"
            f" ({genes})."
        )
        interpretation = (
            " This shows sample-associated organization, not whether its cause is technical."
        )
    else:
        comparison = " No cross-sample identity match passed the gene-level check."
        interpretation = (
            " The gene test therefore did not establish that a shared population is being split "
            "by sample."
        )
    return (
        opening
        + comparison
        + interpretation
        + " "
        "Explain the gene comparisons, then ask whether to integrate, keep combined uncorrected, "
        "analyze separately, or add experimental context."
    )


def _write_decision_report(
    path: Path,
    *,
    decision: str,
    decision_plain: str,
    recommendation_plain: str,
    evidence: dict[str, Any],
    integration_basis: str | None,
    consultation: dict[str, Any] | None,
    rationale: str,
) -> None:
    matches = evidence.get("matched_populations") or []
    lines = [
        "# Batch-handling decision",
        "",
        "## Decision",
        "",
        f"**{decision_plain}.**",
        "",
        "## Evidence considered",
        "",
        recommendation_plain,
        "",
    ]
    if matches:
        lines.append(
            f"The investigation matched {len(matches)} population pair(s) across samples using "
            "within-sample identity DEGs:"
        )
        lines.append("")
        for match in matches:
            genes = ", ".join(match.get("shared_identity_genes", [])[:6]) or "none listed"
            lines.append(
                f"- Cluster {match['cluster_a']} in {match['sample_a']} and cluster "
                f"{match['cluster_b']} in {match['sample_b']} shared "
                f"{match['shared_top25_count']}/25 identity genes ({genes})."
            )
    else:
        lines.append(
            "No cross-sample population pair passed the identity-DEG match, so the gene-level "
            "evidence was limited."
        )
    lines.extend(
        [
            "",
            "The matched-population evidence shows sample-associated organization. It does not "
            "by itself prove that the differences are technical rather than biological.",
        ]
    )
    if consultation:
        lines.extend(
            [
                "",
                "## What the user said",
                "",
                f"- Question: {consultation['question_asked']}",
                f"- Answer: {consultation['user_answer']}",
            ]
        )
    elif decision == "request_guidance":
        lines.extend(
            [
                "",
                "## Current status",
                "",
                "**Pending.** The evidence has been presented, but the user has not chosen how to "
                "handle the samples. Integration remains unauthorized and finalization remains "
                "pending.",
            ]
        )
    lines.extend(["", "## Why this decision was recorded", "", rationale])
    if decision == "integrate":
        lines.extend(
            [
                "",
                "## Effect on the analysis",
                "",
                "Integration is now authorized. The integration result must still be evaluated "
                "after training; this decision does not guarantee that correction will improve "
                "the representation.",
            ]
        )
    elif decision == "keep_uncorrected":
        lines.extend(
            [
                "",
                "## Effect on the analysis",
                "",
                "The samples will remain in one uncorrected analysis, and sample-associated "
                "structure must be considered when interpreting clusters and differential "
                "expression.",
            ]
        )
    elif decision == "separate":
        lines.extend(
            [
                "",
                "## Effect on the analysis",
                "",
                "The samples will be analyzed separately rather than forced into one shared "
                "representation.",
            ]
        )
    lines.extend(
        [
            "",
            "## Audit record",
            "",
            f"- Evidence: `{evidence['evidence_id']}`",
            f"- Integration basis: `{integration_basis or 'not needed'}`",
        ]
    )
    if consultation:
        lines.append(
            f"- Answered in turn `{consultation['turn_id']}` at event "
            f"{consultation['turn_sequence']}, after evidence event "
            f"{consultation['anchor_sequence']}."
        )
        if consultation.get("reaffirmed_without_new_turn"):
            lines.append(
                "- The investigation was re-run after that answer. The batch variable and its "
                "samples are unchanged, so the same recorded answer applies; the user was not "
                "asked to repeat it."
            )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_decision(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    evidence_id = str(arguments["evidence_id"])
    decision = str(arguments["decision"])
    integration_basis = arguments.get("integration_basis")

    batch = context.state_facts.get("batch")
    evidence = batch.get("evidence") if isinstance(batch, dict) else None
    if not isinstance(evidence, dict) or evidence.get("status") not in (
        "complete",
        "not_applicable",
    ):
        raise ValueError("no current batch evidence is available; run investigate_batch first")
    if evidence.get("evidence_id") != evidence_id:
        raise ValueError("evidence_id does not match the current batch evidence")
    if decision == "not_applicable" and evidence.get("status") != "not_applicable":
        raise ValueError("not_applicable decision requires not_applicable evidence")

    recommendation = str(evidence.get("recommendation"))
    decision_plain = DECISION_PLAIN.get(decision, decision)
    recommendation_plain = RECOMMENDATION_PLAIN.get(recommendation, recommendation)
    basis_verdict = validate_integration_basis(integration_basis, evidence)
    if not basis_verdict["ok"]:
        raise ValueError(basis_verdict["violation"])
    verdict = validate_decision(decision, integration_basis)
    if not verdict["ok"]:
        raise ValueError(verdict["violation"])
    question_id = evidence.get("consultation_question_id")
    question_id = question_id if isinstance(question_id, str) else None
    consultation_verdict = validate_consultation(
        decision,
        arguments.get("question_asked"),
        anchor_sequence=int(evidence.get("consultation_anchor_sequence") or 0),
        user_turns=getattr(context, "user_turns", ()),
        question_id=question_id,
        prior_consultation=carry_forward_consultation(context.state_facts, question_id),
    )
    if not consultation_verdict["ok"]:
        raise ValueError(consultation_verdict["violation"])
    consultation = consultation_verdict["consultation"]
    if consultation:
        rationale = (
            f"After reviewing the gene-level batch evidence, the user chose to "
            f"{decision_plain.casefold()}. This records the requested analysis handling; it does "
            "not by itself establish that sample-associated differences are technical."
        )
    elif decision == "request_guidance":
        rationale = "The evidence was presented and the analysis is waiting for the user's choice."
    else:
        rationale = "No multi-sample batch-handling decision applies to this analysis."

    decision_fact = {
        "decision": decision,
        "rationale": rationale,
        "evidence_id": evidence_id,
        "integration_basis": integration_basis,
        "recommendation": recommendation,
        "user_consultation": consultation,
        "validated": True,
        "decision_policy_version": DECISION_POLICY_VERSION,
        "cell_set_id": evidence.get("cell_set_id"),
        "count_representation_id": evidence.get("count_representation_id"),
        "representation_id": evidence.get("representation_id"),
        "clustering_id": evidence.get("clustering_id"),
    }
    _write_decision_report(
        context.staging_dir / "batch-decision.md",
        decision=decision,
        decision_plain=decision_plain,
        recommendation_plain=recommendation_plain,
        evidence=evidence,
        integration_basis=str(integration_basis) if integration_basis is not None else None,
        consultation=consultation,
        rationale=rationale,
    )
    summary = f"Recorded the user's decision: {decision_plain}."
    if decision == "request_guidance":
        summary += " Pending a user answer; finalization stays blocked until one is recorded."
    elif consultation:
        summary += f" Authorized by the user in turn {consultation['turn_id']}."
    return {
        "summary": summary,
        "details": decision_fact,
        "facts_patch": {"batch": {"decision": decision_fact}},
        "decisions_patch": {"batch_handling": {"decision": decision, "rationale": rationale}},
        "artifacts": [
            {
                "name": "batch-decision",
                "relative_path": "batch-decision.md",
                "media_type": "text/markdown",
            }
        ],
    }
