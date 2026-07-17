"""Walk-forward backtesting harness for the football prediction model.

Replays finished matches in chronological order: for each match the model
predicts the 1X2 outcome using only information available *before* kickoff, the
result is scored, and only then are the team ratings updated. This is the
honest way to estimate real-world accuracy/calibration (no look-ahead).

Reuses the exact production logic from ``bot.py`` (rating updates, feature
building, ensemble) so the measured numbers reflect what the bot would do.

Usage
-----
    python backtest.py                 # fetch OpenLigaDB bl1/bl2/bl3 and report
    python backtest.py --seasons 2022 2023
"""
from __future__ import annotations

import argparse
import json
import urllib.request
from typing import Iterable

import bot
import evaluation


def default_team_state() -> dict:
    return {
        "elo_rating": bot.DEFAULT_ELO,
        "goals_avg": bot.DEFAULT_GOALS_AVG,
        "form": bot.DEFAULT_FORM,
        "games_played": 0,
    }


class TeamRatings:
    """In-memory mirror of the trained team_ratings table."""

    def __init__(self) -> None:
        self.teams: dict[str, dict] = {}

    def get(self, name: str) -> dict:
        return self.teams.setdefault(name, default_team_state())

    def update(self, home: str, away: str, home_goals: int, away_goals: int) -> None:
        t1, t2 = self.get(home), self.get(away)
        expected1 = bot.elo_expected_score(t1["elo_rating"], t2["elo_rating"])
        actual1 = bot.match_outcome_score(home_goals, away_goals)
        t1["elo_rating"] = bot.updated_elo(t1["elo_rating"], actual1, expected1)
        t2["elo_rating"] = bot.updated_elo(t2["elo_rating"], 1 - actual1, 1 - expected1)
        t1["goals_avg"] = bot.updated_goals_avg(t1["goals_avg"], home_goals)
        t2["goals_avg"] = bot.updated_goals_avg(t2["goals_avg"], away_goals)
        t1["form"] = bot.updated_form(t1["form"], actual1)
        t2["form"] = bot.updated_form(t2["form"], 1 - actual1)
        t1["games_played"] += 1
        t2["games_played"] += 1


def predict_probs(ratings: TeamRatings, home: str, away: str,
                  weights: dict | None = None,
                  home_advantage: float = bot.HOME_ADVANTAGE) -> tuple[float, float, float]:
    """Return (p_home, p_draw, p_away) as fractions summing to 1."""
    f1, f2 = bot.build_match_features(ratings.get(home), ratings.get(away), home_advantage)
    predictor = bot.EnsemblePredictor(weights_override=weights)
    res = predictor.predict(f1, f2, sport="football", run_mc=False)
    return evaluation.normalize((res["p1"], res["x"], res["p2"]))


def run_backtest(matches: Iterable[dict], warmup_games: int = 5,
                 n_bins: int = 10, home_advantage: float = bot.HOME_ADVANTAGE) -> dict:
    """Walk-forward evaluation over chronologically ordered matches.

    Each match dict needs: team1, team2, home_goals, away_goals.
    Matches where either team has played < ``warmup_games`` are used only for
    training (skipped in the metrics) so cold-start noise doesn't dominate.
    """
    matches = list(matches)
    ratings = TeamRatings()
    log_losses: list[float] = []
    briers: list[float] = []
    correct = 0
    scored = 0
    conf_samples: list[tuple[float, bool]] = []
    outcome_counts = {"home": 0, "draw": 0, "away": 0}

    for m in matches:
        home, away = m["team1"], m["team2"]
        hg, ag = m["home_goals"], m["away_goals"]
        t1, t2 = ratings.get(home), ratings.get(away)
        warm = t1["games_played"] >= warmup_games and t2["games_played"] >= warmup_games

        if warm:
            probs = predict_probs(ratings, home, away, home_advantage=home_advantage)
            outcome = evaluation.outcome_from_score(hg, ag)
            log_losses.append(evaluation.log_loss(probs, outcome))
            briers.append(evaluation.brier_score(probs, outcome))
            pred_idx = max(range(3), key=lambda i: probs[i])
            hit = evaluation.OUTCOMES[pred_idx] == outcome
            correct += int(hit)
            scored += 1
            conf_samples.append((probs[pred_idx], hit))
            outcome_counts[outcome] += 1

        ratings.update(home, away, hg, ag)

    baseline = _base_rate_reference(outcome_counts, scored)
    return {
        "matches_total": len(matches),
        "matches_scored": scored,
        "accuracy": (correct / scored) if scored else 0.0,
        "log_loss": (sum(log_losses) / scored) if scored else 0.0,
        "brier": (sum(briers) / scored) if scored else 0.0,
        "ece": evaluation.expected_calibration_error(conf_samples, n_bins),
        "calibration": evaluation.calibration_curve(conf_samples, n_bins),
        "baseline_log_loss": baseline["log_loss"],
        "baseline_accuracy": baseline["accuracy"],
        "teams_tracked": len(ratings.teams),
    }


def _base_rate_reference(outcome_counts: dict, scored: int) -> dict:
    """Reference model that always predicts the dataset's outcome frequencies.

    A skillful model must beat this on log-loss and accuracy.
    """
    if scored == 0:
        return {"log_loss": 0.0, "accuracy": 0.0}
    rates = {k: v / scored for k, v in outcome_counts.items()}
    base_probs = (rates["home"], rates["draw"], rates["away"])
    ll = sum(
        outcome_counts[o] * evaluation.log_loss(base_probs, o) for o in evaluation.OUTCOMES
    ) / scored
    return {"log_loss": ll, "accuracy": max(rates.values())}


def fetch_openligadb(seasons: list[int]) -> list[dict]:
    """Fetch + parse finished matches for the configured leagues/seasons."""
    all_matches: list[dict] = []
    for league_code, _name in bot.OPENLIGADB_LEAGUES:
        for season in seasons:
            url = f"{bot.OPENLIGADB_BASE}/getmatchdata/{league_code}/{season}"
            try:
                with urllib.request.urlopen(url, timeout=30) as resp:
                    data = json.load(resp)
            except Exception as e:  # noqa: BLE001 - CLI convenience
                print(f"  ! failed {league_code} {season}: {e}")
                continue
            parsed = bot.parse_openligadb_matches(data)
            print(f"  {league_code} {season}: {len(parsed)} finished matches")
            all_matches.extend(parsed)
    all_matches.sort(key=lambda x: x["date"])
    return all_matches


def _print_report(result: dict) -> None:
    print("\n=== Walk-forward backtest ===")
    print(f"teams tracked      : {result['teams_tracked']}")
    print(f"matches scored     : {result['matches_scored']}")
    print(f"accuracy           : {result['accuracy']:.3f}  (base rate {result['baseline_accuracy']:.3f})")
    print(f"log-loss           : {result['log_loss']:.4f}  (base rate {result['baseline_log_loss']:.4f})")
    print(f"Brier score        : {result['brier']:.4f}")
    print(f"calibration error  : {result['ece']:.4f}  (0 = perfectly calibrated)")
    print("\nreliability curve (predicted confidence vs empirical accuracy):")
    for b in result["calibration"]:
        print(f"  [{b['lo']:.1f}-{b['hi']:.1f}] n={b['n']:4d}  predicted={b['avg_predicted']:.3f}  actual={b['empirical']:.3f}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest the football model on OpenLigaDB data.")
    parser.add_argument("--seasons", type=int, nargs="+", default=[2021, 2022, 2023],
                        help="Seasons (start year) to fetch and replay.")
    parser.add_argument("--warmup", type=int, default=5,
                        help="Minimum games per team before a match counts in the metrics.")
    args = parser.parse_args()

    print(f"Fetching OpenLigaDB seasons {args.seasons} ...")
    matches = fetch_openligadb(args.seasons)
    print(f"Total finished matches: {len(matches)}")
    result = run_backtest(matches, warmup_games=args.warmup)
    _print_report(result)


if __name__ == "__main__":
    main()
