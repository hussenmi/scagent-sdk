from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_GENES_PATH = (
    Path(__file__).parents[2] / ".claude" / "skills" / "inspect-dataset" / "scripts" / "genes.py"
)


def _genes() -> Any:
    spec = importlib.util.spec_from_file_location("scagent_genes_under_test", _GENES_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeVar:
    """Minimal ``adata.var`` stand-in exposing ``.columns`` and ``var[col].tolist()``."""

    def __init__(self, columns: dict[str, list]):
        self._columns = columns
        self.columns = list(columns)

    def __getitem__(self, key: str) -> Any:
        return SimpleNamespace(tolist=lambda: list(self._columns[key]))


G = _genes()


@pytest.mark.parametrize(
    "name,expected",
    [
        ("ENSG00000141510", "ensembl"),
        ("ENSMUSG00000051951", "ensembl"),
        ("ENSG00000141510.14", "ensembl"),
        ("12345", "entrez"),
        ("CD3D", "symbol"),
        ("MT-CO1", "symbol"),
        ("!!bad", "other"),
    ],
)
def test_classify_gene_id(name: str, expected: str) -> None:
    assert G.classify_gene_id(name) == expected


def test_infer_id_format() -> None:
    assert G.infer_id_format(["ENSG00000141510", "ENSG00000000005"]) == "ensembl"
    assert G.infer_id_format(["CD3D", "ACTB", "TP53"]) == "symbol"
    assert G.infer_id_format(["ENSG00000141510", "CD3D"]) == "mixed"
    assert G.infer_id_format([]) == "unknown"


def test_strip_ensembl_version_and_genome_prefix() -> None:
    assert G.strip_ensembl_version("ENSG00000000003.14") == "ENSG00000000003"
    assert G.strip_ensembl_version("CD3D") == "CD3D"
    stripped, prefix = G.strip_genome_prefix(["GRCh38_CD3D", "GRCh38_ACTB", "GRCh38_TP53"])
    assert prefix == "GRCh38_"
    assert stripped == ["CD3D", "ACTB", "TP53"]


def test_find_symbol_column_is_content_validated() -> None:
    # A real symbol column is found even when var_names are Ensembl.
    var = _FakeVar(
        {
            "feature_name": ["CD3D", "ACTB", "TP53", "MT-CO1"],
            "mt": ["True", "False", "False", "True"],
        }
    )
    assert G.find_symbol_column(var) == "feature_name"
    # A boolean flag column is never adopted as symbols (the 'renamed every gene False' bug).
    flags = _FakeVar({"mt": ["True", "False", "False", "True"]})
    assert G.find_symbol_column(flags) is None


def test_map_names_to_symbols_via_column() -> None:
    var = _FakeVar({"feature_name": ["CD3D", "", "TP53"]})
    names = ["ENSG1", "ENSG2", "ENSG3"]
    mapped, col = G.map_names_to_symbols(names, var)
    assert col == "feature_name"
    # Empty symbol falls back to the original identifier; no gene is dropped.
    assert mapped == ["CD3D", "ENSG2", "TP53"]


def test_map_names_to_symbols_without_column() -> None:
    mapped, col = G.map_names_to_symbols(["CD3D", "ACTB"], _FakeVar({"other": [1, 2]}))
    assert mapped is None and col is None


def test_to_species_case() -> None:
    assert G.to_species_case("Cd3e", "human") == "CD3E"
    assert G.to_species_case("CD3E", "mouse") == "Cd3e"
    assert G.to_species_case("MT-CO1", "mouse") == "mt-Co1"
    assert G.to_species_case("0610009B22RIK", "mouse") == "0610009B22Rik"
    assert G.to_species_case("H2-D1", "mouse") == "H2-D1"


def test_match_case_to_reference() -> None:
    remapped, n = G.match_case_to_reference(["CD3E", "ACTB", "XIST"], ["Cd3e", "Actb", "Gapdh"])
    # Two names case-realign to the reference; the unmatched one is unchanged.
    assert remapped == ["Cd3e", "Actb", "XIST"]
    assert n == 2
