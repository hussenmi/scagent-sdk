"""Flag-first single-cell QC and separately confirmed filtering."""

from __future__ import annotations

import copy
import hashlib
import json
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


def unreviewed_figures(required: Any, reviewed: Any) -> list[str]:
    """Return the required figures the review did not name, matching on the file name.

    The requirement is that the model looked at each figure, not that it can transcribe a 90-
    character artifact path. Demanding exact full paths turned a scientific gate into a copying
    exercise: a real session failed this check twice in a row on the same assessment, retried with
    the paths copied out of the error text, and the review that finally landed was no better for it.
    A basename, a path suffix, or the full relative path all identify the same figure unambiguously
    here, so all three are accepted; naming a figure that was never required is ignored rather than
    treated as an error.
    """

    def keys(value: str) -> set[str]:
        cleaned = str(value).strip().strip("'\"").replace("\\", "/").rstrip("/")
        parts = [part for part in cleaned.split("/") if part and part != "."]
        # Every trailing sub-path, so "leiden/umap.png" matches ".../cluster-qc/leiden/umap.png".
        return {"/".join(parts[index:]) for index in range(len(parts))} or {cleaned}

    named: set[str] = set()
    for value in reviewed or ():
        named |= keys(str(value))
    missing = []
    for value in required or ():
        path = str(value)
        if not keys(path) & named:
            missing.append(path)
    return sorted(missing)


def _identity(kind: str, value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return f"{kind}:sha256:{hashlib.sha256(encoded).hexdigest()}"


def _count_matrix_identity(matrix: Any, obs_names: Any, var_names: Any) -> str:
    import numpy as np
    from scipy import sparse

    digest = hashlib.sha256()
    digest.update(b"scagent-count-matrix-v1\0")
    digest.update(str(tuple(map(int, matrix.shape))).encode() + b"\0")
    if sparse.issparse(matrix):
        value = matrix.tocsr()
        arrays = (value.data, value.indices, value.indptr)
    else:
        arrays = (np.asarray(matrix),)
    for array in arrays:
        contiguous = np.ascontiguousarray(array)
        digest.update(str(contiguous.dtype).encode() + b"\0")
        digest.update(memoryview(contiguous).cast("B"))
    for names in (obs_names, var_names):
        for name in names:
            digest.update(str(name).encode("utf-8", errors="replace") + b"\0")
    return f"counts:sha256:{digest.hexdigest()}"


def _resolve_layer(adata: Any, layer: str | None) -> str | None:
    if layer == "auto":
        return "counts" if "counts" in adata.layers else None
    return layer


def _matrix(adata: Any, layer: str | None) -> Any:
    import numpy as np

    layer = _resolve_layer(adata, layer)
    if layer is None:
        matrix = adata.X
        label = "X"
    elif layer in adata.layers:
        matrix = adata.layers[layer]
        label = f"layer:{layer}"
    else:
        raise ValueError(f"count layer {layer!r} is absent; use counts_layer=null for X")
    values = np.asarray(matrix.tocsr().data if hasattr(matrix, "tocsr") else matrix).ravel()
    if values.size and (
        not bool(np.all(np.isfinite(values)))
        or not bool(np.all(values >= 0))
        or not bool(np.all(values == np.round(values)))
    ):
        raise ValueError(f"{label} is not finite nonnegative integer counts")
    return matrix


def _symbols(adata: Any) -> list[str]:
    original = [str(value) for value in adata.var_names]
    for column in _SYMBOL_COLUMNS:
        if column in adata.var:
            values = [str(value) for value in adata.var[column]]
            if sum(bool(value) and value.lower() != "nan" for value in values) > len(values) / 2:
                return values
    return original


# Gene-removal class prefixes, matched case-insensitively on the resolved gene symbols (so Ensembl-
# or Entrez-indexed matrices still match via the symbol column). The ribosomal set intentionally
# includes the mitochondrial-ribosomal genes (MRPS/MRPL) alongside the cytoplasmic ones (RPS/RPL),
# matching the legacy removal set; the per-cell ``pct_counts_ribo`` metric stays on RPS/RPL only, so
# the removal set is deliberately the broader of the two.
_RIBO_REMOVAL_PREFIXES = ("rps", "rpl", "mrps", "mrpl")
_MITO_REMOVAL_PREFIX = "mt-"

# Legacy's guaranteed low-detection filter scales with dataset size: keep genes detected in at least
# 2% of cells, capped at 100 absolute cells so a large dataset does not demand a huge count and
# delete rare-cell-type markers. Small datasets are governed by the fraction; the cap takes over on
# big ones. An absolute count overrides both, and a fraction of 0 disables the filter entirely.
_DEFAULT_MIN_CELL_FRACTION = 0.02
_DEFAULT_MAX_CELLS_CAP = 100


def low_detection_threshold(
    n_cells: int,
    *,
    min_cells: int | None,
    min_cell_fraction: float,
    max_cells: int,
) -> tuple[int, str, bool]:
    """Resolve the minimum-cells-per-gene threshold, dataset-size-aware by default.

    Returns ``(threshold, basis, capped)``. An explicit ``min_cells`` overrides the fraction; a
    fraction of 0 disables the filter (threshold 0). The fraction is rounded up, then capped.
    """

    import math

    if min_cells is not None:
        return max(0, int(min_cells)), "absolute_min_cells", False
    if min_cell_fraction <= 0:
        return 0, "disabled", False
    from_fraction = int(math.ceil(min_cell_fraction * n_cells))
    if max_cells > 0 and from_fraction > max_cells:
        return max_cells, "fraction_of_cells_capped", True
    return from_fraction, "fraction_of_cells", False


def is_ribosomal_gene(symbol: str) -> bool:
    return symbol.casefold().startswith(_RIBO_REMOVAL_PREFIXES)


def is_mitochondrial_gene(symbol: str) -> bool:
    return symbol.casefold().startswith(_MITO_REMOVAL_PREFIX)


def _restamped(marker_holder: dict[str, Any]) -> None:
    """Record on a carried-forward evidence dict that it was carried, not recomputed."""

    marker = marker_holder.get("carried_through")
    existing = marker if isinstance(marker, list) else []
    marker_holder["carried_through"] = existing + ["gene_filter"]


def carry_cell_qc_through_gene_filter(
    fact: Any,
    *,
    cell_set_id: str,
    parent_count_representation_id: str | None,
    new_count_representation_id: str,
) -> dict[str, Any] | None:
    """Carry a cell-QC review across a gene-only change.

    Gene filtering removes genes, not cells, so a cell-quality decision made on the full matrix
    stays valid. Re-stamp the review's count-representation identity onto the filtered matrix and
    record that it was carried rather than recomputed (recomputing would read a false ~0% ribosomal,
    since those genes are gone). Returns None when there is no review, it is about different cells,
    or it was not current for the artifact being filtered -- so the floor still asks for a fresh
    review in those cases. The cell set is unchanged, so cell_set_id is preserved as-is.
    """

    if not isinstance(fact, dict) or fact.get("cell_set_id") != cell_set_id:
        return None
    if (
        parent_count_representation_id is not None
        and fact.get("count_representation_id") != parent_count_representation_id
    ):
        return None
    carried = copy.deepcopy(fact)
    carried["count_representation_id"] = new_count_representation_id
    review = carried.get("review")
    if isinstance(review, dict):
        review["count_representation_id"] = new_count_representation_id
    _restamped(carried)
    return carried


def carry_doublets_through_gene_filter(
    fact: Any,
    *,
    cell_set_id: str,
    parent_count_representation_id: str | None,
    new_count_representation_id: str,
) -> dict[str, Any] | None:
    """Carry complete doublet evidence across a gene-only change.

    Scrublet scores are a per-cell property; removing genes does not change which cells are
    doublets. Same currency guard as the cell-QC carry: same cells, current for the input artifact.
    """

    if not isinstance(fact, dict):
        return None
    evidence = fact.get("evidence")
    if not isinstance(evidence, dict) or evidence.get("cell_set_id") != cell_set_id:
        return None
    if (
        parent_count_representation_id is not None
        and evidence.get("count_representation_id") != parent_count_representation_id
    ):
        return None
    carried = copy.deepcopy(fact)
    carried["evidence"]["count_representation_id"] = new_count_representation_id
    _restamped(carried["evidence"])
    return carried


HISTOGRAM_BINS = 60
# Per-cell points drawn over a violin body; past this the strip is solid ink and the extra
# points only cost render time.
VIOLIN_JITTER_CELLS = 20000
VIOLIN_JITTER_WIDTH = 0.2


def log10_positive(values: Any, np: Any) -> tuple[Any, int]:
    """The strictly positive values as log10, and how many could not be transformed.

    Plotting the logarithm is not the same as log-*scaling* raw values. On a log-scaled axis the
    numbers printed are not the numbers being drawn, so a distribution's spread and symmetry cannot
    be read off it and equal spacing carries no fixed meaning. Transforming first makes the axis
    read in decades, makes histogram bins uniform by construction, and makes equal distances equal
    fold-changes -- which is also the space library-size and gene-count cutoffs are chosen in.

    Zero has no logarithm, so those cells are dropped and counted rather than folded in by a log1p
    shift that would present them as ordinary small values. "1,204 cells have zero counts" is QC
    evidence, not a rendering detail.

    Kept identical to the copy in `visualize-single-cell` on purpose. A skill's scripts directory is
    the only thing on its import path, so a shared plotting convention has to be duplicated rather
    than imported; the copies must not drift.
    """

    finite = np.asarray(values, dtype=float)
    finite = finite[np.isfinite(finite)]
    positive = finite[finite > 0.0]
    return np.log10(positive), int(finite.size - positive.size)


def raw_reference_ticks(low: float, high: float) -> list[float]:
    """Raw values to mark opposite a log10 axis spanning ``[low, high]``, both given raw.

    The mantissa set widens as the range narrows. One mark per decade is too sparse to place a
    value against and a mark per integer collides, so 1/2/5 is the default -- but a metric confined
    to a third of a decade would get one mark or none from it, which is no reference at all.
    """

    import math

    if not low > 0.0 or not high > low:
        return []
    decades = math.log10(high) - math.log10(low)
    if decades >= 1.5:
        mantissas: tuple[float, ...] = (1.0, 2.0, 5.0)
    elif decades >= 0.6:
        mantissas = (1.0, 1.5, 2.0, 3.0, 5.0, 7.0)
    else:
        mantissas = (1.0, 1.2, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 8.0)
    ticks: list[float] = []
    for exponent in range(math.floor(math.log10(low)), math.ceil(math.log10(high)) + 1):
        for mantissa in mantissas:
            value = mantissa * 10.0**exponent
            if low <= value <= high:
                ticks.append(value)
    return sorted(ticks)


def label_log10_axis(
    axis: Any, which: str, label: str, np: Any, *, raw_label: str = "raw value"
) -> None:
    """Name an axis carrying log10 values and mirror the raw magnitudes opposite it.

    The mirror is what keeps the change honest: a reader still has to act in raw counts, and the
    log-scaled axis this replaces was previously the only place those numbers appeared.
    """

    from matplotlib.ticker import FuncFormatter

    formatter = FuncFormatter(lambda value, _: compact_tick(value))
    forward_inverse = (
        lambda value: 10.0**value,
        lambda value: np.log10(np.clip(value, 1e-12, None)),
    )
    if which == "x":
        axis.set_xlabel(f"{label} (log10)")
        low, high = axis.get_xlim()
        mirror = axis.secondary_xaxis("top", functions=forward_inverse)
        ticks = raw_reference_ticks(10.0**low, 10.0**high)
        if ticks:
            mirror.set_xticks(ticks)
        mirror.xaxis.set_major_formatter(formatter)
        # No axis label on the top mirror: it would only repeat the metric the primary label
        # already names, and with sparse marks a centred label lands on top of a tick.
    else:
        axis.set_ylabel(f"{label} (log10)")
        low, high = axis.get_ylim()
        mirror = axis.secondary_yaxis("right", functions=forward_inverse)
        ticks = raw_reference_ticks(10.0**low, 10.0**high)
        if ticks:
            mirror.set_yticks(ticks)
        mirror.yaxis.set_major_formatter(formatter)
        mirror.set_ylabel(raw_label, fontsize=8, labelpad=2)
    mirror.tick_params(labelsize=8)


def compact_tick(value: float) -> str:
    """Short axis-tick text for count-scale numbers.

    Spelled-out thousands ("10,000", "20,000", "50,000") overrun each other on a multi-panel row,
    which reintroduces the collision the 1/2/5 locator was chosen to avoid.
    """

    magnitude = abs(float(value))
    if magnitude >= 1_000_000:
        return f"{float(value) / 1_000_000:g}M"
    if magnitude >= 1_000:
        return f"{float(value) / 1_000:g}k"
    return f"{float(value):g}"


def _plain_log_ticks(axis: Any, which: str = "x") -> None:
    """Label a log axis with plain numbers at 1/2/5 per decade instead of mantissa notation.

    Over a two-decade range matplotlib labels the minor ticks as "2 x 10^2", "3 x 10^2", ...,
    which run together into an unreadable smear on the genes-per-cell panel. Decade-only labels
    are legible but too sparse to read a threshold off, so 1/2/5 per decade is used.
    """

    from matplotlib.ticker import FuncFormatter, LogLocator, NullFormatter

    target = axis.xaxis if which == "x" else axis.yaxis
    target.set_major_locator(LogLocator(base=10.0, subs=(1.0, 2.0, 5.0)))
    target.set_major_formatter(FuncFormatter(lambda value, _: compact_tick(value)))
    target.set_minor_formatter(NullFormatter())


def _violin_panel(axis: Any, values: Any, label: str, rng: Any) -> int:
    """Violin body with the per-cell points jittered over it; returns the points drawn.

    The body alone is a smoothed density that looks identical over forty cells and forty
    thousand. Overlaying the cells is what the original scagent QC violins did
    (`sc.pl.violin(..., jitter=0.2)`) and it is what makes the sample size legible.
    """

    import numpy as np

    finite = np.asarray(values, dtype=float)
    finite = finite[np.isfinite(finite)]
    axis.set_xticks([])
    axis.set_title(label, fontsize=10)
    axis.set_ylabel("value")
    if finite.size == 0:
        axis.text(0.5, 0.5, "no finite values", ha="center", va="center", transform=axis.transAxes)
        return 0
    parts = axis.violinplot(finite, showextrema=False, widths=0.85)
    for body in parts["bodies"]:
        body.set_facecolor("#3b75af")
        body.set_edgecolor("#1f3f5f")
        body.set_linewidth(0.8)
        body.set_alpha(0.9)
    shown = min(int(finite.size), VIOLIN_JITTER_CELLS)
    sample = finite if shown == finite.size else rng.choice(finite, size=shown, replace=False)
    axis.scatter(
        1.0 + rng.uniform(-VIOLIN_JITTER_WIDTH, VIOLIN_JITTER_WIDTH, size=sample.size),
        sample,
        s=0.8,
        color="#111111",
        alpha=0.35,
        linewidths=0,
        rasterized=True,
    )
    lower, median, upper = (float(item) for item in np.percentile(finite, [25.0, 50.0, 75.0]))
    axis.vlines(1.0, lower, upper, color="#ffffff", linewidth=3.0, zorder=3)
    axis.plot([1.0], [median], marker="o", markersize=3.5, color="#ffffff", zorder=4)
    return shown


def _add_metrics(
    adata: Any,
    *,
    layer: str | None,
    organism: str,
    min_genes: int | None,
    max_genes: int | None,
    max_pct_mito: float | None,
) -> tuple[Any, dict[str, int]]:
    import numpy as np
    import scanpy as sc

    layer = _resolve_layer(adata, layer)
    counts = _matrix(adata, layer)
    work = adata.copy()
    work.X = counts.copy()
    names = _symbols(work)
    if organism == "mouse":
        mt = [name.startswith(("mt-", "Mt-", "MT-")) for name in names]
    else:
        mt = [name.upper().startswith("MT-") for name in names]
    ribo = [name.upper().startswith(("RPS", "RPL")) for name in names]
    work.var["mt"] = np.asarray(mt, dtype=bool)
    work.var["ribo"] = np.asarray(ribo, dtype=bool)
    sc.pp.calculate_qc_metrics(
        work, qc_vars=["mt", "ribo"], percent_top=None, log1p=True, inplace=True
    )
    keep = np.ones(work.n_obs, dtype=bool)
    flags: dict[str, int] = {}
    if min_genes is not None:
        flag = np.asarray(work.obs["n_genes_by_counts"] < min_genes)
        work.obs["qc_flag_low_genes"] = flag
        flags["low_genes"] = int(flag.sum())
        keep &= ~flag
    if max_genes is not None:
        flag = np.asarray(work.obs["n_genes_by_counts"] > max_genes)
        work.obs["qc_flag_high_genes"] = flag
        flags["high_genes"] = int(flag.sum())
        keep &= ~flag
    if max_pct_mito is not None:
        flag = np.asarray(work.obs["pct_counts_mt"] > max_pct_mito)
        work.obs["qc_flag_high_mito"] = flag
        flags["high_mito"] = int(flag.sum())
        keep &= ~flag
    work.obs["qc_pass_requested_thresholds"] = keep
    flags["any_requested_flag"] = int((~keep).sum())
    return work, flags


def _render_qc_figures(
    adata: Any,
    context: Any,
    *,
    thresholds: dict[str, int | float | None],
) -> list[dict[str, str]]:
    import matplotlib.pyplot as plt
    import numpy as np

    output_dir = context.staging_dir / "qc"
    output_dir.mkdir(parents=True, exist_ok=True)

    def values(column: str) -> Any:
        result = np.asarray(adata.obs[column], dtype=float)
        return result[np.isfinite(result)]

    def paired(*columns: str, positive: tuple[str, ...] = ()) -> tuple[Any, ...]:
        """Columns masked together, keeping only rows usable in every one of them.

        A per-column filter is fine for a marginal histogram but wrong for a scatter: a cell with
        zero counts has an undefined mitochondrial percent, so filtering each column on its own
        leaves two arrays of different lengths that no longer describe the same cells. Columns
        needing a logarithm are additionally required to be positive.
        """

        raw = [np.asarray(adata.obs[column], dtype=float) for column in columns]
        mask = np.ones(raw[0].shape, dtype=bool)
        for column, series in zip(columns, raw, strict=True):
            mask &= np.isfinite(series)
            if column in positive:
                mask &= series > 0.0
        return tuple(series[mask] for series in raw)

    def save(fig: Any, name: str) -> dict[str, str]:
        relative = f"qc/{name}"
        fig.savefig(context.staging_dir / relative, dpi=160, bbox_inches="tight")
        plt.close(fig)
        return {
            "name": Path(name).stem.replace("_", "-"),
            "relative_path": relative,
            "media_type": "image/png",
        }

    figures: list[dict[str, str]] = []
    total = values("total_counts")
    genes = values("n_genes_by_counts")
    mito = values("pct_counts_mt")
    ribo = values("pct_counts_ribo")

    rng = np.random.default_rng(0)

    def log_histogram(axis: Any, series: Any, label: str, unit: str) -> None:
        """Histogram of the metric's log10 values, so the bins are uniform in what is plotted."""

        logged, dropped = log10_positive(series, np)
        axis.hist(logged, bins=HISTOGRAM_BINS, edgecolor="black", linewidth=0.3)
        axis.set_ylabel("Cells")
        label_log10_axis(axis, "x", label, np, raw_label=unit)
        # Padded clear of the raw-magnitude mirror that now runs along the top spine.
        axis.set_title(
            label if not dropped else f"{label} ({dropped:,} zero-count omitted)", pad=26
        )

    fig, axes = plt.subplots(1, 5, figsize=(24, 4.8))
    ranked = np.sort(total)[::-1]
    logged_ranks, _ = log10_positive(np.arange(1, ranked.size + 1), np)
    logged_counts, dropped_ranks = log10_positive(ranked, np)
    # `ranked` is sorted descending, so any zero-count cells are its tail and the surviving counts
    # still line up with the leading ranks.
    axes[0].plot(logged_ranks[: logged_counts.size], logged_counts)
    label_log10_axis(axes[0], "x", "Cell rank", np, raw_label="cell rank")
    label_log10_axis(axes[0], "y", "Total counts", np, raw_label="counts")
    axes[0].set_title(
        "UMI rank (knee)"
        if not dropped_ranks
        else f"UMI rank (knee) ({dropped_ranks:,} zero-count omitted)",
        pad=26,
    )
    log_histogram(axes[1], total, "Library size", "counts")
    log_histogram(axes[2], genes, "Genes per cell", "genes")
    axes[3].hist(mito, bins=HISTOGRAM_BINS, edgecolor="black", linewidth=0.3)
    if thresholds["max_pct_mito"] is not None:
        axes[3].axvline(
            float(thresholds["max_pct_mito"]),
            color="crimson",
            linestyle="--",
            label=f"threshold {float(thresholds['max_pct_mito']):g}%",
        )
        axes[3].legend()
    axes[3].set(xlabel="Mitochondrial percent", ylabel="Cells", title="Mitochondrial content")
    axes[4].hist(ribo, bins=HISTOGRAM_BINS, edgecolor="black", linewidth=0.3)
    axes[4].set(xlabel="Ribosomal percent", ylabel="Cells", title="Ribosomal content")
    fig.suptitle(f"Single-cell QC distributions ({adata.n_obs:,} cells)")
    fig.tight_layout()
    figures.append(save(fig, "qc_distributions.png"))

    # log10 rather than the recorded log1p_* columns, so every logged panel in this figure set reads
    # in one unit: a decade is a decade and 3.7 is recognisably "a few thousand", where natural-log
    # 8.5 has to be converted before it means anything.
    violin_columns = [
        (log10_positive(total, np)[0], "total counts (log10)"),
        (log10_positive(genes, np)[0], "genes per cell (log10)"),
        (mito, "pct_counts_mt"),
        (ribo, "pct_counts_ribo"),
    ]
    fig, axes = plt.subplots(1, len(violin_columns), figsize=(4.2 * len(violin_columns), 4.6))
    for axis, (series, label) in zip(axes, violin_columns, strict=True):
        _violin_panel(axis, series, label, rng)
    fig.suptitle(f"QC metric distributions ({adata.n_obs:,} cells)")
    fig.tight_layout()
    figures.append(save(fig, "qc_violin_metrics.png"))

    fig, axes = plt.subplots(1, 2, figsize=(12, 4.5))
    log_histogram(axes[0], total, "Library size", "counts")
    log_histogram(axes[1], genes, "Genes per cell", "genes")
    fig.tight_layout()
    figures.append(save(fig, "qc_histograms.png"))

    fig, axis = plt.subplots(figsize=(8, 4.5))
    axis.hist(mito, bins=100)
    if thresholds["max_pct_mito"] is not None:
        axis.axvline(
            float(thresholds["max_pct_mito"]),
            color="crimson",
            linestyle="--",
            label=f"threshold {float(thresholds['max_pct_mito']):g}%",
        )
        axis.legend()
    axis.set(xlabel="Mitochondrial percent", ylabel="Cells", title="Mitochondrial content")
    fig.tight_layout()
    figures.append(save(fig, "qc_mt_histogram.png"))

    fig, axes = plt.subplots(1, 2, figsize=(12, 5.4))
    joint_total, joint_genes = paired(
        "total_counts", "n_genes_by_counts", positive=("total_counts", "n_genes_by_counts")
    )
    axes[0].scatter(np.log10(joint_total), np.log10(joint_genes), s=8, alpha=0.55)
    label_log10_axis(axes[0], "x", "Total counts", np, raw_label="counts")
    label_log10_axis(axes[0], "y", "Genes detected", np, raw_label="genes")
    axes[0].set_title("Genes versus counts", pad=26)
    mito_total, mito_paired = paired("total_counts", "pct_counts_mt", positive=("total_counts",))
    axes[1].scatter(np.log10(mito_total), mito_paired, s=8, alpha=0.55)
    label_log10_axis(axes[1], "x", "Total counts", np, raw_label="counts")
    axes[1].set_ylabel("Mitochondrial percent")
    if thresholds["max_pct_mito"] is not None:
        axes[1].axhline(float(thresholds["max_pct_mito"]), color="crimson", linestyle="--")
    axes[1].set_title("Mitochondrial percent versus counts", pad=26)
    fig.tight_layout()
    figures.append(save(fig, "qc_scatter.png"))

    fig, axis = plt.subplots(figsize=(6, 5))
    axis.scatter(*paired("pct_counts_mt", "pct_counts_ribo"), s=8, alpha=0.55)
    if thresholds["max_pct_mito"] is not None:
        axis.axvline(float(thresholds["max_pct_mito"]), color="crimson", linestyle="--")
    axis.set(
        xlabel="Mitochondrial percent",
        ylabel="Ribosomal percent",
        title="Ribosomal versus mitochondrial content",
    )
    fig.tight_layout()
    figures.append(save(fig, "qc_ribo_vs_mt.png"))

    # Doublet scores are plotted only when another skill has already computed them; this tool
    # neither runs Scrublet nor invents a cutoff, so no reference line is drawn.
    doublet_column = next(
        (name for name in ("doublet_score", "scrublet_score") if name in adata.obs), None
    )
    if doublet_column is not None:
        scores = values(doublet_column)
        fig, axes = plt.subplots(1, 2, figsize=(12, 4.4))
        _violin_panel(axes[0], scores, doublet_column, rng)
        axes[1].hist(scores, bins=HISTOGRAM_BINS, edgecolor="black", linewidth=0.3)
        axes[1].set(xlabel=doublet_column, ylabel="Cells", title="Doublet score histogram")
        fig.suptitle("Doublet evidence already present on this artifact")
        fig.tight_layout()
        figures.append(save(fig, "qc_doublet_scores.png"))
    return figures


def _base_metadata(adata: Any) -> dict[str, Any]:
    return dict(adata.uns.get("scagent_sdk", {}))


def _write_qc_artifacts(
    adata: Any, context: Any, *, output_name: str, report: dict[str, Any]
) -> list[dict[str, str]]:
    adata.write_h5ad(context.staging_dir / output_name, compression="gzip")
    adata.obs[
        [
            column
            for column in (
                "total_counts",
                "n_genes_by_counts",
                "pct_counts_mt",
                "pct_counts_ribo",
                "qc_flag_low_genes",
                "qc_flag_high_genes",
                "qc_flag_high_mito",
                "qc_pass_requested_thresholds",
            )
            if column in adata.obs
        ]
    ].to_csv(context.staging_dir / "cell-qc-metrics.csv")
    (context.staging_dir / "cell-qc-summary.json").write_text(
        json.dumps(report, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )
    return [
        {
            "name": "qc-anndata",
            "relative_path": output_name,
            "media_type": "application/x-hdf5",
        },
        {
            "name": "cell-qc-metrics",
            "relative_path": "cell-qc-metrics.csv",
            "media_type": "text/csv",
        },
        {
            "name": "cell-qc-summary",
            "relative_path": "cell-qc-summary.json",
            "media_type": "application/json",
        },
    ]


def calculate_qc(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    import scanpy as sc

    path = Path(str(arguments["path"])).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    layer_arg = arguments.get("counts_layer", "auto")
    layer = str(layer_arg) if layer_arg is not None else None
    organism = str(arguments.get("organism", "human"))
    min_arg = arguments.get("min_genes", 200)
    max_arg = arguments.get("max_genes")
    mito_arg = arguments.get("max_pct_mito", 20)
    thresholds = {
        "min_genes": int(min_arg) if min_arg is not None else None,
        "max_genes": int(max_arg) if max_arg is not None else None,
        "max_pct_mito": float(mito_arg) if mito_arg is not None else None,
    }
    source = sc.read_h5ad(path)
    layer = _resolve_layer(source, layer)
    adata, flags = _add_metrics(source, layer=layer, organism=organism, **thresholds)
    metadata = _base_metadata(adata)
    metadata["qc_assessment_id"] = _identity(
        "cell-qc",
        {
            "path": str(path),
            "cells": sorted(map(str, adata.obs_names)),
            "thresholds": thresholds,
            "flags": flags,
        },
    )
    adata.uns["scagent_sdk"] = metadata
    output_name = "qc-assessed.h5ad"
    final_path = f"artifacts/capabilities/{context.execution_id}/{output_name}"
    report = {
        "operation": "calculate_only",
        "organism": organism,
        "counts_source": "X" if layer is None else f"layer:{layer}",
        "n_cells": int(adata.n_obs),
        "n_genes": int(adata.n_vars),
        "thresholds": thresholds,
        "flag_counts": flags,
        "metric_summary": {
            "median_total_counts": float(adata.obs["total_counts"].median()),
            "median_genes_detected": float(adata.obs["n_genes_by_counts"].median()),
            "median_pct_mito": float(adata.obs["pct_counts_mt"].median()),
            "median_pct_ribo": float(adata.obs["pct_counts_ribo"].median()),
            "n_mitochondrial_genes": int(adata.var["mt"].sum()),
            "n_ribosomal_genes": int(adata.var["ribo"].sum()),
        },
        "mitochondrial_threshold_options": {
            str(value): {
                "threshold": value,
                "cells_flagged": int((adata.obs["pct_counts_mt"] > value).sum()),
                "fraction_flagged": float((adata.obs["pct_counts_mt"] > value).mean()),
            }
            for value in (5, 10, 15, 20, 25, 30)
        },
        "qc_assessment_id": metadata["qc_assessment_id"],
    }
    artifacts = _write_qc_artifacts(adata, context, output_name=output_name, report=report)
    figures = _render_qc_figures(adata, context, thresholds=thresholds)
    artifacts.extend(figures)
    provenance = dict(adata.uns.get("scagent_sdk", {}))
    required_visual_artifacts = [
        f"artifacts/capabilities/{context.execution_id}/{item['relative_path']}"
        for item in figures
    ]
    return {
        "summary": (
            f"Calculated QC for {adata.n_obs:,} cells; "
            f"{flags['any_requested_flag']:,} cells meet at least one requested flag."
        ),
        "details": report,
        "facts_patch": {
            "cell_qc": {
                "status": "assessed",
                "assessment_id": metadata["qc_assessment_id"],
                "artifact_path": final_path,
                "thresholds": thresholds,
                "flag_counts": flags,
                "cell_set_id": provenance.get("cell_set_id"),
                "count_representation_id": provenance.get("count_representation_id"),
                "review_status": "pending",
                "required_visual_artifacts": required_visual_artifacts,
            }
        },
        "artifacts": artifacts,
        "model_media": figures,
    }


def review_qc(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    facts = context.state_facts
    evidence = facts.get("cell_qc")
    if not isinstance(evidence, dict) or evidence.get("status") != "assessed":
        raise ValueError("current cell-QC assessment is absent; run calculate_single_cell_qc")
    assessment_id = str(arguments["assessment_id"])
    if assessment_id != evidence.get("assessment_id"):
        raise ValueError("assessment_id does not match the current cell-QC assessment")
    rationale = str(arguments["rationale"]).strip()
    if not rationale:
        raise ValueError("rationale must not be empty")
    missing = unreviewed_figures(
        evidence.get("required_visual_artifacts", []), arguments.get("reviewed_artifacts", [])
    )
    if missing:
        raise ValueError(
            "visual review is incomplete. Look at these figures, then name them (the file name "
            "alone is enough) in reviewed_artifacts: " + ", ".join(missing)
        )
    decision = str(arguments["decision"])
    resolved = decision in {"keep_all", "not_applicable"}
    review = {
        "status": "resolved" if resolved else "action_required",
        "assessment_id": assessment_id,
        "decision": decision,
        "rationale": rationale,
        "visual_findings": [str(value) for value in arguments.get("visual_findings", [])],
        # The canonical paths, not the shorthand the model used to name them, so the record says
        # which files were reviewed rather than how they happened to be referred to.
        "reviewed_artifacts": sorted(
            str(value) for value in evidence.get("required_visual_artifacts", [])
        ),
        "cell_set_id": evidence.get("cell_set_id"),
        "count_representation_id": evidence.get("count_representation_id"),
    }
    return {
        "summary": (
            f"Recorded cell-QC decision {decision!r}; "
            + ("QC review is resolved." if resolved else "follow-up action is required.")
        ),
        "details": review,
        "facts_patch": {"cell_qc": {"review": review}},
        "decisions_patch": {"cell_qc_handling": review},
    }


def filter_cells(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    import scanpy as sc

    if arguments.get("confirm_filtering") is not True:
        raise ValueError("confirm_filtering must be true before changing the cell set")
    path = Path(str(arguments["path"])).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    layer_arg = arguments.get("counts_layer", "auto")
    layer = str(layer_arg) if layer_arg is not None else None
    organism = str(arguments.get("organism", "human"))
    min_arg = arguments.get("min_genes", 200)
    max_arg = arguments.get("max_genes")
    mito_arg = arguments.get("max_pct_mito", 20)
    thresholds = {
        "min_genes": int(min_arg) if min_arg is not None else None,
        "max_genes": int(max_arg) if max_arg is not None else None,
        "max_pct_mito": float(mito_arg) if mito_arg is not None else None,
    }
    source = sc.read_h5ad(path)
    layer = _resolve_layer(source, layer)
    assessed, flags = _add_metrics(source, layer=layer, organism=organism, **thresholds)
    before = int(assessed.n_obs)
    filtered = assessed[assessed.obs["qc_pass_requested_thresholds"].to_numpy()].copy()
    if filtered.n_obs == 0:
        raise ValueError("requested thresholds would remove every cell")
    cell_set_id = _identity("cells", sorted(map(str, filtered.obs_names)))
    filtered_counts = _matrix(filtered, layer)
    matrix_id = _count_matrix_identity(
        filtered_counts, filtered.obs_names, filtered.var_names
    )
    count_id = _identity(
        "count-representation",
        {
            "matrix_id": matrix_id,
            "cell_set_id": cell_set_id,
            "operation": "filter-cells",
        },
    )
    metadata = _base_metadata(filtered)
    metadata.update(
        {
            "cell_set_id": cell_set_id,
            "count_representation_id": count_id,
            "count_matrix_id": matrix_id,
            "dataset_revision_id": _identity(
                "dataset-revision",
                {
                    "parent_path": str(path),
                    "cell_set_id": cell_set_id,
                    "count_representation_id": count_id,
                    "operation": "filter-cells",
                },
            ),
        }
    )
    for key in ("representation_id", "clustering_id", "qc_assessment_id"):
        metadata.pop(key, None)
    filtered.uns["scagent_sdk"] = metadata
    output_name = "cells-filtered.h5ad"
    report = {
        "operation": "filter_cells",
        "before_cells": before,
        "after_cells": int(filtered.n_obs),
        "removed_cells": before - int(filtered.n_obs),
        "thresholds": thresholds,
        "flag_counts": flags,
        "cell_set_id": cell_set_id,
    }
    artifacts = _write_qc_artifacts(filtered, context, output_name=output_name, report=report)
    return {
        "summary": (
            f"Filtered {before - filtered.n_obs:,} of {before:,} cells; "
            f"{filtered.n_obs:,} remain."
        ),
        "details": report,
        "facts_patch": {
            "analysis": {
                "dataset_revision": {
                    "id": metadata["dataset_revision_id"],
                    "n_cells": int(filtered.n_obs),
                    "n_genes": int(filtered.n_vars),
                },
                "cell_set": {"id": cell_set_id, "n_cells": int(filtered.n_obs)},
                "count_representation": {
                    "id": count_id,
                    "matrix_id": matrix_id,
                    "method": "counts-after-cell-filter",
                    "source_layer": layer or "X",
                },
                "representation": None,
                "clustering": None,
            },
            "cell_qc": None,
            "doublets": None,
            "batch": None,
            "cluster_qc": None,
            "annotation": None,
            "finalization": None,
        },
        "artifacts": artifacts,
    }


def filter_genes(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    import numpy as np
    import scanpy as sc

    if arguments.get("confirm_filtering") is not True:
        raise ValueError("confirm_filtering must be true before changing the feature set")
    path = Path(str(arguments["path"])).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    layer_arg = arguments.get("counts_layer", "auto")
    layer = str(layer_arg) if layer_arg is not None else None
    organism = str(arguments.get("organism", "human"))

    # Threshold knobs. An explicit ``min_cells`` is an absolute override; otherwise the dataset-size
    # fraction (default 2%) capped at ``max_cells`` (default 100) governs, and fraction 0 disables.
    override_arg = arguments.get("min_cells")
    min_cells_override = int(override_arg) if override_arg is not None else None
    try:
        min_cell_fraction = max(
            0.0, float(arguments.get("min_cell_fraction", _DEFAULT_MIN_CELL_FRACTION))
        )
    except (TypeError, ValueError):
        min_cell_fraction = _DEFAULT_MIN_CELL_FRACTION
    try:
        max_cells = max(0, int(arguments.get("max_cells", _DEFAULT_MAX_CELLS_CAP)))
    except (TypeError, ValueError):
        max_cells = _DEFAULT_MAX_CELLS_CAP
    remove_ribosomal = bool(arguments.get("remove_ribosomal", True))
    remove_mitochondrial = bool(arguments.get("remove_mitochondrial", False))

    adata = sc.read_h5ad(path)
    layer = _resolve_layer(adata, layer)
    counts = _matrix(adata, layer)
    before = int(adata.n_vars)
    n_cells_total = int(adata.n_obs)
    symbols = _symbols(adata)

    # Compute per-cell mitochondrial/ribosomal percentages on the full matrix *before* any gene
    # removal, so they survive as obs columns and the QC, embedding, and cluster-QC ribosomal
    # figures keep their data source. Only fill what is missing: a prior calculate_single_cell_qc
    # already wrote these, and its values are the ones to preserve.
    if "pct_counts_ribo" not in adata.obs or "pct_counts_mt" not in adata.obs:
        metrics = adata.copy()
        metrics.X = counts.copy()
        if organism == "mouse":
            mt_flag = [name.startswith(("mt-", "Mt-", "MT-")) for name in symbols]
        else:
            mt_flag = [name.upper().startswith("MT-") for name in symbols]
        metrics.var["mt"] = np.asarray(mt_flag, dtype=bool)
        metrics.var["ribo"] = np.asarray(
            [name.upper().startswith(("RPS", "RPL")) for name in symbols], dtype=bool
        )
        sc.pp.calculate_qc_metrics(
            metrics, qc_vars=["mt", "ribo"], percent_top=None, log1p=True, inplace=True
        )
        for column in ("pct_counts_mt", "pct_counts_ribo"):
            if column not in adata.obs and column in metrics.obs:
                adata.obs[column] = metrics.obs[column].to_numpy()

    # Build the keep mask. Each removed gene is attributed to the first reason it fails, so the
    # per-category counts are disjoint and sum to the total removed.
    detected = np.asarray((counts > 0).sum(axis=0)).ravel()
    threshold, threshold_basis, fraction_capped = low_detection_threshold(
        n_cells_total,
        min_cells=min_cells_override,
        min_cell_fraction=min_cell_fraction,
        max_cells=max_cells,
    )
    remove_low = ~(detected >= threshold) if threshold > 0 else np.zeros(before, dtype=bool)
    if remove_ribosomal:
        remove_ribo = np.asarray([is_ribosomal_gene(name) for name in symbols]) & ~remove_low
    else:
        remove_ribo = np.zeros(before, dtype=bool)
    if remove_mitochondrial:
        remove_mito = (
            np.asarray([is_mitochondrial_gene(name) for name in symbols])
            & ~remove_low
            & ~remove_ribo
        )
    else:
        remove_mito = np.zeros(before, dtype=bool)

    keep = ~(remove_low | remove_ribo | remove_mito)
    removed_low = int(remove_low.sum())
    removed_ribosomal = int(remove_ribo.sum())
    removed_mitochondrial = int(remove_mito.sum())
    filtered = adata[:, keep].copy()
    if filtered.n_vars == 0:
        raise ValueError("requested filters would remove every gene")

    filter_identity = {
        "operation": "filter-genes",
        "min_cells": threshold,
        "threshold_basis": threshold_basis,
        "remove_ribosomal": remove_ribosomal,
        "remove_mitochondrial": remove_mitochondrial,
    }
    metadata = _base_metadata(filtered)
    cell_set_id = metadata.get("cell_set_id") or _identity(
        "cells", sorted(map(str, filtered.obs_names))
    )
    filtered_counts = _matrix(filtered, layer)
    matrix_id = _count_matrix_identity(
        filtered_counts, filtered.obs_names, filtered.var_names
    )
    count_id = _identity(
        "count-representation",
        {"matrix_id": matrix_id, "cell_set_id": cell_set_id, **filter_identity},
    )
    revision_id = _identity(
        "dataset-revision",
        {
            "parent_path": str(path),
            "genes": sorted(map(str, filtered.var_names)),
            "count_representation_id": count_id,
            **filter_identity,
        },
    )
    metadata.update(
        {
            "dataset_revision_id": revision_id,
            "cell_set_id": cell_set_id,
            "count_representation_id": count_id,
            "count_matrix_id": matrix_id,
        }
    )
    for key in (
        "representation_id",
        "clustering_id",
        "qc_assessment_id",
    ):
        metadata.pop(key, None)
    filtered.uns["scagent_sdk"] = metadata

    # Gene filtering preserves the cell set, so a current cell-QC review and complete doublet
    # evidence remain valid for the same cells; carry them forward re-stamped to the new count
    # representation instead of nulling them. Everything gene-dependent (representation, clustering,
    # cluster QC, annotation, batch) is correctly invalidated below.
    state_facts = getattr(context, "state_facts", {}) or {}
    parent_count_representation_id = _base_metadata(adata).get("count_representation_id")
    carried_cell_qc = carry_cell_qc_through_gene_filter(
        state_facts.get("cell_qc"),
        cell_set_id=cell_set_id,
        parent_count_representation_id=parent_count_representation_id,
        new_count_representation_id=count_id,
    )
    carried_doublets = carry_doublets_through_gene_filter(
        state_facts.get("doublets"),
        cell_set_id=cell_set_id,
        parent_count_representation_id=parent_count_representation_id,
        new_count_representation_id=count_id,
    )

    output_name = "genes-filtered.h5ad"
    filtered.write_h5ad(context.staging_dir / output_name, compression="gzip")
    report = {
        "operation": "filter_genes",
        "before_genes": before,
        "after_genes": int(filtered.n_vars),
        "removed_genes": before - int(filtered.n_vars),
        "min_cells": threshold,
        "threshold_basis": threshold_basis,
        "min_cell_fraction": min_cell_fraction if threshold_basis.startswith("fraction") else None,
        "max_cells_cap": max_cells if threshold_basis.startswith("fraction") else None,
        "fraction_capped": fraction_capped,
        "n_cells_total": n_cells_total,
        "removed_low_detection": removed_low,
        "remove_ribosomal": remove_ribosomal,
        "removed_ribosomal": removed_ribosomal,
        "remove_mitochondrial": remove_mitochondrial,
        "removed_mitochondrial": removed_mitochondrial,
    }
    (context.staging_dir / "gene-filter-summary.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    threshold_text = (
        f"detected in <{threshold} cells"
        if threshold > 0
        else "no low-detection threshold"
    )
    parts = [f"{removed_low:,} low-detection ({threshold_text})"]
    if remove_ribosomal:
        parts.append(f"{removed_ribosomal:,} ribosomal")
    if remove_mitochondrial:
        parts.append(f"{removed_mitochondrial:,} mitochondrial")
    return {
        "summary": (
            f"Removed {before - filtered.n_vars:,} of {before:,} genes "
            f"({', '.join(parts)}); {filtered.n_vars:,} remain. "
            "Per-cell mitochondrial/ribosomal percentages are preserved in obs."
        ),
        "details": report,
        "facts_patch": {
            "analysis": {
                "dataset_revision": {
                    "id": revision_id,
                    "n_cells": int(filtered.n_obs),
                    "n_genes": int(filtered.n_vars),
                },
                "cell_set": {"id": cell_set_id, "n_cells": int(filtered.n_obs)},
                "count_representation": {
                    "id": count_id,
                    "matrix_id": matrix_id,
                    "method": "counts-after-gene-filter",
                    "source_layer": layer or "X",
                },
                "representation": None,
                "clustering": None,
            },
            # Per-cell evidence carried forward re-stamped (None when there was nothing current to
            # carry, which correctly leaves the floor asking for it).
            "cell_qc": carried_cell_qc,
            "doublets": carried_doublets,
            "batch": None,
            "cluster_qc": None,
            "annotation": None,
            "finalization": None,
        },
        "artifacts": [
            {
                "name": "gene-filtered-anndata",
                "relative_path": output_name,
                "media_type": "application/x-hdf5",
            },
            {
                "name": "gene-filter-summary",
                "relative_path": "gene-filter-summary.json",
                "media_type": "application/json",
            },
        ],
    }
