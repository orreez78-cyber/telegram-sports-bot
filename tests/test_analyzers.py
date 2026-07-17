"""Unit tests for the match-analysis functions in bot.py.

These orchestrate the models + data layer into user-facing text. Network calls
(bookmaker odds, OpenAI) are monkeypatched; the SQLite layer uses the `db` fixture.
"""
import random

import pytest

import bot
from bot import (
    analyze_live_esports_match,
    analyze_live_football_match,
    analyze_match,
)

pytestmark = pytest.mark.asyncio


class TestAnalyzeLiveFootball:
    async def test_returns_live_text_with_scoreline(self, db):
        match = {
            "team1": "Arsenal", "team2": "Chelsea", "score1": 1, "score2": 0,
            "minute": 55, "tournament": "EPL",
        }
        text = await analyze_live_football_match(match)
        assert "Arsenal" in text and "Chelsea" in text
        assert "1:0" in text
        assert "LIVE" in text

    async def test_late_draw_situation_message(self, db):
        match = {
            "team1": "A", "team2": "B", "score1": 1, "score2": 1,
            "minute": 80, "tournament": "EPL",
        }
        text = await analyze_live_football_match(match)
        assert "ТМ" in text  # late-draw advice


class TestAnalyzeLiveEsports:
    async def test_returns_text_with_probabilities(self, db):
        match = {
            "team1": "NAVI", "team2": "G2", "score1": 1, "score2": 0,
            "game": "CS2", "tournament": "Major",
        }
        text = await analyze_live_esports_match(match)
        assert "NAVI" in text and "G2" in text
        assert "LIVE" in text
        assert "П1" in text


class TestAnalyzeMatch:
    @pytest.fixture(autouse=True)
    def _patch_network(self, monkeypatch):
        async def fake_odds(team1, team2, sport):
            return {
                "home": 2.0, "draw": 3.4, "away": 3.8,
                "bookmaker": "Test", "is_mock": False, "is_dropping": False, "old_odds": 0,
            }

        async def fake_ai(team1, team2, rec, conf, stats):
            return "🧠 test explanation"

        monkeypatch.setattr(bot, "fetch_bookmaker_odds", fake_odds)
        monkeypatch.setattr(bot, "generate_ai_explanation", fake_ai)
        random.seed(123)

    async def test_football_match_shape(self, db):
        match = {"team1": "A", "team2": "B", "sport": "football", "tournament": "EPL"}
        pred = await analyze_match(match)
        assert set(["analysis", "probabilities", "recommendation", "confidence", "bet_type"]) <= set(pred)
        probs = pred["probabilities"]
        assert probs["p1"] + probs["x"] + probs["p2"] == pytest.approx(100.0, abs=0.3)
        assert 0 <= pred["confidence"] <= 100
        assert "Monte Carlo" in pred["analysis"]

    async def test_recommendation_matches_highest_probability(self, db):
        match = {"team1": "A", "team2": "B", "sport": "football", "tournament": "EPL"}
        pred = await analyze_match(match)
        probs = pred["probabilities"]
        best = max(probs["p1"], probs["x"], probs["p2"])
        if best == probs["p1"]:
            assert "П1" in pred["recommendation"]
        elif best == probs["p2"]:
            assert "П2" in pred["recommendation"]
        else:
            assert "Ничья" in pred["recommendation"]

    async def test_esports_match_uses_map_markets(self, db):
        match = {"team1": "NAVI", "team2": "G2", "sport": "esports", "tournament": "Major", "format": 3}
        pred = await analyze_match(match)
        assert "Рынки по картам" in pred["analysis"]
        assert pred["probabilities"]["top_score"] == "N/A"

    async def test_dropping_odds_annotation(self, db, monkeypatch):
        async def dropping_odds(team1, team2, sport):
            return {
                "home": 1.6, "draw": 3.4, "away": 5.0,
                "bookmaker": "Test", "is_mock": False, "is_dropping": True, "old_odds": 2.1,
            }

        monkeypatch.setattr(bot, "fetch_bookmaker_odds", dropping_odds)
        match = {"team1": "A", "team2": "B", "sport": "football", "tournament": "EPL"}
        pred = await analyze_match(match)
        assert "Дроп линии" in pred["analysis"]
