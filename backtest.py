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
        "goals_conceded": bot.DEFAULT_GOALS_AVG,
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
        t1["goals_conceded"] = bot.updated_goals_avg(t1["goals_conceded"], away_goals)
        t2["goals_conceded"] = bot.updated_goals_avg(t2["goals_conceded"], home_goals)
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


def run_value_bet_backtest(matches: Iterable[dict], warmup_games: int = 5,
                           min_ev: float = bot.VALUE_BET_MIN_EV,
                           kelly_fraction: float = bot.VALUE_BET_KELLY_FRACTION,
                           kelly_cap: float = bot.VALUE_BET_KELLY_CAP,
                           home_advantage: float = bot.HOME_ADVANTAGE) -> dict:
    """Walk-forward value-betting simulation. Requires odds on each match.

    Each match dict must additionally carry ``odds`` = {home, draw, away}
    (decimal). For every warmed-up match the model prices the game, and if a
    positive-EV selection clears ``min_ev`` a fractional-Kelly stake is placed
    on a 1-unit bankroll basis and settled against the real result. Reports
    ROI, hit rate, average EV and worst drawdown - the only honest read on
    whether the model beats the offered prices.
    """
    matches = list(matches)
    ratings = TeamRatings()
    settled: list[tuple[float, float, bool]] = []  # (stake, odds, won)
    evs: list[float] = []
    bankroll, peak, max_drawdown = 1.0, 1.0, 0.0

    for m in matches:
        home, away = m["team1"], m["team2"]
        hg, ag = m["home_goals"], m["away_goals"]
        t1, t2 = ratings.get(home), ratings.get(away)
        warm = t1["games_played"] >= warmup_games and t2["games_played"] >= warmup_games
        odds = m.get("odds")

        if warm and odds:
            probs = predict_probs(ratings, home, away, home_advantage=home_advantage)
            pct = tuple(p * 100 for p in probs)
            vb = evaluation.select_value_bet(pct, odds, min_ev=min_ev,
                                             kelly_multiplier=kelly_fraction, kelly_cap=kelly_cap)
            if vb:
                actual = evaluation.outcome_from_score(hg, ag)
                won = vb["outcome"] == actual
                stake = vb["stake_fraction"] * bankroll
                settled.append((stake, vb["odds"], won))
                evs.append(vb["ev"])
                bankroll += stake * (vb["odds"] - 1) if won else -stake
                peak = max(peak, bankroll)
                max_drawdown = max(max_drawdown, (peak - bankroll) / peak)

        ratings.update(home, away, hg, ag)

    summary = evaluation.roi_summary(settled)
    summary.update({
        "final_bankroll": round(bankroll, 4),
        "max_drawdown": round(max_drawdown, 4),
        "avg_ev": round(sum(evs) / len(evs), 4) if evs else 0.0,
    })
    return summary


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


def attach_odds_from_file(matches: list[dict], path: str) -> int:
    """Merge historical odds into matches from a JSON file.

    The file is a list of {team1, team2, date?, odds:{home,draw,away}} records.
    Matches are keyed by fuzzy team-name pairing so provider naming differences
    don't lose records. Returns how many matches got odds attached.

    TODO(you): to auto-pull historical odds instead of a file, use The Odds
    API historical endpoint (paid plan required):
    https://api.the-odds-api.com/v4/historical/sports/{sport}/odds?date=...&apiKey=...
    Write the results to a JSON file in the shape above, or extend this loader.
    """
    with open(path) as fh:
        records = json.load(fh)
    attached = 0
    for m in matches:
        for rec in records:
            if rec.get("odds") and bot.teams_match(rec.get("team1", ""), rec.get("team2", ""), m["team1"], m["team2"]):
                m["odds"] = rec["odds"]
                attached += 1
                break
    return attached


def _print_value_report(roi: dict) -> None:
    print("\n=== Value-betting simulation (needs odds) ===")
    if roi["n"] == 0:
        print("  no bets placed (no +EV opportunities, or no odds attached)")
        return
    print(f"bets placed        : {roi['n']}")
    print(f"hit rate           : {roi['hit_rate']:.3f}")
    print(f"avg model EV        : {roi['avg_ev']:+.3f}")
    print(f"ROI                : {roi['roi']:+.3f}  (profit / staked)")
    print(f"final bankroll     : {roi['final_bankroll']:.3f}  (started at 1.0)")
    print(f"max drawdown       : {roi['max_drawdown']:.3f}")


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
    parser.add_argument("--odds-file", type=str, default=None,
                        help="JSON file of historical odds to run the value-betting ROI simulation.")
    args = parser.parse_args()

    print(f"Fetching OpenLigaDB seasons {args.seasons} ...")
    matches = fetch_openligadb(args.seasons)
    print(f"Total finished matches: {len(matches)}")
    result = run_backtest(matches, warmup_games=args.warmup)
    _print_report(result)

    if args.odds_file:
        attached = attach_odds_from_file(matches, args.odds_file)
        print(f"\nAttached odds to {attached} matches from {args.odds_file}")
        roi = run_value_bet_backtest(matches, warmup_games=args.warmup)
        _print_value_report(roi)
    else:
        print("\n(no --odds-file: skipping value-betting ROI. OpenLigaDB has no odds;")
        print(" supply historical odds to measure real ROI. See attach_odds_from_file.)")


if __name__ == "__main__":
    main()
