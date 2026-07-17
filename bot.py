import asyncio
import json
import logging
import math
import os
import random
import time
from datetime import datetime, timedelta

import aiohttp
import aiosqlite
from aiogram import Bot, Dispatcher, F, types
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

# ==================== НАСТРОЙКИ ====================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "8933270591:AAGSJJkYl99icR7bwHv51-QlYf6Ff3CDMtM")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "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")
FOOTBALL_API_KEY = os.getenv("FOOTBALL_API_KEY", "3540a4964edea4e653d2a322ddec0270")
FOOTBALL_DATA_ORG_KEY = os.getenv("FOOTBALL_DATA_ORG_KEY", "32fcb5cfa8c64b40b4baaf2319c2809c")
PANDASCORE_API_KEY = os.getenv("PANDASCORE_API_KEY", "aXVsIwT4FSLepT021v4nrPAW9i-W-5y8Au0rrvUc4wg7bSf8IlY")
THE_ODDS_API_KEY = os.getenv("THE_ODDS_API_KEY", "9fd1c123735ebb1f95339117edae2765")
HOCKEY_API_KEY = os.getenv("HOCKEY_API_KEY", "123")
HOCKEY_API_SPORTS = os.getenv("HOCKEY_API_SPORTS", "6834e018-33cd-44c2-8195-a442fe73063d")

MIN_CONFIDENCE = 50
PREDICTIONS_PER_HOUR = 3
DB_NAME = "sports_bot.db"
INITIAL_VIRTUAL_BALANCE = 1000.0

SYSTEM_USER_ID = 0
DEFAULT_ELO = 1500
DEFAULT_STRENGTH = 50
DEFAULT_GOALS_AVG = 1.5
DEFAULT_FORM = 50

# Whitelist of user_settings columns that may be referenced by name in SQL.
SPORT_COLUMNS = ("football", "hockey", "esports")

POPULAR_LIVE_LEAGUES = [39, 140, 135, 78, 61, 2, 3]
OPENLIGADB_LEAGUES = [
    ("bundesliga", "Бундеслига"), ("2bundesliga", "2. Бундеслига"),
    ("englischepremierleague", "АПЛ"), ("laliga", "Ла Лига"),
    ("seriea", "Серия А"), ("ligue1", "Лига 1"),
]

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

bot: Bot | None = None
dp = Dispatcher()
_http_session: aiohttp.ClientSession | None = None
_db_conn: aiosqlite.Connection | None = None

# ==================== КЭШ И УТИЛИТЫ ====================

class TTLCache:
    def __init__(self, ttl_seconds=900, maxsize=1000):
        self.cache = {}
        self.ttl = ttl_seconds
        self.maxsize = maxsize

    def get(self, key):
        if key in self.cache:
            val, ts = self.cache[key]
            if time.time() - ts < self.ttl: return val
            del self.cache[key]
        return None

    def set(self, key, val):
        # Защита от переполнения памяти
        if len(self.cache) >= self.maxsize:
            oldest_key = min(self.cache, key=lambda k: self.cache[k][1])
            del self.cache[oldest_key]
        self.cache[key] = (val, time.time())

_matches_cache = TTLCache(ttl_seconds=900, maxsize=500)
_live_cache = TTLCache(ttl_seconds=60, maxsize=100)
_odds_cache = TTLCache(ttl_seconds=300, maxsize=1000)

async def get_session() -> aiohttp.ClientSession:
    global _http_session
    if _http_session is None or _http_session.closed:
        _http_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
    return _http_session

# ИСПРАВЛЕНИЕ ЗАВИСАНИЯ БД (LIVENESS CHECK)
async def get_db() -> aiosqlite.Connection:
    global _db_conn
    if _db_conn is not None:
        try:
            await _db_conn.execute_fetchall("SELECT 1") # Проверка живости соединения
            return _db_conn
        except Exception:
            logger.warning("Соединение с БД потеряно. Переподключаюсь...")
            try: await _db_conn.close()
            except Exception: pass
            _db_conn = None
            
    if _db_conn is None:
        _db_conn = await aiosqlite.connect(DB_NAME)
        _db_conn.row_factory = aiosqlite.Row
        await _db_conn.execute("PRAGMA journal_mode=WAL")
    return _db_conn

def _normalize_date(dt_str: str) -> str:
    if not dt_str: return ""
    return dt_str.replace("T", " ").replace("Z", "")[:19]

def _normalize_name(name: str) -> str:
    return "".join(ch.lower() for ch in name if ch.isalnum())

async def fetch_json_with_retry(url, headers=None, params=None, max_retries=3):
    session = await get_session()
    for attempt in range(max_retries):
        try:
            async with session.get(url, headers=headers, params=params) as resp:
                if resp.status == 429:
                    await asyncio.sleep(2 ** attempt)
                    continue
                if resp.status == 200: return await resp.json()
                return None
        except Exception as e:
            logger.error(f"Network Error {url}: {e}")
            await asyncio.sleep(1)
    return None

# ==================== ИИ-АГЕНТ (ИСПРАВЛЕНИЕ ПРОМПТА) ====================

async def generate_ai_explanation(team1, team2, rec, conf, stats_dict):
    if not OPENAI_API_KEY:
        explain = []
        if stats_dict.get('form1', 50) - stats_dict.get('form2', 50) > 15: explain.append("Хозяева в отличной форме")
        if stats_dict.get('lambda1', 1.5) > 1.8: explain.append("Высокий ожидаемый темп игры у хозяев")
        if stats_dict.get('kelly', 0) > 0: explain.append("Найден Value Bet (перевес над линией БК)")
        return "🧠 <b>Почему мы так думаем:</b>\n• " + "\n• ".join(explain) if explain else "🧠 <b>Почему мы так думаем:</b>\n• Команды сопоставимы по силам"

    bet_status = "Обычная ставка" if stats_dict.get('kelly', 0) <= 0 else f"Value Bet (перевес над БК {stats_dict.get('kelly')}%)"
    prompt = (
        f"Ты профессиональный спортивный аналитик. Напиши строгое обоснование ставки в 2-3 предложениях.\n"
        f"Матч: {team1} против {team2}. Прогноз: {rec} (уверенность {conf}%).\n"
        f"Данные: Форма ({stats_dict.get('form1', 50):.0f} vs {stats_dict.get('form2', 50):.0f}), "
        f"Ожидаемые голы ({stats_dict.get('lambda1', 1.5):.2f} vs {stats_dict.get('lambda2', 1.5):.2f}). "
        f"Статус: {bet_status}\n\n"
        f"Сформулируй ответ напрямую, как экспертный вывод. Без вступлений и нумерации."
    )
    try:
        session = await get_session()
        headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
        payload = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": prompt}], "max_tokens": 80, "temperature": 0.3}
        async with session.post("https://api.openai.com/v1/chat/completions", headers=headers, json=payload) as resp:
            if resp.status == 200:
                data = await resp.json()
                text = data['choices'][0]['message']['content'].strip()
                return f"🧠 <b>ИИ-аналитик:</b>\n<i>{text}</i>"
    except Exception as e:
        logger.error(f"OpenAI API Error: {e}")
    return "🧠 <b>Почему мы так думаем:</b>\n• Математическая модель указывает на перевес."

# ==================== МАТЕМАТИЧЕСКИЕ МОДЕЛИ ====================

class PoissonModel:
    @staticmethod
    def poisson_probability(k, lam):
        return (lam ** k * math.exp(-lam)) / math.factorial(k)

    @staticmethod
    def calculate_match_probabilities(team1_goals_avg, team2_goals_avg):
        p1_total, p2_total, draw_total = 0, 0, 0
        rho = -0.1
        team1_goals_avg = (team1_goals_avg * 0.8) + (1.35 * 0.2)
        team2_goals_avg = (team2_goals_avg * 0.8) + (1.35 * 0.2)
        team1_goals_avg = max(0.2, min(3.5, team1_goals_avg))
        team2_goals_avg = max(0.2, min(3.5, team2_goals_avg))
        tau_lambda1, tau_lambda2 = team1_goals_avg, team2_goals_avg
        
        for i in range(0, 8):
            for j in range(0, 8):
                p_i = PoissonModel.poisson_probability(i, team1_goals_avg)
                p_j = PoissonModel.poisson_probability(j, team2_goals_avg)
                p_score = p_i * p_j
                if i == 0 and j == 0: p_score *= 1 - (rho * tau_lambda1 * tau_lambda2)
                elif i == 0 and j == 1: p_score *= 1 + (rho * tau_lambda1)
                elif i == 1 and j == 0: p_score *= 1 + (rho * tau_lambda2)
                elif i == 1 and j == 1: p_score *= 1 - rho
                if i > j: p1_total += p_score
                elif i < j: p2_total += p_score
                else: draw_total += p_score
        total = p1_total + draw_total + p2_total
        return (round(p1_total/total*100, 1), round(draw_total/total*100, 1), round(p2_total/total*100, 1), team1_goals_avg, team2_goals_avg)

class LivePoissonModel:
    @staticmethod
    def calculate_live_probabilities(lambda1, lambda2, current_score1, current_score2, minute):
        if minute > 90: minute = 90
        if minute < 1: minute = 1
        rem_lambda1 = max(0.01, lambda1 * (((90 - minute) / 90.0) ** 0.9))
        rem_lambda2 = max(0.01, lambda2 * (((90 - minute) / 90.0) ** 0.9))
        p_win, p_draw, p_loss = 0, 0, 0
        for k1 in range(6):
            for k2 in range(6):
                p_score = PoissonModel.poisson_probability(k1, rem_lambda1) * PoissonModel.poisson_probability(k2, rem_lambda2)
                final1, final2 = current_score1 + k1, current_score2 + k2
                if final1 > final2: p_win += p_score
                elif final1 < final2: p_loss += p_score
                else: p_draw += p_score
        total = p_win + p_draw + p_loss
        if total == 0: return 0, 0, 0, 0
        p_over_2_5 = 0
        current_total = current_score1 + current_score2
        for k1 in range(6):
            for k2 in range(6):
                if current_total + k1 + k2 > 2: p_over_2_5 += PoissonModel.poisson_probability(k1, rem_lambda1) * PoissonModel.poisson_probability(k2, rem_lambda2)
        return round(p_win/total*100, 1), round(p_draw/total*100, 1), round(p_loss/total*100, 1), round(p_over_2_5*100, 1)

class MonteCarloSimulator:
    @staticmethod
    def _poisson_random(lam):
        L = math.exp(-lam)
        k = 0
        p = 1.0
        while True:
            k += 1
            p *= random.random()
            if p <= L: return k - 1

    @staticmethod
    def simulate_match(lambda1, lambda2, iterations=5000):
        if lambda1 <= 0 or lambda2 <= 0: return {}
        score_counts, btts_count, over_2_5_count = {}, 0, 0
        dc_1x, dc_12, ah1_minus_1_5, ah2_plus_1 = 0, 0, 0, 0
        for _ in range(iterations):
            s1 = min(MonteCarloSimulator._poisson_random(lambda1), 5)
            s2 = min(MonteCarloSimulator._poisson_random(lambda2), 5)
            score = f"{s1}:{s2}"
            score_counts[score] = score_counts.get(score, 0) + 1
            if s1 > 0 and s2 > 0: btts_count += 1
            if s1 + s2 > 2: over_2_5_count += 1
            if s1 >= s2: dc_1x += 1
            if s1 != s2: dc_12 += 1
            if s1 - s2 > 1.5: ah1_minus_1_5 += 1
            if s2 - s1 > -1: ah2_plus_1 += 1
        top_scores = sorted(score_counts.items(), key=lambda x: x[1], reverse=True)[:3]
        return {
            'top_score': top_scores[0][0] if top_scores else "1:1", 'top_score_prob': round((top_scores[0][1] / iterations) * 100, 1) if top_scores else 0,
            'btts_prob': round((btts_count / iterations) * 100, 1), 'over_2_5_prob': round((over_2_5_count / iterations) * 100, 1),
            'dc_1x': round((dc_1x / iterations) * 100, 1), 'dc_12': round((dc_12 / iterations) * 100, 1),
            'ah1_-1.5': round((ah1_minus_1_5 / iterations) * 100, 1), 'ah2_+1': round((ah2_plus_1 / iterations) * 100, 1)
        }

class BradleyTerryModel:
    @staticmethod
    def win_probability(strength_a, strength_b):
        exp_a, exp_b = math.exp(strength_a / 100), math.exp(strength_b / 100)
        return round(exp_a / (exp_a + exp_b) * 100, 1)

class BookmakerFactor:
    @staticmethod
    def calculate_probability(odds): return 50 if odds <= 0 else 100 / odds
    @staticmethod
    def get_bookmaker_influence(odds_home, odds_away, odds_draw=0):
        p1, p2 = BookmakerFactor.calculate_probability(odds_home), BookmakerFactor.calculate_probability(odds_away)
        draw = BookmakerFactor.calculate_probability(odds_draw) if odds_draw > 0 else 0
        total = p1 + p2 + draw
        if total > 0: p1, p2, draw = (p1/total*100, p2/total*100, draw/total*100)
        return round(p1, 1), round(p2, 1), round(draw, 1)

class KellyCriterion:
    @staticmethod
    def calculate_kelly(bankroll_prob, bookmaker_odds, games_played=0):
        if bookmaker_odds <= 1.0 or bankroll_prob <= 0: return 0
        b, p = bookmaker_odds - 1, bankroll_prob / 100
        kelly = (b * p - (1 - p)) / b
        return max(0, round(kelly * min(1.0, games_played / 20.0) * 100, 1))

def calculate_draw_prob(p1_prob):
    return max(15, round(33 - abs(50 - p1_prob) * 0.4, 1))

class EsportsModel:
    @staticmethod
    def predict(team1_data, team2_data):
        elo_diff = team1_data['elo_rating'] - team2_data['elo_rating']
        p1_elo = 1 / (1 + 10 ** (-elo_diff / 400)) * 100
        p1_strength = 50 + (team1_data['strength'] - team2_data['strength']) * 0.5
        p1_form = 50 + (team1_data['form'] - team2_data['form']) * 0.3
        p1 = max(20, min(80, (p1_elo * 0.40 + p1_strength * 0.30 + p1_form * 0.20 + 2 * 0.10)))
        return {'p1': round(p1, 1), 'p2': round(100 - p1, 1), 'method': 'Esports Model (Elo)'}

class EsportsMapModel:
    """Расчет тотала карт и фор для киберспорта (Bo3/Bo5)"""
    @staticmethod
    def calculate_maps(p1_map_prob, match_format):
        p1 = p1_map_prob / 100.0
        p2 = 1.0 - p1
        results = {}
        
        if match_format == 3: # Bo3
            p_3_maps = (2 * (p1**2) * p2) + (2 * (p2**2) * p1)
            results['tb_2_5'] = round(p_3_maps * 100, 1)
            results['f1_minus_1_5'] = round((p1**2) * 100, 1)       # П1 выиграет 2:0
            results['f2_plus_1_5'] = round((1.0 - (p1**2)) * 100, 1) # П2 выиграет хотя бы 1 карту
            results['f1_plus_1_5'] = round((1.0 - (p2**2)) * 100, 1) # П1 выиграет хотя бы 1 карту
            results['f2_minus_1_5'] = round((p2**2) * 100, 1)       # П2 выиграет 2:0
        elif match_format == 5: # Bo5
            p_4_maps = (3 * (p1**3) * p2) + (3 * (p2**3) * p1)
            p_5_maps = (6 * (p1**3) * (p2**2)) + (6 * (p2**3) * (p1**2))
            results['tb_3_5'] = round((p_4_maps + p_5_maps) * 100, 1)
            results['f1_minus_1_5'] = round((p1**3) * 100, 1)
            results['f2_plus_1_5'] = round((1.0 - (p1**3)) * 100, 1)
            
        return results

DEFAULT_ENSEMBLE_WEIGHTS = {'poisson': 0.35, 'bradley_terry': 0.25, 'form': 0.15, 'bookmaker': 0.25}

class EnsemblePredictor:
    def __init__(self, weights_override: dict | None = None):
        self.weights_sports = weights_override or dict(DEFAULT_ENSEMBLE_WEIGHTS)

    def predict(self, team1_data, team2_data, sport='football', bookmaker_odds=None):
        if sport == 'esports':
            result = EsportsModel.predict(team1_data, team2_data)
            return {'p1': result['p1'], 'x': 0, 'p2': result['p2'], 'total_over_2.5': None, 'method': result['method'], 'components': None, 'mc': None}

        p1_p, x_p, p2_p, adj_l1, adj_l2 = PoissonModel.calculate_match_probabilities(team1_data['goals_avg'], team2_data['goals_avg'])
        p1_bt = BradleyTerryModel.win_probability(team1_data['strength'], team2_data['strength'])
        x_bt = calculate_draw_prob(p1_bt)
        p1_f = round(max(0.0, min(100.0, 50 + (team1_data['form'] - team2_data['form']) * 0.3)), 1)
        x_f = calculate_draw_prob(p1_f)

        bk_p1, bk_p2, bk_x = 0, 0, 0
        if bookmaker_odds and not bookmaker_odds.get('is_mock'):
            bk_p1, bk_p2, bk_x = BookmakerFactor.get_bookmaker_influence(bookmaker_odds.get('home', 0), bookmaker_odds.get('away', 0), bookmaker_odds.get('draw', 0))

        w = self.weights_sports
        eps = 1e-5
        p1 = math.exp(w['poisson']*math.log(max(eps, p1_p)) + w['bradley_terry']*math.log(max(eps, p1_bt)) + w['form']*math.log(max(eps, p1_f)) + (w['bookmaker']*math.log(max(eps, bk_p1)) if bk_p1 > 0 else 0))
        p2 = math.exp(w['poisson']*math.log(max(eps, p2_p)) + w['bradley_terry']*math.log(max(eps, 100-p1_bt)) + w['form']*math.log(max(eps, 100-p1_f)) + (w['bookmaker']*math.log(max(eps, bk_p2)) if bk_p2 > 0 else 0))
        x  = math.exp(w['poisson']*math.log(max(eps, x_p))  + w['bradley_terry']*math.log(max(eps, x_bt))  + w['form']*math.log(max(eps, x_f))  + (w['bookmaker']*math.log(max(eps, bk_x))  if bk_x > 0 else 0))
        
        total = p1 + x + p2
        p1, x, p2 = (round(p1/total*100, 1), round(x/total*100, 1), round(p2/total*100, 1))
        mc_results = MonteCarloSimulator.simulate_match(adj_l1, adj_l2)
        components = {'poisson': {'p1': p1_p, 'x': x_p, 'p2': p2_p}, 'bradley_terry': {'p1': p1_bt, 'x': x_bt, 'p2': 100-p1_bt}, 'form': {'p1': p1_f, 'x': x_f, 'p2': 100-p1_f}, 'bookmaker': {'p1': bk_p1, 'x': bk_x, 'p2': bk_p2} if bookmaker_odds and not bookmaker_odds.get('is_mock') else None}
        return {'p1': p1, 'x': x, 'p2': p2, 'total_over_2.5': mc_results.get('over_2_5_prob', 0), 'method': 'Ансамбль (LOP + MC + DynTau)', 'components': components, 'mc': mc_results}
        # Часть 2: База данных, Сбор данных (Best Odds + Dropping), Обучение

# ==================== БАЗА ДАННЫХ ====================

async def init_db():
    db = await get_db()
    await db.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT, balance REAL DEFAULT 1000.0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    await db.execute("CREATE TABLE IF NOT EXISTS matches (match_id TEXT PRIMARY KEY, sport TEXT, team1 TEXT, team2 TEXT, match_date TEXT, tournament TEXT, team1_score INTEGER, team2_score INTEGER, is_finished INTEGER DEFAULT 0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    await db.execute("""CREATE TABLE IF NOT EXISTS predictions 
        (match_id TEXT, sport TEXT, team1 TEXT, team2 TEXT, tournament TEXT, analysis TEXT, probabilities TEXT, 
        recommendation TEXT, confidence REAL, bet_type TEXT, user_id INTEGER NOT NULL DEFAULT 0, 
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY (match_id, user_id))""")
    await db.execute("CREATE TABLE IF NOT EXISTS prediction_results (id INTEGER PRIMARY KEY AUTOINCREMENT, match_id TEXT, user_id INTEGER, prediction TEXT, actual_result TEXT, is_correct INTEGER, confidence REAL, checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    await db.execute("CREATE TABLE IF NOT EXISTS sent_predictions (id INTEGER PRIMARY KEY AUTOINCREMENT, match_id TEXT, user_id INTEGER, sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    await db.execute("CREATE TABLE IF NOT EXISTS team_ratings (team_id TEXT PRIMARY KEY, sport TEXT, elo REAL DEFAULT 1500, strength REAL DEFAULT 50, goals_avg REAL DEFAULT 1.5, form REAL DEFAULT 50, games_played INTEGER DEFAULT 0, goals_scored_home REAL DEFAULT 1.5, goals_scored_away REAL DEFAULT 1.2, goals_conceded_home REAL DEFAULT 1.2, goals_conceded_away REAL DEFAULT 1.5, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    await db.execute("CREATE TABLE IF NOT EXISTS model_component_scores (id INTEGER PRIMARY KEY AUTOINCREMENT, match_id TEXT, component TEXT, brier REAL, checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    await db.execute("CREATE TABLE IF NOT EXISTS model_weights (component TEXT PRIMARY KEY, weight REAL, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    await db.execute("CREATE TABLE IF NOT EXISTS bootstrap_state (key TEXT PRIMARY KEY, value TEXT, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    await db.execute("CREATE TABLE IF NOT EXISTS user_settings (user_id INTEGER PRIMARY KEY, football INTEGER DEFAULT 1, hockey INTEGER DEFAULT 1, esports INTEGER DEFAULT 1)")
    await db.execute("CREATE TABLE IF NOT EXISTS virtual_bets (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, match_id TEXT, bet_amount REAL, odds REAL, prediction TEXT, status INTEGER DEFAULT 0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_predictions_created ON predictions(created_at, confidence)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_matches_finished ON matches(is_finished, match_date)")
    await db.commit()

async def add_user(user_id: int, username: str):
    db = await get_db()
    await db.execute("INSERT OR IGNORE INTO users (user_id, username, balance) VALUES (?, ?, ?)", (user_id, username, INITIAL_VIRTUAL_BALANCE))
    await db.execute("INSERT OR IGNORE INTO user_settings (user_id) VALUES (?)", (user_id,))
    await db.commit()

async def get_balance(user_id: int) -> float:
    db = await get_db()
    async with db.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,)) as cursor:
        row = await cursor.fetchone()
    return row[0] if row else 0.0

async def place_virtual_bet(user_id: int, match_id: str, bet_amount: float, odds: float, prediction: str):
    db = await get_db()
    await db.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (bet_amount, user_id))
    await db.execute("INSERT INTO virtual_bets (user_id, match_id, bet_amount, odds, prediction) VALUES (?, ?, ?, ?, ?)", (user_id, match_id, bet_amount, odds, prediction))
    await db.commit()

async def get_users_for_sport(sport: str):
    if sport not in SPORT_COLUMNS:
        raise ValueError(f"Unknown sport column: {sport!r}")
    db = await get_db()
    async with db.execute(f"SELECT u.user_id FROM users u JOIN user_settings s ON u.user_id = s.user_id WHERE s.{sport} = 1") as cursor:
        return [row[0] async for row in cursor]

async def save_prediction(match_id, sport, team1, team2, tournament, analysis, probs, rec, conf, bet_type, user_id=SYSTEM_USER_ID):
    db = await get_db()
    await db.execute("""INSERT INTO predictions 
        (match_id, sport, team1, team2, tournament, analysis, probabilities, recommendation, confidence, bet_type, user_id) 
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) 
        ON CONFLICT(match_id, user_id) DO UPDATE SET 
        analysis=excluded.analysis, probabilities=excluded.probabilities, 
        recommendation=excluded.recommendation, confidence=excluded.confidence, bet_type=excluded.bet_type""",
        (match_id, sport, team1, team2, tournament, analysis, json.dumps(probs), rec, conf, bet_type, user_id))
    await db.commit()

async def get_today_predictions():
    db = await get_db()
    async with db.execute("SELECT match_id, sport, team1, team2, tournament, analysis, probabilities, recommendation, confidence, bet_type FROM predictions WHERE date(created_at) = date('now') AND confidence >= ? AND user_id = ? ORDER BY confidence DESC", (MIN_CONFIDENCE, SYSTEM_USER_ID)) as cursor:
        return await cursor.fetchall()

async def get_unsent_predictions(limit=3):
    db = await get_db()
    async with db.execute("""SELECT p.match_id, p.sport, p.team1, p.team2, p.tournament, p.analysis, p.probabilities, p.recommendation, p.confidence, p.bet_type FROM predictions p LEFT JOIN sent_predictions s ON p.match_id = s.match_id WHERE s.id IS NULL AND p.confidence >= ? AND p.user_id = ? ORDER BY p.confidence DESC LIMIT ?""", (MIN_CONFIDENCE, SYSTEM_USER_ID, limit)) as cursor:
        return await cursor.fetchall()

async def mark_prediction_sent(match_id, user_id):
    db = await get_db()
    await db.execute("INSERT INTO sent_predictions (match_id, user_id) VALUES (?, ?)", (match_id, user_id))
    await db.commit()

async def get_team_data(team_name: str, sport: str) -> dict:
    db = await get_db()
    key = f"{sport}_{team_name}"
    async with db.execute("SELECT elo, strength, goals_avg, form, games_played, goals_scored_home, goals_scored_away, goals_conceded_home, goals_conceded_away FROM team_ratings WHERE team_id = ?", (key,)) as cursor:
        row = await cursor.fetchone()
    if row:
        return {'elo_rating': row[0], 'strength': row[1], 'goals_avg': row[2], 'form': row[3] if row[3] is not None else DEFAULT_FORM, 'games_played': row[4] or 0, 'goals_scored_home': row[5] if row[5] is not None else DEFAULT_GOALS_AVG, 'goals_scored_away': row[6] if row[6] is not None else DEFAULT_GOALS_AVG * 0.8, 'goals_conceded_home': row[7] if row[7] is not None else DEFAULT_GOALS_AVG * 0.8, 'goals_conceded_away': row[8] if row[8] is not None else DEFAULT_GOALS_AVG}
    return {'elo_rating': DEFAULT_ELO, 'strength': DEFAULT_STRENGTH, 'goals_avg': DEFAULT_GOALS_AVG, 'form': DEFAULT_FORM, 'games_played': 0, 'goals_scored_home': DEFAULT_GOALS_AVG, 'goals_scored_away': DEFAULT_GOALS_AVG * 0.8, 'goals_conceded_home': DEFAULT_GOALS_AVG * 0.8, 'goals_conceded_away': DEFAULT_GOALS_AVG}

async def garbage_collector_job():
    db = await get_db()
    await db.execute("DELETE FROM matches WHERE match_date < datetime('now', '-30 days')")
    await db.execute("DELETE FROM predictions WHERE created_at < datetime('now', '-30 days')")
    await db.commit()

# ==================== СБОР ДАННЫХ И КОЭФФИЦИЕНТОВ (BEST ODDS + DROPPING) ====================

async def fetch_bookmaker_odds(team1: str, team2: str, sport: str):
    fallback = {'home': 2.0, 'draw': 3.5, 'away': 3.0, 'bookmaker': 'Mock', 'is_mock': True, 'is_dropping': False, 'old_odds': 0}
    if not THE_ODDS_API_KEY: return fallback
    sport_key = 'soccer_epl' if sport == 'football' else ('icehockey_nhl' if sport == 'hockey' else None)
    if not sport_key: return fallback

    cache_key = f"odds_{team1}_{team2}"
    cached_odds = _odds_cache.get(cache_key)

    t1_norm, t2_norm = _normalize_name(team1), _normalize_name(team2)
    data = await fetch_json_with_retry(f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds", params={'apiKey': THE_ODDS_API_KEY, 'regions': 'eu', 'markets': 'h2h', 'dateFormat': 'iso'})
    
    if data:
        for event in data:
            home_team, away_team = event.get('home_team', ''), event.get('away_team', '')
            if (_normalize_name(home_team) == t1_norm and _normalize_name(away_team) == t2_norm) or (_normalize_name(home_team) == t2_norm and _normalize_name(away_team) == t1_norm):
                best_home, best_away, best_draw = 0, 0, 0
                for bk in event.get('bookmakers', []):
                    for market in bk.get('markets', []):
                        for outcome in market.get('outcomes', []):
                            price = outcome.get('price', 0)
                            if price > 0:
                                if outcome.get('name') == home_team: best_home = max(best_home, price)
                                elif outcome.get('name') == away_team: best_away = max(best_away, price)
                                elif outcome.get('name') == 'Draw': best_draw = max(best_draw, price)
                
                if best_home > 0 or best_away > 0:
                    is_dropping = False
                    old_odds = 0
                    if cached_odds and not cached_odds.get('is_mock') and best_home > 0:
                        old_odds = cached_odds.get('home', 0)
                        if old_odds > 0 and (old_odds - best_home) / old_odds >= 0.10:
                            is_dropping = True
                    
                    new_odds = {'home': best_home, 'draw': best_draw, 'away': best_away, 'bookmaker': "Best Market", 'is_mock': False, 'is_dropping': is_dropping, 'old_odds': old_odds}
                    _odds_cache.set(cache_key, new_odds)
                    return new_odds
    return fallback

async def fetch_api_football_matches():
    if not FOOTBALL_API_KEY: return []
    data = await fetch_json_with_retry("https://v3.football.api-sports.io/fixtures", headers={"x-apisports-key": FOOTBALL_API_KEY}, params={"date": datetime.now().strftime("%Y-%m-%d")})
    matches = []
    if data:
        for f in data.get('response', []):
            try:
                team1 = f.get('teams', {}).get('home', {}).get('name')
                team2 = f.get('teams', {}).get('away', {}).get('name')
                if not team1 or not team2: continue # Пропускаем, если команда неизвестна (TBD)
                matches.append({
                    "id": f"af_{f['fixture']['id']}", 
                    "team1": team1, "team2": team2, 
                    "date": f['fixture']['date'], "sport": "football", 
                    "tournament": f.get('league', {}).get('name', 'Unknown')
                })
            except Exception:
                continue
    return matches

async def fetch_football_matches():
    cached = _matches_cache.get("matches_football")
    if cached: return cached
    matches = await fetch_api_football_matches()
    _matches_cache.set("matches_football", matches)
    return matches

async def fetch_pandascore_matches():
    if not PANDASCORE_API_KEY: return []
    cached = _matches_cache.get("matches_esports")
    if cached: return cached
    matches = []
    headers = {"Authorization": f"Bearer {PANDASCORE_API_KEY}", "Accept": "application/json"}
    for game_code in ['cs2', 'dota2', 'lol', 'valorant']:
        data = await fetch_json_with_retry(f"https://api.pandascore.co/{game_code}/matches/upcoming", headers=headers, params={"page[size]": 20, "sort": "begin_at"})
        if data:
            for m in data:
                if len(m.get('opponents', [])) >= 2:
                    t1 = m['opponents'][0].get('opponent', {}).get('name', 'Unknown')
                    t2 = m['opponents'][1].get('opponent', {}).get('name', 'Unknown')
                    if t1 != 'Unknown' and t2 != 'Unknown':
                        match_format = m.get('series', {}).get('type', 3)
                        matches.append({"id": f"ps_{m['id']}", "team1": t1, "team2": t2, "date": m.get('begin_at', ''), "sport": "esports", "tournament": m.get('league', {}).get('name', 'Unknown'), "format": match_format})
    _matches_cache.set("matches_esports", matches)
    return matches

async def fetch_api_sport_hockey_matches():
    """Сбор хоккейных матчей напрямую из API-Sport (Hockey)"""
    hockey_api_key = os.getenv("HOCKEY_API_SPORTS", "")
    if not hockey_api_key: 
        logger.warning("HOCKEY_API_SPORTS не задан в окружении. Использую мок-данные.")
        return []
    
    url = "https://v1.hockey.api-sports.io/games"
    headers = {"x-apisports-key": hockey_api_key}
    params = {"date": datetime.now().strftime("%Y-%m-%d")}
    
    data = await fetch_json_with_retry(url, headers=headers, params=params)
    matches = []
    if data and data.get('response'):
        for g in data['response']:
            try:
                team1 = g.get('teams', {}).get('home', {}).get('name')
                team2 = g.get('teams', {}).get('away', {}).get('name')
                if not team1 or not team2: continue
                
                game_date = g.get('game', {}).get('date')
                if isinstance(game_date, dict):
                    game_date = game_date.get('start', '')
                elif not game_date:
                    game_date = ''
                
                matches.append({
                    "id": f"hk_{g['game']['id']}",
                    "team1": team1,
                    "team2": team2,
                    "date": game_date,
                    "sport": "hockey",
                    "tournament": g.get('league', {}).get('name', 'Hockey')
                })
            except Exception:
                continue
    elif data and data.get('errors'):
        logger.error(f"Ошибка API-Sport Hockey: {data.get('errors')}")
    return matches

async def fetch_hockey_matches():
    cache_key = "matches_hockey"
    cached = _matches_cache.get(cache_key)
    if cached: return cached
    real_matches = await fetch_api_sport_hockey_matches()
    if real_matches:
        _matches_cache.set(cache_key, real_matches)
        return real_matches
    return [{"id": "hk_301", "team1": "ЦСКА", "team2": "СКА", "date": datetime.now().strftime("%Y-%m-%d"), "sport": "hockey", "tournament": "КХЛ", "is_mock_source": True}]

async def fetch_live_football_matches():
    if not FOOTBALL_API_KEY: return []
    cached = _live_cache.get("live_football")
    if cached: return cached
    data = await fetch_json_with_retry("https://v3.football.api-sports.io/fixtures", headers={"x-apisports-key": FOOTBALL_API_KEY}, params={"live": "all"})
    live_matches = []
    if data:
        for f in data.get('response', []):
            if f.get('league', {}).get('id') in POPULAR_LIVE_LEAGUES:
                try:
                    team1 = f.get('teams', {}).get('home', {}).get('name')
                    team2 = f.get('teams', {}).get('away', {}).get('name')
                    if not team1 or not team2: continue
                    live_matches.append({
                        "id": f"af_{f['fixture']['id']}", "team1": team1, "team2": team2, 
                        "score1": f.get('goals', {}).get('home') or 0, 
                        "score2": f.get('goals', {}).get('away') or 0, 
                        "minute": f.get('fixture', {}).get('status', {}).get('elapsed') or 0, 
                        "tournament": f.get('league', {}).get('name', 'Unknown'), "sport": "football"
                    })
                except Exception:
                    continue
    limit = int(os.getenv("MAX_LIVE_FOOTBALL", 2))
    live_matches = live_matches[:limit]
    _live_cache.set("live_football", live_matches)
    return live_matches

async def fetch_live_esports_matches():
    if not PANDASCORE_API_KEY: return []
    cached = _live_cache.get("live_esports")
    if cached: return cached
    data = await fetch_json_with_retry("https://api.pandascore.co/matches/running", headers={"Authorization": f"Bearer {PANDASCORE_API_KEY}", "Accept": "application/json"})
    live_matches = []
    if data:
        for m in data:
            if len(m.get('opponents', [])) >= 2:
                t1 = m['opponents'][0].get('opponent', {}).get('name', 'T1')
                t2 = m['opponents'][1].get('opponent', {}).get('name', 'T2')
                results = m.get('results', [])
                s1 = next((r['score'] for r in results if r.get('team_id') == m['opponents'][0]['opponent'].get('id')), 0)
                s2 = next((r['score'] for r in results if r.get('team_id') == m['opponents'][1]['opponent'].get('id')), 0)
                live_matches.append({"id": f"ps_{m['id']}", "team1": t1, "team2": t2, "score1": s1, "score2": s2, "tournament": m.get('league', {}).get('name', 'Esports'), "game": m.get('videogame', {}).get('name', 'Game'), "sport": "esports"})
    live_matches = live_matches[:3]
    _live_cache.set("live_esports", live_matches)
    return live_matches

# ==================== ОБУЧЕНИЕ (LOG-LOSS + XG) ====================

async def update_team_ratings_from_result(sport: str, team1: str, team2: str, score1: int, score2: int):
    db = await get_db()
    t1 = await get_team_data(team1, sport)
    t2 = await get_team_data(team2, sport)
    expected1 = 1 / (1 + 10 ** ((t2['elo_rating'] - t1['elo_rating']) / 400))
    actual1 = 1.0 if score1 > score2 else (0.0 if score1 < score2 else 0.5)
    new_elo1 = t1['elo_rating'] + 32 * (actual1 - expected1)
    new_elo2 = t2['elo_rating'] + 32 * ((1 - actual1) - (1 - expected1))
    adj_score1 = (score1 + t1['goals_avg']) / 2.0 if score1 > t1['goals_avg'] else score1
    adj_score2 = (score2 + t2['goals_avg']) / 2.0 if score2 > t2['goals_avg'] else score2
    new_goals1 = (t1['goals_avg'] * 0.8) + (adj_score1 * 0.2)
    new_goals2 = (t2['goals_avg'] * 0.8) + (adj_score2 * 0.2)
    
    await db.execute("INSERT INTO team_ratings (team_id, sport, elo, strength, goals_avg, form, games_played, goals_scored_home, goals_scored_away, goals_conceded_home, goals_conceded_away, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now')) ON CONFLICT(team_id) DO UPDATE SET elo=excluded.elo, goals_avg=excluded.goals_avg, form=excluded.form, games_played=excluded.games_played, updated_at=excluded.updated_at", 
                     (f"{sport}_{team1}", sport, new_elo1, t1['strength'], new_goals1, (t1['form']*0.7 + (100 if actual1==1 else 50 if actual1==0.5 else 0)*0.3), t1['games_played']+1, t1['goals_scored_home'], t1['goals_scored_away'], t1['goals_conceded_home'], t1['goals_conceded_away']))
    await db.execute("INSERT INTO team_ratings (team_id, sport, elo, strength, goals_avg, form, games_played, goals_scored_home, goals_scored_away, goals_conceded_home, goals_conceded_away, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now')) ON CONFLICT(team_id) DO UPDATE SET elo=excluded.elo, goals_avg=excluded.goals_avg, form=excluded.form, games_played=excluded.games_played, updated_at=excluded.updated_at", 
                     (f"{sport}_{team2}", sport, new_elo2, t2['strength'], new_goals2, (t2['form']*0.7 + (100 if actual1==0 else 50 if actual1==0.5 else 0)*0.3), t2['games_played']+1, t2['goals_scored_home'], t2['goals_scored_away'], t2['goals_conceded_home'], t2['goals_conceded_away']))
    await db.commit()

async def record_component_scores(match_id: str, components: dict, actual_result: str):
    if not components: return
    db = await get_db()
    actual_vec = {'home_win': (1.0, 0.0, 0.0), 'draw': (0.0, 1.0, 0.0), 'away_win': (0.0, 0.0, 1.0)}.get(actual_result)
    if not actual_vec: return
    for name, comp in components.items():
        if not comp: continue
        p_vec = (max(1e-5, comp.get('p1', 0) / 100), max(1e-5, comp.get('x', 0) / 100), max(1e-5, comp.get('p2', 0) / 100))
        log_loss = -sum(a * math.log(p) for p, a in zip(p_vec, actual_vec))
        await db.execute("INSERT INTO model_component_scores (match_id, component, brier, checked_at) VALUES (?, ?, ?, datetime('now'))", (match_id, name, log_loss))
    await db.commit()

async def analyze_prediction_accuracy(match_id, actual_result):
    db = await get_db()
    async with db.execute("SELECT user_id, recommendation, confidence, probabilities FROM predictions WHERE match_id = ?", (match_id,)) as cursor:
        predictions = await cursor.fetchall()
    for pred in predictions:
        user_id, recommendation, confidence, probs_json = pred
        is_correct = 0
        if actual_result == 'home_win' and 'П1' in recommendation: is_correct = 1
        elif actual_result == 'away_win' and 'П2' in recommendation: is_correct = 1
        elif actual_result == 'draw' and ('Ничья' in recommendation or 'X' in recommendation): is_correct = 1
        await db.execute("INSERT INTO prediction_results (match_id, user_id, prediction, actual_result, is_correct, confidence) VALUES (?, ?, ?, ?, ?, ?)", (match_id, user_id, recommendation, actual_result, is_correct, confidence))
    
    async with db.execute("SELECT id, user_id, bet_amount, odds, prediction FROM virtual_bets WHERE match_id = ? AND status = 0", (match_id,)) as cursor:
        v_bets = await cursor.fetchall()
    for vb in v_bets:
        bet_id, v_user_id, amount, odds, pred = vb[0], vb[1], vb[2], vb[3], vb[4]
        won = (actual_result == 'home_win' and 'П1' in pred) or (actual_result == 'away_win' and 'П2' in pred) or (actual_result == 'draw' and 'Ничья' in pred)
        if won:
            await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount * odds, v_user_id))
            await db.execute("UPDATE virtual_bets SET status = 1 WHERE id = ?", (bet_id,))
        else:
            await db.execute("UPDATE virtual_bets SET status = 2 WHERE id = ?", (bet_id,))
    await db.commit()
    if predictions:
        try:
            first_probs = json.loads(predictions[0][3])
            components = first_probs.get('components')
            if components: await record_component_scores(match_id, components, actual_result)
        except Exception: pass

async def get_current_weights() -> dict | None:
    db = await get_db()
    async with db.execute("SELECT component, weight FROM model_weights") as cursor:
        rows = await cursor.fetchall()
    if not rows: return None
    weights = {row[0]: row[1] for row in rows}
    required = set(DEFAULT_ENSEMBLE_WEIGHTS.keys())
    if not required.issubset(weights.keys()): return None
    total = sum(weights.values())
    if total <= 0: return None
    return {k: weights[k] / total for k in required}

async def compute_adaptive_weights():
    db = await get_db()
    async with db.execute("SELECT component, AVG(brier) as avg_loss, COUNT(*) as n FROM model_component_scores GROUP BY component") as cursor:
        rows = await cursor.fetchall()
    if not rows: return
    if sum(r[2] for r in rows) < 30: return
    eps = 1e-5
    raw_weights = {r[0]: math.exp(-3.0 * (r[1] + eps)) for r in rows}
    total = sum(raw_weights.values())
    normalized = {k: round(v / total, 4) for k, v in raw_weights.items()}
    for component, weight in normalized.items():
        await db.execute("INSERT INTO model_weights (component, weight, updated_at) VALUES (?, ?, datetime('now')) ON CONFLICT(component) DO UPDATE SET weight=excluded.weight, updated_at=excluded.updated_at", (component, weight))
    await db.commit()

async def _check_af_result(fixture_id: str):
    data = await fetch_json_with_retry("https://v3.football.api-sports.io/fixtures", headers={"x-apisports-key": FOOTBALL_API_KEY}, params={"id": fixture_id})
    if data:
        for f in data.get('response', []):
            if f.get('fixture', {}).get('status', {}).get('short') in ('FT', 'AET', 'PEN'):
                goals = f.get('goals', {})
                if goals.get('home') is not None and goals.get('away') is not None: return goals['home'], goals['away']
    return None

async def _check_ps_result(match_numeric_id: str):
    data = await fetch_json_with_retry(f"https://api.pandascore.co/matches/{match_numeric_id}", headers={"Authorization": f"Bearer {PANDASCORE_API_KEY}", "Accept": "application/json"})
    if data and data.get('status') == 'finished':
        results = data.get('results', [])
        opponents = data.get('opponents', [])
        if len(results) >= 2 and len(opponents) >= 2:
            id1 = opponents[0].get('opponent', {}).get('id')
            id2 = opponents[1].get('opponent', {}).get('id')
            score_by_id = {r.get('team_id'): r.get('score') for r in results}
            if id1 in score_by_id and id2 in score_by_id: return score_by_id[id1], score_by_id[id2]
    return None

async def check_and_update_finished_matches():
    db = await get_db()
    async with db.execute("SELECT match_id, sport, team1, team2, match_date FROM matches WHERE is_finished = 0 AND match_date < datetime('now', '-3 hours') AND match_date != ''") as cursor:
        matches = await cursor.fetchall()
    checkers = {'af_': _check_af_result, 'ps_': _check_ps_result}
    for match in matches:
        match_id, sport, team1, team2, match_date = match[0], match[1], match[2], match[3], match[4]
        prefix = next((p for p in checkers if match_id.startswith(p)), None)
        if prefix is None: continue
        result = await checkers[prefix](match_id[len(prefix):])
        if not result: continue
        home, away = result
        await db.execute("UPDATE matches SET team1_score = ?, team2_score = ?, is_finished = 1 WHERE match_id = ?", (home, away, match_id))
        await update_team_ratings_from_result(sport, team1, team2, home, away)
        actual_result = 'home_win' if home > away else ('away_win' if home < away else 'draw')
        await analyze_prediction_accuracy(match_id, actual_result)
    await db.commit()

async def backfill_football_history():
    db = await get_db()
    async with db.execute("SELECT value FROM bootstrap_state WHERE key = 'football_backfill_done'") as cursor:
        if await cursor.fetchone(): return 0
    total_processed = 0
    current_season = datetime.now().year
    for league_code, league_name in OPENLIGADB_LEAGUES:
        for season in [current_season, current_season - 1]:
            data = await fetch_json_with_retry(f"https://www.openligadb.de/api/getmatchdata/{league_code}/{season}")
            if not data: continue
            finished = [m for m in data if m.get('MatchIsFinished')]
            finished.sort(key=lambda m: m.get('MatchDateTime', ''))
            for m in finished:
                results = m.get('MatchResults', [])
                if not results: continue
                final = results[-1]
                home, away = final.get('PointsTeam1'), final.get('PointsTeam2')
                if home is None or away is None: continue
                team1 = m.get('Team1', {}).get('TeamName')
                team2 = m.get('Team2', {}).get('TeamName')
                if team1 and team2:
                    await update_team_ratings_from_result('football', team1, team2, home, away)
                    total_processed += 1
            await asyncio.sleep(1)
    await db.execute("INSERT INTO bootstrap_state (key, value, updated_at) VALUES ('football_backfill_done', '1', datetime('now')) ON CONFLICT(key) DO UPDATE SET value='1', updated_at=datetime('now')")
    await db.commit()
    return total_processed
    # Часть 3: Анализаторы, Пагинация матчей, Динамический Келли, UI, Безопасная рассылка

# ==================== АНАЛИЗАТОРЫ ====================

async def analyze_live_football_match(match):
    t1 = await get_team_data(match['team1'], 'football')
    t2 = await get_team_data(match['team2'], 'football')
    lambda1 = (t1['goals_scored_home'] + t2['goals_conceded_away']) / 2
    lambda2 = (t2['goals_scored_away'] + t1['goals_conceded_home']) / 2
    p_win, p_draw, p_loss, p_over = LivePoissonModel.calculate_live_probabilities(lambda1, lambda2, match['score1'], match['score2'], match['minute'])
    situation = "Игра идет предсказуемо."
    if match['minute'] > 75 and match['score1'] == match['score2']: situation = "Ничья на последних минутах. Рассмотрите ТМ."
    elif match['score1'] < match['score2'] and t1['strength'] > t2['strength']: situation = "Фаворит проигрывает. Ожидается прессинг. Рассмотрите П1 или ТБ."
    return (f"🔴 <b>LIVE: {match['team1']} {match['score1']}:{match['score2']} {match['team2']}</b> ({match['minute']}')\n"
            f"🏆 <b>Турнир:</b> {match['tournament']}\n\n📊 <b>Пересчет:</b>\n• П1: {p_win}% | X: {p_draw}% | П2: {p_loss}%\n• ТБ 2.5: {p_over}%\n\n💡 <b>Ситуация:</b>\n{situation}\n")

async def analyze_live_esports_match(match):
    t1 = await get_team_data(match['team1'], 'esports')
    t2 = await get_team_data(match['team2'], 'esports')
    predictor = EnsemblePredictor()
    result = predictor.predict(t1, t2, sport='esports')
    p1_live = max(5, min(95, result['p1'] + (match['score1'] - match['score2']) * 15))
    p2_live = 100 - p1_live
    situation = "Серия идет предсказуемо."
    if match['score2'] > match['score1'] and result['p1'] > result['p2']: situation = "Фаворит проигрывает в серии. Отличный шанс зайти на высокий кэф."
    return (f"🔴 <b>LIVE: {match['team1']} {match['score1']}:{match['score2']} {match['team2']}</b>\n"
            f"🎮 <b>Игра:</b> {match['game']} | <b>Турнир:</b> {match['tournament']}\n\n📊 <b>Оценка:</b>\n• До: П1={result['p1']}% | П2={result['p2']}%\n• В лайве: П1={p1_live}% | П2={p2_live}%\n\n💡 <b>Ситуация:</b>\n{situation}\n")

async def analyze_match(match):
    sport = match.get('sport', 'football')
    t1 = await get_team_data(match['team1'], sport)
    t2 = await get_team_data(match['team2'], sport)
    
    t1_home_ratio = t1['goals_scored_home'] / max(0.1, t1['goals_scored_away']) if t1['goals_scored_away'] > 0 else 1.2
    t1_home_factor = max(1.0, min(1.30, 0.5 + t1_home_ratio * 0.5))
    lambda1 = round(((t1['goals_scored_home'] + t2['goals_conceded_away']) / 2) * t1_home_factor, 2)
    lambda2 = round(((t2['goals_scored_away'] + t1['goals_conceded_home']) / 2) * 0.95, 2)
    
    t1_data = {'goals_avg': lambda1, 'strength': t1['strength'] + round((t1_home_factor - 1.0) * 50, 0), 'form': t1['form'], 'elo_rating': t1['elo_rating']}
    t2_data = {'goals_avg': lambda2, 'strength': t2['strength'], 'form': t2['form'], 'elo_rating': t2['elo_rating']}
    
    bookmaker_odds = await fetch_bookmaker_odds(match['team1'], match['team2'], sport)
    weights = await get_current_weights()
    predictor = EnsemblePredictor(weights_override=weights)
    result = predictor.predict(t1_data, t2_data, sport=sport, bookmaker_odds=bookmaker_odds)
    
    best = max(result['p1'], result.get('x', 0), result['p2'])
    if best == result['p1']: rec, confidence, odds_val = f"П1 ({match['team1']})", result['p1'], bookmaker_odds.get('home', 0)
    elif best == result['p2']: rec, confidence, odds_val = f"П2 ({match['team2']})", result['p2'], bookmaker_odds.get('away', 0)
    else: rec, confidence, odds_val = "Ничья (X)", result['x'], bookmaker_odds.get('draw', 0)

    avg_games = (t1['games_played'] + t2['games_played']) / 2
    kelly = KellyCriterion.calculate_kelly(confidence, odds_val, avg_games) if not bookmaker_odds.get('is_mock') and odds_val > 0 else 0
    
    stats_for_ai = {'form1': t1['form'], 'form2': t2['form'], 'lambda1': lambda1, 'lambda2': lambda2, 'kelly': kelly}
    ai_text = await generate_ai_explanation(match['team1'], match['team2'], rec, confidence, stats_for_ai)
    
    drop_text = ""
    if bookmaker_odds.get('is_dropping') and bookmaker_odds.get('old_odds', 0) > 0:
        drop_text = f"\n🔥 <b>Дроп линии:</b> кэф упал с {bookmaker_odds['old_odds']} до {odds_val} (умные деньги грузят!)\n"
    
    # Формируем текст рынков Монте-Карло или Киберспорта
    mc_text = ""
    if sport == 'esports':
        # Расчет котировок по картам
        elo_diff = t1['elo_rating'] - t2['elo_rating']
        p1_map_prob = 1 / (1 + 10 ** (-elo_diff / 400)) * 100
        match_format = match.get('format', 3)
        map_markets = EsportsMapModel.calculate_maps(p1_map_prob, match_format)
        
        mc_text = "🎮 <b>Рынки по картам:</b>\n"
        if match_format == 3:
            mc_text += (f"• Тотал карт больше 2.5: {map_markets.get('tb_2_5', 0)}%\n"
                        f"• Фора 1 (-1.5): {map_markets.get('f1_minus_1_5', 0)}% | Фора 2 (+1.5): {map_markets.get('f2_plus_1_5', 0)}%\n"
                        f"• Фора 1 (+1.5): {map_markets.get('f1_plus_1_5', 0)}% | Фора 2 (-1.5): {map_markets.get('f2_minus_1_5', 0)}%\n\n")
        elif match_format == 5:
            mc_text += (f"• Тотал карт больше 3.5: {map_markets.get('tb_3_5', 0)}%\n"
                        f"• Фора 1 (-1.5): {map_markets.get('f1_minus_1_5', 0)}% | Фора 2 (+1.5): {map_markets.get('f2_plus_1_5', 0)}%\n\n")
        else:
            mc_text += "• Формат: Bo1 (Рынки по картам недоступны)\n\n"
    else:
        mc = result.get('mc') or {}
        top_score = mc.get('top_score', '1:1')
        mc_text = (
            f"🎲 <b>Доп. рынки (Monte Carlo):</b>\n"
            f"• Точный счет: <b>{top_score}</b> ({mc.get('top_score_prob', 0)}%)\n"
            f"• Обе забьют (ОЗ-Да): {mc.get('btts_prob', 0)}%\n"
            f"• Тотал больше 2.5: {mc.get('over_2_5_prob', 0)}%\n"
            f"• Двойной шанс (1X): {mc.get('dc_1x', 0)}% | (12): {mc.get('dc_12', 0)}%\n"
            f"• Фора 1 (-1.5): {mc.get('ah1_-1.5', 0)}% | Фора 2 (+1): {mc.get('ah2_+1', 0)}%\n\n"
        )
    
    analysis = (
        f"🏆 <b>{match.get('tournament', '')}</b>\n"
        f"📊 П1={result['p1']}% | X={result.get('x',0)}% | П2={result['p2']}%\n"
        f"📈 Кэф: {odds_val} | Kelly: {kelly}%\n"
        f"{drop_text}\n"
        f"{mc_text}"
        f"{ai_text}\n"
    )
    
    return {'analysis': analysis, 'probabilities': {'p1': result['p1'], 'x': result.get('x', 0), 'p2': result['p2'], 'method': result['method'], 'components': result.get('components'), 'odds': odds_val, 'kelly': kelly, 'rec': rec, 'top_score': top_score if sport != 'esports' else 'N/A', 'btts': mc.get('btts_prob', 0) if sport != 'esports' else 0}, 'recommendation': rec, 'confidence': confidence, 'bet_type': 'Исход'}

async def collect_and_analyze_job():
    db = await get_db()
    matches = await fetch_football_matches()
    matches.extend(await fetch_pandascore_matches())
    matches.extend(await fetch_hockey_matches())
    for m in matches:
        try:
            pred = await analyze_match(m)
            if pred and pred['confidence'] >= MIN_CONFIDENCE:
                await db.execute("INSERT OR REPLACE INTO matches (match_id, sport, team1, team2, match_date, tournament) VALUES (?, ?, ?, ?, ?, ?)", (m['id'], m['sport'], m['team1'], m['team2'], _normalize_date(m.get('date', '')), m.get('tournament', 'Unknown')))
                await save_prediction(m['id'], m['sport'], m['team1'], m['team2'], m.get('tournament', 'Unknown'), pred['analysis'], pred['probabilities'], pred['recommendation'], pred['confidence'], pred['bet_type'])
        except Exception as e:
            logger.error(f"Error analyzing {m.get('team1')}: {e}")
    await db.commit()

# ==================== ИНТЕРФЕЙС БОТА (ПАГИНАЦИЯ) ====================

def get_main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔥 Live Матчи", callback_data="live_matches")],
        [InlineKeyboardButton(text="📅 Прогнозы на сегодня", callback_data="today")],
        [InlineKeyboardButton(text="⚽️ Футбол", callback_data="sport_football"),
         InlineKeyboardButton(text="🏒 Хоккей", callback_data="sport_hockey"),
         InlineKeyboardButton(text="🎮 Киберспорт", callback_data="sport_esports")],
        [InlineKeyboardButton(text="💰 Мой банк", callback_data="my_bank"),
         InlineKeyboardButton(text="⚙️ Настройки", callback_data="settings")]
    ])

def get_back_button():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_start")]])

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await add_user(message.from_user.id, message.from_user.username or "unknown")
    await message.answer("🤖 Я — ML-бот для спортивных прогнозов. У меня есть Виртуальный банк, Монте-Карло и ИИ-аналитик. Выбери раздел:", parse_mode="HTML", reply_markup=get_main_keyboard())

@dp.callback_query(F.data == "back_to_start")
async def back_to_start(callback: types.CallbackQuery):
    await callback.message.edit_text("🏠 <b>Главное меню</b>", parse_mode="HTML", reply_markup=get_main_keyboard())
    await callback.answer()

@dp.callback_query(F.data == "my_bank")
async def show_bank(callback: types.CallbackQuery):
    db = await get_db()
    user_id = callback.from_user.id
    
    # Безопасное извлечение баланса
    async with db.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,)) as cursor:
        row = await cursor.fetchone()
    
    if not row:
        # Если пользователя нет в базе — добавляем его
        await add_user(user_id, callback.from_user.username or "unknown")
        balance = INITIAL_VIRTUAL_BALANCE
    else:
        balance = row[0]
        
    # Безопасное извлечение статистики
    async with db.execute("SELECT COUNT(*) as total, SUM(CASE WHEN status = 1 THEN 1 ELSE 0 END) as won FROM virtual_bets WHERE user_id = ? AND status != 0", (user_id,)) as cursor:
        stats = await cursor.fetchone()
        
    if stats and stats[0] is not None:
        total_bets = stats[0]
        won_bets = stats[1] if stats[1] is not None else 0
        roi = round((won_bets / total_bets * 100), 1) if total_bets > 0 else 0
    else:
        total_bets = 0
        won_bets = 0
        roi = 0
        
    text = (f"💰 <b>Виртуальный банк</b>\n\n"
            f"💵 Текущий баланс: <b>{balance:.2f} у.е.</b>\n"
            f"📊 Разыграно ставок: {total_bets}\n"
            f"✅ Выиграно: {won_bets}\n"
            f"📈 Точность (ROI): {roi}%\n\n"
            f"Делайте ставки на прогнозы, чтобы протестировать бота без риска!")
            
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=get_back_button())
    await callback.answer()

@dp.callback_query(F.data == "live_matches")
async def show_live_matches(callback: types.CallbackQuery):
    await callback.answer("🔴 Загружаю live матчи...")
    fb_matches = await fetch_live_football_matches()
    es_matches = await fetch_live_esports_matches()
    if not fb_matches and not es_matches:
        await callback.message.edit_text("😔 Сейчас нет live матчей в топ-лигах.", reply_markup=get_back_button())
        return
    text = ""
    if fb_matches:
        text += "⚽️ <b>Футбол (Live)</b>\n\n"
        for match in fb_matches: text += await analyze_live_football_match(match) + "\n➖➖➖➖➖➖➖➖➖\n"
    if es_matches:
        text += "\n🎮 <b>Киберспорт (Live)</b>\n\n"
        for match in es_matches: text += await analyze_live_esports_match(match) + "\n➖➖➖➖➖➖➖➖➖\n"
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=get_back_button())

@dp.callback_query(F.data == "today")
async def show_today(callback: types.CallbackQuery):
    await callback.answer("⏳ Загружаю прогнозы...")
    predictions = await get_today_predictions()
    if not predictions:
        await callback.message.edit_text("😔 На сегодня пока нет прогнозов.", reply_markup=get_back_button())
        return
    text = f"📅 <b>Прогнозы на сегодня ({len(predictions)} шт.)</b>\n\n"
    for i, pred in enumerate(predictions[:10], 1):
        text += f"{i}. <b>{pred[2]} vs {pred[3]}</b>\n   💰 {pred[7]} ({pred[8]}%)\n\n"
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=get_back_button())

async def show_sport_page(callback: types.CallbackQuery, sport: str, page: int):
    sport_names = {'football': '⚽️ Футбол', 'hockey': '🏒 Хоккей', 'esports': '🎮 Киберспорт'}
    if sport == 'football': matches = await fetch_football_matches()
    elif sport == 'hockey': matches = await fetch_hockey_matches()
    else: matches = await fetch_pandascore_matches()
    
    if not matches:
        await callback.message.edit_text("Матчи не найдены.", reply_markup=get_back_button())
        return
        
    items_per_page = 7
    start_idx = page * items_per_page
    end_idx = start_idx + items_per_page
    page_matches = matches[start_idx:end_idx]
    
    kb = []
    for m in page_matches:
        kb.append([InlineKeyboardButton(text=f"{m['team1']} vs {m['team2']}", callback_data=f"match_{m['id']}_{sport}")])
    
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"page_{sport}_{page-1}"))
    if end_idx < len(matches):
        nav_buttons.append(InlineKeyboardButton(text="Вперед ➡️", callback_data=f"page_{sport}_{page+1}"))
    if nav_buttons:
        kb.append(nav_buttons)
        
    kb.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_start")])
    
    await callback.message.edit_text(f"{sport_names[sport]} - Страница {page+1} (Выбери матч):", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data.startswith("sport_"))
async def show_sport(callback: types.CallbackQuery):
    sport = callback.data.split("_")[1]
    await callback.answer("🔍 Ищу матчи...")
    await show_sport_page(callback, sport, 0)

@dp.callback_query(F.data.startswith("page_"))
async def change_page(callback: types.CallbackQuery):
    _, sport, page_str = callback.data.split("_")
    await callback.answer(f"Страница {int(page_str)+1}")
    await show_sport_page(callback, sport, int(page_str))

@dp.callback_query(F.data.startswith("match_"))
async def show_match_analysis(callback: types.CallbackQuery):
    parts = callback.data.split("_", 1)[1].rsplit("_", 1)
    match_id, sport = parts[0], parts[1]
    await callback.answer("🧮 Анализирую...")
    
    matches = await fetch_football_matches() if sport == 'football' else (await fetch_hockey_matches() if sport == 'hockey' else await fetch_pandascore_matches())
    match = next((m for m in matches if m['id'] == match_id), None)
    if not match:
        await callback.message.edit_text("Матч не найден.", reply_markup=get_back_button())
        return
        
    pred = await analyze_match(match)
    if not pred:
        await callback.message.edit_text("Не удалось рассчитать вероятности.", reply_markup=get_back_button())
        return
        
    odds = pred['probabilities'].get('odds', 0)
    text = f"🏆 <b>{match['team1']} vs {match['team2']}</b>\n\n{pred['analysis']}"
    
    kb = []
    if odds > 1.0 and pred['probabilities']['p1'] > 0:
        user_id = callback.from_user.id
        balance = await get_balance(user_id)
        kelly_pct = pred['probabilities'].get('kelly', 0)
        bet_amount = max(10.0, round(balance * (kelly_pct / 100.0), 2)) if kelly_pct > 0 else 100.0
        bet_amount = min(bet_amount, balance)
        
        cb_data = f"bet_{match_id}_{odds}_П1_{bet_amount}"
        btn_text = f"Поставить {bet_amount:.2f} у.е. на П1 (Кэф {odds})"
        if kelly_pct > 0: btn_text += f" | Value Bet {kelly_pct}%"
        kb.append([InlineKeyboardButton(text=btn_text, callback_data=cb_data)])
        
    kb.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_start")])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data.startswith("bet_"))
async def process_virtual_bet(callback: types.CallbackQuery):
    try:
        parts = callback.data.split("_", 1)[1].rsplit("_", 3)
        match_id, odds_str, rec, amount_str = parts[0], parts[1], parts[2], parts[3]
        odds = float(odds_str)
        amount = float(amount_str)
    except Exception:
        await callback.answer("Ошибка обработки ставки.", show_alert=True)
        return
    
    user_id = callback.from_user.id
    balance = await get_balance(user_id)
    if balance < amount:
        await callback.answer("Недостаточно средств на балансе!", show_alert=True)
        return
        
    await place_virtual_bet(user_id, match_id, amount, odds, f"{rec} {match_id}")
    await callback.answer(f"Ставка {amount:.2f} у.е. на {rec} принята!", show_alert=True)
    new_balance = await get_balance(user_id)
    await callback.message.edit_text(f"✅ Ставка принята!\nВаш баланс: {new_balance:.2f} у.е.", reply_markup=get_back_button())

@dp.callback_query(F.data == "settings")
async def show_settings(callback: types.CallbackQuery):
    db = await get_db()
    async with db.execute("SELECT football, hockey, esports FROM user_settings WHERE user_id = ?", (callback.from_user.id,)) as cursor:
        settings = await cursor.fetchone()
    if not settings: return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"⚽️ Футбол [{'✅' if settings[0] else '❌'}]", callback_data=f"set_football_{settings[0]}"),
         InlineKeyboardButton(text=f"🏒 Хоккей [{'✅' if settings[1] else '❌'}]", callback_data=f"set_hockey_{settings[1]}"),
         InlineKeyboardButton(text=f"🎮 Киберспорт [{'✅' if settings[2] else '❌'}]", callback_data=f"set_esports_{settings[2]}")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_start")]
    ])
    await callback.message.edit_text("⚙️ <b>Настройки рассылки</b>", parse_mode="HTML", reply_markup=kb)

@dp.callback_query(F.data.startswith("set_"))
async def toggle_setting(callback: types.CallbackQuery):
    _, sport, val = callback.data.split("_")
    if sport not in SPORT_COLUMNS:
        await callback.answer("Неизвестная настройка.", show_alert=True)
        return
    new_val = 0 if int(val) == 1 else 1
    db = await get_db()
    await db.execute(f"UPDATE user_settings SET {sport} = ? WHERE user_id = ?", (new_val, callback.from_user.id))
    await db.commit()
    await show_settings(callback)

# ==================== ПЛАНИРОВЩИК И ЗАПУСК (БЕЗОПАСНАЯ РАССЫЛКА И BACKFILL В ФОНЕ) ====================

async def send_predictions_job():
    predictions = await get_unsent_predictions(limit=PREDICTIONS_PER_HOUR)
    if not predictions: return
    for pred in predictions:
        match_id, sport, team1, team2, tournament, analysis, probs_json, rec, conf, bet_type = pred
        text = f"💰 <b>Прогноз:</b> {rec} ({conf}%)\n{analysis}"
        users = await get_users_for_sport(sport)
        for user_id in users:
            try:
                await bot.send_message(user_id, text, parse_mode="HTML")
                await asyncio.sleep(0.05)
            except TelegramRetryAfter as e:
                logger.warning(f"Flood control. Ожидаю {e.retry_after} сек.")
                await asyncio.sleep(e.retry_after)
            except (TelegramForbiddenError, TelegramBadRequest) as e:
                logger.error(f"Не могу отправить сообщение {user_id} (заблокировал бота?): {e}")
            except Exception as e:
                logger.error(f"Неизвестная ошибка отправки {user_id}: {e}")
        await mark_prediction_sent(match_id, SYSTEM_USER_ID)

async def check_results_job():
    try: await check_and_update_finished_matches()
    except Exception as e: logger.error(f"Ошибка check_results_job: {e}")

async def recompute_weights_job():
    try: await compute_adaptive_weights()
    except Exception as e: logger.error(f"Ошибка recompute_weights_job: {e}")

async def main():
    global bot
    if not TELEGRAM_TOKEN: raise RuntimeError("TELEGRAM_TOKEN не задан")
    bot = Bot(token=TELEGRAM_TOKEN)
    await init_db()
    
    # Backfill запускается в фоне, чтобы не блокировать Polling
    logger.info("📚 Запускаю подгрузку истории матчей в фоне...")
    asyncio.create_task(backfill_football_history())
    
    scheduler = AsyncIOScheduler()
    scheduler.add_job(collect_and_analyze_job, IntervalTrigger(hours=1), next_run_time=datetime.now() + timedelta(minutes=1))
    scheduler.add_job(check_results_job, IntervalTrigger(hours=2), next_run_time=datetime.now() + timedelta(minutes=5))
    scheduler.add_job(send_predictions_job, IntervalTrigger(hours=1), next_run_time=datetime.now() + timedelta(minutes=10))
    scheduler.add_job(recompute_weights_job, IntervalTrigger(hours=24), next_run_time=datetime.now() + timedelta(minutes=15))
    scheduler.add_job(garbage_collector_job, IntervalTrigger(days=7))
    scheduler.start()
    
    logger.info("✅ Бот готов к работе!")
    try: await dp.start_polling(bot)
    finally:
        if _db_conn: await _db_conn.close()
        if _http_session and not _http_session.closed: await _http_session.close()

if __name__ == "__main__":
    try: asyncio.run(main())
    except (KeyboardInterrupt, SystemExit): logger.info("🛑 Бот остановлен")
