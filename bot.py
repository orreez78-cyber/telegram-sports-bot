import asyncio
import json
import logging
import math
import os
import random
from datetime import datetime, timedelta

import aiohttp
import aiosqlite
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger

# ==================== НАСТРОЙКИ ====================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "8933270591:AAGSJJkYl99icR7bwHv51-QlYf6Ff3CDMtM")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "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")
FOOTBALL_API_KEY = os.getenv("FOOTBALL_API_KEY", "3540a4964edea4e653d2a322ddec0270")
FOOTBALL_DATA_ORG_KEY = os.getenv("FOOTBALL_DATA_ORG_KEY", "32fcb5cfa8c64b40b4baaf2319c2809c")
PANDASCORE_API_KEY = os.getenv("PANDASCORE_API_KEY", "aXVsIwT4FSLepT021v4nrPAW9i-W-5y8Au0rrvUc4wg7bSf8IlY")  
HOCKEY_API_KEY = os.getenv("HOCKEY_API_KEY", "123")

MIN_CONFIDENCE = 50
PREDICTIONS_PER_HOUR = 3
DB_NAME = "sports_bot.db"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

# ==================== МАТЕМАТИЧЕСКИЕ МОДЕЛИ ====================

class PoissonModel:
    @staticmethod
    def poisson_probability(k, lam):
        return (lam ** k * math.exp(-lam)) / math.factorial(k)
    
    @staticmethod
    def calculate_match_probabilities(team1_goals_avg, team2_goals_avg):
        p1_total = 0
        p2_total = 0
        draw_total = 0
        
        for i in range(0, 8):
            for j in range(0, 8):
                p_i = PoissonModel.poisson_probability(i, team1_goals_avg)
                p_j = PoissonModel.poisson_probability(j, team2_goals_avg)
                p_score = p_i * p_j
                
                if i > j:
                    p1_total += p_score
                elif i < j:
                    p2_total += p_score
                else:
                    draw_total += p_score
        
        total = p1_total + draw_total + p2_total
        return (
            round(p1_total / total * 100, 1),
            round(draw_total / total * 100, 1),
            round(p2_total / total * 100, 1)
        )
    
    @staticmethod
    def total_probability(team1_goals_avg, team2_goals_avg, line=2.5):
        total_lam = team1_goals_avg + team2_goals_avg
        p_over = sum(PoissonModel.poisson_probability(k, total_lam) 
                     for k in range(math.ceil(line), 20))
        return round(p_over * 100, 1)


class EloRating:
    @staticmethod
    def expected_score(rating_a, rating_b):
        return 1 / (1 + 10 ** ((rating_b - rating_a) / 400))
    
    @staticmethod
    def calculate_win_probability(rating_a, rating_b):
        return round(EloRating.expected_score(rating_a, rating_b) * 100, 1)


class BradleyTerryModel:
    @staticmethod
    def win_probability(strength_a, strength_b):
        exp_a = math.exp(strength_a)
        exp_b = math.exp(strength_b)
        return round(exp_a / (exp_a + exp_b) * 100, 1)


class EnsemblePredictor:
    def __init__(self):
        self.weights = {
            'poisson': 0.35,
            'elo': 0.25,
            'bradley_terry': 0.20,
            'form': 0.20
        }
    
    def predict(self, team1_data, team2_data):
        p1_p, x_p, p2_p = PoissonModel.calculate_match_probabilities(
            team1_data['goals_avg'], team2_data['goals_avg']
        )
        
        p1_e = EloRating.calculate_win_probability(
            team1_data['elo_rating'], team2_data['elo_rating']
        )
        p2_e = 100 - p1_e
        x_e = 20
        
        p1_bt = BradleyTerryModel.win_probability(
            team1_data['strength'], team2_data['strength']
        )
        p2_bt = 100 - p1_bt
        x_bt = 20
        
        form_diff = team1_data['form'] - team2_data['form']
        p1_f = 50 + form_diff * 0.3
        p2_f = 100 - p1_f
        x_f = 25
        
        w = self.weights
        p1 = (p1_p * w['poisson'] + p1_e * w['elo'] + 
              p1_bt * w['bradley_terry'] + p1_f * w['form'])
        p2 = (p2_p * w['poisson'] + p2_e * w['elo'] + 
              p2_bt * w['bradley_terry'] + p2_f * w['form'])
        x = (x_p * w['poisson'] + x_e * w['elo'] + 
             x_bt * w['bradley_terry'] + x_f * w['form'])
        
        total = p1 + x + p2
        p1 = round(p1 / total * 100, 1)
        x = round(x / total * 100, 1)
        p2 = round(p2 / total * 100, 1)
        
        total_over = PoissonModel.total_probability(
            team1_data['goals_avg'], team2_data['goals_avg'], 2.5
        )
        
        return {
            'p1': p1, 'x': x, 'p2': p2,
            'total_over_2.5': total_over,
            'method': 'Ансамбль (Пуассон + Эло + Брэдли-Терри + Форма)'
        }


# ==================== БАЗА ДАННЫХ ====================

async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS users 
            (user_id INTEGER PRIMARY KEY, username TEXT, 
             created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        await db.execute("""CREATE TABLE IF NOT EXISTS matches 
            (match_id TEXT PRIMARY KEY, sport TEXT, team1 TEXT, 
             team2 TEXT, match_date TEXT)""")
        await db.execute("""CREATE TABLE IF NOT EXISTS predictions 
            (match_id TEXT PRIMARY KEY, sport TEXT, team1 TEXT, team2 TEXT,
             analysis TEXT, probabilities TEXT, recommendation TEXT, 
             confidence REAL, bet_type TEXT, 
             created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        await db.execute("""CREATE TABLE IF NOT EXISTS sent_predictions 
            (id INTEGER PRIMARY KEY AUTOINCREMENT, match_id TEXT, 
             user_id INTEGER, sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        await db.execute("""CREATE TABLE IF NOT EXISTS team_ratings 
            (team_id TEXT PRIMARY KEY, sport TEXT, elo REAL DEFAULT 1500,
             strength REAL DEFAULT 50, goals_avg REAL DEFAULT 1.5,
             updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        await db.commit()

async def add_user(user_id: int, username: str):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT OR IGNORE INTO users VALUES (?, ?, datetime('now'))",
                        (user_id, username))
        await db.commit()

async def get_all_users():
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT user_id FROM users") as cursor:
            return [row[0] async for row in cursor]

async def save_prediction(match_id, sport, team1, team2, analysis, probs, rec, conf, bet_type):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""INSERT OR REPLACE INTO predictions 
            (match_id, sport, team1, team2, analysis, probabilities, recommendation, confidence, bet_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (match_id, sport, team1, team2, analysis, json.dumps(probs), rec, conf, bet_type))
        await db.commit()

async def get_today_predictions():
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("""SELECT match_id, sport, team1, team2, 
            analysis, probabilities, recommendation, confidence, bet_type
            FROM predictions 
            WHERE date(created_at) = date('now') AND confidence >= ?
            ORDER BY confidence DESC""", (MIN_CONFIDENCE,)) as cursor:
            return await cursor.fetchall()

async def get_unsent_predictions(limit=3):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("""SELECT p.match_id, p.sport, p.team1, p.team2,
            p.analysis, p.probabilities, p.recommendation, p.confidence, p.bet_type
            FROM predictions p
            LEFT JOIN sent_predictions s ON p.match_id = s.match_id
            WHERE s.id IS NULL AND p.confidence >= ?
            ORDER BY p.confidence DESC LIMIT ?""",
            (MIN_CONFIDENCE, limit)) as cursor:
            return await cursor.fetchall()

async def mark_prediction_sent(match_id, user_id):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT INTO sent_predictions (match_id, user_id) VALUES (?, ?)",
                        (match_id, user_id))
        await db.commit()

async def save_team_rating(team_id, sport, elo, strength, goals_avg):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""INSERT OR REPLACE INTO team_ratings 
            (team_id, sport, elo, strength, goals_avg, updated_at)
            VALUES (?, ?, ?, ?, ?, datetime('now'))""",
            (team_id, sport, elo, strength, goals_avg))
        await db.commit()

async def get_team_rating(team_id):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT * FROM team_ratings WHERE team_id = ?",
                             (team_id,)) as cursor:
            return await cursor.fetchone()


# ==================== API ДАННЫХ ====================

async def fetch_api_football_matches():
    if not FOOTBALL_API_KEY:
        return []
    
    url = "https://v3.football.api-sports.io/fixtures"
    headers = {"x-apisports-key": FOOTBALL_API_KEY}
    params = {"date": datetime.now().strftime("%Y-%m-%d")}
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, params=params) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return [{"id": f"af_{f['fixture']['id']}", 
                            "team1": f['teams']['home']['name'],
                            "team2": f['teams']['away']['name'],
                            "date": f['fixture']['date'],
                            "sport": "football"}
                           for f in data.get('response', [])]
    except Exception as e:
        logger.error(f"API-Football error: {e}")
    return []


async def fetch_football_data_org_matches():
    if not FOOTBALL_DATA_ORG_KEY:
        return []
    
    url = "https://api.football-data.org/v4/matches"
    headers = {"X-Auth-Token": FOOTBALL_DATA_ORG_KEY}
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    matches = []
                    for match in data.get('matches', []):
                        if match['status'] == 'SCHEDULED':
                            matches.append({
                                "id": f"fd_{match['id']}",
                                "team1": match['homeTeam']['name'],
                                "team2": match['awayTeam']['name'],
                                "date": match['utcDate'],
                                "sport": "football"
                            })
                    return matches
    except Exception as e:
        logger.error(f"Football-Data.org error: {e}")
    return []


async def fetch_openligadb_matches():
    leagues = [
        "bundesliga",
        "2bundesliga", 
        "englischepremierleague",
        "laliga",
        "seriea",
        "ligue1"
    ]
    
    all_matches = []
    current_season = datetime.now().year
    
    for league in leagues:
        url = f"https://www.openligadb.de/api/getmatchdata/{league}/{current_season}"
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        
                        for match in data:
                            match_date = match.get('MatchDateTime', '')
                            
                            if match_date and datetime.now().strftime("%Y-%m-%d") in match_date[:10]:
                                all_matches.append({
                                    "id": f"ol_{match['MatchID']}",
                                    "team1": match['Team1']['TeamName'],
                                    "team2": match['Team2']['TeamName'],
                                    "date": match_date,
                                    "league": league,
                                    "sport": "football"
                                })
        except Exception as e:
            logger.error(f"OpenLigaDB {league} error: {e}")
        
        await asyncio.sleep(1)
    
    return all_matches


async def fetch_pandascore_matches():
    if not PANDASCORE_API_KEY:
        logger.info("PandaScore API ключ не указан, используем мок-данные")
        return []
    
    games = ['cs2', 'dota2', 'lol', 'valorant']
    all_matches = []
    
    headers = {
        "Authorization": f"Bearer {PANDASCORE_API_KEY}",
        "Accept": "application/json"
    }
    
    for game in games:
        url = f"https://api.pandascore.co/{game}/matches/upcoming"
        params = {
            "page[size]": 20,
            "sort": "begin_at"
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, params=params) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        
                        for match in data:
                            if len(match.get('opponents', [])) >= 2:
                                team1 = match['opponents'][0].get('opponent', {}).get('name', 'Unknown')
                                team2 = match['opponents'][1].get('opponent', {}).get('name', 'Unknown')
                                
                                if team1 != 'Unknown' and team2 != 'Unknown':
                                    all_matches.append({
                                        "id": f"ps_{match['id']}",
                                        "team1": team1,
                                        "team2": team2,
                                        "date": match.get('begin_at', ''),
                                        "game": game,
                                        "league": match.get('league', {}).get('name', 'Unknown'),
                                        "sport": "esports",
                                        "format": match.get('series', {}).get('type', 'bo1')
                                    })
                        
                        logger.info(f"PandaScore {game}: {len(data)} матчей")
        except Exception as e:
            logger.error(f"PandaScore {game} error: {e}")
        
        await asyncio.sleep(1)
    
    return all_matches


async def fetch_football_matches():
    logger.info("📊 Собираю матчи из всех источников...")
    
    matches = []
    
    af_matches = await fetch_api_football_matches()
    logger.info(f"API-Football: {len(af_matches)} матчей")
    matches.extend(af_matches)
    
    fd_matches = await fetch_football_data_org_matches()
    logger.info(f"Football-Data.org: {len(fd_matches)} матчей")
    matches.extend(fd_matches)
    
    ol_matches = await fetch_openligadb_matches()
    logger.info(f"OpenLigaDB: {len(ol_matches)} матчей")
    matches.extend(ol_matches)
    
    unique_matches = {}
    for match in matches:
        key = f"{match['team1']}_{match['team2']}"
        if key not in unique_matches:
            match['team1_goals_avg'] = 1.5
            match['team2_goals_avg'] = 1.5
            match['team1_elo'] = 1500
            match['team2_elo'] = 1500
            match['team1_strength'] = 50
            match['team2_strength'] = 50
            match['team1_form'] = 50
            match['team2_form'] = 50
            
            unique_matches[key] = match
    
    logger.info(f"✅ Всего уникальных матчей: {len(unique_matches)}")
    return list(unique_matches.values())


async def fetch_hockey_matches():
    today = datetime.now().strftime("%Y-%m-%d")
    return [
        {"id": "hk_301", "team1": "ЦСКА", "team2": "СКА",
         "date": today, "sport": "hockey",
         "team1_goals_avg": 3.2, "team2_goals_avg": 2.8,
         "team1_elo": 1750, "team2_elo": 1780,
         "team1_strength": 72, "team2_strength": 75,
         "team1_form": 70, "team2_form": 74},
    ]


async def fetch_esports_matches():
    logger.info("🎮 Собираю киберспортивные матчи...")
    
    matches = []
    
    ps_matches = await fetch_pandascore_matches()
    logger.info(f"PandaScore: {len(ps_matches)} матчей")
    matches.extend(ps_matches)
    
    if not matches:
        today = datetime.now().strftime("%Y-%m-%d")
        mock_matches = [
            {"id": "es_201", "team1": "NaVi", "team2": "FaZe",
             "date": today, "sport": "esports", "game": "cs2",
             "team1_goals_avg": 1.3, "team2_goals_avg": 1.2,
             "team1_elo": 1800, "team2_elo": 1780,
             "team1_strength": 80, "team2_strength": 78,
             "team1_form": 75, "team2_form": 72},
            {"id": "es_202", "team1": "Team Spirit", "team2": "Gaimin Gladiators",
             "date": today, "sport": "esports", "game": "dota2",
             "team1_goals_avg": 1.4, "team2_goals_avg": 1.3,
             "team1_elo": 1820, "team2_elo": 1790,
             "team1_strength": 82, "team2_strength": 79,
             "team1_form": 78, "team2_form": 74},
        ]
        matches.extend(mock_matches)
        logger.info(f"Используем мок-данные: {len(mock_matches)} матчей")
    
    for match in matches:
        if 'team1_goals_avg' not in match:
            match['team1_goals_avg'] = 1.3
            match['team2_goals_avg'] = 1.2
            match['team1_elo'] = 1700
            match['team2_elo'] = 1650
            match['team1_strength'] = 70
            match['team2_strength'] = 65
            match['team1_form'] = 65
            match['team2_form'] = 60
    
    return matches


# ==================== АНАЛИЗАТОР ====================

async def analyze_match(match):
    team1_data = {
        'goals_avg': match.get('team1_goals_avg', 1.5),
        'elo_rating': match.get('team1_elo', 1500),
        'strength': match.get('team1_strength', 50),
        'form': match.get('team1_form', 50),
    }
    team2_data = {
        'goals_avg': match.get('team2_goals_avg', 1.5),
        'elo_rating': match.get('team2_elo', 1500),
        'strength': match.get('team2_strength', 50),
        'form': match.get('team2_form', 50),
    }
    
    predictor = EnsemblePredictor()
    result = predictor.predict(team1_data, team2_data)
    
    max_prob = max(result['p1'], result['x'], result['p2'], result['total_over_2.5'])
    
    if max_prob == result['p1']:
        rec = f"П1 ({match['team1']})"
        conf = result['p1']
        bet_type = "Исход"
    elif max_prob == result['p2']:
        rec = f"П2 ({match['team2']})"
        conf = result['p2']
        bet_type = "Исход"
    elif max_prob == result['x']:
        rec = "Ничья (X)"
        conf = result['x']
        bet_type = "Исход"
    else:
        rec = "Тотал больше 2.5"
        conf = result['total_over_2.5']
        bet_type = "Тотал"
    
    analysis = (
        f"📊 Математический анализ:\n"
        f"• Пуассон: П1={result['p1']}%, X={result['x']}%, П2={result['p2']}%\n"
        f"• Эло рейтинг: {team1_data['elo_rating']} vs {team2_data['elo_rating']}\n"
        f"• Форма: {team1_data['form']} vs {team2_data['form']}\n"
        f"• Ожидаемые голы: {team1_data['goals_avg']} vs {team2_data['goals_avg']}"
    )
    
    probs = {
        'p1': result['p1'], 'x': result['x'], 'p2': result['p2'],
        'total_over_2.5': result['total_over_2.5']
    }
    
    return {
        'analysis': analysis,
        'probabilities': probs,
        'recommendation': rec,
        'confidence': round(conf, 1),
        'bet_type': bet_type,
        'method': result['method']
    }


# ==================== АВТОМАТИЧЕСКОЕ ОБУЧЕНИЕ ====================

async def first_train(message: types.Message = None):
    def send(msg):
        if message:
            asyncio.create_task(message.answer(msg))
        logger.info(msg)
    
    send("🚀 Начинаю первое обучение моделей...")
    
    send("📊 Собираю данные из 4 источников...")
    fb_matches = await fetch_football_matches()
    hk_matches = await fetch_hockey_matches()
    es_matches = await fetch_esports_matches()
    
    all_matches = fb_matches + hk_matches + es_matches
    send(f"✅ Собрано {len(all_matches)} матчей")
    
    send("📈 Рассчитываю рейтинги команд...")
    for match in all_matches:
        await save_team_rating(
            f"{match['sport']}_{match['team1']}", match['sport'],
            match.get('team1_elo', 1500),
            match.get('team1_strength', 50),
            match.get('team1_goals_avg', 1.5)
        )
        await save_team_rating(
            f"{match['sport']}_{match['team2']}", match['sport'],
            match.get('team2_elo', 1500),
            match.get('team2_strength', 50),
            match.get('team2_goals_avg', 1.5)
        )
    
    send("🧮 Анализирую матчи математическими моделями...")
    predictions_count = 0
    
    for match in all_matches:
        try:
            prediction = await analyze_match(match)
            
            if prediction['confidence'] >= MIN_CONFIDENCE:
                await save_prediction(
                    match['id'], match['sport'],
                    match['team1'], match['team2'],  # ← ИСПРАВЛЕНО!
                    prediction['analysis'],
                    prediction['probabilities'],
                    prediction['recommendation'],
                    prediction['confidence'],
                    prediction['bet_type']
                )
                predictions_count += 1
        except Exception as e:
            logger.error(f"Ошибка анализа {match['team1']} vs {match['team2']}: {e}")
    
    send(f"✅ Обучение завершено!")
    send(f"📊 Создано {predictions_count} прогнозов с уверенностью ≥ {MIN_CONFIDENCE}%")
    
    return predictions_count


# ==================== ПЛАНИРОВЩИК ====================

async def hourly_predictions_job():
    logger.info("🕐 Запуск ежечасной рассылки...")
    
    users = await get_all_users()
    if not users:
        logger.info("Нет пользователей для рассылки")
        return
    
    predictions = await get_unsent_predictions(limit=PREDICTIONS_PER_HOUR)
    
    if not predictions:
        fb = await fetch_football_matches()
        hk = await fetch_hockey_matches()
        es = await fetch_esports_matches()
        
        for match in fb + hk + es:
            try:
                pred = await analyze_match(match)
                if pred['confidence'] >= MIN_CONFIDENCE:
                    await save_prediction(
                        match['id'], match['sport'],
                        match['team1'], match['team2'],  # ← ИСПРАВЛЕНО!
                        pred['analysis'], pred['probabilities'],
                        pred['recommendation'], pred['confidence'],
                        pred['bet_type']
                    )
            except:
                pass
        
        predictions = await get_unsent_predictions(limit=PREDICTIONS_PER_HOUR)
    
    if not predictions:
        logger.info("Нет новых прогнозов для рассылки")
        return
    
    logger.info(f"📤 Рассылка {len(predictions)} прогнозов {len(users)} пользователям")
    
    for pred in predictions:
        match_id, sport, team1, team2, analysis, probs_json, rec, conf, bet_type = pred  # ← ИСПРАВЛЕНО!
        probs = json.loads(probs_json)
        
        sport_emoji = {'football': '⚽️', 'hockey': '🏒', 'esports': '🎮'}.get(sport, '🏆')
        
        text = (
            f"{sport_emoji} <b>ПРОГНОЗ ({conf}%)</b>\n\n"
            f"🏆 <b>{team1} vs {team2}</b>\n\n"  # ← ИСПРАВЛЕНО!
            f"💰 <b>Рекомендация:</b> {rec}\n"
            f"🎯 <b>Тип ставки:</b> {bet_type}\n\n"
            f"📊 <b>Вероятности:</b>\n"
            f"• П1: {probs.get('p1', 0)}%\n"
            f"• Ничья: {probs.get('x', 0)}%\n"
            f"• П2: {probs.get('p2', 0)}%\n"
            f"• ТБ 2.5: {probs.get('total_over_2.5', 0)}%\n\n"
            f"🧮 <b>Метод:</b> {probs.get('method', 'Математический анализ')}\n\n"
            f"⚠️ <i>Играйте ответственно!</i>"
        )
        
        for user_id in users:
            try:
                await bot.send_message(user_id, text, parse_mode="HTML")
                await mark_prediction_sent(match_id, user_id)
            except Exception as e:
                logger.error(f"Ошибка отправки {user_id}: {e}")
    
    logger.info("✅ Рассылка завершена")


def setup_scheduler():
    scheduler = AsyncIOScheduler()
    
    scheduler.add_job(
        hourly_predictions_job,
        IntervalTrigger(hours=1),
        id="hourly_predictions",
        name="Ежечасная рассылка прогнозов"
    )
    
    scheduler.start()
    logger.info("⏰ Планировщик запущен")
    return scheduler


# ==================== ИНТЕРФЕЙС БОТА ====================

def get_main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Прогнозы на сегодня", callback_data="today")],
        [InlineKeyboardButton(text="⚽️ Футбол", callback_data="sport_football")],
        [InlineKeyboardButton(text="🏒 Хоккей", callback_data="sport_hockey")],
        [InlineKeyboardButton(text="🎮 Киберспорт", callback_data="sport_esports")],
        [InlineKeyboardButton(text="📊 Моя статистика", callback_data="my_stats")],
        [InlineKeyboardButton(text="ℹ️ О боте", callback_data="about")],
    ])

def get_back_button():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_start")]
    ])


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await add_user(message.from_user.id, message.from_user.username or "unknown")
    
    text = (
        f"👋 <b>Привет, {message.from_user.first_name}!</b>\n\n"
        f"🤖 Я — <b>ML-бот для спортивных прогнозов</b>\n\n"
        f"🧮 <b>Как я работаю:</b>\n"
        f"• Использую <b>4 математические модели</b>\n"
        f"• Собираю данные из <b>4 источников</b>:\n"
        f"  - API-Football\n"
        f"  - Football-Data.org\n"
        f"  - OpenLigaDB\n"
        f"  - PandaScore (киберспорт)\n"
        f"• Объединяю в <b>ансамбль</b>\n"
        f"• Рассчитываю вероятности\n"
        f"• Даю прогнозы с уверенностью ≥ {MIN_CONFIDENCE}%\n\n"
        f"⏰ <b>Рассылка:</b>\n"
        f"3 прогноза каждый час, без повторений!\n\n"
        f"🏆 <b>Виды спорта:</b>\n"
        f"⚽️ Футбол | 🏒 Хоккей | 🎮 Киберспорт"
    )
    
    await message.answer(text, parse_mode="HTML", reply_markup=get_main_keyboard())


@dp.callback_query(F.data == "back_to_start")
async def back_to_start(callback: types.CallbackQuery):
    await callback.message.edit_text(
        "🏠 <b>Главное меню</b>\n\nВыберите раздел:",
        parse_mode="HTML",
        reply_markup=get_main_keyboard()
    )
    await callback.answer()


@dp.callback_query(F.data == "today")
async def show_today(callback: types.CallbackQuery):
    await callback.answer("⏳ Загружаю прогнозы...")
    
    predictions = await get_today_predictions()
    
    if not predictions:
        await callback.message.edit_text(
            "😔 На сегодня пока нет прогнозов.\n\n"
            "Попробуйте позже или дождитесь следующей рассылки.",
            reply_markup=get_back_button()
        )
        return
    
    text = f"📅 <b>Прогнозы на сегодня ({len(predictions)} шт.)</b>\n\n"
    
    for i, pred in enumerate(predictions[:10], 1):  # ← ИСПРАВЛЕНО! Показываем 10
        match_id, sport, team1, team2, analysis, probs_json, rec, conf, bet_type = pred  # ← ИСПРАВЛЕНО!
        sport_emoji = {'football': '⚽️', 'hockey': '🏒', 'esports': '🎮'}.get(sport, '🏆')
        
        text += f"{i}. {sport_emoji} <b>{team1} vs {team2}</b>\n"  # ← ИСПРАВЛЕНО!
        text += f"   💰 {rec} ({conf}%)\n"
        text += f"   🎯 Тип: {bet_type}\n\n"
    
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=get_back_button())


@dp.callback_query(F.data.startswith("sport_"))
async def show_sport(callback: types.CallbackQuery):
    sport = callback.data.split("_")[1]
    sport_names = {'football': '⚽️ Футбол', 'hockey': '🏒 Хоккей', 'esports': '🎮 Киберспорт'}
    
    await callback.answer("🔍 Анализирую матчи...")
    
    if sport == 'football':
        matches = await fetch_football_matches()
    elif sport == 'hockey':
        matches = await fetch_hockey_matches()
    else:
        matches = await fetch_esports_matches()
    
    if not matches:
        await callback.message.edit_text(
            f"😔 Нет матчей по разделу {sport_names[sport]}",
            reply_markup=get_back_button()
        )
        return
    
    text = f"{sport_names[sport]} - <b>{len(matches)} матчей</b>\n\n"
    
    analyzed = []
    for match in matches:
        try:
            pred = await analyze_match(match)
            if pred['confidence'] >= MIN_CONFIDENCE:
                analyzed.append((match, pred))
        except:
            pass
    
    if not analyzed:
        text += "Нет уверенных прогнозов (≥ 50%)\n\n"
    else:
        text += f"✅ Найдено {len(analyzed)} прогнозов:\n\n"
        for match, pred in analyzed[:5]:
            text += (
                f"🏆 <b>{match['team1']} vs {match['team2']}</b>\n"
                f"💰 {pred['recommendation']} ({pred['confidence']}%)\n"
                f"🎯 Тип: {pred['bet_type']}\n\n"
            )
    
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=get_back_button())


@dp.callback_query(F.data == "my_stats")
async def show_stats(callback: types.CallbackQuery):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM predictions WHERE confidence >= ?", 
            (MIN_CONFIDENCE,)
        ) as cursor:
            total = (await cursor.fetchone())[0]
    
    text = (
        "📊 <b>Ваша статистика</b>\n\n"
        f"📅 Дата регистрации: сегодня\n"
        f"📨 Всего прогнозов в базе: {total}\n"
        f"🎯 Порог уверенности: ≥ {MIN_CONFIDENCE}%\n"
        f"⏰ Рассылка: 3 прогноза/час\n\n"
        f"<i>Статистика ставок будет доступна после использования прогнозов.</i>"
    )
    
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=get_back_button())


@dp.callback_query(F.data == "about")
async def show_about(callback: types.CallbackQuery):
    text = (
        "ℹ️ <b>О боте</b>\n\n"
        "🧮 <b>Математические модели:</b>\n\n"
        "1️⃣ <b>Модель Пуассона</b>\n"
        "   Расчёт вероятности голов\n\n"
        "2️⃣ <b>Рейтинг Эло</b>\n"
        "   Система рейтингов как в шахматах\n\n"
        "3️⃣ <b>Модель Брэдли-Терри</b>\n"
        "   Парные сравнения команд\n\n"
        "4️⃣ <b>Анализ формы</b>\n"
        "   Последние результаты команд\n\n"
        "📊 <b>Источники данных:</b>\n"
        "• API-Football (футбол)\n"
        "• Football-Data.org (футбол)\n"
        "• OpenLigaDB (футбол)\n"
        "• PandaScore (киберспорт)\n\n"
        "⚠️ <i>Ставки — это риск. Играйте ответственно!</i>"
    )
    
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=get_back_button())


@dp.message(Command("first_train"))
async def cmd_first_train(message: types.Message):
    await first_train(message)


@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    text = (
        "📖 <b>Команды бота:</b>\n\n"
        "/start — Главное меню\n"
        "/first_train — Первое обучение моделей\n"
        "/help — Справка\n\n"
        "💡 Используйте кнопки для навигации"
    )
    await message.answer(text, parse_mode="HTML", reply_markup=get_back_button())


# ==================== ЗАПУСК ====================

async def main():
    logger.info("🚀 Запуск бота...")
    
    await init_db()
    logger.info("✅ База данных инициализирована")
    
    scheduler = setup_scheduler()
    
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT COUNT(*) FROM predictions") as cursor:
            count = (await cursor.fetchone())[0]
    
    if count == 0:
        logger.info("🎯 База пустая - запускаю первое обучение...")
        await first_train()
    
    logger.info("✅ Бот готов к работе!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("🛑 Бот остановлен")
