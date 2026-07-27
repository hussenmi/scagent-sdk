"""Report which local SCimilarity reference models exist and what each one can do.

SCimilarity needs a local model directory per organism; there is no implicit download. Two
facets matter and are independent: the embedding model (annotation) and the reference
cell-search index (atlas query), which is a separate multi-gigabyte asset. The reference gene
vocabulary bounds `min_gene_overlap`, and the index size predicts how long a query will spend
loading, so both are reported here rather than discovered by running inference.

Standard library only: this runs in the control plane at session assembly.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

REQUIRED_FILES = ("encoder.ckpt", "gene_order.tsv")
CELLSEARCH_INDEX = Path("cellsearch") / "full_kNN.bin"
CELLSEARCH_METADATA = Path("cellsearch") / "cell_metadata"
ORGANISM_VARIABLES = {
    "human": "SCIMILARITY_MODEL_PATH",
    "mouse": "SCIMILARITY_MODEL_PATH_MOUSE",
}
MAX_GENE_ORDER_BYTES = 8 * 1024 * 1024


def _gene_count(path: Path) -> int | None:
    try:
        if path.stat().st_size > MAX_GENE_ORDER_BYTES:
            return None
        with path.open("rb") as handle:
            return sum(1 for line in handle if line.strip())
    except OSError:
        return None


def _query_facet(directory: Path) -> tuple[bool, str]:
    """Whether reference atlas queries are possible, and what loading the index will cost."""

    index = directory / CELLSEARCH_INDEX
    if not index.is_file() or not (directory / CELLSEARCH_METADATA).is_dir():
        return False, "no cell-search index, so query_reference_cells is unavailable"
    try:
        gib = index.stat().st_size / (1024**3)
    except OSError:
        return True, "cell-search index present"
    return True, (
        f"cell-search index present ({gib:,.1f} GiB, loaded into memory per query call, so "
        "query every group of interest in one call)"
    )


def _inspect(directory: Path) -> tuple[bool, str]:
    if not directory.is_dir():
        return False, f"configured path is absent: {directory}"
    missing = [name for name in REQUIRED_FILES if not (directory / name).is_file()]
    if missing:
        return False, f"incomplete model at {directory}; missing {', '.join(missing)}"
    genes = _gene_count(directory / "gene_order.tsv")
    vocabulary = f", {genes:,}-gene reference vocabulary" if genes else ""
    _, query_note = _query_facet(directory)
    return True, f"{directory}{vocabulary}; {query_note}"


def probe(environment: dict[str, str] | None = None) -> dict[str, Any]:
    resolved = environment or {}
    details: list[str] = []
    usable: list[str] = []
    unusable: list[str] = []
    queryable: list[str] = []
    for organism, variable in ORGANISM_VARIABLES.items():
        configured = resolved.get(variable) or os.environ.get(variable)
        if not configured:
            unusable.append(organism)
            details.append(f"{organism}: {variable} is not configured")
            continue
        directory = Path(configured).expanduser()
        ok, description = _inspect(directory)
        (usable if ok else unusable).append(organism)
        if ok and _query_facet(directory)[0]:
            queryable.append(organism)
        details.append(f"{organism}: {description}")
    if not usable:
        status, summary = "unavailable", "no usable local reference model"
    elif unusable:
        status = "partial"
        summary = f"{', '.join(usable)} model usable; {', '.join(unusable)} unavailable"
    else:
        status = "ready"
        summary = f"{', '.join(usable)} reference models usable"
    if usable:
        summary += (
            f"; atlas query available for {', '.join(queryable)}"
            if queryable
            else "; no atlas query index on this host"
        )
    details.append(
        "Organism is declared per call and verified against both model vocabularies; it is "
        "never inferred from gene-symbol casing."
    )
    details.append(
        "Models are never downloaded implicitly; an organism listed as unavailable needs an "
        "explicit `model_path`."
    )
    return {"status": status, "summary": summary, "details": details}
