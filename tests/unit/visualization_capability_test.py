from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from scagent_sdk.capabilities.registry import CapabilityRegistry


def _package() -> Any:
    skills_root = Path(__file__).parents[2] / ".claude" / "skills"
    return next(
        package
        for package in CapabilityRegistry(skills_root).discover()
        if package.manifest.skill_id == "visualize-single-cell"
    )


def _g(name: str) -> Any:
    package = _package()
    tool = next(
        tool for tool in package.manifest.tools if tool.name == "plot_qc_distributions"
    )
    return package.load_handler(tool).__globals__[name]


# --- manifest ----------------------------------------------------------------


def test_manifest_exposes_six_independent_figure_tools() -> None:
    package = _package()
    names = {tool.name for tool in package.manifest.tools}

    assert names == {
        "plot_qc_distributions",
        "plot_qc_embedding",
        "plot_embedding",
        "plot_group_composition",
        "plot_label_agreement",
        "plot_marker_expression",
    }
    assert all(tool.environment == "gpu-singlecell" for tool in package.manifest.tools)
    assert all(not tool.floors for tool in package.manifest.tools)
    assert all(
        tool.input_schema["additionalProperties"] is False for tool in package.manifest.tools
    )


# --- key resolution ----------------------------------------------------------


def test_value_key_resolves_obs_column_then_gene() -> None:
    resolve = _g("resolve_value_key")

    assert resolve("leiden", ["leiden"], ["CD3D"]) == ("obs", "leiden")
    assert resolve("CD3D", ["leiden"], ["CD3D"]) == ("gene", "CD3D")


def test_unknown_value_key_names_available_obs_columns() -> None:
    resolve = _g("resolve_value_key")

    with pytest.raises(ValueError, match="neither an obs column nor a gene"):
        resolve("CD4", ["leiden", "sample"], ["CD3D"])


def test_embedding_key_prefers_explicit_then_conventional() -> None:
    resolve = _g("resolve_embedding_key")

    assert resolve("X_pca", ["X_pca", "X_umap"]) == "X_pca"
    assert resolve(None, ["X_pca", "X_umap"]) == "X_umap"
    assert resolve(None, ["X_pca", "X_scimilarity"]) == "X_scimilarity"


def test_missing_embedding_is_an_actionable_error_not_a_silent_pipeline() -> None:
    resolve = _g("resolve_embedding_key")

    with pytest.raises(ValueError, match="no embedding to plot"):
        resolve(None, [])
    with pytest.raises(ValueError, match="available embeddings: X_pca"):
        resolve("X_umap", ["X_pca"])


# --- layout and cardinality --------------------------------------------------


def test_composition_layout_switches_to_heatmap_past_the_legend_limit() -> None:
    layout = _g("figure_layout")

    assert layout(10, 5)["mode"] == "bar"
    assert layout(10, 13)["mode"] == "heatmap"
    assert layout(10, 9)["legend_ncol"] == 2
    assert layout(40, 60)["figsize"][0] <= 26.0


def test_panel_grid_caps_columns_and_fills_rows() -> None:
    grid = _g("panel_grid")

    assert grid(1) == (1, 1)
    assert grid(3) == (2, 2)
    assert grid(4) == (2, 2)
    assert grid(5) == (3, 2)
    with pytest.raises(ValueError):
        grid(0)


def test_panel_width_reserves_space_for_the_legend_it_will_draw() -> None:
    width = _g("panel_width")

    assert width(0) < width(5) < width(40)


def test_top_categories_keep_largest_and_report_the_collapsed_remainder() -> None:
    select = _g("select_top_categories")

    kept, collapsed = select({"a": 5, "b": 50, "c": 1, "d": 7}, 2)

    assert kept == ["b", "d"]
    assert collapsed == 2


# --- agreement ---------------------------------------------------------------


def test_agreement_reports_exact_match_fraction_and_top_disagreements() -> None:
    summary = _g("agreement_summary")

    result = summary(
        ["T cell", "T cell", "B cell", "myeloid"],
        ["T cell", "T lymphocyte", "B cell", "monocyte"],
    )

    assert result["n_cells"] == 4
    assert result["exact_agreement"] == 0.5
    assert result["top_disagreements"][0]["cells"] == 1
    assert {item["second"] for item in result["top_disagreements"]} == {
        "T lymphocyte",
        "monocyte",
    }


def test_agreement_rejects_mismatched_or_empty_inputs() -> None:
    summary = _g("agreement_summary")

    with pytest.raises(ValueError, match="same cells"):
        summary(["a"], ["a", "b"])
    with pytest.raises(ValueError, match="no cells"):
        summary([], [])


# --- expression handling -----------------------------------------------------


def test_raw_counts_are_detected_so_display_normalization_is_explicit() -> None:
    counts = _g("looks_like_counts")

    assert counts(2910.0, False) is True
    assert counts(6.2, True) is False
    assert counts(3.0, False) is False


def test_mitochondrial_genes_resolve_through_the_symbol_column_when_present() -> None:
    source = _g("mito_mask_source")
    is_mito = _g("is_mito")

    symbols = source(["ENSG1", "ENSG2"], ["MT-CO1", "CD3D"])

    assert symbols == ["MT-CO1", "CD3D"]
    assert [is_mito(symbol) for symbol in symbols] == [True, False]
    assert source(["MT-ND1", "CD3D"], None) == ["MT-ND1", "CD3D"]
    assert source(["ENSG1"], ["MT-CO1", "extra"]) == ["ENSG1"]
