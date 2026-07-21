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
from aiogram.exceptions import TelegramRetryAfter, TelegramForbiddenError, TelegramBadRequest  # <-- было пропущено
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

# ==================== НАСТРОЙКИ ====================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "8933270591:AAGSJJkYl99icR7bwHv51-QlYf6Ff3CDMtM")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "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")
FOOTBALL_API_KEY = os.getenv("FOOTBALL_API_KEY", "3540a4964edea4e653d2a322ddec0270")
PANDASCORE_API_KEY = os.getenv("PANDASCORE_API_KEY", "aXVsIwT4FSLepT021v4nrPAW9i-W-5y8Au0rrvUc4wg7bSf8IlY")
HIGHLIGHTLY_API_KEY  = os.getenv("HIGHLIGHTLY_API_KEY", "3f2748a3-c083-4243-be9e-360412badaf4")
HIGHLIGHTLY_HOCKEY_BASE = "https://nhl.highlightly.net"
FOOTBALLDATA_KEY = os.getenv("FOOTBALLDATA_KEY", "fd_3cafc903b109cd31cc63ab3cd9edf3fad4068acad44dad7f")
BASKETBALL_API_KEY = os.getenv("BASKETBALL_API_KEY", "3540a4964edea4e653d2a322ddec0270")
MMA_API_KEY = os.getenv("MMA_API_KEY", "3540a4964edea4e653d2a322ddec0270")
THE_ODDS_API_KEY = os.getenv("THE_ODDS_API_KEY", "9fd1c123735ebb1f95339117edae2765")
HOCKEY_API_SPORTS     = os.getenv("HOCKEY_API_SPORTS", "3540a4964edea4e653d2a322ddec0270")

MIN_CONFIDENCE = 50
PREDICTIONS_PER_HOUR = 3
DB_NAME = "sports_bot.db"
INITIAL_VIRTUAL_BALANCE = 1000.0
MAX_LIVE_FOOTBALL = int(os.getenv("MAX_LIVE_FOOTBALL", 5))
MAX_LIVE_ESPORTS  = int(os.getenv("MAX_LIVE_ESPORTS", 3))
ALLOWED_SPORTS = {'football', 'hockey', 'esports', 'basketball', 'mma'}     # whitelist (SQL + роутинг)
ADAPT_MIN_SAMPLES = 30

# Среднее «очков/голов за игру» для НОВЫХ команд (пока нет истории). Баскетбол = очки (~85), хоккей = шайбы (~2.5).
SPORT_DEFAULT_GOALS = {'football': 1.5, 'hockey': 2.5, 'basketball': 85.0, 'esports': 1.5, 'mma': 1.0}
# Маппинг sport -> ключ the-odds-api (для кэфов / Kelly / Value Bet)
THE_ODDS_SPORT_KEY = {'football': 'soccer_epl', 'hockey': 'icehockey_nhl',
                      'basketball': 'basketball_nba', 'mma': 'mma_mixed_martial_arts'}

SYSTEM_USER_ID = 0
DEFAULT_ELO = 1500
DEFAULT_STRENGTH = 50
DEFAULT_GOALS_AVG = 1.5
DEFAULT_FORM = 50

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
        self.cache, self.ttl, self.maxsize = {}, ttl_seconds, maxsize
    def get(self, key):
        if key in self.cache:
            val, ts = self.cache[key]
            if time.time() - ts < self.ttl: return val
            del self.cache[key]
        return None
    def set(self, key, val):
        if len(self.cache) >= self.maxsize:
            oldest = min(self.cache, key=lambda k: self.cache[k][1]); del self.cache[oldest]
        self.cache[key] = (val, time.time())

_matches_cache = TTLCache(ttl_seconds=900, maxsize=500)
_live_cache    = TTLCache(ttl_seconds=60,  maxsize=100)
_odds_cache    = TTLCache(ttl_seconds=300, maxsize=1000)
_odds_raw_ok_cache   = TTLCache(ttl_seconds=120, maxsize=50)   # сырой ответ /sport/odds — 1 запрос на sport_key
_odds_authfail_cache = TTLCache(ttl_seconds=600, maxsize=20)   # circuit breaker на 401/403 (10 минут тишины)

async def get_session() -> aiohttp.ClientSession:
    global _http_session
    if _http_session is None or _http_session.closed:
        _http_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
    return _http_session

async def get_db() -> aiosqlite.Connection:
    global _db_conn
    if _db_conn is not None:
        try:
            await _db_conn.execute_fetchall("SELECT 1"); return _db_conn
        except Exception:
            logger.warning("Соединение с БД потеряно. Переподключаюсь...")
            try: await _db_conn.close()
            except Exception: pass
            _db_conn = None
    _db_conn = await aiosqlite.connect(DB_NAME, timeout=10.0)
    _db_conn.row_factory = aiosqlite.Row
    await _db_conn.execute("PRAGMA journal_mode=WAL")
    await _db_conn.execute("PRAGMA synchronous=NORMAL")
    return _db_conn

def _normalize_date(dt_str: str) -> str:
    return dt_str.replace("T", " ").replace("Z", "")[:19] if dt_str else ""

def _normalize_name(name: str) -> str:
    return "".join(ch.lower() for ch in name if ch.isalnum())

async def fetch_json_with_retry(url, headers=None, params=None, max_retries=3):
    session = await get_session()
    for attempt in range(max_retries):
        try:
            async with session.get(url, headers=headers, params=params) as resp:
                if resp.status == 429:
                    await asyncio.sleep(2 ** attempt); continue
                if resp.status == 200: return await resp.json()
                logger.warning(f"HTTP {resp.status} for {url}")
                return None
        except Exception as e:
            logger.error(f"Network Error {url}: {e}")
            await asyncio.sleep(1)
    return None

# ==================== ИИ-АГЕНТ ====================
async def generate_ai_explanation(team1, team2, rec, conf, stats_dict):
    if not OPENAI_API_KEY:
        explain = []
        if stats_dict.get('form1', 50) - stats_dict.get('form2', 50) > 15: explain.append("Первая команда в отличной форме")
        if stats_dict.get('lambda1', 0) > stats_dict.get('lambda2', 0) * 1.2 and stats_dict.get('lambda1', 0) > 0: explain.append("Модель видит перевес по ожидаемым очкам/голам")
        if stats_dict.get('kelly', 0) > 0: explain.append("Найден Value Bet (перевес над линией БК)")
        return "🧠 <b>Почему мы так думаем:</b>\n• " + "\n• ".join(explain) if explain else "🧠 <b>Почему мы так думаем:</b>\n• Соперники сопоставимы по силам"
    bet_status = "Обычная ставка" if stats_dict.get('kelly', 0) <= 0 else f"Value Bet (перевес над БК {stats_dict.get('kelly')}%)"
    prompt = (
        f"Ты профессиональный спортивный аналитик. Напиши строгое обоснование ставки в 2-3 предложениях.\n"
        f"Матч/бой: {team1} против {team2}. Прогноз: {rec} (уверенность {conf}%).\n"
        f"Данные модели: форма ({stats_dict.get('form1',50):.0f} vs {stats_dict.get('form2',50):.0f}), "
        f"ожидаемый темп/очки ({stats_dict.get('lambda1',0):.1f} vs {stats_dict.get('lambda2',0):.1f}). Статус: {bet_status}\n\n"
        f"Сформулируй ответ напрямую, как экспертный вывод. Без вступлений и нумерации."
    )
    try:
        session = await get_session()
        headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
        payload = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": prompt}], "max_tokens": 80, "temperature": 0.3}
        async with session.post("https://api.openai.com/v1/chat/completions", headers=headers, json=payload) as resp:
            if resp.status == 200:
                data = await resp.json()
                return f"🧠 <b>ИИ-аналитик:</b>\n<i>{data['choices'][0]['message']['content'].strip()}</i>"
    except Exception as e:
        logger.error(f"OpenAI API Error: {e}")
    return "🧠 <b>Почему мы так думаем:</b>\n• Математическая модель указывает на перевес."

# ==================== МАТЕМАТИЧЕСКИЕ МОДЕЛИ ====================
class PoissonModel:
    @staticmethod
    def poisson_probability(k, lam):
        return (lam ** k * math.exp(-lam)) / math.factorial(k)
    @staticmethod
    def calculate_match_probabilities(t1g, t2g):
        p1t = p2t = dt = 0.0; rho = -0.1
        l1 = max(0.2, min(3.5, t1g*0.8 + 1.35*0.2)); l2 = max(0.2, min(3.5, t2g*0.8 + 1.35*0.2))
        for i in range(8):
            for j in range(8):
                ps = PoissonModel.poisson_probability(i, l1) * PoissonModel.poisson_probability(j, l2)
                if i == 0 and j == 0: ps *= 1 - (rho*l1*l2)
                elif i == 0 and j == 1: ps *= 1 + (rho*l1)
                elif i == 1 and j == 0: ps *= 1 + (rho*l2)
                elif i == 1 and j == 1: ps *= 1 - rho
                if i > j: p1t += ps
                elif i < j: p2t += ps
                else: dt += ps
        tot = p1t + dt + p2t
        return round(p1t/tot*100,1), round(dt/tot*100,1), round(p2t/tot*100,1), l1, l2

class LivePoissonModel:
    @staticmethod
    def calculate_live_probabilities(l1, l2, s1, s2, minute):
        minute = max(1, min(90, minute))
        r1 = max(0.01, l1 * (((90-minute)/90.0)**0.9)); r2 = max(0.01, l2 * (((90-minute)/90.0)**0.9))
        pw = pd = pl = 0.0
        for k1 in range(6):
            for k2 in range(6):
                ps = PoissonModel.poisson_probability(k1, r1) * PoissonModel.poisson_probability(k2, r2)
                if s1+k1 > s2+k2: pw += ps
                elif s1+k1 < s2+k2: pl += ps
                else: pd += ps
        tot = pw + pd + pl
        if tot == 0: return 0, 0, 0, 0
        po = sum(PoissonModel.poisson_probability(k1, r1)*PoissonModel.poisson_probability(k2, r2)
                 for k1 in range(6) for k2 in range(6) if s1+s2+k1+k2 > 2)
        return round(pw/tot*100,1), round(pd/tot*100,1), round(pl/tot*100,1), round(po*100,1)

class MonteCarloSimulator:
    @staticmethod
    def _poisson_random(lam):
        L, k, p = math.exp(-lam), 0, 1.0
        while True:
            k += 1; p *= random.random()
            if p <= L: return k - 1
    @staticmethod
    def simulate_match(l1, l2, iterations=5000):
        if l1 <= 0 or l2 <= 0: return {}
        scores, btts, over = {}, 0, 0
        dc1x, dc12, ah1, ah2 = 0, 0, 0, 0
        for _ in range(iterations):
            s1 = min(MonteCarloSimulator._poisson_random(l1), 5); s2 = min(MonteCarloSimulator._poisson_random(l2), 5)
            scores[f"{s1}:{s2}"] = scores.get(f"{s1}:{s2}", 0) + 1
            if s1 > 0 and s2 > 0: btts += 1
            if s1 + s2 > 2: over += 1
            if s1 >= s2: dc1x += 1
            if s1 != s2: dc12 += 1
            if s1 - s2 > 1.5: ah1 += 1
            if s2 - s1 > -1: ah2 += 1
        top = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:3]
        return {'top_score': top[0][0] if top else "1:1", 'top_score_prob': round(top[0][1]/iterations*100,1) if top else 0,
                'btts_prob': round(btts/iterations*100,1), 'over_2_5_prob': round(over/iterations*100,1),
                'dc_1x': round(dc1x/iterations*100,1), 'dc_12': round(dc12/iterations*100,1),
                'ah1_-1.5': round(ah1/iterations*100,1), 'ah2_+1': round(ah2/iterations*100,1)}

class BradleyTerryModel:
    @staticmethod
    def win_probability(a, b):
        ea, eb = math.exp(a/100), math.exp(b/100); return round(ea/(ea+eb)*100, 1)

class BookmakerFactor:
    @staticmethod
    def calculate_probability(o): return 50 if o <= 0 else 100/o
    @staticmethod
    def get_bookmaker_influence(oh, oa, od=0):
        p1, p2 = BookmakerFactor.calculate_probability(oh), BookmakerFactor.calculate_probability(oa)
        dx = BookmakerFactor.calculate_probability(od) if od > 0 else 0
        t = p1 + p2 + dx
        if t > 0: p1, p2, dx = (p1/t*100, p2/t*100, dx/t*100)
        return round(p1,1), round(p2,1), round(dx,1)

class KellyCriterion:
    @staticmethod
    def calculate_kelly(prob, odds, games_played=0):
        if odds <= 1.0 or prob <= 0: return 0
        b, p = odds - 1, prob/100
        k = (b*p - (1-p))/b
        return max(0, round(k * min(1.0, games_played/20.0) * 100, 1))

def calculate_draw_prob(p1): return max(15, round(33 - abs(50 - p1)*0.4, 1))

class EsportsModel:
    W_ELO, W_STR, W_FORM = 0.45, 0.35, 0.20
    @staticmethod
    def predict(t1, t2):
        p1_elo = 1 / (1 + 10 ** (-(t1['elo_rating']-t2['elo_rating'])/400)) * 100
        p1_str = 50 + (t1['strength']-t2['strength'])*0.5
        p1_form = 50 + (t1['form']-t2['form'])*0.3
        p1 = p1_elo*EsportsModel.W_ELO + p1_str*EsportsModel.W_STR + p1_form*EsportsModel.W_FORM
        p1 = max(15, min(85, p1))
        return {'p1': round(p1,1), 'p2': round(100-p1,1), 'method': 'Esports Model (Elo+Str+Form)'}

class EsportsMapModel:
    @staticmethod
    def calculate_maps(p1_map_prob, fmt):
        p1, p2 = p1_map_prob/100.0, 1.0 - p1_map_prob/100.0; r = {}
        if fmt == 3:
            r['tb_2_5'] = round(((2*p1*p1*p2)+(2*p2*p2*p1))*100,1)
            r['f1_minus_1_5'] = round(p1*p1*100,1); r['f2_plus_1_5'] = round((1-p1*p1)*100,1)
            r['f1_plus_1_5'] = round((1-p2*p2)*100,1); r['f2_minus_1_5'] = round(p2*p2*100,1)
        elif fmt == 5:
            p4 = (3*p1**3*p2)+(3*p2**3*p1); p5 = (6*p1**3*p2**2)+(6*p2**3*p1**2)
            r['tb_3_5'] = round((p4+p5)*100,1); r['f1_minus_1_5'] = round(p1**3*100,1); r['f2_plus_1_5'] = round((1-p1**3)*100,1)
        return r

class BasketballModel:
    """Баскетбол: двухисходная модель (ничьих нет) + честные линии тотала и форы (норм. распр.)."""
    @staticmethod
    def _phi(z): return 0.5 * (1 + math.erf(z / math.sqrt(2)))
    @staticmethod
    def win_prob(t1, t2):
        elo_p  = 1 / (1 + 10 ** (-(t1['elo_rating']-t2['elo_rating'])/400))
        str_p  = 1 / (1 + math.exp(-(t1['strength']-t2['strength'])/25))
        form_p = 1 / (1 + math.exp(-(t1['form']-t2['form'])/30))
        return round(max(5, min(95, (0.45*elo_p + 0.35*str_p + 0.20*form_p)*100)), 1)
    @staticmethod
    def fair_lines(mu_total, mu_margin, sigma_total=14.0, sigma_margin=11.0):
        over_line  = round(mu_total - 5.5, 1); under_line = round(mu_total + 5.5, 1)
        p_over  = 1 - BasketballModel._phi((over_line + 0.5 - mu_total)/sigma_total)
        p_under = BasketballModel._phi((under_line - 0.5 - mu_total)/sigma_total)
        spread_line = round(mu_margin, 1)
        p_cover = 1 - BasketballModel._phi((spread_line + 0.5 - mu_margin)/sigma_margin)
        return {'mu_total': round(mu_total,1), 'mu_margin': round(mu_margin,1),
                'over_line': over_line, 'p_over': round(p_over*100,1),
                'under_line': under_line, 'p_under': round(p_under*100,1),
                'spread_line': spread_line, 'p_cover_home': round(p_cover*100,1), 'p_cover_away': round((1-p_cover)*100,1)}

class MmaModel:
    """Бокс/ММА: исход боя (П1 / ничья / П2). Ничья редка (~2%)."""
    @staticmethod
    def predict(t1, t2):
        elo_p  = 1 / (1 + 10 ** (-(t1['elo_rating']-t2['elo_rating'])/400))
        str_p  = 1 / (1 + math.exp(-(t1['strength']-t2['strength'])/25))
        form_p = 1 / (1 + math.exp(-(t1['form']-t2['form'])/30))
        p = max(8, min(92, 0.50*elo_p + 0.30*str_p + 0.20*form_p))
        draw = 2.0
        return round(p*(100-draw)/100, 1), draw, round((100-p)*(100-draw)/100, 1)

DEFAULT_ENSEMBLE_WEIGHTS = {'poisson': 0.35, 'bradley_terry': 0.25, 'form': 0.15, 'bookmaker': 0.25}

class EnsemblePredictor:
    """Только футбол/хоккей/киберспорт. Баскетбол и ММА считаются напрямую в analyze_match
       (иначе Пуассон по ~85 очкам даст переполнение factorial)."""
    def __init__(self, weights_override=None):
        self.w = weights_override or dict(DEFAULT_ENSEMBLE_WEIGHTS)
    def predict(self, t1, t2, sport='football', bookmaker_odds=None):
        if sport == 'esports':
            r = EsportsModel.predict(t1, t2)
            return {'p1': r['p1'], 'x': 0, 'p2': r['p2'], 'total_over_2.5': None, 'method': r['method'], 'components': None, 'mc': None}
        p1_p, x_p, p2_p, l1, l2 = PoissonModel.calculate_match_probabilities(t1['goals_avg'], t2['goals_avg'])
        p1_bt = BradleyTerryModel.win_probability(t1['strength'], t2['strength']); x_bt = calculate_draw_prob(p1_bt)
        p1_f = round(max(0.0, min(100.0, 50 + (t1['form']-t2['form'])*0.3)), 1); x_f = calculate_draw_prob(p1_f)
        bk1 = bk2 = bkx = 0
        if bookmaker_odds and not bookmaker_odds.get('is_mock'):
            bk1, bk2, bkx = BookmakerFactor.get_bookmaker_influence(bookmaker_odds.get('home',0), bookmaker_odds.get('away',0), bookmaker_odds.get('draw',0))
        w, eps = self.w, 1e-5
        def lop(*terms): return math.exp(sum(wt*math.log(max(eps, v)) for wt, v in terms if v > 0))
        p1 = lop((w['poisson'],p1_p),(w['bradley_terry'],p1_bt),(w['form'],p1_f),(w['bookmaker'],bk1))
        p2 = lop((w['poisson'],p2_p),(w['bradley_terry'],100-p1_bt),(w['form'],100-p1_f),(w['bookmaker'],bk2))
        x  = lop((w['poisson'],x_p),(w['bradley_terry'],x_bt),(w['form'],x_f),(w['bookmaker'],bkx))
        tot = p1 + x + p2
        p1, x, p2 = round(p1/tot*100,1), round(x/tot*100,1), round(p2/tot*100,1)
        mc = MonteCarloSimulator.simulate_match(l1, l2)
        comp = {'poisson': {'p1':p1_p,'x':x_p,'p2':p2_p}, 'bradley_terry': {'p1':p1_bt,'x':x_bt,'p2':100-p1_bt},
                'form': {'p1':p1_f,'x':x_f,'p2':100-p1_f},
                'bookmaker': {'p1':bk1,'x':bkx,'p2':bk2} if (bookmaker_odds and not bookmaker_odds.get('is_mock')) else None}
        return {'p1':p1,'x':x,'p2':p2,'total_over_2.5':mc.get('over_2_5_prob',0),'method':'Ансамбль (LOP + MC + DynTau)','components':comp,'mc':mc}

# ==================== БАЗА ДАННЫХ ====================
async def migrate_db():
    """Безопасно добавляет колонки рассылки для баскетбола и ММА (не трогает старые данные)."""
    db = await get_db()
    for col in ('basketball', 'mma'):
        try:
            await db.execute(f"ALTER TABLE user_settings ADD COLUMN {col} INTEGER DEFAULT 1")
        except Exception:
            pass
    await db.commit()

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
    await migrate_db()

async def add_user(user_id, username):
    db = await get_db()
    await db.execute("INSERT OR IGNORE INTO users (user_id, username, balance) VALUES (?, ?, ?)", (user_id, username, INITIAL_VIRTUAL_BALANCE))
    await db.execute("INSERT OR IGNORE INTO user_settings (user_id) VALUES (?)", (user_id,))
    await db.commit()

async def get_balance(user_id):
    db = await get_db()
    async with db.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,)) as c:
        row = await c.fetchone()
    return row[0] if row else 0.0

async def place_virtual_bet(user_id, match_id, amount, odds, prediction):
    db = await get_db()
    cur = await db.execute("UPDATE users SET balance = balance - ? WHERE user_id = ? AND balance >= ?", (amount, user_id, amount))
    if cur.rowcount == 0: return False
    await db.execute("INSERT INTO virtual_bets (user_id, match_id, bet_amount, odds, prediction) VALUES (?, ?, ?, ?, ?)", (user_id, match_id, amount, odds, prediction))
    await db.commit(); return True

async def get_users_for_sport(sport):
    if sport not in ALLOWED_SPORTS: return []
    db = await get_db()
    async with db.execute(f"SELECT u.user_id FROM users u JOIN user_settings s ON u.user_id = s.user_id WHERE s.{sport} = 1") as c:
        return [row[0] async for row in c]

async def save_prediction(match_id, sport, team1, team2, tournament, analysis, probs, rec, conf, bet_type, user_id=SYSTEM_USER_ID):
    db = await get_db()
    await db.execute("""INSERT INTO predictions
        (match_id, sport, team1, team2, tournament, analysis, probabilities, recommendation, confidence, bet_type, user_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(match_id, user_id) DO UPDATE SET analysis=excluded.analysis, probabilities=excluded.probabilities,
        recommendation=excluded.recommendation, confidence=excluded.confidence, bet_type=excluded.bet_type""",
        (match_id, sport, team1, team2, tournament, analysis, json.dumps(probs), rec, conf, bet_type, user_id))
    await db.commit()

async def get_today_predictions():
    db = await get_db()
    async with db.execute("SELECT match_id, sport, team1, team2, tournament, analysis, probabilities, recommendation, confidence, bet_type FROM predictions WHERE date(created_at) = date('now') AND confidence >= ? AND user_id = ? ORDER BY confidence DESC", (MIN_CONFIDENCE, SYSTEM_USER_ID)) as c:
        return await c.fetchall()

async def get_unsent_predictions(limit=3):
    db = await get_db()
    async with db.execute("""SELECT p.match_id, p.sport, p.team1, p.team2, p.tournament, p.analysis, p.probabilities, p.recommendation, p.confidence, p.bet_type
        FROM predictions p LEFT JOIN sent_predictions s ON p.match_id = s.match_id
        WHERE s.id IS NULL AND p.confidence >= ? AND p.user_id = ? ORDER BY p.confidence DESC LIMIT ?""", (MIN_CONFIDENCE, SYSTEM_USER_ID, limit)) as c:
        return await c.fetchall()

async def mark_prediction_sent(match_id, user_id):
    db = await get_db()
    await db.execute("INSERT INTO sent_predictions (match_id, user_id) VALUES (?, ?)", (match_id, user_id)); await db.commit()

async def get_team_data(team_name, sport):
    db = await get_db()
    d = SPORT_DEFAULT_GOALS.get(sport, DEFAULT_GOALS_AVG)
    async with db.execute("SELECT elo, strength, goals_avg, form, games_played, goals_scored_home, goals_scored_away, goals_conceded_home, goals_conceded_away FROM team_ratings WHERE team_id = ?", (f"{sport}_{team_name}",)) as c:
        row = await c.fetchone()
    if row:
        return {'elo_rating': row[0], 'strength': row[1], 'goals_avg': row[2],
                'form': row[3] if row[3] is not None else DEFAULT_FORM, 'games_played': row[4] or 0,
                'goals_scored_home': row[5] if row[5] is not None else d,
                'goals_scored_away': row[6] if row[6] is not None else d*0.9,
                'goals_conceded_home': row[7] if row[7] is not None else d*0.9,
                'goals_conceded_away': row[8] if row[8] is not None else d}
    return {'elo_rating': DEFAULT_ELO, 'strength': DEFAULT_STRENGTH, 'goals_avg': d, 'form': DEFAULT_FORM, 'games_played': 0,
            'goals_scored_home': d, 'goals_scored_away': d*0.9, 'goals_conceded_home': d*0.9, 'goals_conceded_away': d}

async def garbage_collector_job():
    db = await get_db()
    await db.execute("DELETE FROM matches WHERE match_date < datetime('now', '-30 days')")
    await db.execute("DELETE FROM predictions WHERE created_at < datetime('now', '-30 days')")
    await db.commit()

async def get_learning_dashboard():
    db = await get_db()
    async with db.execute("SELECT COUNT(*) FROM matches WHERE is_finished = 1") as c: finished = (await c.fetchone())[0] or 0
    async with db.execute("SELECT COUNT(*), SUM(is_correct) FROM prediction_results WHERE user_id = 0") as c:
        r = await c.fetchone()
    total_p, correct_p = r[0] or 0, int(r[1] or 0)
    acc = round(correct_p/total_p*100, 1) if total_p else 0.0
    async with db.execute("SELECT component, AVG(brier) AS loss, COUNT(*) AS n FROM model_component_scores GROUP BY component ORDER BY loss ASC") as c:
        comp_rows = await c.fetchall()
    async with db.execute("SELECT component, weight FROM model_weights") as c: w_rows = await c.fetchall()
    async with db.execute("SELECT COUNT(*), SUM(CASE WHEN games_played>0 THEN 1 ELSE 0 END) FROM team_ratings") as c:
        tr = await c.fetchone()
    teams_total, teams_learned = tr[0] or 0, int(tr[1] or 0)
    async with db.execute("SELECT COUNT(*) FROM model_component_scores") as c: comp_total = (await c.fetchone())[0] or 0
    adaptive_on = bool(w_rows) and comp_total >= ADAPT_MIN_SAMPLES
    status = "🟢 <b>Адаптивные веса активны</b> — ансамбль перенастраивается по точности компонент." if adaptive_on \
             else f"🟡 <b>Накопление данных</b> — собрано {comp_total} оценок компонент (нужно {ADAPT_MIN_SAMPLES} для авто-адаптации)."
    comp_block = "\n🧩 <b>Точность компонент ансамбля</b> (log-loss, ↓ лучше):\n" if comp_rows else "\n🧩 <b>Компоненты:</b> пока нет завершённых матчей для оценки.\n"
    for name, loss, n in comp_rows:
        bar = "🟢" if loss < 0.9 else ("🟡" if loss < 1.1 else "🔴")
        comp_block += f"   {bar} {name}: {loss:.3f}  (n={n})\n"
    weight_block = ("⚖️ <b>Веса ансамбля (авто):</b> " + ", ".join(f"{k}={v:.2f}" for k, v in w_rows) + "\n") if w_rows \
                   else "⚖️ <b>Веса ансамбля:</b> стандартные (" + ", ".join(f"{k}={v}" for k, v in DEFAULT_ENSEMBLE_WEIGHTS.items()) + ")\n"
    return (f"📚 <b>Обучение модели</b>\n\n{status}\n\n"
            f"✅ Обработано результатов матчей: <b>{finished}</b>\n"
            f"🎯 Точность прогнозов бота (Win-Rate): <b>{acc}%</b>  ({correct_p}/{total_p})\n"
            f"🏟 Команд/бойцов в базе рейтингов: <b>{teams_total}</b> (обучено играми: {teams_learned})\n\n"
            f"{weight_block}{comp_block}\n"
            "💡 <i>Чем больше матчей завершается — тем точнее рейтинги (Elo), а веса ансамбля смещаются в пользу самых точных компонент.</i>")

# ==================== СБОР ДАННЫХ И КОЭФФИЦИЕНТОВ ====================
async def fetch_bookmaker_odds(team1, team2, sport):
    fallback = {'home': 2.0, 'draw': 3.5, 'away': 3.0, 'bookmaker': 'Mock', 'is_mock': True, 'is_dropping': False, 'old_odds': 0}
    if not THE_ODDS_API_KEY: return fallback
    sport_key = THE_ODDS_SPORT_KEY.get(sport)
    if not sport_key: return fallback

    if _odds_authfail_cache.get(sport_key):          # ключ уже отклоняли — не спамим API
        return fallback

    data = _odds_raw_ok_cache.get(sport_key)         # кэш сырого ответа (1 запрос на sport_key)
    if data is None:
        session = await get_session()
        url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds"
        params = {'apiKey': THE_ODDS_API_KEY, 'regions': 'eu', 'markets': 'h2h', 'dateFormat': 'iso'}
        try:
            async with session.get(url, params=params) as resp:
                if resp.status == 200:
                    data = await resp.json()
                elif resp.status in (401, 403):
                    _odds_authfail_cache.set(sport_key, True)
                    logger.warning(f"The-Odds-API: ключ не принят (HTTP {resp.status}) для {sport_key}. "
                                   f"Проверьте ключ / активацию email / файл .env. Кэфы на заглушках 10 минут.")
                    return fallback
                else:
                    logger.warning(f"HTTP {resp.status} for {url}"); data = None
        except Exception as e:
            logger.error(f"Network Error {url}: {e}"); data = None
        _odds_raw_ok_cache.set(sport_key, data or [])
        data = data or []
    if not data: return fallback

    cache_key = f"odds_{sport}_{team1}_{team2}"
    cached = _odds_cache.get(cache_key)
    t1n, t2n = _normalize_name(team1), _normalize_name(team2)
    for ev in data:
        ht, at = ev.get('home_team',''), ev.get('away_team','')
        if (_normalize_name(ht)==t1n and _normalize_name(at)==t2n) or (_normalize_name(ht)==t2n and _normalize_name(at)==t1n):
            bh = ba = bd = 0
            for bk in ev.get('bookmakers', []):
                for mk in bk.get('markets', []):
                    for oc in mk.get('outcomes', []):
                        pr = oc.get('price', 0)
                        if pr > 0:
                            if oc.get('name') == ht: bh = max(bh, pr)
                            elif oc.get('name') == at: ba = max(ba, pr)
                            elif oc.get('name') == 'Draw': bd = max(bd, pr)
            if bh > 0 or ba > 0:
                is_drop, old = False, 0
                if cached and not cached.get('is_mock') and bh > 0:
                    old = cached.get('home', 0)
                    if old > 0 and (old-bh)/old >= 0.10: is_drop = True
                new = {'home': bh, 'draw': bd, 'away': ba, 'bookmaker': "Best Market", 'is_mock': False, 'is_dropping': is_drop, 'old_odds': old}
                _odds_cache.set(cache_key, new); return new
    return fallback

async def fetch_api_football_matches():
    if not FOOTBALL_API_KEY: return []
    data = await fetch_json_with_retry("https://v3.football.api-sports.io/fixtures", headers={"x-apisports-key": FOOTBALL_API_KEY}, params={"date": datetime.now().strftime("%Y-%m-%d")})
    out = []
    if data:
        for f in data.get('response', []):
            try:
                t1 = f.get('teams',{}).get('home',{}).get('name'); t2 = f.get('teams',{}).get('away',{}).get('name')
                if not t1 or not t2: continue
                out.append({"id": f"af_{f['fixture']['id']}", "team1": t1, "team2": t2, "date": f['fixture']['date'], "sport": "football", "tournament": f.get('league',{}).get('name','Unknown')})
            except Exception: continue
    return out

async def fetch_footballdata_matches():
    if not FOOTBALLDATA_KEY: return []
    today = datetime.now().strftime("%Y-%m-%d")
    data = await fetch_json_with_retry("https://api.football-data.org/v4/matches", headers={"X-Auth-Token": FOOTBALLDATA_KEY}, params={"dateFrom": today, "dateTo": today})
    out = []
    if data:
        for m in data.get('matches', []):
            try:
                t1 = m.get('homeTeam',{}).get('name'); t2 = m.get('awayTeam',{}).get('name')
                if not t1 or not t2 or m.get('status') != 'SCHEDULED': continue
                out.append({"id": f"fd_{m['id']}", "team1": t1, "team2": t2, "date": m.get('utcDate',''), "sport": "football", "tournament": m.get('competition',{}).get('name','Football')})
            except Exception: continue
    return out
async def fetch_highlightly_hockey_matches():
    """Основной источник хоккея — Highlightly."""
    if not HIGHLIGHTLY_API_KEY: return []
    headers = {"x-rapidapi-key": HIGHLIGHTLY_API_KEY, "Accept": "application/json"}
    params = {"date": datetime.now().strftime("%Y-%m-%d"), "limit": 100}
    resp = await fetch_json_with_retry(f"{HIGHLIGHTLY_HOCKEY_BASE}/matches", headers=headers, params=params)
    if not resp: return []
    items = resp.get('data') if isinstance(resp, dict) else resp
    if not isinstance(items, list): items = []
    out, skip = [], {'postponed', 'cancelled', 'suspended', 'abandoned'}
    for m in items:
        try:
            ht, at = m.get('homeTeam') or {}, m.get('awayTeam') or {}
            t1 = ht.get('displayName') or ht.get('name'); t2 = at.get('displayName') or at.get('name')
            if not t1 or not t2: continue
            st = m.get('state') or {}
            desc = (st.get('description') or '').lower(); report = (st.get('report') or '').lower()
            if desc in skip: continue
            if report in ('final','finished','aot','ot','so','ended','aet') or 'finished' in desc or 'final' in desc:
                continue
            league = m.get('league')
            lname = league if isinstance(league, str) else (league.get('name') if isinstance(league, dict) else 'Hockey')
            out.append({"id": f"hl_{m.get('id')}", "team1": t1, "team2": t2, "date": m.get('date', ''),
                        "sport": "hockey", "tournament": lname or 'Hockey'})
        except Exception: continue
    return out
 
async def fetch_football_matches():
    c = _matches_cache.get("matches_football")
    if c: return c
    m = await fetch_api_football_matches()
    if not m: m = await fetch_footballdata_matches()      # fallback на footballdata.io
    _matches_cache.set("matches_football", m); return m

async def fetch_pandascore_matches():
    if not PANDASCORE_API_KEY: return []
    c = _matches_cache.get("matches_esports")
    if c: return c
    out, headers = [], {"Authorization": f"Bearer {PANDASCORE_API_KEY}", "Accept": "application/json"}
    for g in ['csgo','dota2','lol','valorant']:   # CS2 у Pandascore идёт под префиксом /csgo/ (legacy)
        data = await fetch_json_with_retry(f"https://api.pandascore.co/{g}/matches/upcoming", headers=headers, params={"page[size]": 20, "sort": "begin_at"})
        if data:
            for m in data:
                if len(m.get('opponents',[])) >= 2:
                    t1 = m['opponents'][0].get('opponent',{}).get('name','Unknown'); t2 = m['opponents'][1].get('opponent',{}).get('name','Unknown')
                    if t1 != 'Unknown' and t2 != 'Unknown':
                        fmt = m.get('series',{}).get('type') or 1
                        out.append({"id": f"ps_{m['id']}", "team1": t1, "team2": t2, "date": m.get('begin_at',''), "sport": "esports", "tournament": m.get('league',{}).get('name','Unknown'), "format": fmt})
    _matches_cache.set("matches_esports", out); return out

async def fetch_api_sport_hockey_matches():
    if not HOCKEY_API_SPORTS: return []
    data = await fetch_json_with_retry("https://v1.hockey.api-sports.io/games", headers={"x-apisports-key": HOCKEY_API_SPORTS}, params={"date": datetime.now().strftime("%Y-%m-%d")})
    out = []
    if data and data.get('response'):
        for g in data['response']:
            try:
                t1 = g.get('teams',{}).get('home',{}).get('name'); t2 = g.get('teams',{}).get('away',{}).get('name')
                if not t1 or not t2: continue
                gd = g.get('game',{}).get('date'); gd = gd.get('start','') if isinstance(gd, dict) else (gd or '')
                out.append({"id": f"hk_{g['game']['id']}", "team1": t1, "team2": t2, "date": gd, "sport": "hockey", "tournament": g.get('league',{}).get('name','Hockey')})
            except Exception: continue
    elif data and data.get('errors'): logger.error(f"Hockey API error: {data.get('errors')}")
    return out

async def fetch_hockey_matches():
    c = _matches_cache.get("matches_hockey")
    if c: return c
    real = await fetch_highlightly_hockey_matches()
    logger.info(f"🏒 Highlightly хоккей: получено {len(real)} матчей")
    if not real:
        real = await fetch_api_sport_hockey_matches()
        logger.info(f"🏒 api-sports хоккей (fallback): получено {len(real)} матчей")
    if real:
        _matches_cache.set("matches_hockey", real); return real
    return [{"id": "hk_301", "team1": "ЦСКА", "team2": "СКА", "date": datetime.now().strftime("%Y-%m-%d"), "sport": "hockey", "tournament": "КХЛ", "is_mock_source": True}]
  
async def fetch_basketball_matches():
    if not BASKETBALL_API_KEY: return []
    c = _matches_cache.get("matches_basketball")
    if c: return c
    data = await fetch_json_with_retry("https://v1.basketball.api-sports.io/games", headers={"x-apisports-key": BASKETBALL_API_KEY}, params={"date": datetime.now().strftime("%Y-%m-%d")})
    out = []
    if data and data.get('response'):
        for g in data['response']:
            try:
                t1 = g.get('teams',{}).get('home',{}).get('name'); t2 = g.get('teams',{}).get('away',{}).get('name')
                if not t1 or not t2: continue
                out.append({"id": f"bk_{g['game']['id']}", "team1": t1, "team2": t2, "date": g.get('game',{}).get('date',''), "sport": "basketball", "tournament": g.get('league',{}).get('name','Basketball')})
            except Exception: continue
    _matches_cache.set("matches_basketball", out); return out

async def fetch_mma_matches():
    if not MMA_API_KEY: return []
    c = _matches_cache.get("matches_mma")
    if c: return c
    data = await fetch_json_with_retry("https://v1.mma.api-sports.io/fights", headers={"x-apisports-key": MMA_API_KEY}, params={"date": datetime.now().strftime("%Y-%m-%d")})
    out = []
    if data and data.get('response'):
        for f in data['response']:
            try:
                t1 = f.get('teams',{}).get('home',{}).get('name'); t2 = f.get('teams',{}).get('away',{}).get('name')
                if not t1 or not t2: continue
                out.append({"id": f"mma_{f['fight']['id']}", "team1": t1, "team2": t2, "date": f.get('fight',{}).get('date',''), "sport": "mma", "tournament": f.get('league',{}).get('name','MMA/Boxing')})
            except Exception: continue
    _matches_cache.set("matches_mma", out); return out

async def fetch_live_football_matches():
    if not FOOTBALL_API_KEY: return []
    c = _live_cache.get("live_football")
    if c: return c
    data = await fetch_json_with_retry("https://v3.football.api-sports.io/fixtures", headers={"x-apisports-key": FOOTBALL_API_KEY}, params={"live": "all"})
    out = []
    if data:
        for f in data.get('response', []):
            if f.get('league',{}).get('id') in POPULAR_LIVE_LEAGUES:
                try:
                    t1 = f.get('teams',{}).get('home',{}).get('name'); t2 = f.get('teams',{}).get('away',{}).get('name')
                    if not t1 or not t2: continue
                    out.append({"id": f"af_{f['fixture']['id']}", "team1": t1, "team2": t2,
                                "score1": f.get('goals',{}).get('home') or 0, "score2": f.get('goals',{}).get('away') or 0,
                                "minute": f.get('fixture',{}).get('status',{}).get('elapsed') or 0,
                                "tournament": f.get('league',{}).get('name','Unknown'), "sport": "football"})
                except Exception: continue
    out = out[:MAX_LIVE_FOOTBALL]; _live_cache.set("live_football", out); return out

async def fetch_live_esports_matches():
    if not PANDASCORE_API_KEY: return []
    c = _live_cache.get("live_esports")
    if c: return c
    data = await fetch_json_with_retry("https://api.pandascore.co/matches/running", headers={"Authorization": f"Bearer {PANDASCORE_API_KEY}", "Accept": "application/json"})
    out = []
    if data:
        for m in data:
            if len(m.get('opponents',[])) >= 2:
                t1 = m['opponents'][0].get('opponent',{}).get('name','T1'); t2 = m['opponents'][1].get('opponent',{}).get('name','T2')
                res = m.get('results',[])
                s1 = next((r['score'] for r in res if r.get('team_id')==m['opponents'][0]['opponent'].get('id')), 0)
                s2 = next((r['score'] for r in res if r.get('team_id')==m['opponents'][1]['opponent'].get('id')), 0)
                out.append({"id": f"ps_{m['id']}", "team1": t1, "team2": t2, "score1": s1, "score2": s2,
                            "tournament": m.get('league',{}).get('name','Esports'), "game": m.get('videogame',{}).get('name','Game'), "sport": "esports"})
    out = out[:MAX_LIVE_ESPORTS]; _live_cache.set("live_esports", out); return out

# ==================== ОБУЧЕНИЕ И ПРОВЕРКА РЕЗУЛЬТАТОВ ====================
async def update_team_ratings_from_result(sport, team1, team2, s1, s2):
    db = await get_db()
    t1, t2 = await get_team_data(team1, sport), await get_team_data(team2, sport)
    exp1 = 1 / (1 + 10 ** ((t2['elo_rating']-t1['elo_rating'])/400))
    act1 = 1.0 if s1 > s2 else (0.0 if s1 < s2 else 0.5)
    e1 = t1['elo_rating'] + 32*(act1-exp1); e2 = t2['elo_rating'] + 32*((1-act1)-(1-exp1))
    a1 = (s1+t1['goals_avg'])/2.0 if s1 > t1['goals_avg'] else s1
    a2 = (s2+t2['goals_avg'])/2.0 if s2 > t2['goals_avg'] else s2
    g1 = t1['goals_avg']*0.8 + a1*0.2; g2 = t2['goals_avg']*0.8 + a2*0.2
    f1 = t1['form']*0.7 + (100 if act1==1 else 50 if act1==0.5 else 0)*0.3
    f2 = t2['form']*0.7 + (100 if act1==0 else 50 if act1==0.5 else 0)*0.3
    sql = ("INSERT INTO team_ratings (team_id, sport, elo, strength, goals_avg, form, games_played, goals_scored_home, "
           "goals_scored_away, goals_conceded_home, goals_conceded_away, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,datetime('now')) "
           "ON CONFLICT(team_id) DO UPDATE SET elo=excluded.elo, goals_avg=excluded.goals_avg, form=excluded.form, "
           "games_played=excluded.games_played, updated_at=excluded.updated_at")
    await db.execute(sql, (f"{sport}_{team1}", sport, e1, t1['strength'], g1, f1, t1['games_played']+1, t1['goals_scored_home'], t1['goals_scored_away'], t1['goals_conceded_home'], t1['goals_conceded_away']))
    await db.execute(sql, (f"{sport}_{team2}", sport, e2, t2['strength'], g2, f2, t2['games_played']+1, t2['goals_scored_home'], t2['goals_scored_away'], t2['goals_conceded_home'], t2['goals_conceded_away']))
    await db.commit()

async def record_component_scores(match_id, components, actual_result):
    if not components: return
    db = await get_db()
    vec = {'home_win': (1,0,0), 'draw': (0,1,0), 'away_win': (0,0,1)}.get(actual_result)
    if not vec: return
    for name, comp in components.items():
        if not comp: continue
        pv = (max(1e-5, comp.get('p1',0)/100), max(1e-5, comp.get('x',0)/100), max(1e-5, comp.get('p2',0)/100))
        ll = -sum(a*math.log(p) for p, a in zip(pv, vec))
        await db.execute("INSERT INTO model_component_scores (match_id, component, brier, checked_at) VALUES (?, ?, ?, datetime('now'))", (match_id, name, ll))
    await db.commit()

async def analyze_prediction_accuracy(match_id, actual_result):
    db = await get_db()
    async with db.execute("SELECT user_id, recommendation, confidence, probabilities FROM predictions WHERE match_id = ?", (match_id,)) as c:
        preds = await c.fetchall()
    for p in preds:
        uid, rec, conf, _ = p
        ok = (actual_result=='home_win' and 'П1' in rec) or (actual_result=='away_win' and 'П2' in rec) or (actual_result=='draw' and ('Ничья' in rec or 'X' in rec))
        await db.execute("INSERT INTO prediction_results (match_id, user_id, prediction, actual_result, is_correct, confidence) VALUES (?,?,?,?,?,?)", (match_id, uid, rec, actual_result, 1 if ok else 0, conf))
    async with db.execute("SELECT id, user_id, bet_amount, odds, prediction FROM virtual_bets WHERE match_id = ? AND status = 0", (match_id,)) as c:
        vbs = await c.fetchall()
    for vb in vbs:
        bid, vuid, amt, odds, pred = vb[0], vb[1], vb[2], vb[3], vb[4]
        won = (actual_result=='home_win' and 'П1' in pred) or (actual_result=='away_win' and 'П2' in pred) or (actual_result=='draw' and 'Ничья' in pred)
        if won:
            await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amt*odds, vuid))
            await db.execute("UPDATE virtual_bets SET status = 1 WHERE id = ?", (bid,))
        else:
            await db.execute("UPDATE virtual_bets SET status = 2 WHERE id = ?", (bid,))
    await db.commit()
    if preds:
        try:
            comp = json.loads(preds[0][3]).get('components')
            if comp: await record_component_scores(match_id, comp, actual_result)
        except Exception: pass

async def get_current_weights():
    db = await get_db()
    async with db.execute("SELECT component, weight FROM model_weights") as c: rows = await c.fetchall()
    if not rows: return None
    w = {r[0]: r[1] for r in rows}
    if not set(DEFAULT_ENSEMBLE_WEIGHTS).issubset(w): return None
    t = sum(w.values())
    return {k: w[k]/t for k in DEFAULT_ENSEMBLE_WEIGHTS} if t > 0 else None

async def compute_adaptive_weights():
    db = await get_db()
    async with db.execute("SELECT component, AVG(brier) AS loss, COUNT(*) AS n FROM model_component_scores GROUP BY component") as c: rows = await c.fetchall()
    if not rows or sum(r[2] for r in rows) < ADAPT_MIN_SAMPLES: return
    raw = {r[0]: math.exp(-3.0*(r[1]+1e-5)) for r in rows}; t = sum(raw.values())
    norm = {k: round(v/t, 4) for k, v in raw.items()}
    for comp, wt in norm.items():
        await db.execute("INSERT INTO model_weights (component, weight, updated_at) VALUES (?, ?, datetime('now')) ON CONFLICT(component) DO UPDATE SET weight=excluded.weight, updated_at=excluded.updated_at", (comp, wt))
    await db.commit()

async def _check_af_result(fid):
    data = await fetch_json_with_retry("https://v3.football.api-sports.io/fixtures", headers={"x-apisports-key": FOOTBALL_API_KEY}, params={"id": fid})
    if data:
        for f in data.get('response', []):
            if f.get('fixture',{}).get('status',{}).get('short') in ('FT','AET','PEN'):
                g = f.get('goals',{})
                if g.get('home') is not None and g.get('away') is not None: return g['home'], g['away']
    return None

async def _check_fd_result(mid):
    data = await fetch_json_with_retry(f"https://api.football-data.org/v4/matches/{mid}", headers={"X-Auth-Token": FOOTBALLDATA_KEY})
    if data and data.get('status') == 'FINISHED':
        ft = data.get('score',{}).get('fullTime',{})
        if ft.get('home') is not None and ft.get('away') is not None: return ft['home'], ft['away']
    return None

async def _check_ps_result(mid):
    data = await fetch_json_with_retry(f"https://api.pandascore.co/matches/{mid}", headers={"Authorization": f"Bearer {PANDASCORE_API_KEY}", "Accept": "application/json"})
    if data and data.get('status') == 'finished':
        res, opp = data.get('results',[]), data.get('opponents',[])
        if len(res) >= 2 and len(opp) >= 2:
            i1, i2 = opp[0].get('opponent',{}).get('id'), opp[1].get('opponent',{}).get('id')
            sb = {r.get('team_id'): r.get('score') for r in res}
            if i1 in sb and i2 in sb: return sb[i1], sb[i2]
    return None

async def _check_hl_result(mid):
    """Проверка завершённого хоккей-матча через Highlightly /matches/{id}."""
    headers = {"x-rapidapi-key": HIGHLIGHTLY_API_KEY, "Accept": "application/json"}
    resp = await fetch_json_with_retry(f"{HIGHLIGHTLY_HOCKEY_BASE}/matches/{mid}", headers=headers)
    if not resp: return None
    if isinstance(resp, list):                       # by-id может вернуть массив или объект — берём оба
        m = resp[0] if resp else None
    elif isinstance(resp, dict):
        m = resp['data'][0] if (isinstance(resp.get('data'), list) and resp['data']) else (resp if 'state' in resp else None)
    else:
        m = None
    if not m: return None
    st = m.get('state') or {}
    desc = (st.get('description') or '').lower(); report = (st.get('report') or '').lower()
    if desc in ('cancelled', 'abandoned', 'postponed'): return None
    finished = report in ('final','finished','aot','ot','so','ended','aet') or ('finished' in desc) or ('final' in desc)
    if not finished: return None
    score = st.get('score')
    cur = score.get('current') if isinstance(score, dict) else (score if isinstance(score, str) else '')
    parts = [p.strip() for p in str(cur).replace('–', '-').split('-')]
    if len(parts) != 2: return None
    try: return int(parts[0]), int(parts[1])
    except Exception: return None
      
async def _check_hk_result(gid):
    data = await fetch_json_with_retry("https://v1.hockey.api-sports.io/games", headers={"x-apisports-key": HOCKEY_API_SPORTS}, params={"id": gid})
    if data and data.get('response'):
        g = data['response'][0]
        st = g.get('game',{}).get('status',{}) or g.get('status',{})
        if st.get('short') in ('FT','AOT','OT','SO','Ended','Finished'):
            sc = g.get('scores',{}); h = sc.get('home',{}).get('total'); a = sc.get('away',{}).get('total')
            if h is not None and a is not None: return h, a
    return None

async def _check_bk_result(gid):
    data = await fetch_json_with_retry("https://v1.basketball.api-sports.io/games", headers={"x-apisports-key": BASKETBALL_API_KEY}, params={"id": gid})
    if data and data.get('response'):
        g = data['response'][0]
        st = g.get('game',{}).get('status',{}) or g.get('status',{})
        if st.get('short') in ('FT','AOT','OT','AP','Ended','Finished'):
            sc = g.get('scores',{}); h = sc.get('home',{}).get('total'); a = sc.get('away',{}).get('total')
            if h is not None and a is not None: return h, a
    return None

async def _check_mma_result(fid):
    data = await fetch_json_with_retry("https://v1.mma.api-sports.io/fights", headers={"x-apisports-key": MMA_API_KEY}, params={"id": fid})
    if not data or not data.get('response'): return None
    f = data['response'][0]
    st = f.get('fight',{}).get('status',{}) or f.get('status',{})
    if st.get('short') not in ('FT','Ended','Result','Decided','Draw','NC'): return None
    if st.get('short') in ('Draw','NC'): return (0, 0)
    w = f.get('fight',{}).get('winner') or f.get('winner')
    if w in ('home','Home'): return (1, 0)
    if w in ('away','Away'): return (0, 1)
    th, ta = f.get('teams',{}).get('home',{}), f.get('teams',{}).get('away',{})
    if th.get('winner') is True: return (1, 0)
    if ta.get('winner') is True: return (0, 1)
    sc = f.get('scores',{})
    if sc.get('home') is not None and sc.get('away') is not None: return sc['home'], sc['away']
    return None

async def check_and_update_finished_matches():
    db = await get_db()
    async with db.execute("SELECT match_id, sport, team1, team2, match_date FROM matches WHERE is_finished = 0 AND match_date < datetime('now', '-3 hours') AND match_date != ''") as c:
        matches = await c.fetchall()
        checkers = {'af_': _check_af_result, 'fd_': _check_fd_result, 'ps_': _check_ps_result,
                'hk_': _check_hk_result, 'hl_': _check_hl_result, 'bk_': _check_bk_result, 'mma_': _check_mma_result}
    for m in matches:
        mid, sport, t1, t2 = m[0], m[1], m[2], m[3]
        pref = next((p for p in checkers if mid.startswith(p)), None)
        if not pref: continue
        res = await checkers[pref](mid[len(pref):])
        if not res: continue
        h, a = res
        await db.execute("UPDATE matches SET team1_score = ?, team2_score = ?, is_finished = 1 WHERE match_id = ?", (h, a, mid))
        await update_team_ratings_from_result(sport, t1, t2, h, a)
        await analyze_prediction_accuracy(mid, 'home_win' if h > a else ('away_win' if h < a else 'draw'))
    await db.commit()

async def backfill_football_history():
    db = await get_db()
    async with db.execute("SELECT value FROM bootstrap_state WHERE key = 'football_backfill_done'") as c:
        if await c.fetchone(): return 0
    total, year = 0, datetime.now().year
    for code, _ in OPENLIGADB_LEAGUES:
        for season in [year, year-1]:
            data = await fetch_json_with_retry(f"https://www.openligadb.de/api/getmatchdata/{code}/{season}")
            if not data: continue
            for m in sorted([x for x in data if x.get('MatchIsFinished')], key=lambda x: x.get('MatchDateTime','')):
                r = m.get('MatchResults', [])
                if not r: continue
                h, a = r[-1].get('PointsTeam1'), r[-1].get('PointsTeam2')
                t1, t2 = m.get('Team1',{}).get('TeamName'), m.get('Team2',{}).get('TeamName')
                if h is None or a is None or not t1 or not t2: continue
                await update_team_ratings_from_result('football', t1, t2, h, a); total += 1
            await asyncio.sleep(1)
    await db.execute("INSERT INTO bootstrap_state (key, value, updated_at) VALUES ('football_backfill_done','1',datetime('now')) ON CONFLICT(key) DO UPDATE SET value='1', updated_at=datetime('now')")
    await db.commit(); return total
    # ==================== (импорт для безопасного HTML — имена команд/турниров) ====================
from html import escape as _html_escape
def _esc(s): return _html_escape(str(s or ""), quote=False)

# ==================== АНАЛИЗАТОРЫ ====================
async def analyze_live_football_match(match):
    t1 = await get_team_data(match['team1'], 'football'); t2 = await get_team_data(match['team2'], 'football')
    l1 = (t1['goals_scored_home'] + t2['goals_conceded_away']) / 2
    l2 = (t2['goals_scored_away'] + t1['goals_conceded_home']) / 2
    pw, pd, pl, po = LivePoissonModel.calculate_live_probabilities(l1, l2, match['score1'], match['score2'], match['minute'])
    sit = "Игра идет предсказуемо."
    if match['minute'] > 75 and match['score1'] == match['score2']: sit = "Ничья на последних минутах. Рассмотрите ТМ."
    elif match['score1'] < match['score2'] and t1['strength'] > t2['strength']: sit = "Фаворит проигрывает. Ожидается прессинг. Рассмотрите П1 или ТБ."
    return (f"🔴 <b>LIVE: {_esc(match['team1'])} {match['score1']}:{match['score2']} {_esc(match['team2'])}</b> ({match['minute']}')\n"
            f"🏆 <b>Турнир:</b> {_esc(match['tournament'])}\n\n📊 <b>Пересчет:</b>\n• П1: {pw}% | X: {pd}% | П2: {pl}%\n• ТБ 2.5: {po}%\n\n💡 <b>Ситуация:</b>\n{sit}\n")

async def analyze_live_esports_match(match):
    t1 = await get_team_data(match['team1'], 'esports'); t2 = await get_team_data(match['team2'], 'esports')
    r = EnsemblePredictor().predict(t1, t2, sport='esports')
    p1l = max(5, min(95, r['p1'] + (match['score1'] - match['score2']) * 15)); p2l = 100 - p1l
    sit = "Серия идет предсказуемо."
    if match['score2'] > match['score1'] and r['p1'] > r['p2']: sit = "Фаворит проигрывает в серии. Шанс зайти на высокий кэф."
    return (f"🔴 <b>LIVE: {_esc(match['team1'])} {match['score1']}:{match['score2']} {_esc(match['team2'])}</b>\n"
            f"🎮 <b>Игра:</b> {_esc(match.get('game','Game'))} | <b>Турнир:</b> {_esc(match['tournament'])}\n\n📊 <b>Оценка:</b>\n• До: П1={r['p1']}% | П2={r['p2']}%\n• В лайве: П1={p1l}% | П2={p2l}%\n\n💡 <b>Ситуация:</b>\n{sit}\n")

async def analyze_match(match):
    sport = match.get('sport', 'football')
    t1 = await get_team_data(match['team1'], sport); t2 = await get_team_data(match['team2'], sport)
    bookmaker_odds = await fetch_bookmaker_odds(match['team1'], match['team2'], sport)
    t1n, t2n, trn = _esc(match['team1']), _esc(match['team2']), _esc(match.get('tournament', ''))
    raw1, raw2 = match['team1'], match['team2']   # сырые имена — только для промпта ИИ (не HTML)

    # ---------- БАСКЕТБОЛ (двухисходная модель + честные линии тотала/форы) ----------
    if sport == 'basketball':
        lambda1 = round((t1['goals_scored_home'] + t2['goals_conceded_away']) / 2, 1)
        lambda2 = round((t2['goals_scored_away'] + t1['goals_conceded_home']) / 2, 1)
        p1 = BasketballModel.win_prob(t1, t2); p2 = round(100 - p1, 1)
        fl = BasketballModel.fair_lines(lambda1 + lambda2, lambda1 - lambda2)
        method = 'Basketball Model (Elo+Str+Form + Norm lines)'
        if p1 >= p2:
            rec, confidence, odds_val, rec_code = f"П1 ({t1n})", p1, bookmaker_odds.get('home', 0), 'P1'
        else:
            rec, confidence, odds_val, rec_code = f"П2 ({t2n})", p2, bookmaker_odds.get('away', 0), 'P2'
        avg_games = (t1['games_played'] + t2['games_played']) / 2
        kelly = KellyCriterion.calculate_kelly(confidence, odds_val, avg_games) if not bookmaker_odds.get('is_mock') and odds_val > 0 else 0
        ai_text = await generate_ai_explanation(raw1, raw2, rec, confidence,
                                                {'form1': t1['form'], 'form2': t2['form'], 'lambda1': lambda1, 'lambda2': lambda2, 'kelly': kelly})
        drop_text = f"\n🔥 <b>Дроп линии:</b> кэф упал с {bookmaker_odds['old_odds']} до {odds_val} (умные деньги грузят!)\n" if bookmaker_odds.get('is_dropping') and bookmaker_odds.get('old_odds', 0) > 0 else ""
        lines_text = (f"🏀 <b>Линии модели (тотал/фора):</b>\n"
                      f"• Ожидаемый тотал очков: ~<b>{fl['mu_total']}</b>\n"
                      f"• ТБ {fl['over_line']}: {fl['p_over']}% | ТМ {fl['under_line']}: {fl['p_under']}%\n"
                      f"• Спред в пользу хозяев: {fl['mu_margin']} → покрыть: хозяева {fl['p_cover_home']}% / гости {fl['p_cover_away']}%\n\n")
        analysis = (f"🏆 <b>{trn}</b>\n📊 П1={p1}% | П2={p2}%  (ничьих в баскетболе нет)\n"
                    f"📈 Кэф: {odds_val} | Kelly: {kelly}%\n{drop_text}\n{lines_text}{ai_text}\n")
        return {'analysis': analysis, 'probabilities': {'p1': p1, 'x': 0, 'p2': p2, 'method': method, 'components': None,
                'odds': odds_val, 'kelly': kelly, 'rec': rec, 'rec_code': rec_code, 'top_score': 'N/A', 'btts': 0, 'lines': fl},
                'recommendation': rec, 'confidence': confidence, 'bet_type': 'Исход'}

    # ---------- БОКС / ММА (исход боя, без тоталов) ----------
    if sport == 'mma':
        p1, x, p2 = MmaModel.predict(t1, t2)
        method = 'MMA/Boxing Model (Elo+Str+Form)'
        best = max(p1, x, p2)
        if best == p1:
            rec, confidence, odds_val, rec_code = f"П1 ({t1n})", p1, bookmaker_odds.get('home', 0), 'P1'
        elif best == p2:
            rec, confidence, odds_val, rec_code = f"П2 ({t2n})", p2, bookmaker_odds.get('away', 0), 'P2'
        else:
            rec, confidence, odds_val, rec_code = "Ничья (X)", x, bookmaker_odds.get('draw', 0), 'X'
        avg_games = (t1['games_played'] + t2['games_played']) / 2
        kelly = KellyCriterion.calculate_kelly(confidence, odds_val, avg_games) if not bookmaker_odds.get('is_mock') and odds_val > 0 else 0
        ai_text = await generate_ai_explanation(raw1, raw2, rec, confidence,
                                                {'form1': t1['form'], 'form2': t2['form'], 'lambda1': t1['strength'], 'lambda2': t2['strength'], 'kelly': kelly})
        drop_text = f"\n🔥 <b>Дроп линии:</b> кэф упал с {bookmaker_odds['old_odds']} до {odds_val} (умные деньги грузят!)\n" if bookmaker_odds.get('is_dropping') and bookmaker_odds.get('old_odds', 0) > 0 else ""
        analysis = (f"🏆 <b>{trn}</b>\n🥊 <b>Вероятности исхода боя:</b>\n📊 П1={p1}% | Ничья={x}% | П2={p2}%\n"
                    f"📈 Кэф: {odds_val} | Kelly: {kelly}%\n{drop_text}\n🧮 <b>Метод:</b> {method}\n\n{ai_text}\n")
        return {'analysis': analysis, 'probabilities': {'p1': p1, 'x': x, 'p2': p2, 'method': method, 'components': None,
                'odds': odds_val, 'kelly': kelly, 'rec': rec, 'rec_code': rec_code, 'top_score': 'N/A', 'btts': 0},
                'recommendation': rec, 'confidence': confidence, 'bet_type': 'Исход'}

    # ---------- ФУТБОЛ / ХОККЕЙ (ансамбль + Монте-Карло) ----------
    if sport == 'esports':
        lambda1, lambda2 = round(t1['goals_avg'], 2), round(t2['goals_avg'], 2)
        t1_data = {'goals_avg': lambda1, 'strength': t1['strength'], 'form': t1['form'], 'elo_rating': t1['elo_rating']}
        t2_data = {'goals_avg': lambda2, 'strength': t2['strength'], 'form': t2['form'], 'elo_rating': t2['elo_rating']}
    else:  # football / hockey — преимущество своей площадки
        ratio = t1['goals_scored_home'] / max(0.1, t1['goals_scored_away']) if t1['goals_scored_away'] > 0 else 1.2
        home_factor = max(1.0, min(1.30, 0.5 + ratio * 0.5))
        lambda1 = round(((t1['goals_scored_home'] + t2['goals_conceded_away']) / 2) * home_factor, 2)
        lambda2 = round(((t2['goals_scored_away'] + t1['goals_conceded_home']) / 2) * 0.95, 2)
        t1_data = {'goals_avg': lambda1, 'strength': t1['strength'] + round((home_factor - 1.0) * 50, 0), 'form': t1['form'], 'elo_rating': t1['elo_rating']}
        t2_data = {'goals_avg': lambda2, 'strength': t2['strength'], 'form': t2['form'], 'elo_rating': t2['elo_rating']}

    weights = await get_current_weights()
    result = EnsemblePredictor(weights_override=weights).predict(t1_data, t2_data, sport=sport, bookmaker_odds=bookmaker_odds)
    mc = result.get('mc') or {}; top_score = mc.get('top_score', '1:1')
    best = max(result['p1'], result.get('x', 0), result['p2'])
    if best == result['p1']:
        rec, confidence, odds_val, rec_code = f"П1 ({t1n})", result['p1'], bookmaker_odds.get('home', 0), 'P1'
    elif best == result['p2']:
        rec, confidence, odds_val, rec_code = f"П2 ({t2n})", result['p2'], bookmaker_odds.get('away', 0), 'P2'
    else:
        rec, confidence, odds_val, rec_code = "Ничья (X)", result['x'], bookmaker_odds.get('draw', 0), 'X'
    avg_games = (t1['games_played'] + t2['games_played']) / 2
    kelly = KellyCriterion.calculate_kelly(confidence, odds_val, avg_games) if not bookmaker_odds.get('is_mock') and odds_val > 0 else 0
    ai_text = await generate_ai_explanation(raw1, raw2, rec, confidence,
                                            {'form1': t1['form'], 'form2': t2['form'], 'lambda1': lambda1, 'lambda2': lambda2, 'kelly': kelly})
    drop_text = f"\n🔥 <b>Дроп линии:</b> кэф упал с {bookmaker_odds['old_odds']} до {odds_val} (умные деньги грузят!)\n" if bookmaker_odds.get('is_dropping') and bookmaker_odds.get('old_odds', 0) > 0 else ""

    if sport == 'esports':
        elo_diff = t1['elo_rating'] - t2['elo_rating']
        p1_map = 1 / (1 + 10 ** (-elo_diff/400)) * 100
        fmt = match.get('format') or 1
        mm = EsportsMapModel.calculate_maps(p1_map, fmt)
        mc_text = "🎮 <b>Рынки по картам:</b>\n"
        if fmt == 3:
            mc_text += (f"• Тотал карт больше 2.5: {mm.get('tb_2_5',0)}%\n"
                        f"• Фора 1 (-1.5): {mm.get('f1_minus_1_5',0)}% | Фора 2 (+1.5): {mm.get('f2_plus_1_5',0)}%\n"
                        f"• Фора 1 (+1.5): {mm.get('f1_plus_1_5',0)}% | Фора 2 (-1.5): {mm.get('f2_minus_1_5',0)}%\n\n")
        elif fmt == 5:
            mc_text += (f"• Тотал карт больше 3.5: {mm.get('tb_3_5',0)}%\n"
                        f"• Фора 1 (-1.5): {mm.get('f1_minus_1_5',0)}% | Фора 2 (+1.5): {mm.get('f2_plus_1_5',0)}%\n\n")
        else:
            mc_text += "• Формат: Bo1 (рынки по картам недоступны)\n\n"
    else:
        mc_text = (f"🎲 <b>Доп. рынки (Monte Carlo):</b>\n"
                   f"• Точный счет: <b>{top_score}</b> ({mc.get('top_score_prob',0)}%)\n"
                   f"• Обе забьют (ОЗ-Да): {mc.get('btts_prob',0)}%\n"
                   f"• Тотал больше 2.5: {mc.get('over_2_5_prob',0)}%\n"
                   f"• Двойной шанс (1X): {mc.get('dc_1x',0)}% | (12): {mc.get('dc_12',0)}%\n"
                   f"• Фора 1 (-1.5): {mc.get('ah1_-1.5',0)}% | Фора 2 (+1): {mc.get('ah2_+1',0)}%\n\n")

    analysis = (f"🏆 <b>{trn}</b>\n📊 П1={result['p1']}% | X={result.get('x',0)}% | П2={result['p2']}%\n"
                f"📈 Кэф: {odds_val} | Kelly: {kelly}%\n{drop_text}\n{mc_text}{ai_text}\n")
    return {'analysis': analysis, 'probabilities': {'p1': result['p1'], 'x': result.get('x', 0), 'p2': result['p2'],
            'method': result['method'], 'components': result.get('components'), 'odds': odds_val, 'kelly': kelly,
            'rec': rec, 'rec_code': rec_code, 'top_score': top_score if sport != 'esports' else 'N/A',
            'btts': mc.get('btts_prob', 0) if sport != 'esports' else 0},
            'recommendation': rec, 'confidence': confidence, 'bet_type': 'Исход'}

async def collect_and_analyze_job():
    db = await get_db()
    matches = list(await fetch_football_matches())      # копия — не мутируем кэш
    matches.extend(await fetch_pandascore_matches())
    matches.extend(await fetch_hockey_matches())
    matches.extend(await fetch_basketball_matches())    # НОВОЕ
    matches.extend(await fetch_mma_matches())           # НОВОЕ
    for m in matches:
        try:
            pred = await analyze_match(m)
            if pred and pred['confidence'] >= MIN_CONFIDENCE:
                await db.execute("INSERT OR REPLACE INTO matches (match_id, sport, team1, team2, match_date, tournament) VALUES (?, ?, ?, ?, ?, ?)",
                                 (m['id'], m['sport'], m['team1'], m['team2'], _normalize_date(m.get('date', '')), m.get('tournament', 'Unknown')))
                await save_prediction(m['id'], m['sport'], m['team1'], m['team2'], m.get('tournament', 'Unknown'),
                                      pred['analysis'], pred['probabilities'], pred['recommendation'], pred['confidence'], pred['bet_type'])
        except Exception as e:
            logger.error(f"Error analyzing {m.get('team1')}: {e}")
    await db.commit()

# ==================== ИНТЕРФЕЙС БОТА (5 видов спорта) ====================
def get_main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔥 Live Матчи", callback_data="live_matches")],
        [InlineKeyboardButton(text="📅 Прогнозы на сегодня", callback_data="today")],
        [InlineKeyboardButton(text="⚽️ Футбол", callback_data="sport_football"),
         InlineKeyboardButton(text="🏒 Хоккей", callback_data="sport_hockey"),
         InlineKeyboardButton(text="🎮 Киберспорт", callback_data="sport_esports")],
        [InlineKeyboardButton(text="🏀 Баскетбол", callback_data="sport_basketball"),
         InlineKeyboardButton(text="🥊 Бокс/ММА", callback_data="sport_mma")],
        [InlineKeyboardButton(text="📚 Обучение модели", callback_data="learning")],
        [InlineKeyboardButton(text="💰 Мой банк", callback_data="my_bank"),
         InlineKeyboardButton(text="⚙️ Настройки", callback_data="settings")]
    ])

def get_back_button():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_start")]])

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await add_user(message.from_user.id, message.from_user.username or "unknown")
    await message.answer("🤖 ML-бот прогнозов: футбол, хоккей, киберспорт, баскетбол, бокс/ММА. Виртуальный банк, Монте-Карло, ИИ и самообучение.", parse_mode="HTML", reply_markup=get_main_keyboard())

@dp.callback_query(F.data == "back_to_start")
async def back_to_start(callback: types.CallbackQuery):
    await callback.message.edit_text("🏠 <b>Главное меню</b>", parse_mode="HTML", reply_markup=get_main_keyboard())
    await callback.answer()

@dp.callback_query(F.data == "learning")
async def show_learning(callback: types.CallbackQuery):
    await callback.answer("📚 Собираю статистику обучения...")
    await callback.message.edit_text(await get_learning_dashboard(), parse_mode="HTML", reply_markup=get_back_button())

@dp.callback_query(F.data == "my_bank")
async def show_bank(callback: types.CallbackQuery):
    db = await get_db(); user_id = callback.from_user.id
    async with db.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,)) as c:
        row = await c.fetchone()
    if not row:
        await add_user(user_id, callback.from_user.username or "unknown"); balance = INITIAL_VIRTUAL_BALANCE
    else:
        balance = row[0]
    async with db.execute("""SELECT COUNT(*) AS total, SUM(CASE WHEN status=1 THEN 1 ELSE 0 END) AS won,
            SUM(bet_amount) AS staked,
            SUM(CASE WHEN status=1 THEN bet_amount*(odds-1) WHEN status=2 THEN -bet_amount ELSE 0 END) AS profit
            FROM virtual_bets WHERE user_id = ? AND status != 0""", (user_id,)) as c:
        s = await c.fetchone()
    total = s[0] or 0; won = int(s[1] or 0); staked = s[2] or 0; profit = s[3] or 0
    winrate = round(won/total*100, 1) if total else 0
    roi = round(profit/staked*100, 1) if staked else 0
    text = (f"💰 <b>Виртуальный банк</b>\n\n💵 Баланс: <b>{balance:.2f} у.е.</b>\n📊 Ставок сыграно: {total}\n✅ Выиграно: {won}\n"
            f"🎯 Win-Rate: <b>{winrate}%</b>\n📈 ROI: <b>{roi}%</b> (прибыль {profit:+.2f} у.е.)\n\nТестируй бота без риска реальными деньгами!")
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=get_back_button())
    await callback.answer()

@dp.callback_query(F.data == "live_matches")
async def show_live_matches(callback: types.CallbackQuery):
    await callback.answer("🔴 Загружаю live матчи...")
    fb = await fetch_live_football_matches(); es = await fetch_live_esports_matches()
    if not fb and not es:
        await callback.message.edit_text("😔 Сейчас нет live матчей в топ-лигах.", reply_markup=get_back_button()); return
    text = ""
    if fb:
        text += "⚽️ <b>Футбол (Live)</b>\n\n"
        for m in fb: text += await analyze_live_football_match(m) + "\n➖➖➖➖➖➖➖➖➖\n"
    if es:
        text += "\n🎮 <b>Киберспорт (Live)</b>\n\n"
        for m in es: text += await analyze_live_esports_match(m) + "\n➖➖➖➖➖➖➖\n"
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=get_back_button())

@dp.callback_query(F.data == "today")
async def show_today(callback: types.CallbackQuery):
    await callback.answer("⏳ Загружаю прогнозы...")
    preds = await get_today_predictions()
    if not preds:
        await callback.message.edit_text("😔 На сегодня пока нет прогнозов.", reply_markup=get_back_button()); return
    text = f"📅 <b>Прогнозы на сегодня ({len(preds)} шт.)</b>\n\n"
    for i, p in enumerate(preds[:10], 1):
        text += f"{i}. <b>{_esc(p[2])} vs {_esc(p[3])}</b>\n   💰 {p[7]} ({p[8]}%)\n\n"
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=get_back_button())

def _fetch_for_sport(sport):
    return {'football': fetch_football_matches, 'hockey': fetch_hockey_matches, 'basketball': fetch_basketball_matches,
            'mma': fetch_mma_matches, 'esports': fetch_pandascore_matches}.get(sport, fetch_pandascore_matches)

async def show_sport_page(callback, sport, page):
    names = {'football': '⚽️ Футбол', 'hockey': '🏒 Хоккей', 'esports': '🎮 Киберспорт',
             'basketball': '🏀 Баскетбол', 'mma': '🥊 Бокс/ММА'}
    matches = await _fetch_for_sport(sport)()
    if not matches:
        await callback.message.edit_text("Матчи не найдены.", reply_markup=get_back_button()); return
    per = 7; start = page*per; end = start+per
    kb = [[InlineKeyboardButton(text=f"{m['team1']} vs {m['team2']}", callback_data=f"match_{m['id']}_{sport}")] for m in matches[start:end]]
    nav = []
    if page > 0: nav.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"page_{sport}_{page-1}"))
    if end < len(matches): nav.append(InlineKeyboardButton(text="Вперед ➡️", callback_data=f"page_{sport}_{page+1}"))
    if nav: kb.append(nav)
    kb.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_start")])
    await callback.message.edit_text(f"{names.get(sport, sport)} — Страница {page+1} (выбери матч):", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data.startswith("sport_"))
async def show_sport(callback: types.CallbackQuery):
    sport = callback.data.split("_", 1)[1]
    if sport not in ALLOWED_SPORTS:
        await callback.answer("Неизвестный раздел.", show_alert=True); return
    await callback.answer("🔍 Ищу матчи...")
    await show_sport_page(callback, sport, 0)

@dp.callback_query(F.data.startswith("page_"))
async def change_page(callback: types.CallbackQuery):
    _, sport, page_str = callback.data.split("_")
    await callback.answer(f"Страница {int(page_str)+1}")
    await show_sport_page(callback, sport, int(page_str))

@dp.callback_query(F.data.startswith("match_"))
async def show_match_analysis(callback: types.CallbackQuery):
    parts = callback.data.split("_", 1)[1].rsplit("_", 1)   # устойчиво к подчёркиваниям в id (af_/hk_/bk_/mma_/ps_)
    match_id, sport = parts[0], parts[1]
    await callback.answer("🧮 Анализирую...")
    matches = await _fetch_for_sport(sport)()
    match = next((m for m in matches if m['id'] == match_id), None)
    if not match:
        await callback.message.edit_text("Матч не найден (возможно, кэш устарел — вернитесь в раздел).", reply_markup=get_back_button()); return
    pred = await analyze_match(match)
    if not pred:
        await callback.message.edit_text("Не удалось рассчитать вероятности.", reply_markup=get_back_button()); return
    text = f"🏆 <b>{_esc(match['team1'])} vs {_esc(match['team2'])}</b>\n\n{pred['analysis']}"
    kb = []
    odds = pred['probabilities'].get('odds', 0)
    rec_code = pred['probabilities'].get('rec_code', 'P1')
    rec_label = {'P1': 'П1', 'P2': 'П2', 'X': 'Ничья'}.get(rec_code, rec_code)
    if odds > 1.0:
        balance = await get_balance(callback.from_user.id)
        kelly_pct = pred['probabilities'].get('kelly', 0)
        bet_amount = max(10.0, round(balance * (kelly_pct/100.0), 2)) if kelly_pct > 0 else 100.0
        bet_amount = min(bet_amount, balance)
        cb_data = f"bet|{match_id}|{odds}|{rec_code}|{bet_amount}"
        btn_text = f"Поставить {bet_amount:.2f} у.е. на {rec_label} (Кэф {odds})"
        if kelly_pct > 0: btn_text += f" | Value {kelly_pct}%"
        kb.append([InlineKeyboardButton(text=btn_text, callback_data=cb_data)])
    kb.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_start")])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data.startswith("bet|"))
async def process_virtual_bet(callback: types.CallbackQuery):
    try:
        _, match_id, odds_str, rec_code, amount_str = callback.data.split("|")
        odds, amount = float(odds_str), float(amount_str)
    except Exception:
        await callback.answer("Ошибка обработки ставки.", show_alert=True); return
    rec_label = {'P1': 'П1', 'P2': 'П2', 'X': 'Ничья'}.get(rec_code, rec_code)
    ok = await place_virtual_bet(callback.from_user.id, match_id, amount, odds, f"{rec_label} ({match_id})")
    if not ok:
        await callback.answer("Недостаточно средств на балансе!", show_alert=True); return
    await callback.answer(f"Ставка {amount:.2f} у.е. на {rec_label} принята!", show_alert=True)
    nb = await get_balance(callback.from_user.id)
    await callback.message.edit_text(f"✅ Ставка принята!\nВаш баланс: {nb:.2f} у.е.", reply_markup=get_back_button())

@dp.callback_query(F.data == "settings")
async def show_settings(callback: types.CallbackQuery):
    db = await get_db()
    async with db.execute("SELECT football, hockey, esports, basketball, mma FROM user_settings WHERE user_id = ?", (callback.from_user.id,)) as c:
        s = await c.fetchone()
    if not s:
        await add_user(callback.from_user.id, callback.from_user.username or "unknown")
        async with db.execute("SELECT football, hockey, esports, basketball, mma FROM user_settings WHERE user_id = ?", (callback.from_user.id,)) as c:
            s = await c.fetchone()
    if not s: s = (1, 1, 1, 1, 1)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"⚽️ Футбол [{'✅' if s[0] else '❌'}]", callback_data=f"set_football_{s[0]}"),
         InlineKeyboardButton(text=f"🏒 Хоккей [{'✅' if s[1] else '❌'}]", callback_data=f"set_hockey_{s[1]}")],
        [InlineKeyboardButton(text=f"🎮 Киберспорт [{'✅' if s[2] else '❌'}]", callback_data=f"set_esports_{s[2]}"),
         InlineKeyboardButton(text=f"🏀 Баскетбол [{'✅' if s[3] else '❌'}]", callback_data=f"set_basketball_{s[3]}")],
        [InlineKeyboardButton(text=f"🥊 Бокс/ММА [{'✅' if s[4] else '❌'}]", callback_data=f"set_mma_{s[4]}")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_start")]
    ])
    await callback.message.edit_text("⚙️ <b>Настройки рассылки</b>", parse_mode="HTML", reply_markup=kb)

@dp.callback_query(F.data.startswith("set_"))
async def toggle_setting(callback: types.CallbackQuery):
    _, sport, val = callback.data.split("_")
    if sport not in ALLOWED_SPORTS:
        await callback.answer("Неизвестный спорт.", show_alert=True); return
    db = await get_db()
    await db.execute(f"UPDATE user_settings SET {sport} = ? WHERE user_id = ?", (0 if int(val) == 1 else 1, callback.from_user.id))
    await db.commit()
    await show_settings(callback)

# ==================== ПЛАНИРОВЩИК И ЗАПУСК ====================
async def send_predictions_job():
    preds = await get_unsent_predictions(limit=PREDICTIONS_PER_HOUR)
    if not preds: return
    for p in preds:
        match_id, sport, _t1, _t2, _tr, analysis, _pj, rec, conf, _bt = p
        text = f"💰 <b>Прогноз:</b> {rec} ({conf}%)\n{analysis}"
        for uid in await get_users_for_sport(sport):
            try:
                # ЗАДАЧА №1: кнопка «Главное меню» под каждым прогнозом в рассылке
                await bot.send_message(uid, text, parse_mode="HTML", reply_markup=get_back_button())
                await asyncio.sleep(0.05)
            except TelegramRetryAfter as e:
                logger.warning(f"Flood control. Жду {e.retry_after} сек."); await asyncio.sleep(e.retry_after)
            except (TelegramForbiddenError, TelegramBadRequest) as e:
                logger.error(f"Не отправить {uid} (заблокировал бота?): {e}")
            except Exception as e:
                logger.error(f"Ошибка отправки {uid}: {e}")
        await mark_prediction_sent(match_id, SYSTEM_USER_ID)

async def check_results_job():
    try: await check_and_update_finished_matches()
    except Exception as e: logger.error(f"check_results_job: {e}")

async def recompute_weights_job():
    try: await compute_adaptive_weights()
    except Exception as e: logger.error(f"recompute_weights_job: {e}")

async def _safe_backfill():
    try:
        n = await backfill_football_history(); logger.info(f"Backfill завершён: обработано {n} матчей.")
    except Exception as e:
        logger.error(f"Backfill failed: {e}")

async def main():
    global bot
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN не задан.")
    for name, val in [("OPENAI_API_KEY", OPENAI_API_KEY), ("FOOTBALL_API_KEY", FOOTBALL_API_KEY),
                      ("FOOTBALLDATA_KEY", FOOTBALLDATA_KEY), ("PANDASCORE_API_KEY", PANDASCORE_API_KEY),
                      ("THE_ODDS_API_KEY", THE_ODDS_API_KEY), ("HOCKEY_API_SPORTS", HOCKEY_API_SPORTS),
                      ("BASKETBALL_API_KEY", BASKETBALL_API_KEY), ("MMA_API_KEY", MMA_API_KEY)]:
        if not val: logger.warning(f"{name} не задан — раздел будет на мок-данных/пустым.")
    bot = Bot(token=TELEGRAM_TOKEN)
    await init_db()
    logger.info("📚 Запускаю подгрузку истории матчей в фоне...")
    asyncio.create_task(_safe_backfill())
    scheduler = AsyncIOScheduler()
    scheduler.add_job(collect_and_analyze_job, IntervalTrigger(hours=1), next_run_time=datetime.now() + timedelta(minutes=1))
    scheduler.add_job(check_results_job, IntervalTrigger(hours=2), next_run_time=datetime.now() + timedelta(minutes=5))
    scheduler.add_job(send_predictions_job, IntervalTrigger(hours=1), next_run_time=datetime.now() + timedelta(minutes=10))
    scheduler.add_job(recompute_weights_job, IntervalTrigger(hours=24), next_run_time=datetime.now() + timedelta(minutes=15))
    scheduler.add_job(garbage_collector_job, IntervalTrigger(days=7))
    scheduler.start()
    logger.info("✅ Бот готов к работе (5 видов спорта)!")
    try:
        await dp.start_polling(bot)
    finally:
        if _db_conn: await _db_conn.close()
        if _http_session and not _http_session.closed: await _http_session.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("🛑 Бот остановлен")
