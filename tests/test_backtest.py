import pytest

import bot
import backtest


def _m(t1, t2, hg, ag, date, odds=None):
    d = {"team1": t1, "team2": t2, "home_goals": hg, "away_goals": ag, "date": date}
    if odds:
        d["odds"] = odds
    return d


def test_team_ratings_update_changes_state():
    r = backtest.TeamRatings()
    before = dict(r.get("A"))
    r.update("A", "B", 3, 0)
    after = r.get("A")
    assert after["elo_rating"] > before["elo_rating"]  # winner gains Elo
    assert after["games_played"] == 1
    assert r.get("B")["elo_rating"] < before["elo_rating"]


def test_predict_probs_normalized():
    r = backtest.TeamRatings()
    p = backtest.predict_probs(r, "A", "B")
    assert sum(p) == pytest.approx(1.0)
    assert all(0 <= x <= 1 for x in p)


def test_run_backtest_no_lookahead_and_metrics_present():
    # Deterministic: A always beats B; after warmup A should be favored.
    matches = [_m("A", "B", 2, 0, f"2023-01-{i:02d}") for i in range(1, 21)]
    result = backtest.run_backtest(matches, warmup_games=3, n_bins=5)
    assert result["matches_total"] == 20
    assert result["matches_scored"] == 20 - 3  # first 3 used for warmup only
    assert result["accuracy"] == pytest.approx(1.0)  # A always wins and is favored
    assert result["log_loss"] >= 0
    assert 0 <= result["ece"] <= 1


def test_run_backtest_warmup_skips_cold_start():
    matches = [_m("A", "B", 1, 0, f"2023-02-{i:02d}") for i in range(1, 11)]
    high_warmup = backtest.run_backtest(matches, warmup_games=100)
    assert high_warmup["matches_scored"] == 0
    assert high_warmup["accuracy"] == 0.0


def test_value_bet_backtest_no_odds_places_no_bets():
    matches = [_m("A", "B", 2, 0, f"2023-01-{i:02d}") for i in range(1, 21)]
    roi = backtest.run_value_bet_backtest(matches, warmup_games=3)
    assert roi["n"] == 0
    assert roi["final_bankroll"] == pytest.approx(1.0)


def test_value_bet_backtest_places_bets_when_odds_present():
    # A always beats B; generous home odds -> model finds +EV and profits.
    odds = {"home": 3.0, "draw": 4.0, "away": 4.0}
    matches = [_m("A", "B", 2, 0, f"2023-01-{i:02d}", odds=odds) for i in range(1, 31)]
    roi = backtest.run_value_bet_backtest(matches, warmup_games=3)
    assert roi["n"] > 0
    assert 0 <= roi["max_drawdown"] <= 1
    assert roi["hit_rate"] == pytest.approx(1.0)  # A always wins here
    assert roi["final_bankroll"] > 1.0


def test_attach_odds_from_file(tmp_path):
    import json
    records = [{"team1": "FC Bayern München", "team2": "Dortmund",
                "odds": {"home": 1.8, "draw": 3.6, "away": 4.2}}]
    p = tmp_path / "odds.json"
    p.write_text(json.dumps(records))
    matches = [_m("Bayern Munich", "Borussia Dortmund", 2, 1, "2023-01-01")]
    n = backtest.attach_odds_from_file(matches, str(p))
    assert n == 1
    assert matches[0]["odds"]["home"] == 1.8


def test_backtester_uses_same_helpers_as_production():
    # The in-memory update must mirror bot.updated_elo / match_outcome_score.
    r = backtest.TeamRatings()
    t1, t2 = r.get("A"), r.get("B")
    exp1 = bot.elo_expected_score(t1["elo_rating"], t2["elo_rating"])
    expected_new = bot.updated_elo(t1["elo_rating"], 1.0, exp1)
    r.update("A", "B", 1, 0)
    assert r.get("A")["elo_rating"] == pytest.approx(expected_new)
