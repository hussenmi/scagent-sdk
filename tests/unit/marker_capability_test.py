from __future__ import annotations

from pathlib import Path
from typing import Any

from scagent_sdk.capabilities.registry import CapabilityRegistry


def _package() -> Any:
    skills_root = Path(__file__).parents[2] / ".claude" / "skills"
    return next(
        package
        for package in CapabilityRegistry(skills_root).discover()
        if package.manifest.skill_id == "marker-annotation"
    )


def _handler() -> Any:
    package = _package()
    tool = next(tool for tool in package.manifest.tools if tool.name == "evaluate_marker_evidence")
    return package.load_handler(tool)


def _markers() -> dict[str, list[str]]:
    return _handler().__globals__["HUMAN_MARKERS"]


def _score(cluster_genes: set[str], min_overlap: int = 2) -> dict[str, dict[str, Any]]:
    handler = _handler()
    marker_sets = _markers()
    frequency = handler.__globals__["_marker_frequency"](marker_sets)
    scored = handler.__globals__["_score_programs"](
        cluster_genes, marker_sets, frequency, min_overlap
    )
    return {row["candidate"]: row for row in scored}


def test_pdc_program_exists_and_is_distinct_from_plasma() -> None:
    markers = _markers()
    assert "Plasmacytoid dendritic cell" in markers
    assert "Plasma cell" in markers
    pdc = {gene.upper() for gene in markers["Plasmacytoid dendritic cell"]}
    plasma = {gene.upper() for gene in markers["Plasma cell"]}
    assert pdc.isdisjoint(plasma)
    # pDC-defining genes must not collide with the plasma/secretory program.
    assert {"LILRA4", "IL3RA", "CLEC4C"}.issubset(pdc)


def test_gzmb_is_shared_and_thus_down_weighted() -> None:
    frequency = _handler().__globals__["_marker_frequency"](_markers())
    # GZMB is deliberately present in both cytotoxic and pDC programs, so it is not specific.
    assert frequency["GZMB"] >= 2


def test_pdc_signature_scores_pdc_over_plasma_and_cytotoxic() -> None:
    scores = _score({"LILRA4", "IL3RA", "CLEC4C", "GZMB", "IRF7", "TCF4", "SERPINF1"})
    pdc = scores["Plasmacytoid dendritic cell"]
    assert pdc["support"] == "supported"
    assert scores["Plasma cell"]["overlap_count"] == 0
    top = max(scores.values(), key=lambda row: row["specificity_weighted_score"])
    assert top["candidate"] == "Plasmacytoid dendritic cell"


def test_plasma_signature_scores_plasma_over_pdc() -> None:
    scores = _score({"MZB1", "JCHAIN", "SDC1", "XBP1", "IGHG1"})
    assert scores["Plasma cell"]["support"] == "supported"
    assert scores["Plasmacytoid dendritic cell"]["overlap_count"] == 0
    top = max(scores.values(), key=lambda row: row["specificity_weighted_score"])
    assert top["candidate"] == "Plasma cell"


def test_isolated_gzmb_cannot_call_pdc_or_plasma() -> None:
    scores = _score({"GZMB"})
    assert scores["Plasmacytoid dendritic cell"]["support"] == "insufficient"
    assert scores["Plasma cell"]["overlap_count"] == 0
