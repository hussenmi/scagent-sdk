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


def test_panel_width_scales_with_label_length_not_just_entry_count() -> None:
    """Cell Ontology labels, not the entry count, are what make a legend wide.

    A fixed reserve produced the sliver-panel defect: forty 60-character labels in two columns
    need far more than the constant that used to be budgeted, so the legend ate the axes.
    """

    width = _g("panel_width")
    base = _g("PANEL_BASE_WIDTH")

    assert width(40, 60) > width(40, 12)
    assert width(40, 60) - base > 4.2
    # The data area must still own a meaningful share of the figure, not a strip beside a legend.
    assert base / width(40, 60) > 0.33


def test_panel_width_is_bounded_for_a_pathological_vocabulary() -> None:
    width = _g("panel_width")

    assert width(60, 500) == _g("MAX_PANEL_WIDTH")


def test_legend_columns_match_what_the_panel_draws() -> None:
    columns = _g("legend_columns")

    assert columns(5) == 1
    assert columns(40) == 2


def test_category_color_limit_never_lets_two_labels_share_a_color() -> None:
    limit = _g("category_color_limit")
    palette_size = _g("CATEGORY_PALETTE_SIZE")

    assert limit(30) == 30
    assert limit(200) == palette_size
    with pytest.raises(ValueError):
        limit(0)


def test_color_slots_follow_the_label_not_its_count_rank() -> None:
    """Counts changing must not repaint a figure, or two views cannot be compared."""

    slots = _g("stable_color_slots")

    first = slots(["NK cell", "B cell", "regulatory T cell"])
    reordered = slots(["regulatory T cell", "NK cell", "B cell"])

    assert first == reordered


def test_color_slots_are_unchanged_when_only_the_counts_change() -> None:
    """The reported defect: filtering cells must not repaint the populations that remain."""

    slots = _g("stable_color_slots")
    select = _g("select_top_categories")

    before, _ = select({"NK cell": 50_000, "B cell": 900, "regulatory T cell": 40}, 3)
    after, _ = select({"NK cell": 40, "B cell": 30_000, "regulatory T cell": 12_000}, 3)

    assert before != after, "count order should differ, or this test proves nothing"
    assert slots(before) == slots(after)


def test_color_slots_shift_when_the_label_set_itself_changes() -> None:
    """Documented limitation, asserted so it cannot regress silently into a surprise.

    Removing a category shifts the labels sorted after it. Full stability under a changing label
    set needs a palette persisted in scientific state, which this deliberately does not do.
    """

    slots = _g("stable_color_slots")

    before = slots(["a", "b", "c"])
    after = slots(["a", "c"])

    assert before["a"] == after["a"]
    assert before["c"] != after["c"]


def test_color_slots_are_unique_and_bounded_by_the_palette() -> None:
    slots = _g("stable_color_slots")
    palette_size = _g("CATEGORY_PALETTE_SIZE")

    assigned = slots([f"cell type {index}" for index in range(palette_size)])

    assert len(set(assigned.values())) == palette_size
    assert all(0 <= slot < palette_size for slot in assigned.values())
    with pytest.raises(ValueError, match="exceed"):
        slots([f"cell type {index}" for index in range(palette_size + 1)])


def test_color_slots_are_identical_across_processes() -> None:
    """Guards against `hash()`, whose per-process salt would recolor an identical figure."""

    import subprocess
    import sys

    script = (
        "import json,sys;"
        f"sys.path.insert(0, {str(_package().root / 'scripts')!r});"
        "import plots;"
        "print(json.dumps(plots.stable_color_slots(['NK cell','B cell','CD8-positive, "
        "alpha-beta T cell'])))"
    )
    runs = [
        subprocess.run(  # noqa: S603
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=True,
            env={"PYTHONHASHSEED": seed, "PATH": "/usr/bin:/bin"},
        ).stdout
        for seed in ("0", "12345")
    ]

    assert runs[0] == runs[1]


def test_color_slots_use_the_separated_front_of_the_palette() -> None:
    """Consecutive slots keep well-separated hues; scattering them across 60 does not."""

    slots = _g("stable_color_slots")

    assigned = slots([f"cell type {index}" for index in range(15)])

    assert max(assigned.values()) == 14


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


def test_ribosomal_genes_are_recognized_case_insensitively() -> None:
    is_ribo = _g("is_ribo")

    # Mouse symbols are title case and human are upper; both name the same gene class.
    assert [is_ribo(symbol) for symbol in ("RPS19", "Rpl13a", "rps6")] == [True, True, True]
    assert [is_ribo(symbol) for symbol in ("CD3D", "RPTOR", "MT-CO1")] == [False, False, False]


# --- log scaling -------------------------------------------------------------


def test_log_bins_are_uniform_on_the_axis_they_are_drawn_on() -> None:
    bins = _g("log_spaced_bins")

    edges = bins(100.0, 100000.0, count=3)

    assert len(edges) == 4
    assert edges[0] == pytest.approx(100.0)
    assert edges[-1] == pytest.approx(100000.0)
    # Equal ratios, not equal differences: that is what makes the drawn bars equal width.
    ratios = [edges[index + 1] / edges[index] for index in range(3)]
    assert ratios == pytest.approx([10.0, 10.0, 10.0])


def test_log_bins_refuse_a_non_positive_minimum_rather_than_silently_clamping() -> None:
    bins = _g("log_spaced_bins")

    with pytest.raises(ValueError, match="positive minimum"):
        bins(0.0, 1000.0)
    with pytest.raises(ValueError, match="count must be"):
        bins(1.0, 10.0, count=0)


def test_log_bins_still_produce_usable_edges_for_a_degenerate_span() -> None:
    bins = _g("log_spaced_bins")

    edges = bins(5.0, 5.0, count=2)

    assert edges[0] == pytest.approx(5.0)
    assert edges[-1] > edges[0]


def test_positive_span_reports_the_cells_a_log_axis_cannot_draw() -> None:
    span = _g("positive_span")

    low, high, dropped = span([0.0, 3.0, 250.0, 0.0, 17.0])

    assert (low, high, dropped) == (3.0, 250.0, 2)
    with pytest.raises(ValueError, match="no positive values"):
        span([0.0, 0.0])


def test_compact_ticks_stay_short_enough_not_to_collide() -> None:
    tick = _g("compact_tick")

    assert [tick(value) for value in (200, 1000, 20000, 50000)] == ["200", "1k", "20k", "50k"]
    assert tick(2_000_000) == "2M"
    assert tick(0.5) == "0.5"


# --- display ordering --------------------------------------------------------


def test_labels_are_ordered_the_way_a_reader_looks_them_up() -> None:
    order = _g("natural_order")

    # Plain string sort would give 1, 10, 2 — so cluster 2 lands after cluster 10 in the grid.
    assert order(["10", "2", "1", "20", "3"]) == ["1", "2", "3", "10", "20"]
    assert order(["res_10_0", "res_2_0"]) == ["res_2_0", "res_10_0"]
    assert order(["B cell", "activated T cell"]) == ["activated T cell", "B cell"]


def test_display_order_is_independent_of_the_size_order_used_to_select() -> None:
    order = _g("natural_order")
    select = _g("select_top_categories")

    kept, collapsed = select({"3": 900, "1": 500, "12": 100, "2": 50}, 3)

    assert kept == ["3", "1", "12"] and collapsed == 1
    assert order(kept) == ["1", "3", "12"]


# --- highlight grid ----------------------------------------------------------


def test_grid_widens_rather_than_growing_a_tall_strip_as_clusters_multiply() -> None:
    layout = _g("highlight_grid_layout")

    assert (layout(3)["rows"], layout(3)["columns"]) == (1, 3)
    assert (layout(20)["rows"], layout(20)["columns"]) == (4, 5)
    # Past thirty the grid goes six wide so a fine-resolution clustering stays proportioned.
    assert (layout(60)["rows"], layout(60)["columns"]) == (10, 6)


def test_grid_layout_always_has_room_for_every_category() -> None:
    layout = _g("highlight_grid_layout")

    for count in range(1, 151):
        shape = layout(count)
        assert shape["rows"] * shape["columns"] >= count

    with pytest.raises(ValueError, match="at least one category"):
        layout(0)


def test_grid_is_skipped_when_it_would_not_beat_the_overlaid_panel() -> None:
    should = _g("should_render_grid")

    assert should(1) is False  # nothing to separate
    assert should(2) is True
    assert should(150) is True
    assert should(151) is False  # panels too small to read


def test_grid_points_shrink_with_cell_count_but_stay_visible() -> None:
    sizes = _g("grid_point_sizes")

    small_background, small_foreground = sizes(1_000)
    large_background, large_foreground = sizes(1_000_000)

    # Highlighted cells always outrank the grey context, at any dataset size.
    assert small_foreground > small_background
    assert large_foreground > large_background
    assert large_background < small_background
    # A million-cell dataset must not render invisible points.
    assert large_background >= 0.4 and large_foreground >= 0.8
    with pytest.raises(ValueError, match="at least one cell"):
        sizes(0)


def test_figure_slug_makes_an_obs_column_safe_as_a_filename() -> None:
    slug = _g("figure_slug")

    assert slug("cell_type") == "cell-type"
    assert slug("leiden_res_1.0") == "leiden-res-1-0"
    assert slug("qc_flag_low_lib") == "qc-flag-low-lib"
    assert slug("///") == "panel"
