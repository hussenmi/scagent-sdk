"""Offline-first gene-identifier handling shared by inspect-dataset tools.

Single source of truth (within this skill) for:

* classifying ``var_names`` as Ensembl / Entrez / symbol,
* finding the ``var`` column that actually carries gene symbols (content-validated,
  so an oddly-named column is still found and a mislabelled one is rejected),
* converting ``var_names`` to gene symbols using the dataset's own column (network-free),
* normalizing symbol **case** to a species convention or to a reference model's vocabulary.

Design constraints (mirrors legacy ``scagent/core/genes.py`` intentions):

* **Offline-first.** The authoritative path is the file's own symbol column; ``mygene`` is an
  optional last-resort online fallback that fails soft when unavailable.
* **Non-destructive.** Callers preserve original identifiers and make duplicate symbols unique
  rather than dropping genes.

The pure-string helpers here take a lightweight ``var`` view (anything exposing ``.columns`` and
``var[col].tolist()``) so they are unit-testable without pandas/anndata.
"""

from __future__ import annotations

import re
from typing import Any

# --- identifier patterns -------------------------------------------------------
ENSEMBL_RE = re.compile(r"^ENS[A-Z]{0,4}G\d{6,}(?:\.\d+)?$")
ENTREZ_RE = re.compile(r"^\d{1,9}$")
# A gene symbol: starts with a letter, then letters/digits/-/./_ (CD3D, HLA-DRB1, MT-CO1, ...).
SYMBOL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9.\-_]{0,30}$")
# Multi-genome CellRanger prefixes: "GRCh38_CD3D", "GRCh38___CD3D", "hg19_ACTB".
GENOME_PREFIX_RE = re.compile(r"^([A-Za-z0-9]+_{1,10})(?=[A-Za-z])")
# RIKEN clone-derived mouse symbols: "0610009B22Rik" (digits, letters, trailing "Rik").
RIKEN_RE = re.compile(r"^\d{4,}[A-Za-z]\d{1,}RIK$", re.IGNORECASE)

SYMBOL_COL_CANDIDATES = (
    "feature_name",  # CELLxGENE
    "gene_symbols",  # 10x / CellRanger features.tsv
    "gene_symbol",
    "gene_name",
    "gene_names",
    "hgnc_symbol",
    "mgi_symbol",
    "symbol",
    "genesymbol",
    "gene",
)
ENSEMBL_COL_CANDIDATES = (
    "gene_ids",  # 10x / scanpy sc.read_10x_mtx default
    "gene_id",
    "ensembl_id",
    "ensembl_ids",
    "ensembl",
    "feature_id",
)

_SAMPLE_SIZE = 500
# Values that satisfy SYMBOL_RE but never denote a gene (boolean/missing flags). Guards against
# adopting a QC-flag column (e.g. var['mt']) whose values stringify to 'True'/'False'.
_NON_SYMBOL_VOCAB = {"true", "false", "nan", "none", "null", "na", "n/a", "-", ""}


# --- classification ------------------------------------------------------------
def classify_gene_id(name: str) -> str:
    """Classify one identifier as 'ensembl' | 'entrez' | 'symbol' | 'other'."""

    text = str(name)
    if ENSEMBL_RE.match(text):
        return "ensembl"
    if ENTREZ_RE.match(text):
        return "entrez"
    if SYMBOL_RE.match(text):
        return "symbol"
    return "other"


def infer_id_format(names: Any) -> str:
    """Infer the dominant identifier format: ensembl | entrez | symbol | mixed | unknown."""

    names = [str(name) for name in names]
    if not names:
        return "unknown"
    sample, _ = strip_genome_prefix(names[:_SAMPLE_SIZE])
    total = len(sample)
    counts = {"ensembl": 0, "entrez": 0, "symbol": 0, "other": 0}
    for name in sample:
        counts[classify_gene_id(name)] += 1
    if counts["ensembl"] > total * 0.5:
        return "ensembl"
    if counts["entrez"] > total * 0.5:
        return "entrez"
    if counts["symbol"] > total * 0.5:
        return "symbol"
    if counts["ensembl"] > 0 and counts["symbol"] > 0:
        return "mixed"
    return "unknown"


def strip_ensembl_version(name: str) -> str:
    """'ENSG00000000003.14' -> 'ENSG00000000003'; other names unchanged."""

    text = str(name)
    if ENSEMBL_RE.match(text) and "." in text:
        return text.split(".", 1)[0]
    return text


def strip_genome_prefix(names: Any) -> tuple[list[str], str | None]:
    """Strip a dominant multi-genome CellRanger prefix ('GRCh38_CD3D' -> 'CD3D').

    Returns (new_names, prefix). If fewer than 30% of names share one prefix, nothing is stripped.
    """

    names = [str(name) for name in names]
    prefix_counts: dict[str, int] = {}
    for name in names[:_SAMPLE_SIZE]:
        match = GENOME_PREFIX_RE.match(name)
        if match:
            prefix_counts[match.group(1)] = prefix_counts.get(match.group(1), 0) + 1
    if not prefix_counts:
        return names, None
    top_prefix, top_count = max(prefix_counts.items(), key=lambda item: item[1])
    if top_count <= min(len(names), _SAMPLE_SIZE) * 0.3:
        return names, None
    stripped = [name[len(top_prefix):] if name.startswith(top_prefix) else name for name in names]
    return stripped, top_prefix


# --- content-validated column detection ----------------------------------------
def _column_values(var: Any, col: str) -> list[str]:
    return [str(value) for value in var[col].tolist()]


def _looks_like_symbols(values: list[str]) -> bool:
    """True if the majority of values look like gene symbols (and not Ensembl or flags)."""

    vals = [str(v) for v in values[:_SAMPLE_SIZE] if str(v).strip().lower() not in {"", "nan"}]
    if not vals:
        return False
    distinct = {v.strip().lower() for v in vals}
    if distinct <= _NON_SYMBOL_VOCAB:
        return False
    # A real symbol column is near-unique; a flag column has ~2 distinct values.
    if len(distinct) < max(2, len(vals) * 0.1):
        return False
    good = sum(1 for v in vals if classify_gene_id(v) == "symbol" and not ENSEMBL_RE.match(v))
    return good > len(vals) * 0.5


def _looks_like_ensembl(values: list[str]) -> bool:
    vals = [str(v) for v in values[:_SAMPLE_SIZE] if str(v).strip().lower() not in {"", "nan"}]
    if not vals:
        return False
    return sum(1 for v in vals if ENSEMBL_RE.match(v)) > len(vals) * 0.5


def find_symbol_column(var: Any) -> str | None:
    """Return the ``var`` column holding gene symbols, or None. Contents are validated."""

    cols = list(var.columns)
    lowered = {str(col).lower(): col for col in cols}
    for cand in SYMBOL_COL_CANDIDATES:
        if cand in lowered and _looks_like_symbols(_column_values(var, lowered[cand])):
            return str(lowered[cand])
    for col in cols:  # fallback: any column whose contents look like symbols
        try:
            if _looks_like_symbols(_column_values(var, col)):
                return str(col)
        except Exception:
            continue
    return None


def find_ensembl_column(var: Any) -> str | None:
    """Return the ``var`` column holding Ensembl IDs, or None. Contents are validated."""

    cols = list(var.columns)
    lowered = {str(col).lower(): col for col in cols}
    for cand in ENSEMBL_COL_CANDIDATES:
        if cand in lowered and _looks_like_ensembl(_column_values(var, lowered[cand])):
            return str(lowered[cand])
    for col in cols:
        try:
            if _looks_like_ensembl(_column_values(var, col)):
                return str(col)
        except Exception:
            continue
    return None


# --- case normalization --------------------------------------------------------
def to_species_case(name: str, species: str) -> str:
    """Best-effort case for a gene symbol under a species convention.

    Human symbols are UPPER (CD3E, MT-CO1); mouse symbols are Title (Cd3e, mt-Co1, 0610009B22Rik).
    Hyphenated families (H2-D1, HLA-DRB1) and the mouse mitochondrial 'mt-' prefix are handled.
    Prefer :func:`match_case_to_reference` whenever a reference vocabulary is available.
    """

    text = str(name)
    if species == "human":
        return text.upper()
    if species != "mouse":
        return text
    if RIKEN_RE.match(text):  # 0610009B22RIK -> 0610009B22Rik
        return text[:-3] + "Rik"
    upper = text.upper()
    if upper.startswith("MT-"):  # mouse mitochondrial genes are mt-Co1, mt-Nd1, ...
        return "mt-" + _title_token(text[3:])
    return "-".join(_title_token(token) for token in text.split("-"))


def _title_token(token: str) -> str:
    return token[:1].upper() + token[1:].lower() if token else token


def match_case_to_reference(names: Any, reference: Any) -> tuple[list[str], int]:
    """Remap ``names`` to the exact casing found in ``reference`` (case-insensitive match).

    Reference vocabularies (a SCimilarity ``gene_order`` or CellTypist feature list) carry the
    canonical casing a model expects; aligning to them is more reliable than guessing species case.
    Returns (remapped_names, n_rematched).
    """

    lookup = {str(gene).upper(): str(gene) for gene in reference}
    remapped: list[str] = []
    matched = 0
    for name in names:
        canonical = lookup.get(str(name).upper())
        if canonical is not None and canonical != str(name):
            matched += 1
        remapped.append(canonical if canonical is not None else str(name))
    return remapped, matched


# --- namespace conversion (pure) -----------------------------------------------
def map_names_to_symbols(names: list[str], var: Any) -> tuple[list[str] | None, str | None]:
    """Map identifiers to symbols using the file's own symbol column.

    Returns (mapped_names, symbol_column) or (None, None) when no symbol column is present.
    Genes without a symbol keep their original identifier.
    """

    symbol_col = find_symbol_column(var)
    if symbol_col is None:
        return None, None
    col_vals = _column_values(var, symbol_col)
    mapped = [
        col_vals[i]
        if i < len(col_vals) and str(col_vals[i]).strip().lower() not in {"", "nan"}
        else names[i]
        for i in range(len(names))
    ]
    return mapped, symbol_col
