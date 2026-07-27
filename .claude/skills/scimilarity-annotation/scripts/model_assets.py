"""Shared SCimilarity model, gene, and organism resolution for this skill's tools.

Every tool here needs the same four things before it may run: a validated local model
directory, a raw-count matrix, an input gene-name source that overlaps the model vocabulary,
and confidence that the declared organism matches the data. Those checks are deliberately
cheap and file-based so a wrong request fails in milliseconds rather than after loading an
encoder, a reference-label table, or a multi-gigabyte neighbor index.

Organism is declared, never guessed from letter case: symbol casing is a pipeline convention,
not a species. It is then *verified* against the two model vocabularies, which is evidence the
host actually has, rather than a curated marker list this skill would have to maintain.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

_SYMBOL_COLUMNS = (
    "feature_name",
    "gene_symbols",
    "gene_symbol",
    "gene_name",
    "gene_names",
    "mgi_symbol",
    "hgnc_symbol",
    "symbol",
)

ORGANISM_VARIABLES = {
    "human": "SCIMILARITY_MODEL_PATH",
    "mouse": "SCIMILARITY_MODEL_PATH_MOUSE",
}
EMBEDDING_FILES = ("encoder.ckpt", "gene_order.tsv")
CELLSEARCH_DIRECTORY = "cellsearch"
CELLSEARCH_INDEX = "full_kNN.bin"
CELLSEARCH_METADATA = "cell_metadata"

# A species verdict needs enough organism-specific symbols to mean anything. Ensembl-only
# inputs legitimately hit zero of them, and must not be refused on that basis; the gene-overlap
# threshold is what catches those. The ratio is deliberately loose because real margins are
# large: human colorectal data hits ~2,300 human-only against ~20 mouse-only symbols, while
# mouse input of any casing hits thousands of mouse-only against zero human-only.
SPECIES_EVIDENCE_MINIMUM = 50
SPECIES_CONTRADICTION_RATIO = 2.0


def _identity(kind: str, value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return f"{kind}:sha256:{hashlib.sha256(encoded).hexdigest()}"


def _validate_counts(matrix: Any, *, label: str) -> None:
    import numpy as np

    values = np.asarray(matrix.tocsr().data if hasattr(matrix, "tocsr") else matrix).ravel()
    if values.size and (
        not bool(np.all(np.isfinite(values)))
        or not bool(np.all(values >= 0))
        or not bool(np.all(values == np.round(values)))
    ):
        raise ValueError(
            f"{label} is not finite nonnegative integer counts. SCimilarity performs its "
            "own normalization, so pass raw counts rather than normalized or scaled values."
        )


def _select_counts(adata: Any, counts_layer: str | None) -> tuple[Any, str]:
    if counts_layer is None or counts_layer == "X":
        return adata.X, "X"
    if counts_layer in adata.layers:
        return adata.layers[counts_layer], f"layer:{counts_layer}"
    raise ValueError(
        f"count layer {counts_layer!r} is absent; use counts_layer=X or null for adata.X"
    )


def _select_gene_names(
    original: list[str],
    columns: dict[str, list[str]],
    reference: Any,
) -> tuple[list[str], str, int]:
    ref_by_upper = {str(gene).upper(): str(gene) for gene in reference}
    candidates: list[tuple[str, list[str]]] = [("var_names", original)]
    for column in _SYMBOL_COLUMNS:
        if column in columns:
            candidates.append((f"var[{column!r}]", columns[column]))
    source, names = max(
        candidates,
        key=lambda item: sum(
            1 for name in item[1] if str(name).upper() in ref_by_upper
        ),
    )
    hits = sum(1 for name in names if str(name).upper() in ref_by_upper)
    aligned = [ref_by_upper.get(str(name).upper(), str(name)) for name in names]
    seen: set[str] = set()
    unique = []
    for aligned_name, original_name in zip(aligned, original, strict=True):
        value = aligned_name if aligned_name not in seen else original_name
        unique.append(value)
        seen.add(value)
    return unique, source, hits


def _best_gene_names(var: Any, reference: Any) -> tuple[Any, str, int]:
    """Choose the input name source with the greatest model-vocabulary overlap."""

    import pandas as pd

    original = [str(name) for name in var.index]
    columns = {
        column: [str(value) for value in var[column]]
        for column in _SYMBOL_COLUMNS
        if column in var.columns
    }
    aligned, source, hits = _select_gene_names(original, columns, reference)
    return pd.Index(aligned), source, hits


def _candidate_names(var: Any) -> list[str]:
    """Every input name the gene selector could have chosen, for species evidence."""

    names = [str(name) for name in var.index]
    for column in _SYMBOL_COLUMNS:
        if column in var.columns:
            names.extend(str(value) for value in var[column])
    return names


def read_gene_order(model_path: Path) -> list[str]:
    """Read a model's exact ordered vocabulary without constructing the encoder."""

    with (model_path / "gene_order.tsv").open("r", encoding="utf-8") as handle:
        return [line.strip() for line in handle if line.strip()]


def declared_organism(arguments: dict[str, Any]) -> str:
    """Return the explicitly declared organism, refusing to assume one.

    There is no default. A wrong species silently produces confident labels from the wrong
    reference, which is worse than an error, and case-based inference cannot tell an
    uppercased mouse matrix from a human one.
    """

    value = str(arguments.get("organism") or "").strip().lower()
    if value not in ORGANISM_VARIABLES:
        raise ValueError(
            "organism must be declared explicitly as 'human' or 'mouse'; it is never inferred "
            "from gene-symbol casing. State the species from the dataset metadata or the user."
        )
    return value


def configured_vocabularies() -> dict[str, list[str]]:
    """Vocabularies of every organism model configured and present on this host."""

    found: dict[str, list[str]] = {}
    for organism, variable in ORGANISM_VARIABLES.items():
        configured = os.environ.get(variable)
        if not configured:
            continue
        directory = Path(configured).expanduser()
        if (directory / "gene_order.tsv").is_file():
            try:
                found[organism] = read_gene_order(directory)
            except OSError:
                continue
    return found


def species_evidence(
    names: list[str],
    *,
    declared: str,
    vocabularies: dict[str, list[str]],
) -> dict[str, Any]:
    """Test a declared organism against the organism-specific parts of the model vocabularies.

    Symbols shared between the two references carry no species information, so only the
    case-folded set difference is scored. The verdict is ``consistent``, ``contradicted``, or
    ``unverified`` when the comparison could not be made — never a silent pass.
    """

    folded = {str(name).upper() for name in names if str(name).strip()}
    counter = next((name for name in ORGANISM_VARIABLES if name != declared), None)
    declared_vocabulary = vocabularies.get(declared)
    counter_vocabulary = vocabularies.get(counter) if counter else None
    evidence: dict[str, Any] = {
        "declared_organism": declared,
        "compared_with": counter if counter_vocabulary else None,
        "declared_specific_hits": None,
        "counter_specific_hits": None,
        "minimum_evidence": SPECIES_EVIDENCE_MINIMUM,
        "contradiction_ratio": SPECIES_CONTRADICTION_RATIO,
    }
    if not declared_vocabulary or not counter_vocabulary:
        evidence["verdict"] = "unverified"
        evidence["reason"] = (
            "only one organism model is configured on this host, so there is nothing to "
            "compare the declared organism against"
        )
        return evidence
    declared_folded = {gene.upper() for gene in declared_vocabulary}
    counter_folded = {gene.upper() for gene in counter_vocabulary}
    declared_specific = declared_folded - counter_folded
    counter_specific = counter_folded - declared_folded
    if not declared_specific or not counter_specific:
        evidence["verdict"] = "unverified"
        evidence["reason"] = "the configured organism models share one vocabulary"
        return evidence
    declared_hits = len(folded & declared_specific)
    counter_hits = len(folded & counter_specific)
    evidence["declared_specific_hits"] = declared_hits
    evidence["counter_specific_hits"] = counter_hits
    if declared_hits + counter_hits < SPECIES_EVIDENCE_MINIMUM:
        evidence["verdict"] = "unverified"
        evidence["reason"] = (
            f"only {declared_hits + counter_hits} organism-specific symbols were recognized, "
            f"below the {SPECIES_EVIDENCE_MINIMUM} needed for a verdict; gene identifiers may "
            "not be symbols"
        )
        return evidence
    if counter_hits > declared_hits * SPECIES_CONTRADICTION_RATIO:
        evidence["verdict"] = "contradicted"
        evidence["reason"] = (
            f"input genes match {counter_hits:,} {counter}-specific symbols but only "
            f"{declared_hits:,} {declared}-specific symbols"
        )
        return evidence
    evidence["verdict"] = "consistent"
    evidence["reason"] = (
        f"input genes match {declared_hits:,} {declared}-specific symbols against "
        f"{counter_hits:,} {counter}-specific symbols"
    )
    return evidence


def verify_species(
    var: Any,
    *,
    declared: str,
    allow_mismatch: bool = False,
) -> dict[str, Any]:
    """Refuse a contradicted organism unless the caller overrides it deliberately."""

    evidence = species_evidence(
        _candidate_names(var),
        declared=declared,
        vocabularies=configured_vocabularies(),
    )
    evidence["override_allowed"] = bool(allow_mismatch)
    if evidence["verdict"] != "contradicted":
        return evidence
    counter = evidence["compared_with"]
    if not allow_mismatch:
        raise ValueError(
            f"declared organism {declared!r} contradicts the input genes: {evidence['reason']}. "
            f"Rerun with organism={counter!r}, or set allow_species_mismatch=true to run the "
            f"{declared} reference against this data deliberately as exploratory output."
        )
    evidence["reason"] += "; run anyway because allow_species_mismatch=true"
    return evidence


def validate_target_celltypes(requested: Any, available: Any) -> list[str]:
    """Check a constrained-annotation safelist against the model's own label set.

    ``safelist_celltypes`` marks every other reference label deleted in the index without
    checking that the requested labels exist, so a single typo silently narrows the reference to
    nothing. Validating first turns that into an actionable error, and the model's exact label
    strings are long ontology names that are easy to get slightly wrong.
    """

    labels = [str(value).strip() for value in requested if str(value).strip()]
    if not labels:
        raise ValueError("target_celltypes was supplied but contains no usable labels")
    known = {str(value) for value in available}
    unknown = [label for label in dict.fromkeys(labels) if label not in known]
    if unknown:
        hints = []
        for label in unknown[:3]:
            folded = label.casefold()
            near = sorted(
                candidate
                for candidate in known
                if folded in candidate.casefold() or candidate.casefold() in folded
            )
            if near:
                hints.append(f"{label!r} → did you mean {', '.join(repr(x) for x in near[:3])}?")
        detail = f" {' '.join(hints)}" if hints else ""
        raise ValueError(
            f"{len(unknown):,} target_celltypes are not labels in this reference model: "
            f"{', '.join(repr(label) for label in unknown[:5])}. Constrained annotation can only "
            f"safelist labels the model knows, and this model has {len(known):,} of them."
            f"{detail}"
        )
    return list(dict.fromkeys(labels))


def resolve_model(arguments: dict[str, Any], *, require_cellsearch: bool = False) -> Path:
    """Resolve and validate the local model directory for the declared organism.

    ``require_cellsearch`` additionally demands the reference cell-search assets, which are a
    separate multi-gigabyte download that annotation does not need.
    """

    organism = declared_organism(arguments)
    variable = ORGANISM_VARIABLES[organism]
    raw_path = arguments.get("model_path") or os.environ.get(variable)
    if not raw_path:
        raise ValueError(f"model_path is required when {variable} is not configured")
    model_path = Path(str(raw_path)).expanduser().resolve()
    if not model_path.is_dir():
        raise FileNotFoundError(f"SCimilarity model directory does not exist: {model_path}")
    missing = [name for name in EMBEDDING_FILES if not (model_path / name).is_file()]
    if missing:
        raise ValueError(
            f"SCimilarity model directory is incomplete; missing: {', '.join(missing)}"
        )
    if require_cellsearch:
        cellsearch = model_path / CELLSEARCH_DIRECTORY
        absent = []
        if not (cellsearch / CELLSEARCH_INDEX).is_file():
            absent.append(f"{CELLSEARCH_DIRECTORY}/{CELLSEARCH_INDEX}")
        if not (cellsearch / CELLSEARCH_METADATA).is_dir():
            absent.append(f"{CELLSEARCH_DIRECTORY}/{CELLSEARCH_METADATA}/")
        if absent:
            raise ValueError(
                f"the {organism} model at {model_path} has no reference cell-search index; "
                f"missing: {', '.join(absent)}. Querying reference cells needs those assets, "
                "which are a separate download from the embedding model; per-cell annotation "
                "with run_scimilarity_annotation does not."
            )
    return model_path


def model_fingerprint(model_path: Path, *, include_cellsearch: bool = False) -> str:
    """Content-addressed identity of the model files a run actually depended on."""

    facts: dict[str, Any] = {"path": str(model_path)}
    for name in EMBEDDING_FILES:
        facts[name] = (model_path / name).stat().st_size
    if include_cellsearch:
        index = model_path / CELLSEARCH_DIRECTORY / CELLSEARCH_INDEX
        facts[CELLSEARCH_INDEX] = index.stat().st_size
    return _identity("scimilarity-model", facts)
