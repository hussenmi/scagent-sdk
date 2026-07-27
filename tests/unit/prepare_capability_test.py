from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest

from scagent_sdk.capabilities.registry import CapabilityRegistry


def _package() -> Any:
    skills_root = Path(__file__).parents[2] / ".claude" / "skills"
    return next(
        package
        for package in CapabilityRegistry(skills_root).discover()
        if package.manifest.skill_id == "single-cell-counts"
    )


def _handler() -> Any:
    package = _package()
    tool = next(tool for tool in package.manifest.tools if tool.name == "materialize_count_matrix")
    return package.load_handler(tool)


def _inspect() -> Any:
    return _handler().__globals__["_inspect_matrix"]


def _choose() -> Any:
    return _handler().__globals__["_choose_count_source"]


class _FakeSparse:
    """Minimal scipy-sparse duck type exercising the ``.tocsr().data`` path."""

    def __init__(self, data: np.ndarray):
        self._data = np.asarray(data)

    def tocsr(self) -> Any:
        return self

    @property
    def data(self) -> np.ndarray:
        return self._data


# --- manifest ---------------------------------------------------------------


def test_manifest_exposes_standalone_counts_source_selection() -> None:
    package = _package()
    tool = next(tool for tool in package.manifest.tools if tool.name == "materialize_count_matrix")
    properties = tool.input_schema["properties"]
    assert properties["counts_source"]["enum"] == ["auto", "X", "raw", "layer"]
    assert properties["counts_source"]["default"] == "auto"
    assert "counts_layer" in properties
    assert tool.floors == ()


# --- matrix inspection ------------------------------------------------------


def test_integer_dense_matrix_is_count_like() -> None:
    stats = _inspect()(np.array([[0, 1, 2], [3, 0, 5]]))
    assert stats["count_like"] is True
    assert stats["all_integer"] is True
    assert stats["max"] == 5.0


def test_normalized_float_matrix_is_not_count_like() -> None:
    stats = _inspect()(np.array([[0.0, 1.5], [2.1, 0.0]]))
    assert stats["count_like"] is False
    assert stats["all_integer"] is False


def test_negative_matrix_is_not_count_like() -> None:
    stats = _inspect()(np.array([[-1.0, 2.0], [3.0, 4.0]]))
    assert stats["count_like"] is False


def test_sparse_integer_values_are_count_like() -> None:
    stats = _inspect()(_FakeSparse(np.array([1.0, 2.0, 3.0])))
    assert stats["count_like"] is True
    assert stats["n_stored"] == 3


def test_all_zero_sparse_matrix_is_vacuously_count_like() -> None:
    stats = _inspect()(_FakeSparse(np.array([])))
    assert stats["count_like"] is True
    assert stats["n_stored"] == 0


# --- source selection -------------------------------------------------------

_COUNTS = {"count_like": True, "min": 0.0, "max": 9.0, "all_integer": True, "finite": True}
_NORMALIZED = {
    "count_like": False,
    "min": 0.0,
    "max": 3.3,
    "all_integer": False,
    "finite": True,
}


def test_auto_prefers_count_like_x() -> None:
    label, reason = _choose()(
        {"X": _COUNTS, "layer:counts": _COUNTS},
        counts_source="auto",
        counts_layer=None,
    )
    assert label == "X"
    assert "X already holds" in reason


def test_auto_selects_single_layer_when_x_is_normalized() -> None:
    label, _reason = _choose()(
        {"X": _NORMALIZED, "layer:counts": _COUNTS},
        counts_source="auto",
        counts_layer=None,
    )
    assert label == "layer:counts"


def test_auto_selects_raw_when_x_is_normalized() -> None:
    label, _reason = _choose()(
        {"X": _NORMALIZED, "raw": _COUNTS},
        counts_source="auto",
        counts_layer=None,
    )
    assert label == "raw"


def test_auto_refuses_double_normalization_when_no_count_source() -> None:
    with pytest.raises(ValueError, match="no finite nonnegative integer count source"):
        _choose()({"X": _NORMALIZED}, counts_source="auto", counts_layer=None)


def test_auto_refuses_ambiguous_multiple_sources() -> None:
    with pytest.raises(ValueError, match="multiple count-like alternatives"):
        _choose()(
            {"X": _NORMALIZED, "raw": _COUNTS, "layer:counts": _COUNTS},
            counts_source="auto",
            counts_layer=None,
        )


def test_explicit_x_rejects_normalized_matrix() -> None:
    with pytest.raises(ValueError, match="not finite nonnegative integer counts"):
        _choose()({"X": _NORMALIZED}, counts_source="X", counts_layer=None)


def test_explicit_layer_requires_layer_name() -> None:
    with pytest.raises(ValueError, match="requires counts_layer"):
        _choose()({"X": _NORMALIZED}, counts_source="layer", counts_layer=None)


def test_explicit_layer_rejects_missing_layer() -> None:
    with pytest.raises(ValueError, match="is absent"):
        _choose()({"X": _NORMALIZED}, counts_source="layer", counts_layer="counts")


def test_explicit_layer_rejects_non_count_layer() -> None:
    with pytest.raises(ValueError, match="not finite nonnegative integer counts"):
        _choose()(
            {"X": _COUNTS, "layer:norm": _NORMALIZED},
            counts_source="layer",
            counts_layer="norm",
        )


def test_explicit_raw_rejects_absent_raw() -> None:
    with pytest.raises(ValueError, match="no aligned count-like .raw"):
        _choose()({"X": _COUNTS}, counts_source="raw", counts_layer=None)


def test_unknown_counts_source_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown counts_source"):
        _choose()({"X": _COUNTS}, counts_source="bogus", counts_layer=None)
