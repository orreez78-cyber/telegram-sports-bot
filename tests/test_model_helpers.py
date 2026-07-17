import math

import pytest

import bot


# ---- Elo / rating helpers ----

def test_elo_expected_score_symmetry():
    assert bot.elo_expected_score(1500, 1500) == pytest.approx(0.5)
    e = bot.elo_expected_score(1700, 1500)
    assert e > 0.5
    # complementary
    assert e + bot.elo_expected_score(1500, 1700) == pytest.approx(1.0)


def test_updated_elo_moves_toward_result():
    # underdog wins -> elo increases
    exp = bot.elo_expected_score(1400, 1600)
    new = bot.updated_elo(1400, 1.0, exp, k=32)
    assert new > 1400
    assert new == pytest.approx(1400 + 32 * (1.0 - exp))


def test_match_outcome_score():
    assert bot.match_outcome_score(2, 0) == 1.0
    assert bot.match_outcome_score(1, 1) == 0.5
    assert bot.match_outcome_score(0, 3) == 0.0


def test_updated_goals_avg_caps_blowouts():
    prev = 1.5
    # a huge score is damped toward the mean, not taken at face value
    damped = bot.updated_goals_avg(prev, 6, weight=0.2)
    raw = prev * 0.8 + 6 * 0.2
    assert damped < raw
    # a low score is taken directly
    assert bot.updated_goals_avg(prev, 0, weight=0.2) == pytest.approx(prev * 0.8)


def test_updated_form_bounds():
    assert bot.updated_form(50, 1.0, weight=0.3) == pytest.approx(50 * 0.7 + 100 * 0.3)
    assert bot.updated_form(50, 0.0, weight=0.3) == pytest.approx(50 * 0.7)
    assert bot.updated_form(50, 0.5, weight=0.3) == pytest.approx(50 * 0.7 + 50 * 0.3)


# ---- Poisson / Dixon-Coles ----

def test_poisson_probabilities_sum_to_100():
    p1, x, p2, l1, l2 = bot.PoissonModel.calculate_match_probabilities(1.6, 1.1)
    assert p1 + x + p2 == pytest.approx(100.0, abs=0.2)
    assert p1 > p2  # stronger attack favored


def test_shrink_goals_clamped():
    assert bot.POISSON_MIN_LAMBDA <= bot.PoissonModel.shrink_goals(0.0) <= bot.POISSON_MAX_LAMBDA
    assert bot.PoissonModel.shrink_goals(99) == pytest.approx(bot.POISSON_MAX_LAMBDA)


def test_dixon_coles_tau_only_adjusts_low_scores():
    assert bot.PoissonModel.dixon_coles_tau(3, 2, 1.5, 1.2, -0.1) == 1.0
    assert bot.PoissonModel.dixon_coles_tau(0, 0, 1.5, 1.2, -0.1) != 1.0
    assert bot.PoissonModel.dixon_coles_tau(1, 1, 1.5, 1.2, -0.1) == pytest.approx(1.1)


def test_score_matrix_shape_and_positive():
    m = bot.PoissonModel.score_matrix(1.4, 1.1, rho=-0.1, grid=6)
    assert len(m) == 6 and all(len(r) == 6 for r in m)
    assert all(v > 0 for row in m for v in row)


# ---- Elo -> strength & feature building ----

def test_strength_from_elo_monotonic_and_clamped():
    assert bot.strength_from_elo(bot.DEFAULT_ELO) == pytest.approx(50.0)
    assert bot.strength_from_elo(bot.DEFAULT_ELO + 100) > 50
    assert 1.0 <= bot.strength_from_elo(9999) <= 99.0
    assert 1.0 <= bot.strength_from_elo(-9999) <= 99.0


def test_build_match_features_uses_home_advantage_and_elo():
    la = bot.POISSON_LEAGUE_AVG
    t1 = {"goals_avg": 1.5, "goals_conceded": 1.0, "form": 60, "elo_rating": 1600}
    t2 = {"goals_avg": 1.5, "goals_conceded": 1.2, "form": 40, "elo_rating": 1400}
    f1, f2 = bot.build_match_features(t1, t2, home_advantage=1.2)
    # expected goals = attack * opponent_defense / league_avg, home side * HA
    assert f1["goals_avg"] == pytest.approx(1.5 * 1.2 / la * 1.2)
    assert f2["goals_avg"] == pytest.approx(1.5 * 1.0 / la)
    assert f1["strength"] > f2["strength"]  # higher Elo -> higher strength


def test_expected_goals_attack_vs_defense():
    la = bot.POISSON_LEAGUE_AVG
    # weaker opponent defense (concedes more) -> more expected goals
    strong_def = bot.expected_goals(1.5, 0.8, league_avg=la)
    weak_def = bot.expected_goals(1.5, 2.0, league_avg=la)
    assert weak_def > strong_def
    assert bot.expected_goals(1.5, la, league_avg=la) == pytest.approx(1.5)
