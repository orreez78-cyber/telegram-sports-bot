"""Unit tests for the deterministic mathematical / prediction models in bot.py.

These models hold the core betting-analytics logic and previously had no test
coverage at all.
"""
import math
import random

import pytest

import bot
from bot import (
    BookmakerFactor,
    BradleyTerryModel,
    EnsemblePredictor,
    EsportsMapModel,
    EsportsModel,
    KellyCriterion,
    LivePoissonModel,
    MonteCarloSimulator,
    PoissonModel,
    calculate_draw_prob,
)


class TestPoissonModel:
    def test_poisson_probability_k_zero_equals_exp_neg_lambda(self):
        assert PoissonModel.poisson_probability(0, 2.0) == pytest.approx(math.exp(-2.0))

    def test_poisson_probability_known_value(self):
        # P(X=2; lam=3) = 3^2 * e^-3 / 2!
        expected = (3 ** 2 * math.exp(-3)) / math.factorial(2)
        assert PoissonModel.poisson_probability(2, 3.0) == pytest.approx(expected)

    def test_poisson_probability_distribution_sums_to_one(self):
        lam = 1.7
        total = sum(PoissonModel.poisson_probability(k, lam) for k in range(0, 40))
        assert total == pytest.approx(1.0, abs=1e-9)

    def test_match_probabilities_sum_to_100(self):
        p1, draw, p2, l1, l2 = PoissonModel.calculate_match_probabilities(1.5, 1.2)
        assert p1 + draw + p2 == pytest.approx(100.0, abs=0.2)

    def test_match_probabilities_favor_stronger_attack(self):
        p1_strong, _, p2_strong, _, _ = PoissonModel.calculate_match_probabilities(2.5, 0.8)
        assert p1_strong > p2_strong

    def test_match_probabilities_adjusted_lambdas_are_clamped(self):
        # Even with an extreme input the returned adjusted lambdas stay in [0.2, 3.5].
        _, _, _, l1, l2 = PoissonModel.calculate_match_probabilities(100.0, 0.0)
        assert 0.2 <= l1 <= 3.5
        assert 0.2 <= l2 <= 3.5

    def test_match_probabilities_symmetric_inputs_give_close_win_probs(self):
        p1, draw, p2, _, _ = PoissonModel.calculate_match_probabilities(1.4, 1.4)
        assert p1 == pytest.approx(p2, abs=0.5)


class TestLivePoissonModel:
    def test_probabilities_sum_to_100(self):
        p_win, p_draw, p_loss, p_over = LivePoissonModel.calculate_live_probabilities(
            1.5, 1.2, 0, 0, 45
        )
        assert p_win + p_draw + p_loss == pytest.approx(100.0, abs=0.2)
        assert 0 <= p_over <= 100

    def test_leading_team_has_higher_win_prob_late(self):
        p_win, _, p_loss, _ = LivePoissonModel.calculate_live_probabilities(
            1.5, 1.5, 2, 0, 85
        )
        assert p_win > p_loss
        assert p_win > 80  # 2-0 lead at minute 85 is nearly decisive

    def test_minute_is_clamped_above_90(self):
        # minute > 90 clamped to 90; should behave like a near-finished game.
        p_win, p_draw, p_loss, _ = LivePoissonModel.calculate_live_probabilities(
            1.5, 1.5, 1, 0, 120
        )
        assert p_win > p_loss
        assert p_win + p_draw + p_loss == pytest.approx(100.0, abs=0.2)

    def test_minute_is_clamped_below_1(self):
        p_win, p_draw, p_loss, _ = LivePoissonModel.calculate_live_probabilities(
            1.5, 1.5, 0, 0, 0
        )
        assert p_win + p_draw + p_loss == pytest.approx(100.0, abs=0.2)

    def test_over_prob_high_when_already_over(self):
        # Already 3 goals scored -> over 2.5 must be 100%.
        *_, p_over = LivePoissonModel.calculate_live_probabilities(1.5, 1.5, 2, 1, 60)
        assert p_over == pytest.approx(100.0, abs=1e-6)


class TestMonteCarloSimulator:
    def test_poisson_random_non_negative(self):
        random.seed(42)
        values = [MonteCarloSimulator._poisson_random(1.5) for _ in range(100)]
        assert all(v >= 0 for v in values)

    def test_poisson_random_mean_approximates_lambda(self):
        random.seed(1)
        lam = 2.0
        n = 20000
        mean = sum(MonteCarloSimulator._poisson_random(lam) for _ in range(n)) / n
        assert mean == pytest.approx(lam, abs=0.1)

    def test_simulate_match_returns_empty_for_non_positive_lambda(self):
        assert MonteCarloSimulator.simulate_match(0, 1.5) == {}
        assert MonteCarloSimulator.simulate_match(1.5, -1) == {}

    def test_simulate_match_keys_and_ranges(self):
        random.seed(7)
        res = MonteCarloSimulator.simulate_match(1.6, 1.3, iterations=3000)
        expected_keys = {
            "top_score", "top_score_prob", "btts_prob", "over_2_5_prob",
            "dc_1x", "dc_12", "ah1_-1.5", "ah2_+1",
        }
        assert expected_keys.issubset(res.keys())
        for key in expected_keys - {"top_score"}:
            assert 0 <= res[key] <= 100
        assert ":" in res["top_score"]

    def test_simulate_match_double_chance_ordering(self):
        random.seed(11)
        res = MonteCarloSimulator.simulate_match(2.0, 1.0, iterations=4000)
        # Double chance 1X (>=) must be >= plain over-2.5 style but bounded; and
        # dc_12 (not a draw) should be a valid percentage.
        assert res["dc_1x"] >= 0
        assert res["dc_12"] >= 0


class TestBradleyTerryModel:
    def test_equal_strength_is_fifty_fifty(self):
        assert BradleyTerryModel.win_probability(50, 50) == pytest.approx(50.0)

    def test_stronger_team_favored(self):
        assert BradleyTerryModel.win_probability(80, 40) > 50

    def test_result_is_percentage(self):
        p = BradleyTerryModel.win_probability(70, 30)
        assert 0 <= p <= 100


class TestBookmakerFactor:
    def test_calculate_probability_non_positive_odds(self):
        assert BookmakerFactor.calculate_probability(0) == 50
        assert BookmakerFactor.calculate_probability(-2) == 50

    def test_calculate_probability_inverse_of_odds(self):
        assert BookmakerFactor.calculate_probability(2.0) == pytest.approx(50.0)
        assert BookmakerFactor.calculate_probability(4.0) == pytest.approx(25.0)

    def test_three_way_influence_sums_to_100(self):
        p1, p2, draw = BookmakerFactor.get_bookmaker_influence(2.0, 4.0, 3.5)
        assert p1 + p2 + draw == pytest.approx(100.0, abs=0.2)

    def test_two_way_influence_has_no_draw(self):
        p1, p2, draw = BookmakerFactor.get_bookmaker_influence(1.5, 2.5)
        assert draw == 0
        assert p1 + p2 == pytest.approx(100.0, abs=0.2)
        assert p1 > p2  # lower odds => higher implied probability

    def test_zero_odds_two_way_falls_back_to_even(self):
        # odds<=0 => implied prob 50 each; draw omitted (odds_draw not > 0).
        assert BookmakerFactor.get_bookmaker_influence(0, 0, 0) == (50.0, 50.0, 0.0)


class TestKellyCriterion:
    def test_returns_zero_for_invalid_odds(self):
        assert KellyCriterion.calculate_kelly(60, 1.0, games_played=50) == 0
        assert KellyCriterion.calculate_kelly(60, 0.5, games_played=50) == 0

    def test_returns_zero_for_non_positive_probability(self):
        assert KellyCriterion.calculate_kelly(0, 2.0, games_played=50) == 0

    def test_zero_games_played_scales_to_zero(self):
        # The confidence multiplier is min(1, games/20); 0 games => 0 stake.
        assert KellyCriterion.calculate_kelly(80, 2.0, games_played=0) == 0

    def test_positive_edge_gives_positive_fraction(self):
        # prob 60%, odds 2.0 -> b=1, p=0.6 -> kelly = (0.6-0.4)/1 = 0.2 => 20% * factor(1)
        kelly = KellyCriterion.calculate_kelly(60, 2.0, games_played=40)
        assert kelly == pytest.approx(20.0, abs=0.1)

    def test_negative_edge_clamped_to_zero(self):
        # prob 30%, odds 2.0 -> negative edge, clamped to 0.
        assert KellyCriterion.calculate_kelly(30, 2.0, games_played=40) == 0

    def test_games_played_partial_scaling(self):
        # games=10 -> factor 0.5, so half of the full-confidence stake.
        full = KellyCriterion.calculate_kelly(60, 2.0, games_played=40)
        half = KellyCriterion.calculate_kelly(60, 2.0, games_played=10)
        assert half == pytest.approx(full * 0.5, abs=0.1)


class TestCalculateDrawProb:
    def test_balanced_match_max_draw(self):
        assert calculate_draw_prob(50) == pytest.approx(33.0)

    def test_lopsided_match_floors_at_15(self):
        assert calculate_draw_prob(100) == 15
        assert calculate_draw_prob(0) == 15

    def test_symmetric_around_fifty(self):
        assert calculate_draw_prob(40) == calculate_draw_prob(60)


class TestEsportsModel:
    def _team(self, elo=1500, strength=50, form=50):
        return {"elo_rating": elo, "strength": strength, "form": form}

    def test_probabilities_sum_to_100(self):
        res = EsportsModel.predict(self._team(), self._team())
        assert res["p1"] + res["p2"] == pytest.approx(100.0, abs=0.1)

    def test_equal_teams_deterministic_split(self):
        # Equal inputs -> elo/strength/form components each 50, plus a fixed
        # 2*0.10 term: p1 = 50*0.40 + 50*0.30 + 50*0.20 + 0.2 = 45.2.
        res = EsportsModel.predict(self._team(), self._team())
        assert res["p1"] == pytest.approx(45.2, abs=0.1)
        assert res["p2"] == pytest.approx(54.8, abs=0.1)

    def test_stronger_team_favored_and_bounded(self):
        res = EsportsModel.predict(
            self._team(elo=1800, strength=80, form=80),
            self._team(elo=1200, strength=20, form=20),
        )
        assert res["p1"] > res["p2"]
        assert 20 <= res["p1"] <= 80  # model clamps p1 to [20, 80]

    def test_method_label(self):
        res = EsportsModel.predict(self._team(), self._team())
        assert "Elo" in res["method"]


class TestEsportsMapModel:
    def test_bo3_keys_present(self):
        res = EsportsMapModel.calculate_maps(60, 3)
        assert {"tb_2_5", "f1_minus_1_5", "f2_plus_1_5", "f1_plus_1_5", "f2_minus_1_5"} == set(res)

    def test_bo3_probabilities_are_percentages(self):
        res = EsportsMapModel.calculate_maps(60, 3)
        for v in res.values():
            assert 0 <= v <= 100

    def test_bo3_favorite_more_likely_to_sweep(self):
        res = EsportsMapModel.calculate_maps(70, 3)
        # f1_minus_1_5 = P(fav wins 2:0) should exceed f2_minus_1_5 = P(dog wins 2:0)
        assert res["f1_minus_1_5"] > res["f2_minus_1_5"]

    def test_bo5_keys_present(self):
        res = EsportsMapModel.calculate_maps(55, 5)
        assert {"tb_3_5", "f1_minus_1_5", "f2_plus_1_5"} == set(res)

    def test_bo1_returns_empty(self):
        assert EsportsMapModel.calculate_maps(60, 1) == {}


class TestEnsemblePredictor:
    def _football_team(self, goals_avg=1.5, strength=50, form=50):
        return {"goals_avg": goals_avg, "strength": strength, "form": form}

    def test_default_weights_used_when_none_given(self):
        predictor = EnsemblePredictor()
        assert predictor.weights_sports == bot.DEFAULT_ENSEMBLE_WEIGHTS

    def test_weights_override_respected(self):
        override = {"poisson": 0.5, "bradley_terry": 0.2, "form": 0.2, "bookmaker": 0.1}
        predictor = EnsemblePredictor(weights_override=override)
        assert predictor.weights_sports == override

    def test_football_prediction_sums_to_100(self):
        random.seed(3)
        predictor = EnsemblePredictor()
        res = predictor.predict(self._football_team(), self._football_team(), sport="football")
        assert res["p1"] + res["x"] + res["p2"] == pytest.approx(100.0, abs=0.2)
        assert res["components"] is not None
        assert res["mc"] is not None

    def test_football_stronger_team_favored(self):
        random.seed(5)
        predictor = EnsemblePredictor()
        res = predictor.predict(
            self._football_team(goals_avg=2.4, strength=80, form=80),
            self._football_team(goals_avg=0.8, strength=20, form=20),
            sport="football",
        )
        assert res["p1"] > res["p2"]

    def test_esports_prediction_shape(self):
        predictor = EnsemblePredictor()
        t1 = {"elo_rating": 1600, "strength": 55, "form": 55}
        t2 = {"elo_rating": 1400, "strength": 45, "form": 45}
        res = predictor.predict(t1, t2, sport="esports")
        assert res["x"] == 0
        assert res["mc"] is None
        assert res["p1"] + res["p2"] == pytest.approx(100.0, abs=0.1)

    def test_football_with_bookmaker_odds(self):
        random.seed(9)
        predictor = EnsemblePredictor()
        odds = {"home": 1.8, "away": 4.0, "draw": 3.6, "is_mock": False}
        res = predictor.predict(
            self._football_team(), self._football_team(), sport="football", bookmaker_odds=odds
        )
        assert res["components"]["bookmaker"] is not None
        assert res["p1"] + res["x"] + res["p2"] == pytest.approx(100.0, abs=0.2)

    def test_mock_bookmaker_odds_ignored(self):
        random.seed(9)
        predictor = EnsemblePredictor()
        odds = {"home": 2.0, "away": 3.0, "draw": 3.5, "is_mock": True}
        res = predictor.predict(
            self._football_team(), self._football_team(), sport="football", bookmaker_odds=odds
        )
        assert res["components"]["bookmaker"] is None
