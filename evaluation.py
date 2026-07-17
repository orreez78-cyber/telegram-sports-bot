"""Probabilistic-forecast evaluation and value-betting metrics.

Pure, dependency-free functions used by the backtester (``backtest.py``) and by
tests to quantify how good the model's probabilities actually are. Nothing here
touches the network or the database.

Conventions
-----------
* ``probs`` is a 3-tuple ``(p_home, p_draw, p_away)`` of probabilities. They are
  normalized internally, so they may be given either as fractions summing to 1
  or as percentages summing to 100.
* ``outcome`` is one of ``OUTCOMES = ("home", "draw", "away")``.
* Odds are decimal (European) odds, e.g. ``2.50``.
"""
from __future__ import annotations

import math
from typing import Iterable, Sequence

OUTCOMES = ("home", "draw", "away")
_EPS = 1e-12


def normalize(probs: Sequence[float]) -> tuple[float, float, float]:
    """Return probs as fractions summing to 1. Raises on non-positive totals."""
    if len(probs) != 3:
        raise ValueError("probs must have exactly 3 elements (home, draw, away)")
    total = sum(probs)
    if total <= 0:
        raise ValueError("probabilities must sum to a positive value")
    return tuple(p / total for p in probs)  # type: ignore[return-value]


def _outcome_index(outcome: str) -> int:
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of {OUTCOMES}, got {outcome!r}")
    return OUTCOMES.index(outcome)


def outcome_from_score(home_goals: int, away_goals: int) -> str:
    if home_goals > away_goals:
        return "home"
    if home_goals < away_goals:
        return "away"
    return "draw"


def brier_score(probs: Sequence[float], outcome: str) -> float:
    """Multiclass Brier score (0 = perfect, 2 = worst). Lower is better."""
    p = normalize(probs)
    idx = _outcome_index(outcome)
    return sum((p[i] - (1.0 if i == idx else 0.0)) ** 2 for i in range(3))


def log_loss(probs: Sequence[float], outcome: str) -> float:
    """Negative log-likelihood of the realized outcome. Lower is better."""
    p = normalize(probs)
    idx = _outcome_index(outcome)
    return -math.log(max(_EPS, p[idx]))


def implied_probabilities(odds_home: float, odds_draw: float, odds_away: float) -> tuple[float, float, float]:
    """Convert decimal odds to vig-free (normalized) implied probabilities."""
    raw = [1.0 / o if o and o > 0 else 0.0 for o in (odds_home, odds_draw, odds_away)]
    return normalize(raw)


def expected_value(prob: float, decimal_odds: float) -> float:
    """EV per unit staked at a bookmaker: prob * odds - 1 (>0 means +EV)."""
    return prob * decimal_odds - 1.0


def kelly_fraction(prob: float, decimal_odds: float) -> float:
    """Full-Kelly stake fraction; 0 when there is no positive edge."""
    b = decimal_odds - 1.0
    if b <= 0 or prob <= 0:
        return 0.0
    f = (b * prob - (1.0 - prob)) / b
    return max(0.0, f)


def calibration_curve(samples: Iterable[tuple[float, bool]], n_bins: int = 10):
    """Bin (predicted_probability, was_correct) pairs into a reliability curve.

    Returns a list of dicts per non-empty bin: ``{lo, hi, n, avg_predicted,
    empirical}``. A well-calibrated model has ``avg_predicted ≈ empirical``.
    """
    if n_bins < 1:
        raise ValueError("n_bins must be >= 1")
    buckets: list[list[tuple[float, bool]]] = [[] for _ in range(n_bins)]
    for prob, hit in samples:
        b = min(n_bins - 1, max(0, int(prob * n_bins)))
        buckets[b].append((prob, bool(hit)))
    curve = []
    for b, items in enumerate(buckets):
        if not items:
            continue
        curve.append({
            "lo": b / n_bins,
            "hi": (b + 1) / n_bins,
            "n": len(items),
            "avg_predicted": sum(p for p, _ in items) / len(items),
            "empirical": sum(1 for _, h in items if h) / len(items),
        })
    return curve


def expected_calibration_error(samples: Iterable[tuple[float, bool]], n_bins: int = 10) -> float:
    """Weighted mean gap between predicted confidence and empirical accuracy."""
    samples = list(samples)
    if not samples:
        return 0.0
    curve = calibration_curve(samples, n_bins)
    n = len(samples)
    return sum(bin_["n"] / n * abs(bin_["avg_predicted"] - bin_["empirical"]) for bin_ in curve)


def roi_summary(bets: Iterable[tuple[float, float, bool]]) -> dict:
    """Summarize a set of settled bets.

    Each bet is ``(stake, decimal_odds, won)``. Returns counts, total staked,
    total returned, net profit, ROI (profit / staked) and hit-rate.
    """
    n = 0
    staked = 0.0
    returned = 0.0
    wins = 0
    for stake, odds, won in bets:
        n += 1
        staked += stake
        if won:
            returned += stake * odds
            wins += 1
    profit = returned - staked
    return {
        "n": n,
        "staked": staked,
        "returned": returned,
        "profit": profit,
        "roi": (profit / staked) if staked > 0 else 0.0,
        "hit_rate": (wins / n) if n > 0 else 0.0,
    }
