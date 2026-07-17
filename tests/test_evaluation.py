import math

import pytest

import evaluation as ev


def test_normalize_from_percentages_and_fractions():
    assert ev.normalize((50, 30, 20)) == pytest.approx((0.5, 0.3, 0.2))
    assert ev.normalize((0.5, 0.3, 0.2)) == pytest.approx((0.5, 0.3, 0.2))


def test_normalize_rejects_bad_input():
    with pytest.raises(ValueError):
        ev.normalize((1, 2))
    with pytest.raises(ValueError):
        ev.normalize((0, 0, 0))


def test_outcome_from_score():
    assert ev.outcome_from_score(2, 1) == "home"
    assert ev.outcome_from_score(0, 3) == "away"
    assert ev.outcome_from_score(1, 1) == "draw"


def test_brier_perfect_and_worst():
    assert ev.brier_score((1.0, 0.0, 0.0), "home") == pytest.approx(0.0)
    assert ev.brier_score((0.0, 0.0, 1.0), "home") == pytest.approx(2.0)


def test_brier_known_value():
    # (0.5, 0.3, 0.2), outcome home -> (0.5-1)^2 + 0.3^2 + 0.2^2 = 0.38
    assert ev.brier_score((0.5, 0.3, 0.2), "home") == pytest.approx(0.38)


def test_log_loss_matches_manual():
    assert ev.log_loss((0.5, 0.3, 0.2), "away") == pytest.approx(-math.log(0.2))


def test_log_loss_is_finite_for_zero_prob():
    assert math.isfinite(ev.log_loss((1.0, 0.0, 0.0), "draw"))


def test_implied_probabilities_removes_vig():
    p = ev.implied_probabilities(2.0, 4.0, 4.0)
    assert sum(p) == pytest.approx(1.0)
    assert p[0] == pytest.approx(0.5)


def test_expected_value_sign():
    assert ev.expected_value(0.6, 2.0) == pytest.approx(0.2)
    assert ev.expected_value(0.4, 2.0) == pytest.approx(-0.2)


def test_kelly_fraction():
    # p=0.6, odds=2.0 -> b=1, f=(1*0.6-0.4)/1 = 0.2
    assert ev.kelly_fraction(0.6, 2.0) == pytest.approx(0.2)
    # no edge -> no stake
    assert ev.kelly_fraction(0.4, 2.0) == 0.0
    assert ev.kelly_fraction(0.9, 1.0) == 0.0


def test_calibration_curve_bins():
    samples = [(0.35, False), (0.35, True), (0.75, True), (0.75, True)]
    curve = ev.calibration_curve(samples, n_bins=10)
    by_lo = {round(b["lo"], 1): b for b in curve}
    assert by_lo[0.3]["n"] == 2
    assert by_lo[0.3]["empirical"] == pytest.approx(0.5)
    assert by_lo[0.7]["empirical"] == pytest.approx(1.0)


def test_expected_calibration_error_zero_when_perfect():
    # predicted prob equals empirical rate in each bin -> ECE 0
    samples = [(0.5, True), (0.5, False)]
    assert ev.expected_calibration_error(samples, n_bins=2) == pytest.approx(0.0)


def test_select_value_bet_picks_positive_ev():
    # model thinks home is 60% but book prices it at 2.0 (implied 50%) -> +EV
    probs = (60, 25, 15)
    odds = {"home": 2.0, "draw": 3.5, "away": 6.0}
    vb = ev.select_value_bet(probs, odds, min_ev=0.0, kelly_multiplier=0.25, kelly_cap=0.05)
    assert vb is not None
    assert vb["outcome"] == "home"
    assert vb["ev"] == pytest.approx(0.6 * 2.0 - 1)
    assert 0 < vb["stake_fraction"] <= 0.05


def test_select_value_bet_none_when_no_edge():
    # fair/margined book, model matches implied probs -> no +EV bet
    probs = (50, 30, 20)
    odds = {"home": 1.9, "draw": 3.0, "away": 4.5}
    assert ev.select_value_bet(probs, odds, min_ev=0.03) is None


def test_select_value_bet_respects_min_ev_threshold():
    probs = (52, 28, 20)
    odds = {"home": 2.0, "draw": 3.5, "away": 5.0}  # home EV = 0.04
    assert ev.select_value_bet(probs, odds, min_ev=0.10) is None
    assert ev.select_value_bet(probs, odds, min_ev=0.01) is not None


def test_select_value_bet_stake_capped():
    probs = (90, 6, 4)
    odds = {"home": 3.0, "draw": 5.0, "away": 8.0}  # huge edge -> full Kelly large
    vb = ev.select_value_bet(probs, odds, kelly_multiplier=1.0, kelly_cap=0.05)
    assert vb["stake_fraction"] == pytest.approx(0.05)  # capped


def test_select_value_bet_ignores_missing_odds():
    probs = (60, 25, 15)
    odds = {"home": 0, "draw": 0, "away": 0}
    assert ev.select_value_bet(probs, odds) is None


def test_roi_summary():
    bets = [(1.0, 2.0, True), (1.0, 3.0, False), (1.0, 1.5, True)]
    r = ev.roi_summary(bets)
    assert r["n"] == 3
    assert r["staked"] == pytest.approx(3.0)
    assert r["returned"] == pytest.approx(3.5)
    assert r["profit"] == pytest.approx(0.5)
    assert r["roi"] == pytest.approx(0.5 / 3.0)
    assert r["hit_rate"] == pytest.approx(2 / 3)


def test_roi_summary_empty():
    r = ev.roi_summary([])
    assert r["n"] == 0 and r["roi"] == 0.0 and r["hit_rate"] == 0.0
