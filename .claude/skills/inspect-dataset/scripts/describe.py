"""Deterministic, judgment-free content inspection of an AnnData file.

This answers "what is in this dataset" without the model writing exploratory code. It opens the
file read-only (backed, so a large matrix is never fully loaded), and reports facts only: shape,
value characteristics of ``X``/layers/``raw`` sampled safely, per-column ``obs``/``var`` summaries,
embedding/uns keys, and gene-identifier signals. Roles, species, and "is this counts" verdicts are
deliberately left to the model, which reads these facts.

The matrix helpers are hardened against the shapes that break naive inspection code: scipy sparse,
cupy/cupyx GPU arrays left by an interrupted step, and anndata backed ``_CSRDataset`` objects (which
have no ``.data`` and must not be coerced whole). Anything that cannot be sampled numerically yields
an empty sample rather than raising.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import genes  # noqa: E402  (sibling module; path inserted above)

ENSEMBL_HUMAN_RE = re.compile(r"^ENSG\d{6,}")
ENSEMBL_MOUSE_RE = re.compile(r"^ENSMUSG\d{6,}")
ENSEMBL_ANY_RE = re.compile(r"^ENS[A-Z]{0,3}G\d{6,}")
ENTREZ_RE = re.compile(r"^\d{1,8}$")
UPPER_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9\-.]{1,20}$")
TITLE_SYMBOL_RE = re.compile(r"^[A-Z][a-z0-9\-.]{1,20}$")
TENX_BARCODE_RE = re.compile(r"^[ACGT]{16}(-\d+)?$")


def _stored_values(matrix: Any) -> Any:
    """Return the stored (nonzero, for sparse) values of a matrix as a 1-D numpy array.

    Never raises: unsupported/backed/exotic matrices yield an empty array so inspection of the
    rest of the dataset still succeeds.
    """

    import numpy as np

    module = type(matrix).__module__.split(".", 1)[0]
    if module in ("cupy", "cupyx"):
        try:
            gpu = matrix.data if hasattr(matrix, "data") else matrix.reshape(-1)
            return np.asarray(gpu.get())
        except Exception:
            return np.array([])
    if hasattr(matrix, "tocsr"):  # scipy sparse duck type
        try:
            return np.asarray(matrix.tocsr().data)
        except Exception:
            return np.array([])
    try:
        array = np.asarray(matrix)
    except Exception:
        return np.array([])
    # e.g. a backed _CSRDataset that could not be materialized numerically.
    if array.dtype == object or array.ndim == 0:
        return np.array([])
    return array.ravel()


def _sample_matrix_values(matrix: Any, *, n: int = 20000, seed: int = 0) -> Any:
    """Randomly sample stored values so integer-ness/range reflect the matrix, not just its head."""

    import numpy as np

    data = _stored_values(matrix)
    size = len(data)
    if size == 0:
        return np.array([])
    if size <= n:
        return np.asarray(data)
    index = np.sort(np.random.default_rng(seed).choice(size, size=n, replace=False))
    return np.asarray(data[index])


def _matrix_facts(matrix: Any, *, sample_n: int = 20000) -> dict[str, Any]:
    """Judgment-free value facts for a matrix: enough to decide "counts?" without a verdict."""

    import numpy as np

    facts: dict[str, Any] = {
        "dtype": str(getattr(matrix, "dtype", "unknown")),
        "is_sparse": bool(hasattr(matrix, "tocsr")),
    }
    sample = _sample_matrix_values(matrix, n=sample_n)
    if len(sample) == 0:
        facts["n_sampled"] = 0
        return facts
    finite = sample[np.isfinite(sample)]
    integer_valued = np.isclose(sample, np.round(sample))
    facts.update(
        {
            "n_sampled": int(len(sample)),
            "sample_min": round(float(sample.min()), 4),
            "sample_max": round(float(sample.max()), 4),
            "fraction_integer_valued": round(float(np.mean(integer_valued)), 6),
            "all_integer_sample": bool(integer_valued.all()),
            "has_negative_sample": bool(len(finite) > 0 and float(finite.min()) < 0),
        }
    )
    return facts


def _is_symbol_like(name: str, regex: re.Pattern[str]) -> bool:
    """A gene *symbol* has symbol shape but is not an accession.

    Ensembl IDs (``ENSG…``) and Entrez IDs are uppercase/numeric and otherwise pass the shape
    test, so they must be excluded — counting them as symbols is what made an all-Ensembl var
    look like it was full of gene symbols.
    """

    return bool(regex.match(name)) and not ENSEMBL_ANY_RE.match(name) and not ENTREZ_RE.match(name)


def _gene_namespace_facts(names: list[str]) -> dict[str, Any]:
    """Raw gene-identifier signals (counts only, no species/format conclusion)."""

    if not names:
        return {"n_checked": 0}
    return {
        "n_checked": len(names),
        "ensembl_human_ensg": sum(1 for name in names if ENSEMBL_HUMAN_RE.match(name)),
        "ensembl_mouse_ensmusg": sum(1 for name in names if ENSEMBL_MOUSE_RE.match(name)),
        "ensembl_other": sum(
            1
            for name in names
            if ENSEMBL_ANY_RE.match(name)
            and not ENSEMBL_HUMAN_RE.match(name)
            and not ENSEMBL_MOUSE_RE.match(name)
        ),
        "entrez_like": sum(1 for name in names if ENTREZ_RE.match(name)),
        "uppercase_symbol_like": sum(1 for name in names if _is_symbol_like(name, UPPER_SYMBOL_RE)),
        "title_symbol_like": sum(1 for name in names if _is_symbol_like(name, TITLE_SYMBOL_RE)),
        "mt_prefixed": sum(1 for name in names if name.upper().startswith("MT-")),
        "ribo_prefixed": sum(1 for name in names if name.upper().startswith(("RPS", "RPL"))),
    }


def _clean_symbol(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"", "nan", "none", "na", "<na>"} else text


def _gene_symbol_facts(var_names: list[str], var: Any) -> dict[str, Any]:
    """Symbol-aware biology signals: MT / ribosomal gene counts and mapping coverage.

    Gene-level MT/ribo detection must read *symbols*. For many datasets the symbols live in a var
    column (e.g. ``feature_name``) while ``var_names`` are Ensembl IDs — scanning the wrong field
    silently reports zero MT genes. This locates the symbol source explicitly, scans every gene
    (not a 200-gene head), and — when identifiers map to symbols — reports how complete that
    mapping is. Counts are facts; no species or "is this counts" conclusion is drawn.
    """

    # Content-validated column detection lives in the shared genes module (single source of truth).
    symbol_col = genes.find_symbol_column(var)
    ensembl_col = genes.find_ensembl_column(var)

    if symbol_col is not None:
        symbols = [_clean_symbol(value) for value in var[symbol_col].tolist()]
        source = f"var['{symbol_col}']"
    else:
        symbols = list(var_names)
        source = "var_names"
    upper = [symbol.upper() for symbol in symbols]

    facts: dict[str, Any] = {
        "symbol_column": None if symbol_col is None else str(symbol_col),
        "ensembl_id_column": None if ensembl_col is None else str(ensembl_col),
        "symbol_source": source,
        "n_symbols": sum(1 for symbol in symbols if symbol),
        "mt_genes": sum(1 for symbol in upper if symbol.startswith("MT-")),
        "ribo_genes": sum(1 for symbol in upper if symbol.startswith(("RPS", "RPL"))),
        "mt_examples": [symbol for symbol in symbols if symbol.upper().startswith("MT-")][:12],
    }

    n_ensembl = sum(1 for name in var_names if ENSEMBL_ANY_RE.match(name))
    if symbol_col is not None and var_names and n_ensembl >= max(1, len(var_names) // 2):
        n_mapped = sum(
            1
            for ident, symbol in zip(var_names, symbols, strict=False)
            if symbol and symbol != ident and not ENSEMBL_ANY_RE.match(symbol)
        )
        facts["symbol_mapping"] = {
            "n_total": len(var_names),
            "n_mapped": n_mapped,
            "fraction_mapped": round(n_mapped / len(var_names), 4),
        }
    return facts


def _obs_names_facts(names: list[str]) -> dict[str, Any]:
    if not names:
        return {"n_checked": 0}
    tenx = sum(1 for name in names if TENX_BARCODE_RE.match(name))
    suffixes = sorted({match.group(1) for name in names if (match := re.search(r"-(\d+)$", name))})
    return {
        "n_checked": len(names),
        "tenx_barcode_like": tenx,
        "looks_like_10x_barcodes": tenx >= max(1, int(0.7 * len(names))),
        "numeric_suffixes": suffixes[:10],
    }


def _truncate(value: Any, limit: int = 60) -> str:
    text = str(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _column_facts(series: Any, n_ref: int, *, max_values: int = 10) -> dict[str, Any]:
    """Factual summary of one obs/var column. Makes no role decision.

    A near-unique column reports ``unique_fraction`` close to 1.0 so the consumer can conclude it is
    an identifier; a low-cardinality column reports its value-count distribution.
    """

    import numpy as np
    import pandas as pd

    non_null = series.dropna()
    n_unique = int(non_null.nunique())
    facts: dict[str, Any] = {
        "dtype": str(series.dtype),
        "n_unique": n_unique,
        "n_missing": int(series.isna().sum()),
        "unique_fraction": round(n_unique / n_ref, 4) if n_ref else 0.0,
    }
    if n_unique == 0:
        return facts
    if pd.api.types.is_numeric_dtype(series) and n_unique > max_values:
        try:
            values = non_null.to_numpy(dtype=float)
            finite = values[np.isfinite(values)]
            if len(finite):
                facts.update(
                    {
                        "min": round(float(finite.min()), 4),
                        "max": round(float(finite.max()), 4),
                        "mean": round(float(finite.mean()), 4),
                        "all_integer": bool(np.allclose(finite, np.round(finite))),
                    }
                )
        except (TypeError, ValueError):
            pass
        return facts
    try:
        counts = non_null.value_counts()
        facts["top_values"] = [
            {"value": _truncate(index), "count": int(count)}
            for index, count in counts.head(max_values).items()
        ]
        if n_unique > max_values:
            facts["values_truncated"] = True
    except (TypeError, ValueError):
        pass
    return facts


def _matrix_signals(
    x_facts: dict[str, Any], uns_keys: list[str], var_columns: list[str]
) -> dict[str, Any]:
    """Raw signals for judging normalization state — evidence, not a verdict.

    Cheap metadata (``uns['log1p']``, HVG presence) is reported alongside the value sample so the
    model can distinguish float32-but-integer counts from log-normalized data from the values.
    """

    return {
        "log1p_in_uns": "log1p" in uns_keys,
        "highly_variable_in_var": "highly_variable" in var_columns,
        "x_all_integer_sample": x_facts.get("all_integer_sample"),
        "x_has_negative_sample": x_facts.get("has_negative_sample"),
        "x_sample_max": x_facts.get("sample_max"),
    }


def _render_markdown(sheet: dict[str, Any]) -> str:
    shape = sheet["shape"]
    raw = sheet["raw"]
    raw_line = f"present, {raw.get('n_vars')} genes" if raw["present"] else "none"
    lines = [
        f"# Dataset contents: {Path(sheet['path']).name}",
        "",
        f"- Shape: **{shape['n_obs']:,} cells × {shape['n_vars']:,} genes**"
        + (" (read backed)" if sheet.get("backed") else ""),
        f"- X: {sheet['X']}",
        f"- Layers: {sheet['layers'] or 'none'}",
        f"- raw: {raw_line}",
        f"- Embeddings (obsm): {sheet['obsm_keys'] or 'none'}",
        f"- uns keys: {sheet['uns_keys'] or 'none'}",
        f"- Gene-identifier signals: {sheet['gene_namespace']}",
        f"- Gene symbols (MT/ribo, mapping): {sheet['gene_symbols']}",
        f"- Normalization signals: {sheet['matrix_signals']}",
        "",
        "## obs columns",
        "",
        "| column | dtype | n_unique | unique_fraction | examples / stats |",
        "|---|---|---:|---:|---|",
    ]
    for name, facts in sheet["obs_columns"].items():
        if "top_values" in facts:
            summary = ", ".join(
                f"{item['value']}({item['count']})" for item in facts["top_values"][:6]
            )
        elif "min" in facts:
            summary = f"min={facts['min']}, max={facts['max']}, mean={facts['mean']}"
        else:
            summary = ""
        lines.append(
            f"| {name} | {facts['dtype']} | {facts['n_unique']} | "
            f"{facts.get('unique_fraction')} | {summary} |"
        )
    lines.extend(["", f"Example gene names: {sheet['var_names_examples']}", ""])
    return "\n".join(lines) + "\n"


def run(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    import json

    import anndata as ad

    raw_path = arguments.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValueError("path must be a non-empty string")
    path = Path(raw_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"dataset file not found: {path}")
    if path.suffix.lower() != ".h5ad":
        raise ValueError("describe_dataset supports .h5ad files; use inspect_dataset for identity")
    max_values = int(arguments.get("max_values", 10))
    sample_rows = int(arguments.get("sample_rows", 4000))

    adata = ad.read_h5ad(path, backed="r")
    n_obs, n_vars = int(adata.n_obs), int(adata.n_vars)

    # Bound matrix value sampling to a small in-memory row block so a backed matrix is never
    # loaded whole; obs/var are already resident and used at full cardinality.
    rows = min(sample_rows, n_obs)
    block = adata[:rows]
    if getattr(adata, "isbacked", False):
        block = block.to_memory()

    layer_names = list(adata.layers.keys())
    raw_present = adata.raw is not None
    x_facts = _matrix_facts(block.X)
    uns_keys = list(adata.uns.keys())
    var_columns = list(adata.var.columns)

    # var is resident even when the matrix is backed, so scan every gene name (not a head slice):
    # a 200-gene head misses MT/ribosomal genes and understates identifier flavors.
    var_names = [str(name) for name in adata.var_names]
    sheet: dict[str, Any] = {
        "path": str(path),
        "backed": bool(getattr(adata, "isbacked", False)),
        "shape": {"n_obs": n_obs, "n_vars": n_vars},
        "matrix_sample_rows": int(rows),
        "X": x_facts,
        "layers": layer_names,
        "layer_facts": {name: _matrix_facts(block.layers[name]) for name in layer_names},
        "raw": {
            "present": raw_present,
            "n_vars": int(adata.raw.n_vars) if raw_present else 0,
            "X": _matrix_facts(block.raw.X) if raw_present and block.raw is not None else {},
        },
        "obsm_keys": list(adata.obsm.keys()),
        "varm_keys": list(adata.varm.keys()),
        "obsp_keys": list(adata.obsp.keys()),
        "uns_keys": uns_keys,
        "obs_columns": {
            col: _column_facts(adata.obs[col], n_obs, max_values=max_values)
            for col in adata.obs.columns
        },
        "var_columns": {
            col: _column_facts(adata.var[col], n_vars, max_values=max_values)
            for col in adata.var.columns
        },
        "var_names_examples": var_names[:12],
        "obs_names_examples": [str(name) for name in adata.obs_names[:8]],
        "gene_namespace": _gene_namespace_facts(var_names),
        "gene_symbols": _gene_symbol_facts(var_names, adata.var),
        "obs_names": _obs_names_facts([str(name) for name in adata.obs_names[:50]]),
    }
    sheet["matrix_signals"] = _matrix_signals(x_facts, uns_keys, var_columns)

    (context.staging_dir / "dataset-contents.json").write_text(
        json.dumps(sheet, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )
    (context.staging_dir / "dataset-contents.md").write_text(
        _render_markdown(sheet), encoding="utf-8"
    )

    counts_note = ""
    if x_facts.get("n_sampled"):
        counts_note = (
            " X sample is "
            + ("all-integer" if x_facts.get("all_integer_sample") else "non-integer")
            + f" (max {x_facts.get('sample_max')})."
        )
    gene_symbols = sheet["gene_symbols"]
    symbol_note = ""
    if gene_symbols.get("mt_genes") or gene_symbols.get("ribo_genes"):
        symbol_note = (
            f" {gene_symbols['mt_genes']} MT / {gene_symbols['ribo_genes']} ribosomal gene(s)"
            f" via {gene_symbols['symbol_source']}."
        )
    return {
        "schema_version": 1,
        "summary": (
            f"Described {path.name}: {n_obs:,} cells × {n_vars:,} genes, "
            f"{len(layer_names)} layer(s), raw={'yes' if raw_present else 'no'}."
            f"{counts_note}{symbol_note}"
        ),
        "details": sheet,
        "facts_patch": {"dataset_contents": sheet},
        "artifacts": [
            {
                "name": "dataset-contents",
                "relative_path": "dataset-contents.json",
                "media_type": "application/json",
            },
            {
                "name": "dataset-contents-report",
                "relative_path": "dataset-contents.md",
                "media_type": "text/markdown",
            },
        ],
    }
