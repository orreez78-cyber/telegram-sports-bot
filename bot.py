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

MIN_CONFIDENCE = 45
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
    """Модель Пуассона - ТОЛЬКО для футбола/хоккея"""
    
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
    """Рейтинг Эло - универсальный"""
    
    @staticmethod
    def expected_score(rating_a, rating_b):
        return 1 / (1 + 10 ** ((rating_b - rating_a) / 400))
    
    @staticmethod
    def calculate_win_probability(rating_a, rating_b):
        return round(EloRating.expected_score(rating_a, rating_b) * 100, 1)
    
    @staticmethod
    def update_rating(rating, expected, actual, k=32):
        return rating + k * (actual - expected)


class BradleyTerryModel:
    """Модель Брэдли-Терри - для парных сравнений"""
    
    @staticmethod
    def win_probability(strength_a, strength_b):
        exp_a = math.exp(strength_a / 100)
        exp_b = math.exp(strength_b / 100)
        return round(exp_a / (exp_a + exp_b) * 100, 1)


class BookmakerFactor:
    """Коэффициенты БК как фактор в ансамбле (с небольшим весом)"""
    
    @staticmethod
    def calculate_probability(odds):
        if odds <= 0:
            return 50
        return 100 / odds
    
    @staticmethod
    def get_bookmaker_influence(odds_home, odds_away, odds_draw=0):
        p1 = BookmakerFactor.calculate_probability(odds_home)
        p2 = BookmakerFactor.calculate_probability(odds_away)
        draw = BookmakerFactor.calculate_probability(odds_draw) if odds_draw > 0 else 0
        
        total = p1 + p2 + draw
        if total > 0:
            p1 = p1 / total * 100
            p2 = p2 / total * 100
            draw = draw / total * 100
        
        return round(p1, 1), round(p2, 1), round(draw, 1)


class EsportsModel:
    """СПЕЦИАЛИЗИРОВАННАЯ модель для киберспорта"""
    
    @staticmethod
    def predict(team1_data, team2_data):
        elo_diff = team1_data['elo_rating'] - team2_data['elo_rating']
        p1_elo = 1 / (1 + 10 ** (-elo_diff / 400)) * 100
        
        strength_diff = team1_data['strength'] - team2_data['strength']
        p1_strength = 50 + strength_diff * 0.5
        
        form_diff = team1_data['form'] - team2_data['form']
        p1_form = 50 + form_diff * 0.3
        
        home_advantage = 2
        
        p1 = (p1_elo * 0.40 + p1_strength * 0.30 + p1_form * 0.20 + home_advantage * 0.10)
        p2 = 100 - p1
        
        p1 = max(20, min(80, p1))
        p2 = max(20, min(80, p2))
        
        total = p1 + p2
        p1 = round(p1 / total * 100, 1)
        p2 = round(p2 / total * 100, 1)
        
        return {
            'p1': p1,
            'p2': p2,
            'method': 'Esports Model (Elo + Strength + Form)'
        }


class EnsemblePredictor:
    """Ансамбль моделей с учётом коэффициентов БК"""
    
    def __init__(self):
        self.weights_sports = {
            'poisson': 0.40,
            'elo': 0.25,
            'bradley_terry': 0.15,
            'form': 0.20,
            'bookmaker': 0.10
        }
        
        self.weights_esports = {
            'elo': 0.30,
            'bradley_terry': 0.25,
            'form': 0.25,
            'bookmaker': 0.20
        }
    
    def predict(self, team1_data, team2_data, sport='football', bookmaker_odds=None):
        if sport == 'esports':
            esports_model = EsportsModel()
            result = esports_model.predict(team1_data, team2_data)
            
            if bookmaker_odds:
                bk_p1, bk_p2, bk_draw = BookmakerFactor.get_bookmaker_influence(
                    bookmaker_odds.get('home', 0),
                    bookmaker_odds.get('away', 0)
                )
                
                p1 = result['p1'] * 0.80 + bk_p1 * 0.20
                p2 = result['p2'] * 0.80 + bk_p2 * 0.20
                
                total = p1 + p2
                p1 = round(p1 / total * 100, 1)
                p2 = round(p2 / total * 100, 1)
                
                result['p1'] = p1
                result['p2'] = p2
                result['method'] = 'Esports Model + Bookmaker Factor'
            
            return {
                'p1': result['p1'],
                'x': 0,
                'p2': result['p2'],
                'total_over_2.5': 0,
                'method': result['method']
            }
        
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
        
        bk_p1, bk_p2, bk_x = 0, 0, 0
        if bookmaker_odds:
            bk_p1, bk_p2, bk_x = BookmakerFactor.get_bookmaker_influence(
                bookmaker_odds.get('home', 0),
                bookmaker_odds.get('away', 0),
                bookmaker_odds.get('draw', 0)
            )
        
        w = self.weights_sports
        p1 = (p1_p * w['poisson'] + p1_e * w['elo'] + 
              p1_bt * w['bradley_terry'] + p1_f * w['form'] +
              bk_p1 * w['bookmaker'])
        p2 = (p2_p * w['poisson'] + p2_e * w['elo'] + 
              p2_bt * w['bradley_terry'] + p2_f * w['form'] +
              bk_p2 * w['bookmaker'])
        x = (x_p * w['poisson'] + x_e * w['elo'] + 
             x_bt * w['bradley_terry'] + x_f * w['form'] +
             bk_x * w['bookmaker'])
        
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
            'method': 'Ансамбль (Пуассон + Эло + Брэдли-Терри + Форма + БК)'
        }


# ==================== БАЗА ДАННЫХ ====================

async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS users 
            (user_id INTEGER PRIMARY KEY, username TEXT, 
             created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        
        await db.execute("""CREATE TABLE IF NOT EXISTS matches 
            (match_id TEXT PRIMARY KEY, sport TEXT, team1 TEXT, 
             team2 TEXT, match_date TEXT, tournament TEXT,
             team1_score INTEGER, team2_score INTEGER,
             is_finished INTEGER DEFAULT 0,
             created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        
        await db.execute("""CREATE TABLE IF NOT EXISTS predictions 
            (match_id TEXT, sport TEXT, team1 TEXT, team2 TEXT,
             tournament TEXT, analysis TEXT, probabilities TEXT, 
             recommendation TEXT, confidence REAL, bet_type TEXT,
             user_id INTEGER,
             created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
             PRIMARY KEY (match_id, user_id))""")
        
        await db.execute("""CREATE TABLE IF NOT EXISTS prediction_results
            (id INTEGER PRIMARY KEY AUTOINCREMENT,
             match_id TEXT,
             user_id INTEGER,
             prediction TEXT,
             actual_result TEXT,
             is_correct INTEGER,
             confidence REAL,
             checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        
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

async def save_prediction(match_id, sport, team1, team2, tournament, analysis, probs, rec, conf, bet_type, user_id=None):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""INSERT OR REPLACE INTO predictions 
            (match_id, sport, team1, team2, tournament, analysis, probabilities, recommendation, confidence, bet_type, user_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (match_id, sport, team1, team2, tournament, analysis, json.dumps(probs), rec, conf, bet_type, user_id))
        await db.commit()

async def get_today_predictions(user_id=None):
    async with aiosqlite.connect(DB_NAME) as db:
        if user_id:
            async with db.execute("""SELECT match_id, sport, team1, team2, tournament,
                analysis, probabilities, recommendation, confidence, bet_type
                FROM predictions 
                WHERE date(created_at) = date('now') AND confidence >= ? AND user_id = ?
                ORDER BY confidence DESC""", (MIN_CONFIDENCE, user_id)) as cursor:
                return await cursor.fetchall()
        else:
            async with db.execute("""SELECT match_id, sport, team1, team2, tournament,
                analysis, probabilities, recommendation, confidence, bet_type
                FROM predictions 
                WHERE date(created_at) = date('now') AND confidence >= ?
                ORDER BY confidence DESC""", (MIN_CONFIDENCE,)) as cursor:
                return await cursor.fetchall()

async def get_unsent_predictions(limit=3):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("""SELECT p.match_id, p.sport, p.team1, p.team2, p.tournament,
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

async def save_match_result(match_id, team1_score, team2_score):
    """Сохранить результат матча"""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""UPDATE matches 
            SET team1_score = ?, team2_score = ?, is_finished = 1
            WHERE match_id = ?""",
            (team1_score, team2_score, match_id))
        await db.commit()

async def check_and_update_finished_matches():
    """Проверить завершённые матчи и обновить результаты"""
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("""SELECT match_id, sport, team1, team2, match_date 
            FROM matches WHERE is_finished = 0 AND match_date < datetime('now', '-3 hours')""") as cursor:
            matches = await cursor.fetchall()
    
    for match in matches:
        match_id, sport, team1, team2, match_date = match
        # Здесь можно добавить парсинг результатов с API
        # Пока просто помечаем как проверенные
        logger.info(f"Проверка матча: {team1} vs {team2}")
    
    return len(matches)

async def analyze_prediction_accuracy(match_id, actual_result):
    """Проанализировать точность прогнозов для матча"""
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("""SELECT user_id, recommendation, confidence, probabilities
            FROM predictions WHERE match_id = ?""", (match_id,)) as cursor:
            predictions = await cursor.fetchall()
    
    correct_count = 0
    total_count = len(predictions)
    
    for pred in predictions:
        user_id, recommendation, confidence, probs_json = pred
        probs = json.loads(probs_json)
        
        # Определяем фактический результат
        if actual_result == 'home_win' and 'П1' in recommendation:
            is_correct = 1
        elif actual_result == 'away_win' and 'П2' in recommendation:
            is_correct = 1
        elif actual_result == 'draw' and 'Ничья' in recommendation or 'X' in recommendation:
            is_correct = 1
        else:
            is_correct = 0
        
        if is_correct:
            correct_count += 1
        
        # Сохраняем результат
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("""INSERT INTO prediction_results 
                (match_id, user_id, prediction, actual_result, is_correct, confidence)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (match_id, user_id, recommendation, actual_result, is_correct, confidence))
            await db.commit()
    
    accuracy = (correct_count / total_count * 100) if total_count > 0 else 0
    logger.info(f"Точность прогнозов для {match_id}: {accuracy:.1f}% ({correct_count}/{total_count})")
    
    return accuracy

async def get_user_stats(user_id):
    """Получить статистику пользователя"""
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("""
            SELECT 
                COUNT(*) as total,
                SUM(CASE WHEN is_correct = 1 THEN 1 ELSE 0 END) as correct,
                AVG(confidence) as avg_confidence
            FROM prediction_results 
            WHERE user_id = ?""",
            (user_id,)) as cursor:
            stats = await cursor.fetchone()
    
    if stats[0] > 0:
        total, correct, avg_conf = stats
        accuracy = (correct / total * 100) if total > 0 else 0
        return {
            'total': total,
            'correct': correct,
            'accuracy': round(accuracy, 1),
            'avg_confidence': round(avg_conf, 1) if avg_conf else 0
        }
    return None


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
                    matches = []
                    for f in data.get('response', []):
                        matches.append({
                            "id": f"af_{f['fixture']['id']}",
                            "team1": f['teams']['home']['name'],
                            "team2": f['teams']['away']['name'],
                            "date": f['fixture']['date'],
                            "sport": "football",
                            "tournament": f['league']['name'],
                            "team1_goals_avg": 1.5,
                            "team2_goals_avg": 1.5,
                            "team1_elo": 1500,
                            "team2_elo": 1500,
                            "team1_strength": 50,
                            "team2_strength": 50,
                            "team1_form": 50,
                            "team2_form": 50
                        })
                    return matches
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
                                "sport": "football",
                                "tournament": match.get('competition', {}).get('name', 'Unknown'),
                                "team1_goals_avg": 1.5,
                                "team2_goals_avg": 1.5,
                                "team1_elo": 1500,
                                "team2_elo": 1500,
                                "team1_strength": 50,
                                "team2_strength": 50,
                                "team1_form": 50,
                                "team2_form": 50
                            })
                    return matches
    except Exception as e:
        logger.error(f"Football-Data.org error: {e}")
    return []


async def fetch_openligadb_matches():
    leagues = [
        ("bundesliga", "Бундеслига"),
        ("2bundesliga", "2. Бундеслига"),
        ("englischepremierleague", "АПЛ"),
        ("laliga", "Ла Лига"),
        ("seriea", "Серия А"),
        ("ligue1", "Лига 1")
    ]
    
    all_matches = []
    current_season = datetime.now().year
    
    for league_code, league_name in leagues:
        url = f"https://www.openligadb.de/api/getmatchdata/{league_code}/{current_season}"
        
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
                                    "league": league_name,
                                    "tournament": league_name,
                                    "sport": "football",
                                    "team1_goals_avg": 1.5,
                                    "team2_goals_avg": 1.5,
                                    "team1_elo": 1500,
                                    "team2_elo": 1500,
                                    "team1_strength": 50,
                                    "team2_strength": 50,
                                    "team1_form": 50,
                                    "team2_form": 50
                                })
        except Exception as e:
            logger.error(f"OpenLigaDB {league_code} error: {e}")
        
        await asyncio.sleep(1)
    
    return all_matches

async def fetch_football_matches():
    """Собрать все футбольные матчи из всех источников"""
    logger.info("📊 Собираю футбольные матчи...")
    
    matches = []
    
    # 1. API-Football
    af_matches = await fetch_api_football_matches()
    logger.info(f"API-Football: {len(af_matches)} матчей")
    matches.extend(af_matches)
    
    # 2. Football-Data.org
    fd_matches = await fetch_football_data_org_matches()
    logger.info(f"Football-Data.org: {len(fd_matches)} матчей")
    matches.extend(fd_matches)
    
    # 3. OpenLigaDB
    ol_matches = await fetch_openligadb_matches()
    logger.info(f"OpenLigaDB: {len(ol_matches)} матчей")
    matches.extend(ol_matches)
    
    # Удаляем дубликаты
    unique_matches = {}
    for match in matches:
        key = f"{match['team1']}_{match['team2']}"
        if key not in unique_matches:
            unique_matches[key] = match
    
    logger.info(f"✅ Всего футбольных матчей: {len(unique_matches)}")
    return list(unique_matches.values())


async def fetch_pandascore_matches():
    if not PANDASCORE_API_KEY:
        logger.info("PandaScore API ключ не указан")
        return []
    
    games = [
        ('cs2', 'CS2'),
        ('dota2', 'Dota 2'),
        ('lol', 'LoL'),
        ('valorant', 'Valorant')
    ]
    all_matches = []
    
    headers = {
        "Authorization": f"Bearer {PANDASCORE_API_KEY}",
        "Accept": "application/json"
    }
    
    for game_code, game_name in games:
        url = f"https://api.pandascore.co/{game_code}/matches/upcoming"
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
                                        "game": game_name,
                                        "league": match.get('league', {}).get('name', 'Unknown'),
                                        "tournament": match.get('league', {}).get('name', 'Unknown'),
                                        "sport": "esports",
                                        "format": match.get('series', {}).get('type', 'bo1'),
                                        "team1_goals_avg": 1.3,
                                        "team2_goals_avg": 1.2,
                                        "team1_elo": 1700,
                                        "team2_elo": 1650,
                                        "team1_strength": 70,
                                        "team2_strength": 65,
                                        "team1_form": 65,
                                        "team2_form": 60
                                    })
                        
                        logger.info(f"PandaScore {game_name}: {len(data)} матчей")
        except Exception as e:
            logger.error(f"PandaScore {game_name} error: {e}")
        
        await asyncio.sleep(1)
    
    return all_matches


async def fetch_football_matches():
    """Собрать все футбольные матчи из всех источников"""
    logger.info("📊 Собираю футбольные матчи...")
    
    matches = []
    
    # 1. API-Football
    af_matches = await fetch_api_football_matches()
    logger.info(f"API-Football: {len(af_matches)} матчей")
    matches.extend(af_matches)
    
    # 2. Football-Data.org
    fd_matches = await fetch_football_data_org_matches()
    logger.info(f"Football-Data.org: {len(fd_matches)} матчей")
    matches.extend(fd_matches)
    
    # 3. OpenLigaDB
    ol_matches = await fetch_openligadb_matches()
    logger.info(f"OpenLigaDB: {len(ol_matches)} матчей")
    matches.extend(ol_matches)
    
    # Удаляем дубликаты
    unique_matches = {}
    for match in matches:
        key = f"{match['team1']}_{match['team2']}"
        if key not in unique_matches:
            unique_matches[key] = match
    
    logger.info(f"✅ Всего футбольных матчей: {len(unique_matches)}")
    return list(unique_matches.values())


async def fetch_hockey_matches():
    today = datetime.now().strftime("%Y-%m-%d")
    return [
        {
            "id": "hk_301",
            "team1": "ЦСКА",
            "team2": "СКА",
            "date": today,
            "sport": "hockey",
            "tournament": "КХЛ",
            "team1_goals_avg": 3.2,
            "team2_goals_avg": 2.8,
            "team1_elo": 1750,
            "team2_elo": 1780,
            "team1_strength": 72,
            "team2_strength": 75,
            "team1_form": 70,
            "team2_form": 74
        },
    ]


async def fetch_esports_matches():
    logger.info("🎮 Собираю киберспортивные матчи...")
    
    matches = await fetch_pandascore_matches()
    logger.info(f"PandaScore: {len(matches)} матчей")
    
    if not matches:
        today = datetime.now().strftime("%Y-%m-%d")
        mock_matches = [
            {
                "id": "es_201",
                "team1": "NaVi",
                "team2": "FaZe",
                "date": today,
                "sport": "esports",
                "game": "CS2",
                "tournament": "IEM Katowice 2026",
                "team1_goals_avg": 1.3,
                "team2_goals_avg": 1.2,
                "team1_elo": 1800,
                "team2_elo": 1780,
                "team1_strength": 80,
                "team2_strength": 78,
                "team1_form": 75,
                "team2_form": 72
            },
        ]
        matches.extend(mock_matches)
        logger.info(f"Используем мок-данные: {len(mock_matches)} матчей")
    
    return matches

# ==================== АНАЛИЗАТОР ====================

async def fetch_bookmaker_odds(match_id: str, sport: str):
    odds = {
        'home': 0,
        'draw': 0,
        'away': 0,
        'bookmaker': 'BetBoom'
    }
    
    if THE_ODDS_API_KEY:
        try:
            if sport == 'football':
                sport_key = 'soccer_epl'
            elif sport == 'esports':
                sport_key = 'cs2'
            else:
                return odds
            
            url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds"
            params = {
                'apiKey': THE_ODDS_API_KEY,
                'regions': 'eu',
                'markets': 'h2h',
                'dateFormat': 'iso'
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        
                        for event in data:
                            if (match_id in str(event) or 
                                event.get('home_team') in match_id or
                                event.get('away_team') in match_id):
                                
                                bookmakers = event.get('bookmakers', [])
                                if bookmakers:
                                    bk = bookmakers[0]
                                    markets = bk.get('markets', [])
                                    
                                    if markets:
                                        outcomes = markets[0].get('outcomes', [])
                                        for outcome in outcomes:
                                            name = outcome.get('name')
                                            price = outcome.get('price')
                                            
                                            if name == event.get('home_team'):
                                                odds['home'] = price
                                            elif name == event.get('away_team'):
                                                odds['away'] = price
                                            elif name == 'Draw':
                                                odds['draw'] = price
                                        
                                        odds['bookmaker'] = bk.get('title', 'BetBoom')
                                        break
                        
                        return odds
        except Exception as e:
            logger.error(f"Ошибка получения odds: {e}")
    
    return {
        'home': 2.0,
        'draw': 3.5,
        'away': 3.0,
        'bookmaker': 'BetBoom'
    }


async def self_check_prediction(match, bot_probability, bookmaker_probability, sport='football'):
    divergence = abs(bot_probability - bookmaker_probability)
    
    check_result = {
        'needs_review': False,
        'reason': '',
        'adjusted_confidence': bot_probability,
        'confidence_level': 'high'
    }
    
    if divergence < 10:
        check_result['confidence_level'] = 'high'
        check_result['reason'] = 'Прогноз совпадает с БК'
        return check_result
    
    if divergence < 20:
        check_result['needs_review'] = True
        check_result['confidence_level'] = 'medium'
        
        reasons = []
        team1_form = match.get('team1_form', 50)
        team2_form = match.get('team2_form', 50)
        if abs(team1_form - team2_form) < 10:
            reasons.append('Форма команд близка')
        
        team1_elo = match.get('team1_elo', 1500)
        team2_elo = match.get('team2_elo', 1500)
        if abs(team1_elo - team2_elo) < 100:
            reasons.append('Рейтинги команд близки')
        
        check_result['reason'] = 'Небольшое расхождение. ' + '; '.join(reasons) if reasons else 'Небольшое расхождение'
        
        adjustment = divergence * 0.2
        if bot_probability > bookmaker_probability:
            check_result['adjusted_confidence'] = bot_probability - adjustment
        else:
            check_result['adjusted_confidence'] = bot_probability + adjustment
        
        return check_result
    
    if divergence < 30:
        check_result['needs_review'] = True
        check_result['confidence_level'] = 'low'
        check_result['reason'] = 'Большое расхождение с БК - требуется осторожность'
        
        adjustment = divergence * 0.3
        if bot_probability > bookmaker_probability:
            check_result['adjusted_confidence'] = bot_probability - adjustment
        else:
            check_result['adjusted_confidence'] = bot_probability + adjustment
        
        check_result['adjusted_confidence'] = max(25, min(75, check_result['adjusted_confidence']))
        
        return check_result
    
    check_result['needs_review'] = True
    check_result['confidence_level'] = 'very_low'
    check_result['reason'] = 'Очень большое расхождение с БК - прогноз спорный'
    
    adjustment = divergence * 0.4
    if bot_probability > bookmaker_probability:
        check_result['adjusted_confidence'] = bot_probability - adjustment
    else:
        check_result['adjusted_confidence'] = bot_probability + adjustment
    
    check_result['adjusted_confidence'] = max(20, min(80, check_result['adjusted_confidence']))
    
    return check_result


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
    
    sport = match.get('sport', 'football')
    tournament = match.get('tournament', 'Unknown')
    
    bookmaker_odds = await fetch_bookmaker_odds(match['id'], sport)
    
    predictor = EnsemblePredictor()
    result = predictor.predict(team1_data, team2_data, sport=sport, bookmaker_odds=bookmaker_odds)
    
    if sport == 'esports':
        if result['p1'] > result['p2']:
            rec = f"П1 ({match['team1']})"
            bot_confidence = result['p1']
            bet_odds = bookmaker_odds.get('home', 0) if isinstance(bookmaker_odds, dict) else 0
        else:
            rec = f"П2 ({match['team2']})"
            bot_confidence = result['p2']
            bet_odds = bookmaker_odds.get('away', 0) if isinstance(bookmaker_odds, dict) else 0
        bet_type = "Исход"
    else:
        max_prob = max(result['p1'], result['x'], result['p2'], result['total_over_2.5'])
        
        if max_prob == result['p1']:
            rec = f"П1 ({match['team1']})"
            bot_confidence = result['p1']
            bet_odds = bookmaker_odds.get('home', 0) if isinstance(bookmaker_odds, dict) else 0
            bet_type = "Исход"
        elif max_prob == result['p2']:
            rec = f"П2 ({match['team2']})"
            bot_confidence = result['p2']
            bet_odds = bookmaker_odds.get('away', 0) if isinstance(bookmaker_odds, dict) else 0
            bet_type = "Исход"
        elif max_prob == result['x']:
            rec = "Ничья (X)"
            bot_confidence = result['x']
            bet_odds = bookmaker_odds.get('draw', 0) if isinstance(bookmaker_odds, dict) else 0
            bet_type = "Исход"
        else:
            rec = "Тотал больше 2.5"
            bot_confidence = result['total_over_2.5']
            bet_odds = 1.9
            bet_type = "Тотал"
    
    bookmaker_probability = 100 / bet_odds if bet_odds > 0 else 0
    
    if bookmaker_probability > 0:
        self_check = await self_check_prediction(
            match, bot_confidence, bookmaker_probability, sport
        )
        final_confidence = self_check['adjusted_confidence']
        confidence_level = self_check['confidence_level']
        check_reason = self_check['reason']
    else:
        final_confidence = bot_confidence
        confidence_level = 'unknown'
        check_reason = 'Коэффициенты БК недоступны'
    
    divergence = abs(bot_confidence - bookmaker_probability) if bookmaker_probability > 0 else 0
    
    if sport == 'esports':
        analysis = (
            f"🏆 <b>Турнир:</b> {tournament}\n\n"
            f"📊 <b>Киберспортивный анализ:</b>\n"
            f"• Эло: {team1_data['elo_rating']} vs {team2_data['elo_rating']}\n"
            f"• Сила: {team1_data['strength']} vs {team2_data['strength']}\n"
            f"• Форма: {team1_data['form']} vs {team2_data['form']}\n\n"
            f"📈 <b>Коэффициент BetBoom:</b> {bet_odds}\n"
            f"📊 <b>Вероятность БК:</b> {bookmaker_probability:.1f}%\n"
            f"🤖 <b>Наш прогноз:</b> {bot_confidence:.1f}%\n"
            f"🎯 <b>Итоговый:</b> {final_confidence:.1f}%\n\n"
            f"🔍 <b>Самопроверка:</b>\n"
            f"• Расхождение: {divergence:.1f}%\n"
            f"• Доверие: {confidence_level}\n"
            f"• {check_reason}"
        )
    else:
        analysis = (
            f"🏆 <b>Турнир:</b> {tournament}\n\n"
            f"📊 <b>Математический анализ:</b>\n"
            f"• Пуассон: П1={result['p1']}%, X={result['x']}%, П2={result['p2']}%\n"
            f"• Эло: {team1_data['elo_rating']} vs {team2_data['elo_rating']}\n"
            f"• Форма: {team1_data['form']} vs {team2_data['form']}\n"
            f"• Голы: {team1_data['goals_avg']} vs {team2_data['goals_avg']}\n\n"
            f"📈 <b>Коэффициент BetBoom:</b> {bet_odds}\n"
            f"📊 <b>Вероятность БК:</b> {bookmaker_probability:.1f}%\n"
            f"🤖 <b>Наш прогноз:</b> {bot_confidence:.1f}%\n"
            f"🎯 <b>Итоговый:</b> {final_confidence:.1f}%\n\n"
            f"🔍 <b>Самопроверка:</b>\n"
            f"• Расхождение: {divergence:.1f}%\n"
            f"• Доверие: {confidence_level}\n"
            f"• {check_reason}"
        )
    
    probs = {
        'p1': result['p1'], 
        'x': result.get('x', 0), 
        'p2': result['p2'],
        'total_over_2.5': result.get('total_over_2.5', 0),
        'bookmaker_odds': bet_odds,
        'bookmaker_prob': bookmaker_probability,
        'original_confidence': bot_confidence,
        'final_confidence': final_confidence,
        'confidence_level': confidence_level,
        'divergence': divergence,
        'check_reason': check_reason,
        'tournament': tournament
    }
    
    return {
        'analysis': analysis,
        'probabilities': probs,
        'recommendation': rec,
        'confidence': round(final_confidence, 1),
        'bet_type': bet_type,
        'method': result['method'],
        'bookmaker': bookmaker_odds.get('bookmaker', 'BetBoom') if isinstance(bookmaker_odds, dict) else 'BetBoom',
        'tournament': tournament
    }


# ==================== АВТОМАТИЧЕСКОЕ ОБУЧЕНИЕ ====================

async def first_train(message: types.Message = None):
    async def send(msg):
        if message:
            try:
                await message.answer(msg)
            except Exception as e:
                logger.error(f"Ошибка отправки: {e}")
        logger.info(msg)
    
    await send("🚀 Начинаю первое обучение моделей...")
    
    await send("📊 Собираю данные из 4 источников...")
    fb_matches = await fetch_football_matches()
    hk_matches = await fetch_hockey_matches()
    es_matches = await fetch_esports_matches()
    
    all_matches = fb_matches + hk_matches + es_matches
    await send("✅ Собрано {len(all_matches)} матчей")

await send(" Рассчитываю рейтинги команд...")

for match in all_matches:
    await save_team_rating(...)  # Первый рейтинг
    await save_team_rating(...)  # Второй рейтинг
    # Сохраняем матч в БД

# ← ПЕРЕМЕСТИТЕ СЮДА (после цикла сохранения рейтингов)
await send("🧮 Анализирую матчи математическими моделями...")

predictions_count = 0
debug_count = 0

for match in all_matches:
    try:
        prediction = await analyze_match(match)
        # ... анализ ...
    predictions_count = 0
    debug_count = 0
    
    for match in all_matches:
        try:
            prediction = await analyze_match(match)
            
            debug_count += 1
            if debug_count <= 5:
                logger.info(f"🔍 {match['team1']} vs {match['team2']}: {prediction['confidence']}% ({prediction['recommendation']})")
            
            if prediction['confidence'] >= MIN_CONFIDENCE:
                await save_prediction(
                    match['id'], match['sport'],
                    match['team1'], match['team2'],
                    match.get('tournament', 'Unknown'),
                    prediction['analysis'],
                    prediction['probabilities'],
                    prediction['recommendation'],
                    prediction['confidence'],
                    prediction['bet_type']
                )
                predictions_count += 1
        except Exception as e:
            logger.error(f"Ошибка анализа {match['team1']} vs {match['team2']}: {e}")
    
    await send(f"✅ Обучение завершено!")
    await send(f"📊 Создано {predictions_count} прогнозов с уверенностью ≥ {MIN_CONFIDENCE}%")
    
    return predictions_count


# ==================== ПЛАНИРОВЩИК ====================

async def hourly_predictions_job():
    logger.info("🕐 Запуск ежечасной рассылки...")
    
    users = await get_all_users()
    if not users:
        logger.info("Нет пользователей для рассылки")
        return
    
    # Проверяем завершённые матчи
    finished_count = await check_and_update_finished_matches()
    if finished_count > 0:
        logger.info(f"Проверено {finished_count} завершённых матчей")
    
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
                        match['team1'], match['team2'],
                        match.get('tournament', 'Unknown'),
                        pred['analysis'], pred['probabilities'],
                        pred['recommendation'], pred['confidence'],
                        pred['bet_type']
                    )
                    
                    # Сохраняем матч
                    async with aiosqlite.connect(DB_NAME) as db:
                        await db.execute("""INSERT OR REPLACE INTO matches 
                            (match_id, sport, team1, team2, match_date, tournament)
                            VALUES (?, ?, ?, ?, ?, ?)""",
                            (match['id'], match['sport'], match['team1'], match['team2'], 
                             match.get('date', ''), match.get('tournament', 'Unknown')))
                        await db.commit()
            except Exception as e:
                logger.error(f"Ошибка анализа: {e}")
        
        predictions = await get_unsent_predictions(limit=PREDICTIONS_PER_HOUR)
    
    if not predictions:
        logger.info("Нет новых прогнозов для рассылки")
        return
    
    logger.info(f"📤 Рассылка {len(predictions)} прогнозов {len(users)} пользователям")
    
    for pred in predictions:
        match_id, sport, team1, team2, tournament, analysis, probs_json, rec, conf, bet_type = pred
        probs = json.loads(probs_json)
        
        sport_emoji = {'football': '⚽️', 'hockey': '🏒', 'esports': '🎮'}.get(sport, '🏆')
        
        original_conf = probs.get('original_confidence', conf)
        bk_odds = probs.get('bookmaker_odds', 0)
        bk_prob = probs.get('bookmaker_prob', 0)
        divergence = probs.get('divergence', 0)
        adjustment = probs.get('adjustment', 0)
        conf_level = probs.get('confidence_level', 'unknown')
        
        if conf_level == 'high':
            quality_marker = "✅ <b>ВЫСОКАЯ ДОСТОВЕРНОСТЬ</b>\n\n"
        elif conf_level == 'medium':
            quality_marker = "⚠️ <b>СРЕДНЯЯ ДОСТОВЕРНОСТЬ</b>\n\n"
        else:
            quality_marker = "🚨 <b>НИЗКАЯ ДОСТОВЕРНОСТЬ</b>\n\n"
        
        if sport == 'esports':
            text = (
                f"{sport_emoji} {quality_marker}<b>{team1} vs {team2}</b>\n"
                f"🏆 <b>Турнир:</b> {tournament}\n\n"
                f"💰 <b>Прогноз:</b> {rec} ({conf}%)\n"
                f"🎯 <b>Тип:</b> {bet_type}\n"
                f"📊 <b>Коэффициент:</b> {bk_odds}\n\n"
                f"📈 <b>Калибровка:</b>\n"
                f"• Исходный: {original_conf}%\n"
                f"• БК: {bk_prob}%\n"
                f"• Итоговый: {conf}%\n"
                f"• Расхождение: {divergence:.1f}%\n\n"
                f"🧮 <b>Метод:</b> {probs.get('method', 'Математический анализ')}\n\n"
                f"⚠️ <i>Играйте ответственно!</i>"
            )
        else:
            text = (
                f"{sport_emoji} {quality_marker}<b>{team1} vs {team2}</b>\n"
                f"🏆 <b>Турнир:</b> {tournament}\n\n"
                f"💰 <b>Прогноз:</b> {rec} ({conf}%)\n"
                f"🎯 <b>Тип:</b> {bet_type}\n"
                f"📊 <b>Коэффициент:</b> {bk_odds}\n\n"
                f"📈 <b>Калибровка:</b>\n"
                f"• Исходный: {original_conf}%\n"
                f"• БК: {bk_prob}%\n"
                f"• Итоговый: {conf}%\n"
                f"• Расхождение: {divergence:.1f}%\n\n"
                f"📊 <b>Вероятности:</b>\n"
                f"• П1: {probs.get('p1', 0)}%\n"
                f"• Ничья: {probs.get('x', 0)}%\n"
                f"• П2: {probs.get('p2', 0)}%\n\n"
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
        name="Ежечасная рассылка"
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
        [InlineKeyboardButton(text="📈 Результаты прогнозов", callback_data="my_results")],
        [InlineKeyboardButton(text="ℹ️ О боте", callback_data="about")],
    ])

def get_back_button():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_start")]
    ])

def get_pagination_keyboard(current_page: int, total_pages: int, sport: str):
    """Создать клавиатуру с пагинацией"""
    keyboard = []
    row = []
    
    # Кнопка "Назад"
    if current_page > 1:
        row.append(InlineKeyboardButton(
            text="◀️ Назад", 
            callback_data=f"page_{sport}_{current_page - 1}"
        ))
    
    # Индикатор страницы
    row.append(InlineKeyboardButton(
        text=f"{current_page}/{total_pages}",
        callback_data="page_info"
    ))
    
    # Кнопка "Вперёд"
    if current_page < total_pages:
        row.append(InlineKeyboardButton(
            text="Вперёд ▶️", 
            callback_data=f"page_{sport}_{current_page + 1}"
        ))
    
    keyboard.append(row)
    
    # Кнопка "Главное меню"
    keyboard.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_start")])
    
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


@dp.callback_query(F.data == "back_to_start")
async def back_to_start(callback: types.CallbackQuery):
    await callback.message.edit_text(
        "🏠 <b>Главное меню</b>\n\nВыберите раздел:",
        parse_mode="HTML",
        reply_markup=get_main_keyboard()
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("page_"))
async def handle_pagination(callback: types.CallbackQuery):
    """Обработка переключения страниц"""
    callback_data = callback.data
    
    if callback_data == "page_info":
        await callback.answer()
        return
    
    # Разбираем callback: page_sport_1
    parts = callback_data.split("_")
    sport = parts[1]
    page = int(parts[2])
    
    await callback.answer()
    
    # Показываем прогнозы для выбранного вида спорта
    if sport == "football":
        await show_sport_page(callback, "football", page)
    elif sport == "hockey":
        await show_sport_page(callback, "hockey", page)
    elif sport == "esports":
        await show_sport_page(callback, "esports", page)


async def show_sport_page(callback: types.CallbackQuery, sport: str, page: int = 1):
    """Показать страницу с прогнозами для вида спорта"""
    sport_names = {
        'football': '⚽️ Футбол', 
        'hockey': '🏒 Хоккей', 
        'esports': '🎮 Киберспорт'
    }
    
    await callback.answer("🔍 Загружаю матчи...")
    
    # Получаем матчи
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
    
    # Анализируем матчи
    analyzed = []
    for match in matches:
        try:
            pred = await analyze_match(match)
            if pred['confidence'] >= MIN_CONFIDENCE:
                analyzed.append((match, pred))
        except Exception as e:
            logger.error(f"Ошибка анализа: {e}")
    
    if not analyzed:
        await callback.message.edit_text(
            f"{sport_names[sport]} - <b>{len(matches)} матчей</b>\n\n"
            "Нет уверенных прогнозов (≥ 50%)\n\n",
            parse_mode="HTML",
            reply_markup=get_back_button()
        )
        return
    
    # Пагинация: 5 прогнозов на страницу
    per_page = 5
    total_pages = (len(analyzed) + per_page - 1) // per_page
    
    # Проверяем, что страница в пределах
    if page < 1:
        page = 1
    elif page > total_pages:
        page = total_pages
    
    # Получаем прогнозы для текущей страницы
    start_idx = (page - 1) * per_page
    end_idx = start_idx + per_page
    page_predictions = analyzed[start_idx:end_idx]
    
    # Формируем текст
    text = f"{sport_names[sport]} - <b>{len(analyzed)} прогнозов</b>\n"
    text += f"📄 <b>Страница {page} из {total_pages}</b>\n\n"
    
    for i, (match, pred) in enumerate(page_predictions, start_idx + 1):
        tournament = match.get('tournament', 'Unknown')
        
        if sport == 'esports':
            # Для киберспорта: только команды и турнир
            text += (
                f"{i}. 🏆 <b>{tournament}</b>\n"
                f"   {match['team1']} vs {match['team2']}\n"
                f"   💰 {pred['recommendation']} ({pred['confidence']}%)\n"
                f"   🎯 Тип: {pred['bet_type']}\n\n"
            )
        else:
            # Для футбола/хоккея: турнир + команды
            text += (
                f"{i}. 🏆 <b>{tournament}</b>\n"
                f"   {match['team1']} vs {match['team2']}\n"
                f"   💰 {pred['recommendation']} ({pred['confidence']}%)\n"
                f"   🎯 Тип: {pred['bet_type']}\n\n"
            )
    
    # Добавляем информацию о диапазоне
    text += f"📊 Показаны {start_idx + 1}-{min(end_idx, len(analyzed))} из {len(analyzed)}\n"
    
    # Создаём клавиатуру с пагинацией
    keyboard = get_pagination_keyboard(page, total_pages, sport)
    
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)


@dp.callback_query(F.data.startswith("sport_"))
async def show_sport(callback: types.CallbackQuery):
    """Показать первую страницу прогнозов для вида спорта"""
    sport = callback.data.split("_")[1]
    await show_sport_page(callback, sport, page=1)


@dp.callback_query(F.data == "today")
async def show_today(callback: types.CallbackQuery):
    await callback.answer("⏳ Загружаю прогнозы...")
    
    predictions = await get_today_predictions()
    
    if not predictions:
        await callback.message.edit_text(
            "😔 На сегодня пока нет прогнозов.\n\n"
            "Попробуйте позже или дождитесь рассылки.",
            reply_markup=get_back_button()
        )
        return
    
    # Пагинация для "Прогнозы на сегодня"
    per_page = 5
    total_pages = (len(predictions) + per_page - 1) // per_page
    
    # Показываем первую страницу
    page_predictions = predictions[:per_page]
    
    text = f"📅 <b>Прогнозы на сегодня ({len(predictions)} шт.)</b>\n"
    text += f"📄 <b>Страница 1 из {total_pages}</b>\n\n"
    
    for i, pred in enumerate(page_predictions, 1):
        match_id, sport, team1, team2, tournament, analysis, probs_json, rec, conf, bet_type = pred
        sport_emoji = {'football': '⚽️', 'hockey': '🏒', 'esports': '🎮'}.get(sport, '🏆')
        
        text += f"{i}. {sport_emoji} <b>{team1} vs {team2}</b>\n"
        text += f"   🏆 {tournament}\n"
        text += f"   💰 {rec} ({conf}%)\n"
        text += f"   🎯 Тип: {bet_type}\n\n"
    
    if total_pages > 1:
        text += f"📊 Показаны 1-{min(per_page, len(predictions))} из {len(predictions)}\n"
        keyboard = get_pagination_keyboard(1, total_pages, "today")
    else:
        keyboard = get_back_button()
    
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)


@dp.callback_query(F.data == "my_results")
async def show_my_results(callback: types.CallbackQuery):
    """Показать результаты прогнозов пользователя"""
    await callback.answer("📊 Загружаю результаты...")
    
    user_id = callback.from_user.id
    stats = await get_user_stats(user_id)
    
    if not stats or stats['total'] == 0:
        await callback.message.edit_text(
            "📊 <b>Ваши результаты</b>\n\n"
            "Пока нет проверенных прогнозов.\n"
            "Статистика появится после завершения матчей.",
            parse_mode="HTML",
            reply_markup=get_back_button()
        )
        return
    
    text = (
        "📊 <b>Ваши результаты</b>\n\n"
        f"✅ <b>Верных прогнозов:</b> {stats['correct']} из {stats['total']}\n"
        f"📈 <b>Точность:</b> {stats['accuracy']}%\n"
        f"🎯 <b>Средняя уверенность:</b> {stats['avg_confidence']}%\n\n"
    )
    
    if stats['accuracy'] >= 60:
        text += "✅ Отличный результат! Продолжайте в том же духе!"
    elif stats['accuracy'] >= 50:
        text += "⚠️ Неплохо, но есть куда расти."
    else:
        text += "🚨 Точность ниже ожидаемой. Помните о рисках!"
    
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=get_back_button())


@dp.callback_query(F.data == "my_stats")
async def show_stats(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    stats = await get_user_stats(user_id)
    
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
    )
    
    if stats:
        text += (
            f"✅ <b>Верных прогнозов:</b> {stats['correct']} из {stats['total']}\n"
            f"📈 <b>Точность:</b> {stats['accuracy']}%\n"
            f"🎯 <b>Средняя уверенность:</b> {stats['avg_confidence']}%\n\n"
        )
    else:
        text += "<i>Статистика появится после проверки матчей.</i>\n\n"
    
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
        "🔍 <b>Самопроверка:</b>\n"
        "Бот сравнивает свои прогнозы с коэффициентами букмекеров и корректирует их при больших расхождениях.\n\n"
        "📈 <b>Обучение:</b>\n"
        "После завершения матчей бот анализирует свои ошибки и улучшает модель.\n\n"
        "⚠️ <i>Ставки — это риск. Играйте ответственно!</i>"
    )
    
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=get_back_button())


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await add_user(message.from_user.id, message.from_user.username or "unknown")
    
    text = (
        f"👋 <b>Привет, {message.from_user.first_name}!</b>\n\n"
        f"🤖 Я — <b>ML-бот для спортивных прогнозов</b>\n\n"
        f"🧮 <b>Как я работаю:</b>\n"
        f"• Использую <b>4 математические модели</b>\n"
        f"• Собираю данные из <b>4 источников</b>\n"
        f"• Сравниваю с коэффициентами БК\n"
        f"• Самопроверка при расхождениях\n"
        f"• Учусь на своих ошибках\n\n"
        f"⏰ <b>Рассылка:</b>\n"
        f"3 прогноза каждый час\n\n"
        f"🏆 <b>Виды спорта:</b>\n"
        f"⚽️ Футбол | 🏒 Хоккей | 🎮 Киберспорт"
    )
    
    await message.answer(text, parse_mode="HTML", reply_markup=get_main_keyboard())


@dp.message(Command("first_train"))
async def cmd_first_train(message: types.Message):
    await first_train(message)


@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    text = (
        "📖 <b>Команды бота:</b>\n\n"
        "/start — Главное меню\n"
        "/first_train — Первое обучение\n"
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
