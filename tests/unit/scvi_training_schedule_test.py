"""Deterministic tests for the scVI epoch cap and convergence summary.

These exercise the pure decision logic only; they import the skill script without scvi, torch, or
pandas, so they run in the control-plane venv. Whether the resulting representation is
scientifically sound is a separate (live) question.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

_INTEGRATE_PATH = (
    Path(__file__).parents[2]
    / ".claude"
    / "skills"
    / "scvi-integration"
    / "scripts"
    / "integrate.py"
)


def _module() -> Any:
    spec = importlib.util.spec_from_file_location("scvi_integrate_under_test", _INTEGRATE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


M = _module()


@pytest.mark.parametrize(
    "n_cells,expected",
    [
        (500, 400),
        (20_000, 400),
        (40_000, 200),
        (200_000, 40),
        (2_000_000, 4),
        (100_000_000, 1),
        (0, 400),
    ],
)
def test_epoch_cap_scales_inversely_with_cell_count(n_cells: int, expected: int) -> None:
    assert M.heuristic_epoch_cap(n_cells) == expected


def test_unset_epochs_defer_to_scvi_heuristic() -> None:
    cap, source = M.resolve_epoch_cap(50_000, None, lambda n_obs: 123)
    assert (cap, source) == (123, "scvi_cell_count_heuristic")


def test_missing_scvi_heuristic_falls_back_to_the_cell_count_formula() -> None:
    cap, source = M.resolve_epoch_cap(200_000, None, None)
    assert (cap, source) == (40, "cell_count_formula")
    # A heuristic that a future release renamed or broke must not silence the cell count.
    broken, broken_source = M.resolve_epoch_cap(200_000, None, lambda n_obs: None)
    assert (broken, broken_source) == (40, "cell_count_formula")


def test_explicit_epochs_win_over_the_heuristic() -> None:
    assert M.resolve_epoch_cap(200_000, 10, lambda n_obs: 400) == (10, "explicit")


def test_early_stopped_run_is_reported_as_such() -> None:
    verdict = M.convergence_verdict(
        [100.0, 90.0, 85.0, 84.0, 83.9],
        [101.0, 92.0, 87.0, 86.5, 86.4],
        max_epochs=400,
        early_stopping=True,
    )
    assert verdict["epochs_trained"] == 5
    assert verdict["early_stopped"] is True
    assert verdict["hit_cap"] is False
    assert verdict["best_val_epoch"] == 4
    assert verdict["final_elbo_train"] == pytest.approx(83.9)
    assert verdict["overfitting_warning"] is None


def test_running_the_full_cap_is_not_reported_as_early_stopped() -> None:
    verdict = M.convergence_verdict(
        [100.0, 90.0, 80.0],
        [101.0, 91.0, 81.0],
        max_epochs=3,
        early_stopping=True,
    )
    assert verdict["early_stopped"] is False
    assert verdict["hit_cap"] is True
    assert "had not plateaued" in M.epoch_narrative(20_000, verdict, "scvi_cell_count_heuristic")


def test_rising_validation_elbo_raises_an_overfitting_warning() -> None:
    verdict = M.convergence_verdict(
        [100.0, 80.0, 70.0, 60.0, 50.0],
        [100.0, 80.0, 70.0, 75.0, 90.0],
        max_epochs=400,
        early_stopping=True,
    )
    assert verdict["best_val_epoch"] == 2
    assert verdict["overfitting_warning"] is not None
    assert "28.6%" in verdict["overfitting_warning"]


def test_a_validation_curve_that_only_dips_at_the_end_is_not_overfitting() -> None:
    # Best epoch is the last one, so there is no post-best rise to warn about.
    verdict = M.convergence_verdict(
        [100.0, 90.0, 80.0],
        [100.0, 90.0, 80.0],
        max_epochs=400,
        early_stopping=True,
    )
    assert verdict["overfitting_warning"] is None
    assert verdict["best_val_epoch"] == 2


def test_a_negligible_validation_rise_stays_below_the_warning_threshold() -> None:
    verdict = M.convergence_verdict(
        [100.0, 90.0, 80.0, 79.0],
        [100.0, 90.0, 80.0, 80.8],
        max_epochs=400,
        early_stopping=True,
    )
    assert verdict["overfitting_warning"] is None


def test_missing_history_does_not_invent_epoch_counts() -> None:
    verdict = M.convergence_verdict([], [], max_epochs=400, early_stopping=True)
    assert verdict["epochs_trained"] is None
    assert verdict["early_stopped"] is False
    assert verdict["hit_cap"] is False
    assert verdict["final_elbo_train"] is None
    assert verdict["tail_relative_improvement"] is None


def test_narrative_states_cell_count_cap_and_ending() -> None:
    verdict = M.convergence_verdict(
        [100.0, 90.0, 85.0, 84.0],
        [101.0, 92.0, 88.0, 87.0],
        max_epochs=40,
        early_stopping=True,
    )
    narrative = M.epoch_narrative(200_000, verdict, "scvi_cell_count_heuristic")
    assert "200,000 cells" in narrative
    assert "40 epochs" in narrative
    assert "small-dataset ceiling" in narrative
    assert "early stopping halted training at 4 epoch(s)" in narrative
    explicit = M.epoch_narrative(200_000, verdict, "explicit")
    assert "explicitly requested cap of 40" in explicit
    assert "heuristic" not in explicit


def test_disabled_early_stopping_is_not_described_as_non_convergence() -> None:
    verdict = M.convergence_verdict(
        [100.0, 90.0],
        [],
        max_epochs=2,
        early_stopping=False,
    )
    narrative = M.epoch_narrative(1_000, verdict, "explicit")
    assert "early stopping was disabled" in narrative
    assert "consider a higher cap" not in narrative
