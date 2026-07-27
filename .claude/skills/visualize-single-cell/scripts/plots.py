"""Deterministic single-cell figures: QC, embeddings, composition, agreement, markers.

Each tool reads one H5AD artifact, produces figures plus the table behind them, and returns the
figures as model media so the plot is actually inspected rather than merely saved. Decision
logic — key resolution, layout choice, category selection, agreement arithmetic — is kept in
plain-Python helpers so it is testable without the compute environment.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

FIGURE_DPI = 160
# Above this many categories a legend stops being readable and the figure switches strategy.
LEGEND_CARDINALITY_LIMIT = 12
MAX_PANEL_COLUMNS = 2
PANEL_BASE_WIDTH = 6.0
PANEL_HEIGHT = 5.6
MAX_CATEGORIES = 30
MAX_GENES = 60
MITO_PREFIXES = ("MT-", "mt-", "Mt-")


# --- pure helpers -------------------------------------------------------------


def resolve_value_key(key: str, obs_columns: list[str], var_names: list[str]) -> tuple[str, str]:
    """Resolve a requested color/value key to an obs column or a gene, or explain the failure."""

    if key in obs_columns:
        return "obs", key
    if key in var_names:
        return "gene", key
    obs_hint = ", ".join(sorted(obs_columns)[:12]) or "none"
    raise ValueError(
        f"{key!r} is neither an obs column nor a gene in this artifact. "
        f"Available obs columns include: {obs_hint}."
    )


def resolve_embedding_key(key: str | None, obsm_keys: list[str]) -> str:
    """Pick the embedding to plot, preferring an explicit key then a conventional one."""

    if key:
        if key in obsm_keys:
            return key
        available = ", ".join(sorted(obsm_keys)) or "none"
        raise ValueError(f"embedding {key!r} is absent; available embeddings: {available}")
    for candidate in ("X_umap", "umap", "X_tsne", "X_scimilarity", "X_scVI", "X_pca"):
        if candidate in obsm_keys:
            return candidate
    raise ValueError(
        "this artifact has no embedding to plot; compute UMAP (or pass an embedding_key such as "
        "X_pca or X_scimilarity) first"
    )


def panel_grid(n_panels: int) -> tuple[int, int]:
    """Rows and columns for n panels, capped at MAX_PANEL_COLUMNS per row."""

    if n_panels < 1:
        raise ValueError("at least one panel is required")
    columns = min(n_panels, MAX_PANEL_COLUMNS)
    rows = -(-n_panels // columns)
    return rows, columns


def panel_width(legend_entries: int) -> float:
    """Width in inches for one embedding panel including the space its legend needs.

    A legend drawn outside the axes takes its width from the axes unless the figure budgets for
    it, which is how scatter panels end up as unreadable slivers. Long cell-type vocabularies are
    the normal case here, so the space is reserved up front.
    """

    if legend_entries <= 0:
        return PANEL_BASE_WIDTH + 1.1  # colorbar
    if legend_entries <= LEGEND_CARDINALITY_LIMIT:
        return PANEL_BASE_WIDTH + 2.4
    return PANEL_BASE_WIDTH + 4.2  # two-column legend


def figure_layout(n_groups: int, n_classes: int) -> dict[str, Any]:
    """Choose a readable composition layout; many categories become a heatmap, not a legend."""

    if n_classes > LEGEND_CARDINALITY_LIMIT:
        width = float(max(6.0, min(0.35 * n_classes + 3.0, 26.0)))
        height = float(max(4.0, min(0.30 * n_groups + 2.0, 22.0)))
        return {
            "mode": "heatmap",
            "figsize": (width, height),
            "annotate": n_classes <= 30 and n_groups <= 30,
            "tick_fontsize": 7 if max(n_classes, n_groups) > 20 else 8,
        }
    width = float(max(7.0, min(0.5 * n_groups + 2.0, 26.0)))
    return {
        "mode": "bar",
        "figsize": (width, 5.0),
        "legend_ncol": 1 if n_classes <= 8 else 2,
        "rotate_xticks": n_groups > 12,
    }


def select_top_categories(counts: dict[str, int], top_n: int) -> tuple[list[str], int]:
    """Keep the largest categories in descending size, reporting how many were collapsed."""

    if top_n < 1:
        raise ValueError("top_n must be at least 1")
    ordered = sorted(counts.items(), key=lambda item: (-item[1], str(item[0])))
    kept = [str(name) for name, _ in ordered[:top_n]]
    return kept, max(0, len(ordered) - len(kept))


def agreement_summary(first: list[str], second: list[str]) -> dict[str, Any]:
    """Exact-match agreement plus the most frequent disagreements between two label columns."""

    if len(first) != len(second):
        raise ValueError("label columns must describe the same cells")
    if not first:
        raise ValueError("no cells to compare")
    matches = sum(1 for a, b in zip(first, second, strict=True) if str(a) == str(b))
    disagreements: dict[tuple[str, str], int] = {}
    for a, b in zip(first, second, strict=True):
        if str(a) != str(b):
            key = (str(a), str(b))
            disagreements[key] = disagreements.get(key, 0) + 1
    top = sorted(disagreements.items(), key=lambda item: (-item[1], item[0]))[:10]
    return {
        "n_cells": len(first),
        "exact_agreement": matches / len(first),
        "top_disagreements": [
            {"first": pair[0], "second": pair[1], "cells": count} for pair, count in top
        ],
    }


def looks_like_counts(maximum: float, has_fractional: bool) -> bool:
    """Raw counts are nonnegative integers with a large dynamic range; log data are not."""

    return not has_fractional and maximum >= 20


def mito_mask_source(var_names: list[str], symbol_column: list[str] | None) -> list[str]:
    """Gene symbols to test for mitochondrial prefixes, preferring an explicit symbol column."""

    if symbol_column is not None and len(symbol_column) == len(var_names):
        return [str(item) for item in symbol_column]
    return [str(item) for item in var_names]


def is_mito(symbol: str) -> bool:
    return str(symbol).upper().startswith("MT-")


# --- shared plumbing ----------------------------------------------------------


def _load(arguments: dict[str, Any]) -> tuple[Path, Any]:
    import scanpy as sc

    path = Path(str(arguments["path"])).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    return path, sc.read_h5ad(path)


def _figure(context: Any, name: str, title: str) -> dict[str, str]:
    return {"name": name, "relative_path": f"{name}.png", "media_type": "image/png"}


def _save(figure: Any, context: Any, name: str) -> None:
    figure.savefig(
        context.staging_dir / f"{name}.png", dpi=FIGURE_DPI, bbox_inches="tight"
    )


def _write_json(context: Any, name: str, value: Any) -> dict[str, str]:
    (context.staging_dir / f"{name}.json").write_text(
        json.dumps(value, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )
    return {
        "name": name,
        "relative_path": f"{name}.json",
        "media_type": "application/json",
    }


def _write_csv(context: Any, name: str, frame: Any) -> dict[str, str]:
    frame.to_csv(context.staging_dir / f"{name}.csv")
    return {"name": name, "relative_path": f"{name}.csv", "media_type": "text/csv"}


def _matrix(adata: Any, layer: str | None) -> Any:
    if layer in (None, "X"):
        return adata.X
    if layer not in adata.layers:
        available = ", ".join(sorted(adata.layers)) or "none"
        raise ValueError(f"layer {layer!r} is absent; available layers: {available}")
    return adata.layers[layer]


def _dense_column(matrix: Any, index: int) -> Any:
    import numpy as np

    column = matrix[:, index]
    if hasattr(column, "toarray"):
        column = column.toarray()
    return np.asarray(column).ravel()


def _prepare_expression(adata: Any, layer: str | None) -> tuple[Any, bool]:
    """Return a log-normalized view for expression plots, normalizing raw counts when needed."""

    import numpy as np
    import scanpy as sc

    matrix = _matrix(adata, layer)
    sample = matrix[: min(200, adata.n_obs)]
    if hasattr(sample, "toarray"):
        sample = sample.toarray()
    sample = np.asarray(sample, dtype=float)
    maximum = float(sample.max()) if sample.size else 0.0
    fractional = bool(sample.size and not np.allclose(sample, np.rint(sample)))
    if not looks_like_counts(maximum, fractional):
        return matrix, False
    prepared = adata.copy()
    if layer not in (None, "X"):
        prepared.X = prepared.layers[layer].copy()
    sc.pp.normalize_total(prepared, target_sum=1e4)
    sc.pp.log1p(prepared)
    return prepared.X, True


def _categorical(adata: Any, key: str) -> Any:
    if key not in adata.obs:
        available = ", ".join(sorted(map(str, adata.obs.columns))[:15]) or "none"
        raise ValueError(f"obs column {key!r} is absent; available columns include: {available}")
    return adata.obs[key].astype(str)


def _envelope(
    summary: str,
    details: dict[str, Any],
    artifacts: list[dict[str, str]],
    media: list[dict[str, str]],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "summary": summary,
        "details": details,
        "artifacts": artifacts,
        "model_media": media,
    }


# --- tools --------------------------------------------------------------------


def plot_qc_distributions(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    """Library size, detected genes, mitochondrial fraction, and the UMI-rank knee."""

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    path, adata = _load(arguments)
    layer = arguments.get("counts_layer")
    layer = str(layer) if layer is not None else None
    symbol_key = arguments.get("gene_symbol_column")
    group_key = arguments.get("group_key")
    matrix = _matrix(adata, layer)
    totals = np.asarray(matrix.sum(axis=1)).ravel()
    if hasattr(matrix, "getnnz"):
        detected = matrix.getnnz(axis=1)
    else:
        detected = (np.asarray(matrix) > 0).sum(axis=1)
    detected = np.asarray(detected).ravel()
    symbols = mito_mask_source(
        [str(name) for name in adata.var_names],
        [str(item) for item in adata.var[symbol_key]] if symbol_key in adata.var else None,
    )
    mito_index = [index for index, symbol in enumerate(symbols) if is_mito(symbol)]
    if mito_index:
        mito_totals = np.asarray(matrix[:, mito_index].sum(axis=1)).ravel()
        mito_pct = np.divide(
            mito_totals * 100.0, totals, out=np.zeros_like(totals, dtype=float), where=totals > 0
        )
    else:
        mito_pct = np.zeros(adata.n_obs, dtype=float)

    figure, axes = plt.subplots(1, 4, figsize=(19.0, 4.2))
    ranked = np.sort(totals)[::-1]
    axes[0].plot(np.arange(1, ranked.size + 1), np.maximum(ranked, 1e-9), color="#1f77b4")
    axes[0].set_xscale("log")
    axes[0].set_yscale("log")
    axes[0].set_xlabel("cell rank")
    axes[0].set_ylabel("total counts")
    axes[0].set_title("UMI rank (knee)")
    for axis, values, label in (
        (axes[1], totals, "total counts per cell"),
        (axes[2], detected, "genes detected per cell"),
        (axes[3], mito_pct, "mitochondrial percent"),
    ):
        axis.hist(values, bins=60, color="#4c72b0")
        axis.set_xlabel(label)
        axis.set_ylabel("cells")
        axis.set_title(label)
    if not mito_index:
        axes[3].set_title("mitochondrial percent (no MT- genes found)")
    figure.suptitle(f"QC distributions — {path.name} ({adata.n_obs:,} cells)")
    figure.tight_layout()
    _save(figure, context, "qc-distributions")
    plt.close(figure)

    artifacts = [_figure(context, "qc-distributions", "QC distributions")]
    media = [dict(artifacts[0])]
    if group_key:
        groups = _categorical(adata, str(group_key))
        names, collapsed = select_top_categories(
            groups.value_counts().to_dict(), int(arguments.get("max_groups", MAX_CATEGORIES))
        )
        figure, axes = plt.subplots(1, 3, figsize=(max(12.0, 0.5 * len(names) + 8.0), 4.5))
        for axis, values, label in (
            (axes[0], totals, "total counts"),
            (axes[1], detected, "genes detected"),
            (axes[2], mito_pct, "mitochondrial percent"),
        ):
            axis.boxplot(
                [values[np.asarray(groups == name)] for name in names],
                tick_labels=names,
                showfliers=False,
            )
            axis.set_ylabel(label)
            axis.set_title(f"{label} by {group_key}")
            plt.setp(axis.get_xticklabels(), rotation=90, fontsize=7)
        figure.tight_layout()
        _save(figure, context, "qc-by-group")
        plt.close(figure)
        artifacts.append(_figure(context, "qc-by-group", "QC by group"))
        media.append(dict(artifacts[-1]))
        if collapsed:
            artifacts.append(
                _write_json(context, "qc-group-note", {"collapsed_groups": collapsed})
            )

    details = {
        "input_path": str(path),
        "n_cells": int(adata.n_obs),
        "n_genes": int(adata.n_vars),
        "counts_layer": layer or "X",
        "mitochondrial_genes": len(mito_index),
        "median_total_counts": float(np.median(totals)),
        "median_genes_detected": float(np.median(detected)),
        "median_mito_percent": float(np.median(mito_pct)),
    }
    artifacts.append(_write_json(context, "qc-distribution-facts", details))
    return _envelope(
        f"Plotted QC distributions for {adata.n_obs:,} cells "
        f"(median {np.median(totals):,.0f} counts, {np.median(detected):,.0f} genes, "
        f"{np.median(mito_pct):.1f}% mitochondrial).",
        details,
        artifacts,
        media,
    )


def plot_embedding(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    """Scatter an existing embedding, one panel per requested obs column or gene."""

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    path, adata = _load(arguments)
    embedding_key = resolve_embedding_key(
        str(arguments["embedding_key"]) if arguments.get("embedding_key") else None,
        list(adata.obsm),
    )
    coordinates = np.asarray(adata.obsm[embedding_key])[:, :2]
    color_keys = [str(item) for item in (arguments.get("color_keys") or [])]
    if not color_keys:
        raise ValueError("color_keys must name at least one obs column or gene to color by")
    if len(color_keys) > 9:
        raise ValueError("plot at most nine panels at once so each stays legible")
    layer = arguments.get("expression_layer")
    layer = str(layer) if layer is not None else None
    point_size = float(arguments.get("point_size", 0.0)) or max(
        2.0, min(24.0, 12000.0 / max(adata.n_obs, 1))
    )
    obs_columns = [str(column) for column in adata.obs.columns]
    var_names = [str(name) for name in adata.var_names]
    resolved = [resolve_value_key(key, obs_columns, var_names) for key in color_keys]
    expression = None
    if any(kind == "gene" for kind, _ in resolved):
        expression, normalized = _prepare_expression(adata, layer)
    else:
        normalized = False

    # Decide each panel's kind before sizing: the legend a categorical panel needs is what makes
    # the figure wide enough to stay readable.
    max_categories = int(arguments.get("max_categories", MAX_CATEGORIES))
    panels: list[dict[str, Any]] = []
    for key, (kind, name) in zip(color_keys, resolved, strict=True):
        if kind == "gene":
            panels.append({"key": key, "kind": "gene", "name": name, "entries": 0})
            continue
        series = adata.obs[name]
        if series.dtype.kind in "fiu" and series.nunique() > LEGEND_CARDINALITY_LIMIT:
            panels.append({"key": key, "kind": "continuous", "name": name, "entries": 0})
            continue
        kept, collapsed = select_top_categories(
            series.astype(str).value_counts().to_dict(), max_categories
        )
        panels.append(
            {
                "key": key,
                "kind": "categorical",
                "name": name,
                "kept": kept,
                "collapsed": collapsed,
                "entries": len(kept) + (1 if collapsed else 0),
            }
        )

    rows, columns = panel_grid(len(color_keys))
    widths = [
        max(
            panel_width(int(panels[index]["entries"]))
            for index in range(column, len(panels), columns)
        )
        for column in range(columns)
    ]
    figure, axes = plt.subplots(
        rows,
        columns,
        figsize=(sum(widths), PANEL_HEIGHT * rows),
        squeeze=False,
        gridspec_kw={"width_ratios": widths},
        layout="constrained",
    )
    rng = np.random.default_rng(int(arguments.get("random_seed", 0)))
    order = rng.permutation(coordinates.shape[0])
    legends: dict[str, Any] = {}
    for index, panel in enumerate(panels):
        key, kind, name = str(panel["key"]), str(panel["kind"]), str(panel["name"])
        axis = axes[index // columns][index % columns]
        if kind == "gene":
            assert expression is not None
            values = _dense_column(expression, var_names.index(name))[order]
            points = axis.scatter(
                coordinates[order, 0],
                coordinates[order, 1],
                c=values,
                s=point_size,
                cmap="viridis",
                linewidths=0,
            )
            figure.colorbar(points, ax=axis, label="log-normalized expression")
        elif kind == "continuous":
            values = np.asarray(adata.obs[name], dtype=float)[order]
            points = axis.scatter(
                coordinates[order, 0],
                coordinates[order, 1],
                c=values,
                s=point_size,
                cmap="viridis",
                linewidths=0,
            )
            figure.colorbar(points, ax=axis, label=name)
        else:
            kept = [str(item) for item in panel["kept"]]
            collapsed = int(panel["collapsed"])
            legends[name] = {"shown": len(kept), "collapsed": collapsed}
            palette = plt.get_cmap("tab20")
            mapping = {label: palette(position % 20) for position, label in enumerate(kept)}
            shuffled = adata.obs[name].astype(str).to_numpy()[order]
            colors = [mapping.get(label, (0.82, 0.82, 0.82, 1.0)) for label in shuffled]
            axis.scatter(
                coordinates[order, 0],
                coordinates[order, 1],
                c=colors,
                s=point_size,
                linewidths=0,
            )
            handles = [
                plt.Line2D([], [], marker="o", linestyle="", color=mapping[label], label=label)
                for label in kept
            ]
            if collapsed:
                handles.append(
                    plt.Line2D(
                        [],
                        [],
                        marker="o",
                        linestyle="",
                        color=(0.82, 0.82, 0.82, 1.0),
                        label=f"other ({collapsed})",
                    )
                )
            axis.legend(
                handles=handles,
                loc="center left",
                bbox_to_anchor=(1.01, 0.5),
                fontsize=7,
                frameon=False,
                ncol=1 if len(handles) <= LEGEND_CARDINALITY_LIMIT else 2,
                borderaxespad=0.0,
                handletextpad=0.4,
                labelspacing=0.35,
            )
        axis.set_title(key)
        axis.set_xlabel(f"{embedding_key}[0]")
        axis.set_ylabel(f"{embedding_key}[1]")
        axis.set_xticks([])
        axis.set_yticks([])
    for empty in range(len(color_keys), rows * columns):
        axes[empty // columns][empty % columns].axis("off")
    figure.suptitle(f"{embedding_key} — {path.name} ({adata.n_obs:,} cells)")
    _save(figure, context, "embedding")
    plt.close(figure)

    details = {
        "input_path": str(path),
        "embedding_key": embedding_key,
        "color_keys": color_keys,
        "n_cells": int(adata.n_obs),
        "expression_normalized_internally": normalized,
        "legends": legends,
    }
    artifacts = [
        _figure(context, "embedding", "Embedding"),
        _write_json(context, "embedding-facts", details),
    ]
    return _envelope(
        f"Plotted {embedding_key} for {adata.n_obs:,} cells colored by "
        f"{', '.join(color_keys)}.",
        details,
        artifacts,
        [dict(artifacts[0])],
    )


def plot_qc_embedding(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    """Paint every available standard QC metric and flag on an existing embedding."""

    _, adata = _load(arguments)
    preferred = (
        "total_counts",
        "n_genes_by_counts",
        "pct_counts_mt",
        "pct_counts_ribo",
        "doublet_score",
        "predicted_doublet",
    )
    keys = [column for column in preferred if column in adata.obs]
    keys.extend(
        column
        for column in map(str, adata.obs.columns)
        if column.startswith("qc_flag_") and column not in keys
    )
    keys = keys[:9]
    if not keys:
        raise ValueError(
            "no standard QC metrics or qc_flag_* columns are present; calculate QC first"
        )
    delegated = plot_embedding(
        {
            "path": arguments["path"],
            "color_keys": keys,
            "embedding_key": arguments.get("embedding_key"),
            "point_size": arguments.get("point_size", 0),
            "max_categories": arguments.get("max_categories", MAX_CATEGORIES),
            "random_seed": arguments.get("random_seed", 0),
        },
        context,
    )
    replacements = {
        "embedding.png": "qc-embedding.png",
        "embedding-facts.json": "qc-embedding-facts.json",
    }
    for old, new in replacements.items():
        (context.staging_dir / old).replace(context.staging_dir / new)
    for collection in ("artifacts", "model_media"):
        for item in delegated.get(collection, []):
            path = str(item.get("relative_path", ""))
            if path in replacements:
                item["relative_path"] = replacements[path]
            if item.get("name") == "embedding":
                item["name"] = "qc-embedding"
            elif item.get("name") == "embedding-facts":
                item["name"] = "qc-embedding-facts"
    delegated["summary"] = (
        f"Plotted {delegated['details']['embedding_key']} with {len(keys)} QC overlays: "
        + ", ".join(keys)
        + "."
    )
    delegated["details"]["qc_keys"] = keys
    return delegated


def plot_group_composition(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    """Composition of one categorical column within another, as fractions."""

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import pandas as pd

    path, adata = _load(arguments)
    group_key = str(arguments["group_key"])
    class_key = str(arguments["class_key"])
    groups = _categorical(adata, group_key)
    classes = _categorical(adata, class_key)
    kept_classes, collapsed_classes = select_top_categories(
        classes.value_counts().to_dict(), int(arguments.get("max_classes", MAX_CATEGORIES))
    )
    classes = classes.where(classes.isin(kept_classes), other="other")
    table = pd.crosstab(groups, classes)
    fractions = table.div(table.sum(axis=1), axis=0)
    layout = figure_layout(int(fractions.shape[0]), int(fractions.shape[1]))

    if layout["mode"] == "heatmap":
        figure, axis = plt.subplots(figsize=layout["figsize"])
        image = axis.imshow(
            fractions.to_numpy(), aspect="auto", cmap="viridis", vmin=0.0, vmax=1.0
        )
        axis.set_xticks(range(fractions.shape[1]))
        axis.set_xticklabels(
            [str(item) for item in fractions.columns],
            rotation=90,
            fontsize=layout["tick_fontsize"],
        )
        axis.set_yticks(range(fractions.shape[0]))
        axis.set_yticklabels(
            [str(item) for item in fractions.index], fontsize=layout["tick_fontsize"]
        )
        figure.colorbar(image, ax=axis, label=f"fraction of {group_key}")
    else:
        figure, axis = plt.subplots(figsize=layout["figsize"])
        fractions.plot(kind="bar", stacked=True, ax=axis, colormap="tab20", width=0.85)
        axis.set_ylabel(f"fraction within {group_key}")
        axis.legend(
            title=class_key,
            bbox_to_anchor=(1.02, 1.0),
            loc="upper left",
            ncol=layout["legend_ncol"],
            fontsize=8,
            frameon=False,
        )
        if layout["rotate_xticks"]:
            plt.setp(axis.get_xticklabels(), rotation=90, fontsize=8)
    axis.set_title(f"{class_key} composition within {group_key}")
    figure.tight_layout()
    _save(figure, context, "group-composition")
    plt.close(figure)

    details = {
        "input_path": str(path),
        "group_key": group_key,
        "class_key": class_key,
        "n_groups": int(fractions.shape[0]),
        "n_classes": int(fractions.shape[1]),
        "collapsed_classes": collapsed_classes,
        "layout": layout["mode"],
    }
    artifacts = [
        _figure(context, "group-composition", "Group composition"),
        _write_csv(context, "group-composition-fractions", fractions),
        _write_csv(context, "group-composition-counts", table),
        _write_json(context, "group-composition-facts", details),
    ]
    return _envelope(
        f"Plotted {class_key} composition across {fractions.shape[0]} {group_key} groups.",
        details,
        artifacts,
        [dict(artifacts[0])],
    )


def plot_label_agreement(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    """Cross-tabulate two label columns to show where independent annotations agree."""

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import pandas as pd

    path, adata = _load(arguments)
    first_key = str(arguments["first_key"])
    second_key = str(arguments["second_key"])
    if first_key == second_key:
        raise ValueError("compare two different label columns")
    first = _categorical(adata, first_key)
    second = _categorical(adata, second_key)
    max_labels = int(arguments.get("max_labels", 25))
    kept_first, collapsed_first = select_top_categories(first.value_counts().to_dict(), max_labels)
    kept_second, collapsed_second = select_top_categories(
        second.value_counts().to_dict(), max_labels
    )
    first = first.where(first.isin(kept_first), other="other")
    second = second.where(second.isin(kept_second), other="other")
    table = pd.crosstab(first, second)
    fractions = table.div(table.sum(axis=1).replace(0, 1), axis=0)
    summary = agreement_summary(
        [str(item) for item in adata.obs[first_key]],
        [str(item) for item in adata.obs[second_key]],
    )

    width = float(max(7.0, min(0.45 * fractions.shape[1] + 4.0, 24.0)))
    height = float(max(5.0, min(0.42 * fractions.shape[0] + 3.0, 22.0)))
    figure, axis = plt.subplots(figsize=(width, height))
    image = axis.imshow(fractions.to_numpy(), aspect="auto", cmap="magma", vmin=0.0, vmax=1.0)
    axis.set_xticks(range(fractions.shape[1]))
    axis.set_xticklabels([str(item) for item in fractions.columns], rotation=90, fontsize=7)
    axis.set_yticks(range(fractions.shape[0]))
    axis.set_yticklabels([str(item) for item in fractions.index], fontsize=7)
    axis.set_xlabel(second_key)
    axis.set_ylabel(first_key)
    axis.set_title(
        f"{first_key} vs {second_key} — {summary['exact_agreement']:.1%} exact label agreement"
    )
    figure.colorbar(image, ax=axis, label=f"fraction of each {first_key} label")
    figure.tight_layout()
    _save(figure, context, "label-agreement")
    plt.close(figure)

    details = {
        "input_path": str(path),
        "first_key": first_key,
        "second_key": second_key,
        "collapsed_first": collapsed_first,
        "collapsed_second": collapsed_second,
        **summary,
    }
    artifacts = [
        _figure(context, "label-agreement", "Label agreement"),
        _write_csv(context, "label-agreement-counts", table),
        _write_json(context, "label-agreement-facts", details),
    ]
    return _envelope(
        f"Compared {first_key} and {second_key} across {summary['n_cells']:,} cells: "
        f"{summary['exact_agreement']:.1%} exact label agreement. Exact string agreement "
        "understates concordance when the two vocabularies differ.",
        details,
        artifacts,
        [dict(artifacts[0])],
    )


def plot_marker_expression(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    """Dot plot of mean expression and detection fraction for genes across groups."""

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    path, adata = _load(arguments)
    group_key = str(arguments["group_key"])
    genes = [str(item) for item in (arguments.get("genes") or [])]
    if not genes:
        raise ValueError("genes must name at least one gene to plot")
    if len(genes) > MAX_GENES:
        raise ValueError(f"plot at most {MAX_GENES} genes at once so labels stay legible")
    var_names = [str(name) for name in adata.var_names]
    present = [gene for gene in genes if gene in var_names]
    missing = [gene for gene in genes if gene not in var_names]
    if not present:
        raise ValueError(
            f"none of the requested genes exist in this artifact: {', '.join(missing)}. "
            "Check the gene identifier type (symbols vs Ensembl IDs)."
        )
    # A gene absent after filtering is a fact about the data, not a reason to fail the figure —
    # but it must never be dropped silently, so it is reported in the summary and the facts.
    genes = present
    groups = _categorical(adata, group_key)
    kept, collapsed = select_top_categories(
        groups.value_counts().to_dict(), int(arguments.get("max_groups", MAX_CATEGORIES))
    )
    layer = arguments.get("expression_layer")
    expression, normalized = _prepare_expression(adata, str(layer) if layer is not None else None)

    means = np.zeros((len(kept), len(genes)), dtype=float)
    fractions = np.zeros_like(means)
    membership = {name: np.asarray(groups == name) for name in kept}
    for column, gene in enumerate(genes):
        values = _dense_column(expression, var_names.index(gene))
        for row, name in enumerate(kept):
            selected = values[membership[name]]
            if selected.size:
                means[row, column] = float(selected.mean())
                fractions[row, column] = float((selected > 0).mean())
    scaled = means.copy()
    spread = scaled.max(axis=0) - scaled.min(axis=0)
    safe = np.where(spread > 0, spread, 1.0)
    scaled = (scaled - scaled.min(axis=0)) / safe

    figure, axis = plt.subplots(
        figsize=(max(6.0, 0.5 * len(genes) + 4.0), max(4.0, 0.42 * len(kept) + 2.5))
    )
    grid_x, grid_y = np.meshgrid(np.arange(len(genes)), np.arange(len(kept)))
    points = axis.scatter(
        grid_x.ravel(),
        grid_y.ravel(),
        s=fractions.ravel() * 220.0 + 4.0,
        c=scaled.ravel(),
        cmap="Reds",
        edgecolors="#444444",
        linewidths=0.3,
    )
    axis.set_xticks(range(len(genes)))
    axis.set_xticklabels(genes, rotation=90, fontsize=8)
    axis.set_yticks(range(len(kept)))
    axis.set_yticklabels(kept, fontsize=8)
    axis.set_xlabel("gene")
    axis.set_ylabel(group_key)
    axis.set_title(f"Marker expression by {group_key}")
    figure.colorbar(points, ax=axis, label="mean expression (scaled per gene)")
    handles = [
        plt.Line2D(
            [], [], marker="o", linestyle="", markersize=size, color="#888888",
            label=f"{fraction:.0%} of cells",
        )
        for fraction, size in ((0.25, 5), (0.5, 8), (1.0, 11))
    ]
    axis.legend(
        handles=handles,
        loc="center left",
        bbox_to_anchor=(1.16, 0.5),
        frameon=False,
        fontsize=7,
        title="detected in",
        title_fontsize=7,
    )
    figure.tight_layout()
    _save(figure, context, "marker-expression")
    plt.close(figure)

    details = {
        "input_path": str(path),
        "group_key": group_key,
        "genes": genes,
        "genes_absent": missing,
        "groups_shown": kept,
        "collapsed_groups": collapsed,
        "expression_normalized_internally": normalized,
    }
    artifacts = [
        _figure(context, "marker-expression", "Marker expression"),
        _write_json(context, "marker-expression-facts", details),
    ]
    absent = f" {len(missing)} requested gene(s) absent: {', '.join(missing)}." if missing else ""
    return _envelope(
        f"Plotted {len(genes)} genes across {len(kept)} {group_key} groups "
        f"({'normalized internally' if normalized else 'using supplied values'}).{absent}",
        details,
        artifacts,
        [dict(artifacts[0])],
    )
