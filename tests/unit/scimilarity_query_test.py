"""Deterministic contracts for SCimilarity organism verification and reference atlas queries.

These exercise the pure decision logic — asset validation, organism evidence, selection
planning, ranking, and output bounding — without the scientific stack, which lives in the
compute runtime rather than the control plane.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scagent_sdk.capabilities.registry import CapabilityRegistry

HUMAN_ONLY = [f"HGENE{index}" for index in range(200)]
MOUSE_ONLY = [f"Mgene{index}" for index in range(200)]
SHARED = ["CD3D", "MS4A1", "LYZ"]


def _package() -> Any:
    root = Path(__file__).parents[2] / ".claude" / "skills"
    return next(
        package
        for package in CapabilityRegistry(root).discover()
        if package.manifest.skill_id == "scimilarity-annotation"
    )


def _handler(tool_name: str) -> Any:
    package = _package()
    tool = next(tool for tool in package.manifest.tools if tool.name == tool_name)
    return package.load_handler(tool)


def _globals() -> dict[str, Any]:
    return _handler("query_reference_cells").__globals__


def _assets() -> dict[str, Any]:
    """The skill's shared model/gene/organism module, loaded the way its tools load it."""

    import importlib.util

    source = (
        Path(__file__).parents[2]
        / ".claude"
        / "skills"
        / "scimilarity-annotation"
        / "scripts"
        / "model_assets.py"
    )
    spec = importlib.util.spec_from_file_location("scimilarity_model_assets", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return vars(module)


def _model_directory(root: Path, *, vocabulary: list[str], cellsearch: bool = False) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "encoder.ckpt").write_bytes(b"weights")
    (root / "gene_order.tsv").write_text("\n".join(vocabulary) + "\n", encoding="utf-8")
    if cellsearch:
        (root / "cellsearch").mkdir(exist_ok=True)
        (root / "cellsearch" / "full_kNN.bin").write_bytes(b"index")
        (root / "cellsearch" / "cell_metadata").mkdir(exist_ok=True)
    return root


def _configure_models(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    cellsearch: bool = False,
    mouse: bool = True,
) -> tuple[Path, Path]:
    human = _model_directory(
        tmp_path / "human", vocabulary=HUMAN_ONLY + SHARED, cellsearch=cellsearch
    )
    mouse_path = _model_directory(
        tmp_path / "mouse",
        vocabulary=MOUSE_ONLY + [gene.capitalize() for gene in SHARED],
        cellsearch=cellsearch,
    )
    monkeypatch.setenv("SCIMILARITY_MODEL_PATH", str(human))
    if mouse:
        monkeypatch.setenv("SCIMILARITY_MODEL_PATH_MOUSE", str(mouse_path))
    else:
        monkeypatch.delenv("SCIMILARITY_MODEL_PATH_MOUSE", raising=False)
    return human, mouse_path


class _Var:
    """A minimal var stand-in: pandas is unavailable in the control-plane environment."""

    def __init__(self, names: list[str], columns: dict[str, list[str]] | None = None) -> None:
        self.index = list(names)
        self._columns = dict(columns or {})
        self.columns = list(self._columns)

    def __getitem__(self, key: str) -> list[str]:
        return self._columns[key]


# --- organism ---------------------------------------------------------------------------


def test_organism_must_be_declared_explicitly() -> None:
    declared = _assets()["declared_organism"]

    assert declared({"organism": "Mouse"}) == "mouse"
    for arguments in ({}, {"organism": ""}, {"organism": "rat"}, {"organism": None}):
        with pytest.raises(ValueError, match="declared explicitly"):
            declared(arguments)


def test_organism_schema_requires_organism_and_has_no_default() -> None:
    package = _package()
    for tool_name in ("run_scimilarity_annotation", "query_reference_cells"):
        tool = next(tool for tool in package.manifest.tools if tool.name == tool_name)
        organism = tool.input_schema["properties"]["organism"]
        assert "organism" in tool.input_schema["required"], tool_name
        assert "default" not in organism, tool_name
        assert organism["enum"] == ["human", "mouse"], tool_name


def test_species_evidence_uses_organism_specific_vocabulary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assets = _assets()
    _configure_models(tmp_path, monkeypatch)
    vocabularies = assets["configured_vocabularies"]()
    evidence = assets["species_evidence"]

    consistent = evidence(HUMAN_ONLY + SHARED, declared="human", vocabularies=vocabularies)
    assert consistent["verdict"] == "consistent"
    assert consistent["declared_specific_hits"] == 200
    assert consistent["counter_specific_hits"] == 0

    # Casing is a pipeline convention, not a species: uppercased mouse input is still mouse.
    contradicted = evidence(
        [gene.upper() for gene in MOUSE_ONLY], declared="human", vocabularies=vocabularies
    )
    assert contradicted["verdict"] == "contradicted"
    assert contradicted["counter_specific_hits"] == 200

    assert (
        evidence(MOUSE_ONLY, declared="mouse", vocabularies=vocabularies)["verdict"]
        == "consistent"
    )


def test_species_evidence_declines_to_judge_without_enough_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assets = _assets()
    _configure_models(tmp_path, monkeypatch)
    vocabularies = assets["configured_vocabularies"]()
    evidence = assets["species_evidence"]

    ensembl = evidence(
        [f"ENSG{index:011d}" for index in range(2000)],
        declared="human",
        vocabularies=vocabularies,
    )
    assert ensembl["verdict"] == "unverified"
    assert "organism-specific symbols" in ensembl["reason"]

    single = evidence(MOUSE_ONLY, declared="human", vocabularies={"human": HUMAN_ONLY})
    assert single["verdict"] == "unverified"
    assert single["compared_with"] is None


def test_verify_species_refuses_a_contradicted_organism_unless_overridden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assets = _assets()
    _configure_models(tmp_path, monkeypatch)
    verify = assets["verify_species"]
    var = _Var([gene.upper() for gene in MOUSE_ONLY])

    with pytest.raises(ValueError, match="contradicts the input genes"):
        verify(var, declared="human")

    allowed = verify(var, declared="human", allow_mismatch=True)
    assert allowed["verdict"] == "contradicted"
    assert allowed["override_allowed"] is True
    assert "allow_species_mismatch=true" in allowed["reason"]

    assert verify(_Var(HUMAN_ONLY), declared="human")["verdict"] == "consistent"


def test_species_evidence_reads_symbol_columns_not_just_var_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The check must see whatever the gene selector could choose, including symbol columns."""

    assets = _assets()
    _configure_models(tmp_path, monkeypatch)
    var = _Var(
        [f"ENSMUSG{index:011d}" for index in range(200)],
        {"mgi_symbol": MOUSE_ONLY},
    )

    with pytest.raises(ValueError, match="contradicts the input genes"):
        assets["verify_species"](var, declared="human")
    assert assets["verify_species"](var, declared="mouse")["verdict"] == "consistent"


# --- model assets -----------------------------------------------------------------------


def test_query_requires_cellsearch_assets_annotation_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assets = _assets()
    human, _ = _configure_models(tmp_path, monkeypatch, cellsearch=False)
    resolve = assets["resolve_model"]

    assert resolve({"organism": "human"}) == human.resolve()
    with pytest.raises(ValueError, match="no reference cell-search index"):
        resolve({"organism": "human"}, require_cellsearch=True)

    (human / "cellsearch").mkdir()
    (human / "cellsearch" / "cell_metadata").mkdir()
    with pytest.raises(ValueError, match=r"cellsearch/full_kNN\.bin"):
        resolve({"organism": "human"}, require_cellsearch=True)

    (human / "cellsearch" / "full_kNN.bin").write_bytes(b"index")
    assert resolve({"organism": "human"}, require_cellsearch=True) == human.resolve()


def test_model_fingerprint_separates_embedding_from_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assets = _assets()
    human, _ = _configure_models(tmp_path, monkeypatch, cellsearch=True)
    fingerprint = assets["model_fingerprint"]

    embedding_only = fingerprint(human)
    with_index = fingerprint(human, include_cellsearch=True)
    assert embedding_only != with_index

    (human / "cellsearch" / "full_kNN.bin").write_bytes(b"a different index")
    assert fingerprint(human) == embedding_only
    assert fingerprint(human, include_cellsearch=True) != with_index


# --- query selection --------------------------------------------------------------------


def test_group_queries_skip_small_groups_and_refuse_to_truncate() -> None:
    plan = _globals()["_plan_group_queries"]
    labels = ["0"] * 30 + ["1"] * 25 + ["2"] * 3

    kept, skipped = plan(labels, None, group_key="leiden", min_query_cells=10, max_queries=40)
    assert kept == [("0", 30), ("1", 25)]
    assert skipped == [{"group": "2", "n_cells": 3, "reason": "below min_query_cells"}]

    kept, skipped = plan(labels, ["1"], group_key="leiden", min_query_cells=10, max_queries=40)
    assert kept == [("1", 25)] and skipped == []

    with pytest.raises(ValueError, match="exceeds max_queries"):
        plan(labels, None, group_key="leiden", min_query_cells=1, max_queries=2)

    with pytest.raises(ValueError, match="not present in obs"):
        plan(labels, ["7"], group_key="leiden", min_query_cells=10, max_queries=40)

    with pytest.raises(ValueError, match="fewer than min_query_cells"):
        plan(labels, None, group_key="leiden", min_query_cells=100, max_queries=40)


def test_cell_id_selection_validates_membership_and_size() -> None:
    plan = _globals()["_plan_cell_ids"]
    obs_names = [f"cell{index}" for index in range(50)]

    assert plan(obs_names, obs_names[:20], min_query_cells=10) == 20
    with pytest.raises(ValueError, match="absent from this dataset"):
        plan(obs_names, ["cell1", "ghost"], min_query_cells=1)
    with pytest.raises(ValueError, match="below min_query_cells"):
        plan(obs_names, obs_names[:3], min_query_cells=10)


def test_selection_requires_exactly_one_of_group_key_or_cell_ids() -> None:
    resolve = _globals()["_resolve_queries"]
    adata = SimpleNamespace(obs={"leiden": ["0"] * 20}, obs_names=[f"c{i}" for i in range(20)])

    with pytest.raises(ValueError, match="not both"):
        resolve(
            adata,
            {"group_key": "leiden", "cell_ids": ["c0"]},
            min_query_cells=10,
            max_queries=40,
        )
    with pytest.raises(ValueError, match="a query selection is required"):
        resolve(adata, {}, min_query_cells=10, max_queries=40)
    with pytest.raises(ValueError, match="absent from obs"):
        resolve(adata, {"group_key": "missing"}, min_query_cells=10, max_queries=40)


def test_resolve_queries_builds_one_mask_per_group() -> None:
    resolve = _globals()["_resolve_queries"]
    labels = ["a"] * 12 + ["b"] * 11 + ["c"] * 2
    adata = SimpleNamespace(
        obs={"cell_type": labels},
        obs_names=[f"c{index}" for index in range(len(labels))],
    )

    queries, selection = resolve(
        adata, {"group_key": "cell_type"}, min_query_cells=10, max_queries=40
    )
    assert [name for name, _ in queries] == ["a", "b"]
    assert [int(mask.sum()) for _, mask in queries] == [12, 11]
    assert selection["kind"] == "group"
    assert selection["group_key"] == "cell_type"
    assert selection["requested_groups"] == 3
    assert selection["skipped_groups"] == [
        {"group": "c", "n_cells": 2, "reason": "below min_query_cells"}
    ]

    queries, selection = resolve(
        adata,
        {"cell_ids": [f"c{index}" for index in range(15)]},
        min_query_cells=10,
        max_queries=40,
    )
    assert [name for name, _ in queries] == ["selected-cells"]
    assert int(queries[0][1].sum()) == 15
    assert selection["kind"] == "cell_ids"


# --- summarization and bounding ---------------------------------------------------------


def test_rank_counts_is_fraction_bearing_and_tie_stable() -> None:
    rank = _globals()["_rank_counts"]
    ranked = rank({"b cell": 2, "T cell": 6, "monocyte": 2}, top_n=2)

    assert ranked[0] == {"value": "T cell", "count": 6, "fraction": 0.6}
    assert ranked[1]["value"] == "b cell"  # ties break on value, not dict order
    assert rank({}, top_n=3) == []


def test_distance_summary_reports_none_when_absent() -> None:
    summarize = _globals()["_distance_summary"]

    assert summarize(None) is None
    assert summarize([]) is None
    assert summarize([0.1, 0.3, 0.2]) == {
        "minimum": 0.1,
        "median": 0.2,
        "mean": 0.2,
        "maximum": 0.3,
    }


def test_coherence_is_planned_from_query_size_and_the_cost_flag() -> None:
    plan = _globals()["_coherence_plan"]

    assert plan(400, requested=True) == (True, 10, None)
    assert plan(30, requested=True) == (True, 10, None)
    assert plan(24, requested=True) == (True, 10, None)
    measured, subcentroids, reason = plan(21, requested=True)
    assert (measured, subcentroids, reason) == (True, 10, None)

    measured, _, reason = plan(19, requested=True)
    assert measured is False and "would not be meaningful" in (reason or "")

    measured, _, reason = plan(4000, requested=False)
    assert measured is False and reason == "measure_coherence=false"


def test_coherence_record_carries_its_k_and_its_skip_reason() -> None:
    record = _globals()["_coherence_record"]

    measured = record(value=71.4, measured=True, subcentroids=10, k=100)
    assert measured["value"] == 71.4
    assert measured["compared_against_k"] == 100
    assert measured["neighbors_per_subcentroid"] == 100

    skipped = record(value=None, measured=False, subcentroids=2, k=100, reason="because")
    assert skipped["measured"] is False
    assert skipped["value"] is None
    assert skipped["subcentroids"] is None
    assert skipped["reason"] == "because"


def test_query_schema_exposes_the_coherence_cost_control() -> None:
    package = _package()
    tool = next(
        tool for tool in package.manifest.tools if tool.name == "query_reference_cells"
    )
    coherence = tool.input_schema["properties"]["measure_coherence"]
    assert coherence["default"] is True
    assert "cost" in coherence["description"] or "costs" in coherence["description"]


def test_summary_only_reports_composition_columns_that_exist() -> None:
    summarize = _globals()["_summarize"]
    record = _globals()["_coherence_record"]

    summary = summarize(
        "3",
        n_query_cells=40,
        n_neighbors=100,
        distances=[0.02, 0.04],
        composition_counts={"celltype_name": {"T cell": 90, "NK cell": 10}, "study": {"S1": 100}},
        coherence=record(value=80.0, measured=True, subcentroids=10, k=100),
        top_n=10,
    )
    assert set(summary["composition"]) == {"celltype_name", "study"}
    assert summary["composition"]["celltype_name"][0]["fraction"] == 0.9
    assert summary["n_reference_studies"] == 1
    assert _globals()["_summary_row"](summary) == {
        "query": "3",
        "n_query_cells": 40,
        "n_reference_neighbors": 100,
        "top_celltype": "T cell",
        "top_celltype_fraction": 0.9,
        "coherence": 80.0,
        "median_neighbor_distance": 0.03,
    }


def test_inline_view_bounds_queries_and_labels() -> None:
    inline_view = _globals()["_inline_view"]
    summarize = _globals()["_summarize"]
    record = _globals()["_coherence_record"]
    summaries = [
        summarize(
            str(index),
            n_query_cells=30,
            n_neighbors=100,
            distances=[0.05],
            composition_counts={
                "celltype_name": {f"type{rank}": 100 - rank for rank in range(12)}
            },
            coherence=record(value=50.0, measured=True, subcentroids=10, k=100),
            top_n=10,
        )
        for index in range(30)
    ]

    inline, bounding = inline_view(summaries)
    assert bounding["queries_truncated"] is True
    assert bounding["queries_shown"] == len(inline) == 25
    assert bounding["queries_total"] == 30
    assert len(inline[0]["composition"]["celltype_name"]) == 5

    few, bounding = inline_view(summaries[:3])
    assert bounding["queries_truncated"] is False
    assert len(few) == 3


def test_inline_view_keeps_the_new_evidence_and_fits_the_executor_budget() -> None:
    """The executor drops `details` whole when it is oversized, so this must trim, not overflow."""

    import json

    globals_ = _globals()
    inline_view = globals_["_inline_view"]
    summarize = globals_["_summarize"]
    record = globals_["_coherence_record"]
    background = globals_["_background_comparison"]

    def _summary(index: int) -> dict[str, Any]:
        composition = {
            column: {f"{column}-value-{rank}": 100 - rank for rank in range(12)}
            for column in ("celltype_name", "tissue_general", "tissue", "disease", "study")
        }
        summary = summarize(
            f"cluster-{index}",
            n_query_cells=50,
            n_neighbors=100,
            distances=[0.01, 0.02],
            composition_counts=composition,
            coherence=record(value=40.0, measured=True, subcentroids=10, k=100),
            top_n=10,
            exclusion=globals_["_exclusion_record"](before=100, after=90, studies=["S1"]),
            samples=[
                {
                    "study": f"study-{rank}",
                    "sample": f"sample-{rank}-with-a-long-identifier",
                    "tissue": "colonic epithelium",
                    "disease": "Crohn disease",
                    "cells": 20,
                    "fraction_of_sample": 0.004,
                    "sample_size": 4363,
                }
                for rank in range(10)
            ],
        )
        summary["reference_background"] = background(
            "enterocyte",
            summary["composition"],
            {"disease": {"normal": 900, "Crohn disease": 100}},
            top_n=10,
        )
        return summary

    inline, bounding = inline_view([_summary(index) for index in range(3)])
    assert "study_exclusion" in inline[0]
    assert "reference_background" in inline[0]
    assert "top_reference_samples" in inline[0]
    assert bounding["omitted_blocks"] == []

    # Many rich queries must still fit, by dropping blocks in a stated order rather than silently.
    many, bounding = inline_view([_summary(index) for index in range(60)])
    payload = len(json.dumps(many, default=str).encode())
    assert payload <= globals_["_INLINE_BUDGET_BYTES"]
    assert bounding["queries_truncated"] is True
    assert bounding["queries_total"] == 60
    assert "artifact" in bounding["note"]

    # A tiny budget forces the narrowest plan instead of returning something oversized.
    tight, bounding = inline_view([_summary(0)], budget_bytes=800)
    assert len(json.dumps(tight, default=str).encode()) <= 1500
    assert bounding["queries_shown"] == 1


# --- per-cell mode, exclusion, background -----------------------------------------------


def test_per_cell_mode_refuses_a_large_selection_instead_of_subsampling() -> None:
    plan = _globals()["_plan_cell_queries"]
    obs_names = [f"cell{index}" for index in range(500)]

    assert plan(
        obs_names, cell_ids=["cell3", "cell9"], group_selected=None, max_query_cells=10
    ) == ["cell3", "cell9"]
    # Duplicates collapse rather than paying for the same search twice.
    assert plan(
        obs_names, cell_ids=["cell3", "cell3"], group_selected=None, max_query_cells=10
    ) == ["cell3"]

    with pytest.raises(ValueError, match="absent from this dataset"):
        plan(obs_names, cell_ids=["ghost"], group_selected=None, max_query_cells=10)
    with pytest.raises(ValueError, match=r"above max_query_cells=10"):
        plan(obs_names, cell_ids=None, group_selected=obs_names[:40], max_query_cells=10)
    with pytest.raises(ValueError, match="resolved to no cells"):
        plan(obs_names, cell_ids=None, group_selected=[], max_query_cells=10)


def test_per_cell_mode_default_cap_is_small() -> None:
    """The point of the default is that a query cannot quietly run for a long time."""

    assert _globals()["_DEFAULT_MAX_QUERY_CELLS"] == 10
    package = _package()
    tool = next(
        tool for tool in package.manifest.tools if tool.name == "query_reference_cells"
    )
    properties = tool.input_schema["properties"]
    assert properties["max_query_cells"]["default"] == 10
    assert properties["query_mode"]["default"] == "centroid"
    assert properties["k"]["default"] == 100  # SCimilarity's own default is 10,000


def test_resolve_queries_makes_one_query_per_cell_in_cells_mode() -> None:
    resolve = _globals()["_resolve_queries"]
    labels = ["a"] * 4 + ["b"] * 3
    adata = SimpleNamespace(
        obs={"cell_type": labels},
        obs_names=[f"c{index}" for index in range(len(labels))],
    )

    queries, selection = resolve(
        adata,
        {"cell_ids": ["c0", "c5"]},
        min_query_cells=10,
        max_queries=40,
        query_mode="cells",
    )
    assert [name for name, _ in queries] == ["c0", "c5"]
    assert [int(mask.sum()) for _, mask in queries] == [1, 1]
    assert selection["kind"] == "cells"
    assert selection["max_query_cells"] == 10

    # A group selection in cells mode expands to its members, and min_query_cells does not apply.
    queries, selection = resolve(
        adata,
        {"group_key": "cell_type", "group_values": ["b"]},
        min_query_cells=10,
        max_queries=40,
        query_mode="cells",
    )
    assert [name for name, _ in queries] == ["c4", "c5", "c6"]
    assert selection["group_key"] == "cell_type"


def test_study_exclusion_is_reported_including_when_it_empties_a_query() -> None:
    record = _globals()["_exclusion_record"]

    partial = record(before=100, after=60, studies=["S1"])
    assert partial["neighbors_removed"] == 40
    assert "warning" not in partial

    emptied = record(before=100, after=0, studies=["S1", "S2"])
    assert emptied["neighbors_after"] == 0
    assert "no independent reference support" in emptied["warning"]

    untouched = record(before=0, after=0, studies=[])
    assert "warning" not in untouched


def test_reference_background_turns_fractions_into_enrichment() -> None:
    compare = _globals()["_background_comparison"]
    composition = {
        "disease": [
            {"value": "Crohn disease", "count": 60, "fraction": 0.6},
            {"value": "normal", "count": 40, "fraction": 0.4},
        ]
    }
    background = {"disease": {"Crohn disease": 1_000, "normal": 9_000}}

    result = compare("enterocyte", composition, background, top_n=5)

    assert result["reference_celltype"] == "enterocyte"
    assert result["reference_cells"] == 10_000
    crohn, normal = result["axes"]["disease"]
    assert crohn == {
        "value": "Crohn disease",
        "query_fraction": 0.6,
        "reference_fraction": 0.1,
        "enrichment": 6.0,
    }
    assert normal["enrichment"] == 0.44

    # A value absent from the background cannot be given a ratio.
    absent = compare(
        "enterocyte",
        {"disease": [{"value": "rare", "count": 1, "fraction": 1.0}]},
        background,
        top_n=5,
    )
    assert absent["axes"]["disease"][0]["enrichment"] is None


def test_neighbor_budget_caps_rows_per_query() -> None:
    budget = _globals()["_neighbor_budget"]

    assert budget([100, 100]) == (None, False)
    assert budget([], cap=10) == (None, False)
    assert budget([600, 600], cap=1000) == (500, True)
    assert budget([10_000] * 3, cap=2) == (1, True)
