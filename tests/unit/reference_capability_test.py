from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from scagent_sdk.capabilities.registry import CapabilityRegistry


def _handler(skill_id: str, tool_name: str) -> Any:
    root = Path(__file__).parents[2] / ".claude" / "skills"
    package = next(
        package
        for package in CapabilityRegistry(root).discover()
        if package.manifest.skill_id == skill_id
    )
    tool = next(tool for tool in package.manifest.tools if tool.name == tool_name)
    return package.load_handler(tool)


def test_scimilarity_prefers_symbol_column_with_measured_overlap() -> None:
    handler = _handler("scimilarity-annotation", "run_scimilarity_annotation")
    choose = handler.__globals__["_select_gene_names"]
    names, source, overlap = choose(
        ["ENSG1", "ENSG2", "ENSG3"],
        {"feature_name": ["CD3D", "MS4A1", "LYZ"]},
        ["CD3D", "MS4A1", "LYZ", "TP53"],
    )
    assert names == ["CD3D", "MS4A1", "LYZ"]
    assert source == "var['feature_name']"
    assert overlap == 3


def test_scimilarity_aligns_mouse_case_to_model_vocabulary() -> None:
    handler = _handler("scimilarity-annotation", "run_scimilarity_annotation")
    choose = handler.__globals__["_select_gene_names"]
    names, source, overlap = choose(
        ["CD3D", "MS4A1", "LYZ", "TRP53"],
        {},
        ["Cd3d", "Ms4a1", "Lyz", "Trp53"],
    )
    assert names == ["Cd3d", "Ms4a1", "Lyz", "Trp53"]
    assert source == "var_names"
    assert overlap == 4


def test_scimilarity_count_validation_is_intrinsic() -> None:
    handler = _handler("scimilarity-annotation", "run_scimilarity_annotation")
    validate = handler.__globals__["_validate_counts"]
    validate(np.array([[0, 1], [2, 3]]), label="X")
    with pytest.raises(ValueError, match="performs its own normalization"):
        validate(np.array([[0.0, 1.2], [2.0, 3.0]]), label="X")
    with pytest.raises(ValueError, match="performs its own normalization"):
        validate(np.array([[0.0, -1.0]]), label="X")


@pytest.mark.parametrize(
    ("skill_id", "tool_name"),
    [
        ("scimilarity-annotation", "run_scimilarity_annotation"),
        ("celltypist-annotation", "run_celltypist_annotation"),
    ],
)
def test_reference_models_accept_x_as_an_explicit_count_source(
    skill_id: str, tool_name: str
) -> None:
    handler = _handler(skill_id, tool_name)
    select = handler.__globals__["_select_counts"]
    adata = SimpleNamespace(X="matrix-x", layers={"counts": "matrix-layer"})

    assert select(adata, "X") == ("matrix-x", "X")
    assert select(adata, None) == ("matrix-x", "X")
    assert select(adata, "counts") == ("matrix-layer", "layer:counts")
    with pytest.raises(ValueError, match="counts_layer=X"):
        select(adata, "missing")


def test_constrained_annotation_validates_labels_against_the_model() -> None:
    """safelist_celltypes marks every other label deleted without checking, so a typo would
    silently narrow the reference to nothing."""

    handler = _handler("scimilarity-annotation", "run_scimilarity_annotation")
    validate = handler.__globals__["validate_target_celltypes"]
    available = {"B cell", "plasma cell", "classical monocyte", "goblet cell"}

    assert validate(["B cell", "goblet cell"], available) == ["B cell", "goblet cell"]
    assert validate(["B cell", "B cell"], available) == ["B cell"]  # deduplicated

    with pytest.raises(ValueError, match="not labels in this reference model"):
        validate(["B cell", "Bcell"], available)
    with pytest.raises(ValueError, match="no usable labels"):
        validate(["", "  "], available)

    # A near miss names the candidate rather than making the caller guess the exact ontology text.
    with pytest.raises(ValueError, match="did you mean 'classical monocyte'"):
        validate(["monocyte"], available)


def test_annotation_surfaces_the_vote_margins_scimilarity_already_computes() -> None:
    handler = _handler("scimilarity-annotation", "run_scimilarity_annotation")
    confidence = handler.__globals__["_vote_confidence"]
    summary = handler.__globals__["_confidence_summary"]

    class _Stats:
        columns = ("vs2nd", "vsAll", "vs2nd_weighted", "vsAll_weighted")

        def __getitem__(self, key: str) -> Any:
            return {
                "vs2nd": [0.9, 0.5],
                "vsAll": [0.8, 0.3],
                "vs2nd_weighted": [0.95, 0.55],
                "vsAll_weighted": [0.85, 0.35],
            }[key]

    unweighted = confidence(_Stats(), weighting=False, n_obs=2)
    assert set(unweighted) == {"vs_second", "vs_all"}
    assert list(unweighted["vs_all"]) == [0.8, 0.3]

    weighted = confidence(_Stats(), weighting=True, n_obs=2)
    assert list(weighted["vs_all"]) == [0.85, 0.35]

    # Wrong cell dimension or missing stats degrade to no columns rather than a bad join.
    assert confidence(_Stats(), weighting=False, n_obs=3) == {}
    assert confidence(None, weighting=False, n_obs=2) == {}

    assert summary([0.8, 0.3, 0.9]) == {
        "median": 0.8,
        "minimum": 0.3,
        "fraction_below_0_5": 0.3333,
    }
    assert summary([]) is None


def test_annotation_schema_exposes_voting_and_constraint_controls() -> None:
    root = Path(__file__).parents[2] / ".claude" / "skills"
    package = next(
        package
        for package in CapabilityRegistry(root).discover()
        if package.manifest.skill_id == "scimilarity-annotation"
    )
    tool = next(
        tool for tool in package.manifest.tools if tool.name == "run_scimilarity_annotation"
    )
    properties = tool.input_schema["properties"]
    assert properties["knn_k"]["default"] == 50
    assert properties["weighting"]["default"] is False
    assert properties["target_celltypes"]["default"] is None
    assert properties["target_celltypes"]["items"] == {"type": "string"}


def test_scimilarity_model_validation_uses_ckpt_and_gene_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = _handler("scimilarity-annotation", "run_scimilarity_annotation")
    resolve = handler.__globals__["resolve_model"]
    (tmp_path / "encoder.ckpt").write_bytes(b"weights")
    (tmp_path / "gene_order.tsv").write_text("CD3D\n", encoding="utf-8")
    monkeypatch.setenv("SCIMILARITY_MODEL_PATH", str(tmp_path))
    assert resolve({"organism": "human"}) == tmp_path.resolve()
    (tmp_path / "encoder.ckpt").unlink()
    with pytest.raises(ValueError, match="encoder.ckpt"):
        resolve({"organism": "human"})
