"""Unit tests for the async SQLite data-access layer in bot.py.

All tests use the isolated `db` fixture (see conftest.py) which points bot.DB_NAME
at a temporary file.
"""
import json

import pytest

import bot
from bot import (
    add_user,
    analyze_prediction_accuracy,
    compute_adaptive_weights,
    garbage_collector_job,
    get_balance,
    get_current_weights,
    get_team_data,
    get_today_predictions,
    get_unsent_predictions,
    get_users_for_sport,
    mark_prediction_sent,
    place_virtual_bet,
    save_prediction,
    update_team_ratings_from_result,
)

pytestmark = pytest.mark.asyncio


async def _table_exists(db, name):
    async with db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ) as cur:
        return await cur.fetchone() is not None


class TestInitDb:
    async def test_core_tables_created(self, db):
        for table in ["users", "matches", "predictions", "team_ratings", "virtual_bets", "user_settings"]:
            assert await _table_exists(db, table), table


class TestUsers:
    async def test_add_user_sets_initial_balance(self, db):
        await add_user(101, "alice")
        assert await get_balance(101) == bot.INITIAL_VIRTUAL_BALANCE

    async def test_add_user_is_idempotent(self, db):
        await add_user(101, "alice")
        await place_virtual_bet(101, "m1", 200.0, 2.0, "П1 m1")
        # Re-adding must NOT reset the (now reduced) balance.
        await add_user(101, "alice")
        assert await get_balance(101) == pytest.approx(bot.INITIAL_VIRTUAL_BALANCE - 200.0)

    async def test_get_balance_unknown_user_is_zero(self, db):
        assert await get_balance(999) == 0.0

    async def test_add_user_creates_settings_row(self, db):
        await add_user(101, "alice")
        async with db.execute("SELECT football, hockey, esports FROM user_settings WHERE user_id=?", (101,)) as cur:
            row = await cur.fetchone()
        assert tuple(row) == (1, 1, 1)


class TestVirtualBets:
    async def test_place_bet_deducts_balance_and_records(self, db):
        await add_user(1, "u")
        await place_virtual_bet(1, "m1", 150.0, 1.9, "П1 m1")
        assert await get_balance(1) == pytest.approx(bot.INITIAL_VIRTUAL_BALANCE - 150.0)
        async with db.execute("SELECT bet_amount, odds, status FROM virtual_bets WHERE user_id=1") as cur:
            row = await cur.fetchone()
        assert row[0] == pytest.approx(150.0)
        assert row[1] == pytest.approx(1.9)
        assert row[2] == 0  # pending


class TestPredictions:
    async def test_save_and_fetch_today_prediction(self, db):
        probs = {"p1": 60, "x": 20, "p2": 20}
        await save_prediction("m1", "football", "A", "B", "EPL", "text", probs, "П1 (A)", 70, "Исход")
        rows = await get_today_predictions()
        assert len(rows) == 1
        assert rows[0][2] == "A" and rows[0][3] == "B"

    async def test_low_confidence_prediction_excluded_from_today(self, db):
        await save_prediction("m_low", "football", "A", "B", "EPL", "t", {}, "П1", 10, "Исход")
        assert await get_today_predictions() == []

    async def test_save_prediction_upserts(self, db):
        await save_prediction("m1", "football", "A", "B", "EPL", "t1", {}, "П1", 70, "Исход")
        await save_prediction("m1", "football", "A", "B", "EPL", "t2", {}, "П2", 80, "Исход")
        rows = await get_today_predictions()
        assert len(rows) == 1
        assert rows[0][8] == 80  # confidence updated

    async def test_unsent_then_marked_sent(self, db):
        await save_prediction("m1", "football", "A", "B", "EPL", "t", {}, "П1", 70, "Исход")
        assert len(await get_unsent_predictions()) == 1
        await mark_prediction_sent("m1", bot.SYSTEM_USER_ID)
        assert await get_unsent_predictions() == []

    async def test_unsent_respects_limit(self, db):
        for i in range(5):
            await save_prediction(f"m{i}", "football", "A", "B", "EPL", "t", {}, "П1", 60 + i, "Исход")
        assert len(await get_unsent_predictions(limit=2)) == 2


class TestUserSettings:
    async def test_get_users_for_sport(self, db):
        await add_user(1, "a")
        await add_user(2, "b")
        await db.execute("UPDATE user_settings SET hockey = 0 WHERE user_id = 2")
        await db.commit()
        football_users = await get_users_for_sport("football")
        hockey_users = await get_users_for_sport("hockey")
        assert set(football_users) == {1, 2}
        assert set(hockey_users) == {1}

    async def test_get_users_for_sport_rejects_unknown_column(self, db):
        # Guards against SQL injection via the interpolated column name.
        with pytest.raises(ValueError):
            await get_users_for_sport("football; DROP TABLE users;--")
        with pytest.raises(ValueError):
            await get_users_for_sport("password")


class TestTeamData:
    async def test_get_team_data_defaults_for_unknown(self, db):
        data = await get_team_data("Nowhere FC", "football")
        assert data["elo_rating"] == bot.DEFAULT_ELO
        assert data["strength"] == bot.DEFAULT_STRENGTH
        assert data["form"] == bot.DEFAULT_FORM
        assert data["games_played"] == 0

    async def test_update_ratings_persists_and_moves_elo(self, db):
        await update_team_ratings_from_result("football", "Home", "Away", 3, 0)
        home = await get_team_data("Home", "football")
        away = await get_team_data("Away", "football")
        # Winner gains Elo, loser drops below the default 1500.
        assert home["elo_rating"] > bot.DEFAULT_ELO
        assert away["elo_rating"] < bot.DEFAULT_ELO
        assert home["games_played"] == 1
        assert away["games_played"] == 1

    async def test_draw_keeps_elo_roughly_stable(self, db):
        await update_team_ratings_from_result("football", "H", "A", 1, 1)
        home = await get_team_data("H", "football")
        assert home["elo_rating"] == pytest.approx(bot.DEFAULT_ELO, abs=1e-6)


class TestPredictionAccuracy:
    async def test_correct_prediction_and_winning_bet_pays_out(self, db):
        await add_user(7, "u")
        await save_prediction("m1", "football", "A", "B", "EPL", "t", {}, "П1 (A)", 70, "Исход", user_id=7)
        await place_virtual_bet(7, "m1", 100.0, 2.0, "П1 m1")
        balance_after_bet = await get_balance(7)

        await analyze_prediction_accuracy("m1", "home_win")

        # Winning bet returns amount * odds on top of the post-bet balance.
        assert await get_balance(7) == pytest.approx(balance_after_bet + 100.0 * 2.0)
        async with db.execute("SELECT is_correct FROM prediction_results WHERE match_id='m1'") as cur:
            row = await cur.fetchone()
        assert row[0] == 1
        async with db.execute("SELECT status FROM virtual_bets WHERE match_id='m1'") as cur:
            row = await cur.fetchone()
        assert row[0] == 1  # settled as won

    async def test_losing_bet_marked_and_no_payout(self, db):
        await add_user(8, "u")
        await save_prediction("m2", "football", "A", "B", "EPL", "t", {}, "П1 (A)", 70, "Исход", user_id=8)
        await place_virtual_bet(8, "m2", 100.0, 2.0, "П1 m2")
        balance_after_bet = await get_balance(8)

        await analyze_prediction_accuracy("m2", "away_win")

        assert await get_balance(8) == pytest.approx(balance_after_bet)
        async with db.execute("SELECT is_correct FROM prediction_results WHERE match_id='m2'") as cur:
            assert (await cur.fetchone())[0] == 0
        async with db.execute("SELECT status FROM virtual_bets WHERE match_id='m2'") as cur:
            assert (await cur.fetchone())[0] == 2  # settled as lost


class TestWeights:
    async def test_get_current_weights_none_when_empty(self, db):
        assert await get_current_weights() is None

    async def test_get_current_weights_normalizes(self, db):
        for comp in bot.DEFAULT_ENSEMBLE_WEIGHTS:
            await db.execute(
                "INSERT INTO model_weights (component, weight) VALUES (?, ?)", (comp, 2.0)
            )
        await db.commit()
        weights = await get_current_weights()
        assert weights is not None
        assert sum(weights.values()) == pytest.approx(1.0)

    async def test_get_current_weights_none_when_incomplete(self, db):
        await db.execute("INSERT INTO model_weights (component, weight) VALUES ('poisson', 1.0)")
        await db.commit()
        assert await get_current_weights() is None

    async def test_compute_adaptive_weights_needs_min_samples(self, db):
        # Fewer than 30 component scores -> no weights written.
        for i in range(5):
            await db.execute(
                "INSERT INTO model_component_scores (match_id, component, brier) VALUES (?, 'poisson', 0.5)",
                (f"m{i}",),
            )
        await db.commit()
        await compute_adaptive_weights()
        assert await get_current_weights() is None

    async def test_compute_adaptive_weights_writes_when_enough_samples(self, db):
        components = list(bot.DEFAULT_ENSEMBLE_WEIGHTS.keys())
        for i in range(40):
            comp = components[i % len(components)]
            await db.execute(
                "INSERT INTO model_component_scores (match_id, component, brier) VALUES (?, ?, ?)",
                (f"m{i}", comp, 0.4),
            )
        await db.commit()
        await compute_adaptive_weights()
        async with db.execute("SELECT COUNT(*) FROM model_weights") as cur:
            assert (await cur.fetchone())[0] == len(components)


class TestGarbageCollector:
    async def test_removes_old_rows_keeps_recent(self, db):
        await db.execute(
            "INSERT INTO matches (match_id, sport, team1, team2, match_date) VALUES ('old', 'football', 'A', 'B', datetime('now', '-40 days'))"
        )
        await db.execute(
            "INSERT INTO matches (match_id, sport, team1, team2, match_date) VALUES ('new', 'football', 'A', 'B', datetime('now'))"
        )
        await db.commit()
        await garbage_collector_job()
        async with db.execute("SELECT match_id FROM matches") as cur:
            remaining = {row[0] async for row in cur}
        assert remaining == {"new"}
