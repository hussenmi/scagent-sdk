from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np

from scagent_sdk.capabilities.registry import CapabilityRegistry


def _package() -> Any:
    skills_root = Path(__file__).parents[2] / ".claude" / "skills"
    return next(
        package
        for package in CapabilityRegistry(skills_root).discover()
        if package.manifest.skill_id == "inspect-dataset"
    )


def _describe_globals() -> dict[str, Any]:
    package = _package()
    tool = next(t for t in package.manifest.tools if t.name == "describe_dataset")
    return package.load_handler(tool).__globals__


class _DuckSparse:
    """scipy-sparse-like: exposes stored values via .tocsr().data."""

    def __init__(self, data: np.ndarray):
        self._data = np.asarray(data)
        self.dtype = self._data.dtype

    def tocsr(self) -> Any:
        return self

    @property
    def data(self) -> np.ndarray:
        return self._data


class _BackedCSRDataset:
    """Mimics anndata's backed _CSRDataset: no .data, not numerically coercible."""

    dtype = np.dtype("float32")


class _FakeGPU:
    def __init__(self, data: np.ndarray):
        self._data = np.asarray(data)

    @property
    def data(self) -> Any:
        return _FakeGPU._Host(self._data)

    class _Host:
        def __init__(self, values: np.ndarray):
            self._values = values

        def get(self) -> np.ndarray:
            return self._values


_FakeGPU.__module__ = "cupy"


# --- manifest ---------------------------------------------------------------


def test_describe_manifest_is_strict_and_routed() -> None:
    package = _package()
    tool = next(t for t in package.manifest.tools if t.name == "describe_dataset")
    assert tool.environment == "gpu-singlecell"
    assert tool.input_schema["additionalProperties"] is False
    assert tool.input_schema["required"] == ["path"]


# --- matrix value facts -----------------------------------------------------


def _matrix_facts() -> Any:
    return _describe_globals()["_matrix_facts"]


def test_integer_dense_reads_as_counts_signal() -> None:
    facts = _matrix_facts()(np.array([[0, 1, 2], [3, 0, 5]]))
    assert facts["all_integer_sample"] is True
    assert facts["fraction_integer_valued"] == 1.0
    assert facts["has_negative_sample"] is False
    assert facts["sample_max"] == 5.0


def test_float_normalized_matrix_is_not_all_integer() -> None:
    facts = _matrix_facts()(np.array([[0.0, 0.5], [1.5, 2.1]]))
    assert facts["all_integer_sample"] is False
    assert facts["fraction_integer_valued"] < 1.0


def test_negative_values_are_flagged() -> None:
    facts = _matrix_facts()(np.array([[-1.0, 2.0], [3.0, 4.0]]))
    assert facts["has_negative_sample"] is True


def test_duck_sparse_uses_stored_values() -> None:
    facts = _matrix_facts()(_DuckSparse(np.array([1.0, 2.0, 3.0])))
    assert facts["is_sparse"] is True
    assert facts["n_sampled"] == 3
    assert facts["all_integer_sample"] is True


def test_backed_csr_dataset_yields_empty_sample_not_crash() -> None:
    # This is the exact object that broke the model's hand-written inspection code.
    facts = _matrix_facts()(_BackedCSRDataset())
    assert facts["n_sampled"] == 0
    assert "sample_max" not in facts


def test_gpu_array_is_pulled_to_host() -> None:
    facts = _matrix_facts()(_FakeGPU(np.array([2.0, 4.0, 6.0])))
    assert facts["n_sampled"] == 3
    assert facts["all_integer_sample"] is True


# --- gene / obs name signals ------------------------------------------------


def test_gene_namespace_counts_identifier_flavors() -> None:
    facts = _describe_globals()["_gene_namespace_facts"](
        ["ENSG00000141510", "ENSMUSG00000051951", "CD3D", "MT-CO1", "RPS6"]
    )
    assert facts["ensembl_human_ensg"] == 1
    assert facts["ensembl_mouse_ensmusg"] == 1
    # Only the three real symbols count; the two Ensembl accessions are excluded.
    assert facts["uppercase_symbol_like"] == 3
    assert facts["mt_prefixed"] == 1
    assert facts["ribo_prefixed"] == 1


def test_ensembl_ids_are_not_counted_as_symbol_like() -> None:
    facts = _describe_globals()["_gene_namespace_facts"](
        ["ENSG00000000003", "ENSG00000000005", "ENSMUSG00000051951", "12345"]
    )
    # All-accession var: zero symbol-like names, despite their uppercase/numeric shape.
    assert facts["uppercase_symbol_like"] == 0
    assert facts["title_symbol_like"] == 0
    assert facts["entrez_like"] == 1


class _FakeVar:
    """Minimal ``adata.var`` stand-in (pandas is a compute dep, absent from this venv)."""

    def __init__(self, columns: dict[str, list]):
        self._columns = columns
        self.columns = list(columns)

    def __getitem__(self, key: str) -> Any:
        return SimpleNamespace(tolist=lambda: list(self._columns[key]))


def test_gene_symbols_read_mt_from_symbol_column_not_ensembl_var_names() -> None:
    facts_fn = _describe_globals()["_gene_symbol_facts"]
    var_names = ["ENSG00000198804", "ENSG00000198899", "ENSG00000141510", "ENSG00000000005"]
    var = _FakeVar({"feature_name": ["MT-CO1", "MT-ATP6", "TP53", ""]})

    facts = facts_fn(var_names, var)

    # var_names are Ensembl (0 MT there); the symbols live in feature_name (2 MT).
    assert facts["symbol_column"] == "feature_name"
    assert facts["symbol_source"] == "var['feature_name']"
    assert facts["mt_genes"] == 2
    assert set(facts["mt_examples"]) == {"MT-CO1", "MT-ATP6"}
    # One of four genes has no symbol -> mapping coverage is 3/4.
    assert facts["symbol_mapping"] == {"n_total": 4, "n_mapped": 3, "fraction_mapped": 0.75}


def test_gene_symbols_fall_back_to_var_names_when_no_symbol_column() -> None:
    facts_fn = _describe_globals()["_gene_symbol_facts"]
    var_names = ["CD3D", "MT-CO1", "RPS6", "RPL13"]
    var = _FakeVar({"other": [1, 2, 3, 4]})

    facts = facts_fn(var_names, var)

    assert facts["symbol_column"] is None
    assert facts["symbol_source"] == "var_names"
    assert facts["mt_genes"] == 1
    assert facts["ribo_genes"] == 2
    # No identifier->symbol mapping is claimed when names are already symbols.
    assert "symbol_mapping" not in facts


def test_obs_names_detect_10x_barcodes() -> None:
    facts = _describe_globals()["_obs_names_facts"](
        ["AAACCTGAGAAACCAT-1", "AAACCTGAGAAACCGC-1", "AAACCTGAGAAACCTA-1"]
    )
    assert facts["looks_like_10x_barcodes"] is True
    assert facts["numeric_suffixes"] == ["1"]


def test_matrix_signals_surface_log1p_and_value_evidence() -> None:
    x_facts = {"all_integer_sample": False, "has_negative_sample": True, "sample_max": 8.5}
    signals = _describe_globals()["_matrix_signals"](x_facts, ["log1p", "pca"], ["highly_variable"])
    assert signals["log1p_in_uns"] is True
    assert signals["highly_variable_in_var"] is True
    assert signals["x_all_integer_sample"] is False
