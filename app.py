import os
import sys
import sqlite3
import json
import logging
import random
import urllib.request
from functools import wraps
from werkzeug.security import generate_password_hash, check_password_hash
from flask import (
    Flask, g, render_template, request, redirect, url_for,
    flash, session
)

logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                    format='%(asctime)s %(levelname)s %(message)s')

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'change-this-to-a-random-secret')
DATABASE_URL = os.environ.get('SUPABASE_URL', os.environ.get('DATABASE_URL', ''))
DATABASE = os.path.join(app.root_path, 'betting.db')

STARTING_BALANCE = 1000  # €10.00 in cents
ROUNDS = ['Group Stage', 'Round of 16', 'Quarter-final', 'Semi-final', 'Final']

# ISO 3166-1 alpha-2 team -> flag helper
COUNTRY_CODES = {
    'Algeria': 'DZ', 'Argentina': 'AR', 'Australia': 'AU', 'Austria': 'AT',
    'Belgium': 'BE', 'Bosnia-Herzegovina': 'BA', 'Brazil': 'BR', 'Canada': 'CA',
    'Cape Verde Islands': 'CV', 'Colombia': 'CO', 'Congo DR': 'CD', 'Croatia': 'HR',
    'Curaçao': 'CW', 'Czech Republic': 'CZ', 'Ecuador': 'EC', 'Egypt': 'EG',
    'England': 'GB-ENG', 'France': 'FR', 'Germany': 'DE', 'Ghana': 'GH',
    'Haiti': 'HT', 'Iran': 'IR', 'Iraq': 'IQ', 'Ivory Coast': 'CI',
    'Japan': 'JP', 'Jordan': 'JO', 'Mexico': 'MX', 'Morocco': 'MA',
    'Netherlands': 'NL', 'New Zealand': 'NZ', 'Norway': 'NO', 'Panama': 'PA',
    'Paraguay': 'PY', 'Portugal': 'PT', 'Qatar': 'QA', 'Saudi Arabia': 'SA',
    'Scotland': 'GB-SCT', 'Senegal': 'SN', 'South Africa': 'ZA', 'South Korea': 'KR',
    'Spain': 'ES', 'Sweden': 'SE', 'Switzerland': 'CH', 'Tunisia': 'TN',
    'Turkey': 'TR', 'United States': 'US', 'Uruguay': 'UY', 'Uzbekistan': 'UZ',
}


def team_flag(team_name):
    code = COUNTRY_CODES.get(team_name)
    if not code or code == 'TBD':
        return ''
    # Use GB flag for England and Scotland (tag sequences don't render on Windows)
    if code in ('GB-ENG', 'GB-SCT'):
        code = 'GB'
    return ''.join(chr(0x1F1E6 + ord(c) - ord('A')) for c in code.upper())


CITY_OFFSETS = {
    'East Rutherford, NJ': -4, 'Los Angeles, CA': -7,
    'Arlington, TX': -5, 'Miami Gardens, FL': -4,
    'Atlanta, GA': -4, 'Houston, TX': -5,
    'Philadelphia, PA': -4, 'Santa Clara, CA': -7,
    'Kansas City, MO': -5, 'Foxborough, MA': -4,
    'Seattle, WA': -7, 'Landover, MD': -4,
    'Nashville, TN': -5, 'Glendale, AZ': -7,
    'Toronto, ON': -4, 'Vancouver, BC': -7,
    'Mexico City': -6, 'Guadalajara': -6, 'Monterrey': -6,
}


def lisbon_time(date_str, time_str, city=None):
    """Convert UTC time (from wheniskickoff.com) to Lisbon/London time (UTC+1)."""
    if not time_str or not date_str:
        return date_str, time_str or ''
    try:
        from datetime import datetime, timedelta
        dt = datetime.strptime(f'{date_str} {time_str}', '%Y-%m-%d %H:%M')
        lisbon_dt = dt + timedelta(hours=1)
        return lisbon_dt.strftime('%Y-%m-%d'), lisbon_dt.strftime('%H:%M')
    except (ValueError, TypeError):
        return date_str, time_str


def get_db():
    if 'db' in g:
        return g.db

    if DATABASE_URL:
        import psycopg2
        from psycopg2.extras import RealDictCursor
        g.db = psycopg2.connect(DATABASE_URL, sslmode='require', cursor_factory=RealDictCursor)
        g.db.autocommit = False
    else:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row

    return g.db


def is_pg():
    return bool(DATABASE_URL)


def p():
    return '%s' if is_pg() else '?'


def query(sql, params=None):
    db = get_db()
    if is_pg():
        cur = db.cursor()
        if params:
            cur.execute(sql, params)
        else:
            cur.execute(sql)
        return cur
    else:
        if params:
            return db.execute(sql, params)
        return db.execute(sql)


def now():
    return 'NOW()' if is_pg() else "datetime('now')"


def close_db(e=None):
    db = g.pop('db', None)
    if db is not None:
        if e and is_pg():
            try:
                db.rollback()
            except Exception:
                pass
        db.close()


def init_db():
    db = get_db()
    try:
        if is_pg():
            query("""
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT NOW()
                )
            """)
            query("""
                CREATE TABLE IF NOT EXISTS balance_records (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    balance INTEGER NOT NULL,
                    notes TEXT,
                    created_at TIMESTAMP NOT NULL DEFAULT NOW()
                )
            """)
            query("""
                CREATE INDEX IF NOT EXISTS idx_balance_user ON balance_records(user_id)
            """)
            query("""
                CREATE INDEX IF NOT EXISTS idx_balance_created ON balance_records(created_at)
            """)
            query("""
                CREATE TABLE IF NOT EXISTS bets (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    match_info TEXT NOT NULL,
                    round TEXT NOT NULL,
                    stake INTEGER NOT NULL,
                    odds REAL NOT NULL,
                    pick TEXT NOT NULL,
                    bet_type TEXT NOT NULL DEFAULT 'single',
                    selections TEXT,
                    result TEXT NOT NULL DEFAULT 'pending',
                    settled_at TIMESTAMP,
                    created_at TIMESTAMP NOT NULL DEFAULT NOW()
                )
            """)
            query("""
                CREATE INDEX IF NOT EXISTS idx_bets_user ON bets(user_id)
            """)
            query("""
                CREATE TABLE IF NOT EXISTS fixtures (
                    id SERIAL PRIMARY KEY,
                    match_number INTEGER,
                    round TEXT NOT NULL,
                    group_name TEXT,
                    home_team TEXT NOT NULL,
                    away_team TEXT NOT NULL,
                    date TEXT NOT NULL,
                    match_time TEXT,
                    venue TEXT,
                    city TEXT,
                    home_score INTEGER,
                    away_score INTEGER,
                    status TEXT NOT NULL DEFAULT 'upcoming'
                )
            """)
        else:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
                CREATE TABLE IF NOT EXISTS balance_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    balance INTEGER NOT NULL,
                    notes TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_balance_user ON balance_records(user_id);
                CREATE INDEX IF NOT EXISTS idx_balance_created ON balance_records(created_at);
                CREATE TABLE IF NOT EXISTS bets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    match_info TEXT NOT NULL,
                    round TEXT NOT NULL,
                    stake INTEGER NOT NULL,
                    odds REAL NOT NULL,
                    pick TEXT NOT NULL,
                    bet_type TEXT NOT NULL DEFAULT 'single',
                    selections TEXT,
                    result TEXT NOT NULL DEFAULT 'pending',
                    settled_at TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_bets_user ON bets(user_id);
                CREATE TABLE IF NOT EXISTS fixtures (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    match_number INTEGER,
                    round TEXT NOT NULL,
                    group_name TEXT,
                    home_team TEXT NOT NULL,
                    away_team TEXT NOT NULL,
                    date TEXT NOT NULL,
                    match_time TEXT,
                    venue TEXT,
                    city TEXT,
                    home_score INTEGER,
                    away_score INTEGER,
                    status TEXT NOT NULL DEFAULT 'upcoming'
                );
            """)
        db.commit()

        # Migration: add bet_type + selections columns for existing DBs
        try:
            if is_pg():
                query("ALTER TABLE bets ADD COLUMN IF NOT EXISTS bet_type TEXT NOT NULL DEFAULT 'single'")
                query("ALTER TABLE bets ADD COLUMN IF NOT EXISTS selections TEXT")
            else:
                query("ALTER TABLE bets ADD COLUMN bet_type TEXT NOT NULL DEFAULT 'single'")
                query("ALTER TABLE bets ADD COLUMN selections TEXT")
            db.commit()
        except Exception:
            db.rollback()

        # Seed/refresh fixtures every startup (ensures latest data)
        try:
            if is_pg():
                query('TRUNCATE TABLE fixtures')
            else:
                query('DELETE FROM fixtures')
            seed_fixtures()
            db.commit()
        except Exception:
            db.rollback()

        # migration: add match_time column if missing
        try:
            if is_pg():
                query("ALTER TABLE fixtures ADD COLUMN IF NOT EXISTS match_time TEXT")
            else:
                query("ALTER TABLE fixtures ADD COLUMN match_time TEXT")
            db.commit()
        except Exception:
            db.rollback()

        # migration: add match_number to bets
        try:
            if is_pg():
                query("ALTER TABLE bets ADD COLUMN IF NOT EXISTS match_number INTEGER")
            else:
                query("ALTER TABLE bets ADD COLUMN match_number INTEGER")
            db.commit()
        except Exception:
            db.rollback()
    except Exception as e:
        logging.error(f"init_db failed: {e}")
        raise


ROUNDS_FIXTURES = ['Group Stage', 'Round of 32', 'Round of 16', 'Quarter-final', 'Semi-final', 'Third Place', 'Final']


def seed_fixtures():
    TEAM_STRENGTH = {
        'Argentina': 92, 'Brazil': 91, 'France': 90, 'England': 89, 'Spain': 88,
        'Germany': 87, 'Netherlands': 86, 'Portugal': 85, 'Belgium': 84, 'Croatia': 83,
        'Uruguay': 82, 'United States': 80, 'Mexico': 80, 'Japan': 79, 'Morocco': 78,
        'Switzerland': 78, 'Senegal': 77, 'Iran': 76, 'South Korea': 76, 'Australia': 75,
        'Canada': 75, 'Norway': 75, 'Sweden': 75, 'Turkey': 74, 'Ecuador': 74,
        'Ivory Coast': 73, 'Egypt': 73, 'Algeria': 72, 'Paraguay': 72, 'Saudi Arabia': 71,
        'Scotland': 71, 'Austria': 71, 'Czech Republic': 70, 'Ghana': 70, 'Tunisia': 70,
        'Bosnia-Herzegovina': 69, 'Congo DR': 68, 'Jordan': 68, 'Iraq': 67, 'New Zealand': 66,
        'South Africa': 65, 'Uzbekistan': 64, 'Panama': 63, 'Haiti': 61, 'Curaçao': 60,
        'Cape Verde Islands': 60, 'Qatar': 59,
    }

    def sim(home, away):
        r = random.Random(home + away + 'wc2026')
        h = max(0, int(round(r.gauss(TEAM_STRENGTH.get(home, 70) / 30 - 1.5, 1.2))))
        a = max(0, int(round(r.gauss(TEAM_STRENGTH.get(away, 70) / 30 - 1.5, 1.2))))
        if h == a and r.random() < 0.25:
            if r.random() < 0.5: h += 1
            else: a += 1
        return h, a

    fixtures = [
        # ---- Group Stage (72 matches with simulated scores) ----
        (1,'Group Stage','A','Mexico','South Africa','2026-06-11','19:00','MetLife Stadium','East Rutherford, NJ'),
        (2,'Group Stage','A','South Korea','Czech Republic','2026-06-12','02:00','SoFi Stadium','Los Angeles, CA'),
        (3,'Group Stage','B','Canada','Bosnia-Herzegovina','2026-06-12','19:00',"AT&T Stadium",'Arlington, TX'),
        (4,'Group Stage','D','United States','Paraguay','2026-06-13','01:00','Hard Rock Stadium','Miami Gardens, FL'),
        (5,'Group Stage','B','Qatar','Switzerland','2026-06-13','19:00','Mercedes-Benz Stadium','Atlanta, GA'),
        (6,'Group Stage','C','Brazil','Morocco','2026-06-13','22:00','NRG Stadium','Houston, TX'),
        (7,'Group Stage','C','Haiti','Scotland','2026-06-14','01:00','Lincoln Financial Field','Philadelphia, PA'),
        (8,'Group Stage','D','Australia','Turkey','2026-06-14','04:00','Lumen Field','Seattle, WA'),
        (9,'Group Stage','E','Germany','Curaçao','2026-06-14','17:00',"Levi's Stadium",'Santa Clara, CA'),
        (10,'Group Stage','F','Netherlands','Japan','2026-06-14','20:00','Arrowhead Stadium','Kansas City, MO'),
        (11,'Group Stage','E','Ivory Coast','Ecuador','2026-06-14','23:00','Gillette Stadium','Foxborough, MA'),
        (12,'Group Stage','F','Sweden','Tunisia','2026-06-15','02:00','Estadio Azteca','Mexico City'),
        (13,'Group Stage','H','Spain','Cape Verde Islands','2026-06-15','16:00','Estadio Akron','Guadalajara'),
        (14,'Group Stage','G','Belgium','Egypt','2026-06-15','19:00','Estadio BBVA','Monterrey'),
        (15,'Group Stage','H','Saudi Arabia','Uruguay','2026-06-15','22:00','BMO Field','Toronto'),
        (16,'Group Stage','G','Iran','New Zealand','2026-06-16','01:00','BC Place','Vancouver'),
        (17,'Group Stage','I','France','Senegal','2026-06-16','19:00','MetLife Stadium','East Rutherford, NJ'),
        (18,'Group Stage','I','Iraq','Norway','2026-06-16','22:00','SoFi Stadium','Los Angeles, CA'),
        (19,'Group Stage','J','Argentina','Algeria','2026-06-17','01:00',"AT&T Stadium",'Arlington, TX'),
        (20,'Group Stage','J','Austria','Jordan','2026-06-17','04:00','Hard Rock Stadium','Miami Gardens, FL'),
        (21,'Group Stage','K','Portugal','Congo DR','2026-06-17','17:00','Mercedes-Benz Stadium','Atlanta, GA'),
        (22,'Group Stage','L','England','Croatia','2026-06-17','20:00','NRG Stadium','Houston, TX'),
        (23,'Group Stage','L','Ghana','Panama','2026-06-17','23:00','Lincoln Financial Field','Philadelphia, PA'),
        (24,'Group Stage','K','Uzbekistan','Colombia','2026-06-18','02:00','Lumen Field','Seattle, WA'),
        (25,'Group Stage','A','Czech Republic','South Africa','2026-06-18','16:00',"Levi's Stadium",'Santa Clara, CA'),
        (26,'Group Stage','B','Switzerland','Bosnia-Herzegovina','2026-06-18','19:00','Arrowhead Stadium','Kansas City, MO'),
        (27,'Group Stage','B','Canada','Qatar','2026-06-18','22:00','Gillette Stadium','Foxborough, MA'),
        (28,'Group Stage','A','Mexico','South Korea','2026-06-19','01:00','Estadio Azteca','Mexico City'),
        (29,'Group Stage','D','United States','Australia','2026-06-19','19:00','Estadio Akron','Guadalajara'),
        (30,'Group Stage','C','Scotland','Morocco','2026-06-19','22:00','Estadio BBVA','Monterrey'),
        (31,'Group Stage','C','Brazil','Haiti','2026-06-20','00:30','BMO Field','Toronto'),
        (32,'Group Stage','D','Turkey','Paraguay','2026-06-20','03:00','BC Place','Vancouver'),
        (33,'Group Stage','F','Netherlands','Sweden','2026-06-20','17:00','MetLife Stadium','East Rutherford, NJ'),
        (34,'Group Stage','E','Germany','Ivory Coast','2026-06-20','20:00','SoFi Stadium','Los Angeles, CA'),
        (35,'Group Stage','E','Ecuador','Curaçao','2026-06-21','00:00',"AT&T Stadium",'Arlington, TX'),
        (36,'Group Stage','F','Tunisia','Japan','2026-06-21','04:00','Hard Rock Stadium','Miami Gardens, FL'),
        (37,'Group Stage','H','Spain','Saudi Arabia','2026-06-21','16:00','Mercedes-Benz Stadium','Atlanta, GA'),
        (38,'Group Stage','G','Belgium','Iran','2026-06-21','19:00','NRG Stadium','Houston, TX'),
        (39,'Group Stage','H','Uruguay','Cape Verde Islands','2026-06-21','22:00','Lincoln Financial Field','Philadelphia, PA'),
        (40,'Group Stage','G','New Zealand','Egypt','2026-06-22','01:00','Lumen Field','Seattle, WA'),
        (41,'Group Stage','J','Argentina','Austria','2026-06-22','17:00',"Levi's Stadium",'Santa Clara, CA'),
        (42,'Group Stage','I','France','Iraq','2026-06-22','21:00','Arrowhead Stadium','Kansas City, MO'),
        (43,'Group Stage','I','Norway','Senegal','2026-06-23','00:00','Gillette Stadium','Foxborough, MA'),
        (44,'Group Stage','J','Jordan','Algeria','2026-06-23','03:00','Estadio Azteca','Mexico City'),
        (45,'Group Stage','K','Portugal','Uzbekistan','2026-06-23','17:00','Estadio Akron','Guadalajara'),
        (46,'Group Stage','L','England','Ghana','2026-06-23','20:00','Estadio BBVA','Monterrey'),
        (47,'Group Stage','L','Panama','Croatia','2026-06-23','23:00','BMO Field','Toronto'),
        (48,'Group Stage','K','Colombia','Congo DR','2026-06-24','02:00','BC Place','Vancouver'),
        (49,'Group Stage','B','Switzerland','Canada','2026-06-24','19:00','MetLife Stadium','East Rutherford, NJ'),
        (50,'Group Stage','B','Bosnia-Herzegovina','Qatar','2026-06-24','19:00','SoFi Stadium','Los Angeles, CA'),
        (51,'Group Stage','C','Morocco','Haiti','2026-06-24','22:00',"AT&T Stadium",'Arlington, TX'),
        (52,'Group Stage','C','Scotland','Brazil','2026-06-24','22:00','Hard Rock Stadium','Miami Gardens, FL'),
        (53,'Group Stage','A','Czech Republic','Mexico','2026-06-25','01:00','Mercedes-Benz Stadium','Atlanta, GA'),
        (54,'Group Stage','A','South Africa','South Korea','2026-06-25','01:00','NRG Stadium','Houston, TX'),
        (55,'Group Stage','E','Ecuador','Germany','2026-06-25','20:00','Lincoln Financial Field','Philadelphia, PA'),
        (56,'Group Stage','E','Curaçao','Ivory Coast','2026-06-25','20:00','Lumen Field','Seattle, WA'),
        (57,'Group Stage','F','Tunisia','Netherlands','2026-06-25','23:00',"Levi's Stadium",'Santa Clara, CA'),
        (58,'Group Stage','F','Japan','Sweden','2026-06-25','23:00','Arrowhead Stadium','Kansas City, MO'),
        (59,'Group Stage','D','Turkey','United States','2026-06-26','02:00','Gillette Stadium','Foxborough, MA'),
        (60,'Group Stage','D','Paraguay','Australia','2026-06-26','02:00','Estadio Azteca','Mexico City'),
        (61,'Group Stage','I','Norway','France','2026-06-26','19:00','Estadio Akron','Guadalajara'),
        (62,'Group Stage','I','Senegal','Iraq','2026-06-26','19:00','Estadio BBVA','Monterrey'),
        (63,'Group Stage','H','Uruguay','Spain','2026-06-27','00:00','BMO Field','Toronto'),
        (64,'Group Stage','H','Cape Verde Islands','Saudi Arabia','2026-06-27','00:00','BC Place','Vancouver'),
        (65,'Group Stage','G','New Zealand','Belgium','2026-06-27','03:00','MetLife Stadium','East Rutherford, NJ'),
        (66,'Group Stage','G','Egypt','Iran','2026-06-27','03:00','SoFi Stadium','Los Angeles, CA'),
        (67,'Group Stage','L','Panama','England','2026-06-27','21:00',"AT&T Stadium",'Arlington, TX'),
        (68,'Group Stage','L','Croatia','Ghana','2026-06-27','21:00','Hard Rock Stadium','Miami Gardens, FL'),
        (69,'Group Stage','K','Colombia','Portugal','2026-06-27','23:30','Mercedes-Benz Stadium','Atlanta, GA'),
        (70,'Group Stage','K','Congo DR','Uzbekistan','2026-06-27','23:30','NRG Stadium','Houston, TX'),
        (71,'Group Stage','J','Jordan','Argentina','2026-06-28','02:00','Lincoln Financial Field','Philadelphia, PA'),
        (72,'Group Stage','J','Algeria','Austria','2026-06-28','02:00','Lumen Field','Seattle, WA'),
        # ---- Knockout (no scores until groups finish) ----
        (73,'Round of 32',None,'TBD','TBD','2026-06-28','19:00',"Levi's Stadium",'Santa Clara, CA'),
        (74,'Round of 32',None,'TBD','TBD','2026-06-29','17:00','Arrowhead Stadium','Kansas City, MO'),
        (75,'Round of 32',None,'TBD','TBD','2026-06-29','20:30','Gillette Stadium','Foxborough, MA'),
        (76,'Round of 32',None,'TBD','TBD','2026-06-30','01:00','Estadio Azteca','Mexico City'),
        (77,'Round of 32',None,'TBD','TBD','2026-06-30','17:00','Estadio Akron','Guadalajara'),
        (78,'Round of 32',None,'TBD','TBD','2026-06-30','21:00','Estadio BBVA','Monterrey'),
        (79,'Round of 32',None,'TBD','TBD','2026-07-01','01:00','BMO Field','Toronto'),
        (80,'Round of 32',None,'TBD','TBD','2026-07-01','16:00','BC Place','Vancouver'),
        (81,'Round of 32',None,'TBD','TBD','2026-07-01','20:00','MetLife Stadium','East Rutherford, NJ'),
        (82,'Round of 32',None,'TBD','TBD','2026-07-02','00:00','SoFi Stadium','Los Angeles, CA'),
        (83,'Round of 32',None,'TBD','TBD','2026-07-02','19:00',"AT&T Stadium",'Arlington, TX'),
        (84,'Round of 32',None,'TBD','TBD','2026-07-02','23:00','Hard Rock Stadium','Miami Gardens, FL'),
        (85,'Round of 32',None,'TBD','TBD','2026-07-03','03:00','Mercedes-Benz Stadium','Atlanta, GA'),
        (86,'Round of 32',None,'TBD','TBD','2026-07-03','18:00','NRG Stadium','Houston, TX'),
        (87,'Round of 32',None,'TBD','TBD','2026-07-03','22:00','Lincoln Financial Field','Philadelphia, PA'),
        (88,'Round of 32',None,'TBD','TBD','2026-07-04','01:30','Lumen Field','Seattle, WA'),
        (89,'Round of 16',None,'TBD','TBD','2026-07-04','17:00',"Levi's Stadium",'Santa Clara, CA'),
        (90,'Round of 16',None,'TBD','TBD','2026-07-04','21:00','Arrowhead Stadium','Kansas City, MO'),
        (91,'Round of 16',None,'TBD','TBD','2026-07-05','20:00','Gillette Stadium','Foxborough, MA'),
        (92,'Round of 16',None,'TBD','TBD','2026-07-06','00:00','Estadio Azteca','Mexico City'),
        (93,'Round of 16',None,'TBD','TBD','2026-07-06','19:00','Estadio Akron','Guadalajara'),
        (94,'Round of 16',None,'TBD','TBD','2026-07-07','00:00','Estadio BBVA','Monterrey'),
        (95,'Round of 16',None,'TBD','TBD','2026-07-07','16:00','BMO Field','Toronto'),
        (96,'Round of 16',None,'TBD','TBD','2026-07-07','20:00','BC Place','Vancouver'),
        (97,'Quarter-final',None,'TBD','TBD','2026-07-09','20:00','MetLife Stadium','East Rutherford, NJ'),
        (98,'Quarter-final',None,'TBD','TBD','2026-07-10','19:00','SoFi Stadium','Los Angeles, CA'),
        (99,'Quarter-final',None,'TBD','TBD','2026-07-11','21:00',"AT&T Stadium",'Arlington, TX'),
        (100,'Quarter-final',None,'TBD','TBD','2026-07-12','01:00','Hard Rock Stadium','Miami Gardens, FL'),
        (101,'Semi-final',None,'TBD','TBD','2026-07-14','19:00','Mercedes-Benz Stadium','Atlanta, GA'),
        (102,'Semi-final',None,'TBD','TBD','2026-07-15','19:00','NRG Stadium','Houston, TX'),
        (103,'Third Place',None,'TBD','TBD','2026-07-18','21:00','Lincoln Financial Field','Philadelphia, PA'),
        (104,'Final',None,'TBD','TBD','2026-07-19','19:00','Lumen Field','Seattle, WA'),
    ]
    import datetime
    now = datetime.datetime.utcnow()
    for f in fixtures:
        mn, rnd, grp, home, away, dt, tm, ven, cty = f
        hs, a_s = None, None
        st = 'upcoming'
        if home != 'TBD':
            try:
                match_dt = datetime.datetime.strptime(dt + ' ' + tm, '%Y-%m-%d %H:%M')
                is_past = match_dt <= now
            except (ValueError, TypeError):
                is_past = False
            if is_past:
                hs, a_s = sim(home, away)
                st = 'completed'
        query(
            'INSERT INTO fixtures (match_number, round, group_name, home_team, away_team, date, match_time, venue, city, home_score, away_score, status) '
            f'VALUES ({p()}, {p()}, {p()}, {p()}, {p()}, {p()}, {p()}, {p()}, {p()}, {p()}, {p()}, {p()})',
            (mn, rnd, grp, home, away, dt, tm, ven, cty, hs, a_s, st)
        )


def user_balance(user_id):
    db = get_db()
    total_staked = 0
    total_returned = 0
    rows = query(
        f'SELECT stake, odds, result FROM bets WHERE user_id = {p()}',
        (user_id,)
    ).fetchall()
    for r in rows:
        total_staked += r['stake']
        if r['result'] == 'won':
            total_returned += round(r['stake'] * r['odds'])
    return STARTING_BALANCE - total_staked + total_returned


def user_bets(user_id):
    db = get_db()
    rows = query(
        f'SELECT * FROM bets WHERE user_id = {p()} ORDER BY created_at DESC',
        (user_id,)
    ).fetchall()
    bets = []
    for r in rows:
        d = dict(r)
        d['stake'] = d['stake'] / 100.0
        if d.get('selections'):
            try:
                d['selections'] = json.loads(d['selections'])
            except (json.JSONDecodeError, TypeError):
                d['selections'] = None
        bets.append(d)
    return bets


def user_bet_stats(user_id):
    db = get_db()
    rows = query(
        f'SELECT result, COUNT(*) as cnt, SUM(stake) as total FROM bets WHERE user_id = {p()} GROUP BY result',
        (user_id,)
    ).fetchall()
    stats = {'won': 0, 'lost': 0, 'pending': 0, 'cashout': 0, 'total': 0, 'staked': 0, 'won_amount': 0}
    for r in rows:
        result = r['result']
        stats[result] = r['cnt']
        stats['total'] += r['cnt']
        stats['staked'] += r['total'] if r['total'] else 0
    # pending stake (unsettled bets)
    pending_row = query(
        f'SELECT COALESCE(SUM(stake), 0) as pending_total FROM bets WHERE user_id = {p()} AND result = \'pending\'',
        (user_id,)
    ).fetchone()
    stats['pending_stake'] = pending_row['pending_total'] if pending_row else 0
    # calculate win rate
    settled = stats['won'] + stats['lost']
    stats['win_rate'] = round(stats['won'] / settled * 100) if settled > 0 else 0
    stats['staked'] = stats['staked'] / 100.0
    stats['pending_stake'] = stats['pending_stake'] / 100.0
    return stats


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            flash('Please log in first.', 'warning')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            flash('Please log in first.', 'warning')
            return redirect(url_for('login'))
        db = get_db()
        user = query(f'SELECT username FROM users WHERE id = {p()}', (session['user_id'],)).fetchone()
        if not user or user['username'] != 'Joao':
            flash('Admin access required.', 'danger')
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated


@app.teardown_appcontext
def teardown(exception):
    close_db()


@app.context_processor
def inject_globals():
    return dict(team_flag=team_flag, lisbon_time=lisbon_time)


@app.errorhandler(500)
def server_error(e):
    logging.error(f"500 error: {e}")
    return "Internal Server Error. Check logs for details.", 500


@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))


@app.route('/debug-env')
def debug_env():
    import psycopg2
    su = os.environ.get('SUPABASE_URL', 'NOT SET')
    du = os.environ.get('DATABASE_URL', 'NOT SET')
    using = su if su != 'NOT SET' else du
    masked = using[:30] + '...' + using[-10:] if using not in ('NOT SET', '') else using
    # Try connecting and counting tables
    try:
        conn = psycopg2.connect(using, sslmode='require')
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")
        tbl = cur.fetchone()[0]
        conn.close()
        return (f"SUPABASE_URL: {masked}<br>"
                f"DATABASE_URL: {'SET' if du != 'NOT SET' else 'NOT SET'}<br>"
                f"Using: {using[:8]}...<br>"
                f"Tables in public: {tbl}")
    except Exception as e:
        return f"SUPABASE_URL: {masked}<br>Connection error: {e}"


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username'].strip()
        password = request.form['password']
        confirm = request.form['confirm_password']

        if not username or not password:
            flash('Username and password are required.', 'danger')
            return render_template('register.html')

        if password != confirm:
            flash('Passwords do not match.', 'danger')
            return render_template('register.html')

        db = get_db()
        existing = query(
            f'SELECT id FROM users WHERE username = {p()}', (username,)
        ).fetchone()
        if existing:
            flash('Username already taken.', 'danger')
            return render_template('register.html')

        pw_hash = generate_password_hash(password, method='pbkdf2:sha256')
        query(
            f'INSERT INTO users (username, password_hash) VALUES ({p()}, {p()})',
            (username, pw_hash)
        )
        db.commit()

        flash('Account created! You can now log in.', 'success')
        return redirect(url_for('login'))

    return render_template('register.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username'].strip()
        password = request.form['password']

        db = get_db()
        user = query(
            f'SELECT * FROM users WHERE username = {p()}', (username,)
        ).fetchone()

        if user and check_password_hash(user['password_hash'], password):
            session['user_id'] = user['id']
            session['username'] = user['username']
            flash(f'Welcome back, {user["username"]}!', 'success')
            return redirect(url_for('dashboard'))

        flash('Invalid username or password.', 'danger')

    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
    flash('You have been logged out.', 'info')
    return redirect(url_for('login'))


@app.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    db = get_db()
    user_id = session['user_id']
    user = query(f'SELECT * FROM users WHERE id = {p()}', (user_id,)).fetchone()

    if request.method == 'POST':
        current = request.form.get('current_password', '')
        new_pass = request.form.get('new_password', '')
        confirm = request.form.get('confirm_password', '')

        if not check_password_hash(user['password_hash'], current):
            flash('Current password is incorrect.', 'danger')
        elif not new_pass or len(new_pass) < 4:
            flash('New password must be at least 4 characters.', 'danger')
        elif new_pass != confirm:
            flash('New passwords do not match.', 'danger')
        else:
            new_hash = generate_password_hash(new_pass, method='pbkdf2:sha256')
            query(f'UPDATE users SET password_hash = {p()} WHERE id = {p()}', (new_hash, user_id))
            db.commit()
            flash('Password updated successfully!', 'success')
            return redirect(url_for('profile'))

    return render_template('profile.html', username=user['username'])


@app.route('/dashboard')
@login_required
def dashboard():
    db = get_db()
    user_id = session['user_id']

    try:
        balance = user_balance(user_id)
        bets = user_bets(user_id)
        stats = user_bet_stats(user_id)
        # recent activity from all users
        recent = query(
            f'SELECT u.username, b.match_info, b.pick, b.stake, b.odds, b.result, b.bet_type, b.created_at '
            f'FROM bets b JOIN users u ON b.user_id = u.id '
            f'ORDER BY b.created_at DESC LIMIT 20'
        ).fetchall()
        activity = []
        for r in recent:
            d = dict(r)
            d['stake'] = d['stake'] / 100.0
            activity.append(d)
        # fixtures for dropdown
        fixtures_list = query(
            "SELECT match_number, home_team, away_team, round, date FROM fixtures WHERE home_team != 'TBD' AND away_team != 'TBD' ORDER BY match_number"
        ).fetchall()
        fixtures_for_select = [dict(r) for r in fixtures_list]
        # balance history for sparkline
        hist = query(
            f'SELECT balance, created_at FROM balance_records WHERE user_id = {p()} ORDER BY created_at ASC',
            (user_id,)
        ).fetchall()
        balance_history = [dict(r) for r in hist]
        # fixture scores lookup for bet details (include future matches too for info)
        score_rows = query(
            'SELECT match_number, home_team, away_team, home_score, away_score, status, date, match_time FROM fixtures'
        ).fetchall()
        fixture_scores = {str(r['match_number']): r for r in score_rows}
        # next upcoming match
        next_match = query(
            f'SELECT * FROM fixtures WHERE home_team != \'TBD\' AND away_team != \'TBD\' AND home_score IS NULL ORDER BY date ASC, match_time ASC LIMIT 1'
        ).fetchone()
        if next_match:
            next_match = dict(next_match)
            next_match['lisbon_date'], next_match['lisbon_time'] = lisbon_time(
                next_match['date'], next_match['match_time'], next_match.get('city', ''))
    except Exception as e:
        logging.error(f"dashboard error: {e}")
        raise

    return render_template('dashboard.html',
                           balance=balance / 100.0,
                           bets=bets,
                           stats=stats,
                           activity=activity,
                           fixtures=fixtures_for_select,
                           balance_history=balance_history,
                           starting=STARTING_BALANCE / 100.0,
                           next_match=next_match,
                           fixture_scores=fixture_scores)


@app.route('/bet/new', methods=['POST'])
@login_required
def new_bet():
    bet_type = request.form.get('bet_type', 'single')
    stake_str = request.form.get('stake', '').strip()
    odds_str = request.form.get('odds', '').strip()

    if bet_type == 'multi':
        matches = request.form.getlist('multi_match[]')
        picks = request.form.getlist('multi_pick[]')
        match_numbers = request.form.getlist('multi_match_number[]')
        round_ = 'Multi'
        if not matches or not picks or not stake_str or not odds_str:
            flash('All bet fields are required.', 'danger')
            return redirect(url_for('dashboard'))
        selections = [{"match": m.strip(), "pick": p.strip()} for m, p in zip(matches, picks) if m.strip() and p.strip()]
        if len(selections) < 2:
            flash('Multi-bet must have at least 2 selections.', 'danger')
            return redirect(url_for('dashboard'))
        match_info = ' / '.join(s['match'] for s in selections)
        pick = ' / '.join(s['pick'] for s in selections)
        selections_json = json.dumps(selections)
        match_number = int(match_numbers[0]) if match_numbers and match_numbers[0] else None
    else:
        match_info = request.form.get('match_info', '').strip()
        pick = request.form.get('pick', '').strip()
        match_number = request.form.get('match_number', type=int)
        if not all([match_info, stake_str, odds_str, pick]):
            flash('All bet fields are required.', 'danger')
            return redirect(url_for('dashboard'))
        # look up round from the selected fixture
        if match_number:
            row = query(f'SELECT round FROM fixtures WHERE match_number = {p()}', (match_number,)).fetchone()
            round_ = row['round'] if row else ''
        else:
            round_ = ''
        selections_json = None
        bet_type = 'single'

    try:
        stake_str = stake_str.replace(',', '.')
        stake_raw = float(stake_str)
        if stake_raw <= 0:
            raise ValueError
        if '.' in stake_str and len(stake_str.split('.')[1]) > 2:
            flash('Stake must have at most 2 decimal places (e.g. 3.00 or 3).', 'danger')
            return redirect(url_for('dashboard'))
        stake = round(stake_raw * 100)
        odds_str = odds_str.replace(',', '.')
        odds = float(odds_str)
        if '.' in odds_str and len(odds_str.split('.')[1]) > 2:
            flash('Odds must have at most 2 decimal places (e.g. 2.10 or 3).', 'danger')
            return redirect(url_for('dashboard'))
    except ValueError:
        flash('Stake and odds must be valid positive numbers.', 'danger')
        return redirect(url_for('dashboard'))

    if stake < 1:
        flash('Stake must be at least €0.01.', 'danger')
        return redirect(url_for('dashboard'))

    if odds <= 1:
        flash('Odds must be greater than 1.00.', 'danger')
        return redirect(url_for('dashboard'))

    db = get_db()
    if user_balance(session['user_id']) < stake:
        db.rollback()
        flash('Insufficient balance. You cannot stake more than your available funds.', 'danger')
        return redirect(url_for('dashboard'))

    query(
        'INSERT INTO bets (user_id, match_info, round, stake, odds, pick, bet_type, selections, match_number) '
        f'VALUES ({p()}, {p()}, {p()}, {p()}, {p()}, {p()}, {p()}, {p()}, {p()})',
        (session['user_id'], match_info, round_, stake, odds, pick, bet_type, selections_json,
         match_number)
    )
    db.commit()
    # record balance after bet placed
    try:
        bal = user_balance(session['user_id'])
        query(
            f'INSERT INTO balance_records (user_id, balance, notes) VALUES ({p()}, {p()}, {p()})',
            (session['user_id'], bal, f"Bet placed: {match_info}")
        )
        db.commit()
    except Exception:
        db.rollback()

    flash('Bet placed!', 'success')
    return redirect(url_for('dashboard'))


@app.route('/bet/<int:bet_id>/settle', methods=['POST'])
@login_required
def settle_bet(bet_id):
    result = request.form.get('result', '').strip()
    if result not in ('won', 'lost', 'cashout'):
        flash('Invalid result.', 'danger')
        return redirect(url_for('dashboard'))

    db = get_db()
    bet = query(
        f'SELECT * FROM bets WHERE id = {p()} AND user_id = {p()}',
        (bet_id, session['user_id'])
    ).fetchone()

    if not bet:
        flash('Bet not found.', 'danger')
        return redirect(url_for('dashboard'))

    if bet['result'] != 'pending':
        flash('This bet has already been settled.', 'danger')
        return redirect(url_for('dashboard'))

    query(
        f'UPDATE bets SET result = {p()}, settled_at = {now()} WHERE id = {p()}',
        (result, bet_id)
    )
    db.commit()

    # record balance after settlement
    try:
        bal = user_balance(session['user_id'])
        query(
            f'INSERT INTO balance_records (user_id, balance, notes) VALUES ({p()}, {p()}, {p()})',
            (session['user_id'], bal, f"Bet {result}: {bet['match_info']}")
        )
        db.commit()
    except Exception:
        db.rollback()

    flash('Bet result updated!', 'success')
    return redirect(url_for('dashboard'))


@app.route('/bet/<int:bet_id>/delete', methods=['POST'])
@login_required
def delete_bet(bet_id):
    db = get_db()
    bet = query(
        f'SELECT * FROM bets WHERE id = {p()} AND user_id = {p()}',
        (bet_id, session['user_id'])
    ).fetchone()

    if not bet:
        flash('Bet not found.', 'danger')
        return redirect(url_for('dashboard'))

    if bet['result'] != 'pending':
        flash('Cannot delete a settled bet.', 'danger')
        return redirect(url_for('dashboard'))

    query(
        f'DELETE FROM bets WHERE id = {p()}',
        (bet_id,)
    )
    db.commit()

    flash('Bet deleted.', 'success')
    return redirect(url_for('dashboard'))


@app.route('/bet/<int:bet_id>/edit', methods=['POST'])
@login_required
def edit_bet(bet_id):
    db = get_db()
    bet = query(f'SELECT * FROM bets WHERE id = {p()}', (bet_id,)).fetchone()
    if not bet:
        flash('Bet not found.', 'danger')
        return redirect(url_for('dashboard'))

    # only owner or admin can edit
    is_admin = False
    user = query(f'SELECT username FROM users WHERE id = {p()}', (session['user_id'],)).fetchone()
    if user and user['username'] == 'Joao':
        is_admin = True
    if bet['user_id'] != session['user_id'] and not is_admin:
        flash('You can only edit your own bets.', 'danger')
        return redirect(url_for('dashboard'))

    if bet['result'] != 'pending':
        flash('Only pending bets can be edited.', 'danger')
        return redirect(url_for('dashboard'))

    match_info = request.form.get('match_info', '').strip()
    pick = request.form.get('pick', '').strip()
    match_number = request.form.get('match_number', type=int)
    stake_str = request.form.get('stake', '').strip()
    odds_str = request.form.get('odds', '').strip()

    if not all([match_info, pick, stake_str, odds_str]):
        flash('All fields are required.', 'danger')
        return redirect(url_for('dashboard'))

    try:
        stake_str = stake_str.replace(',', '.')
        stake_raw = float(stake_str)
        if stake_raw <= 0:
            raise ValueError
        if '.' in stake_str and len(stake_str.split('.')[1]) > 2:
            flash('Stake must have at most 2 decimal places.', 'danger')
            return redirect(url_for('dashboard'))
        new_stake_cents = round(stake_raw * 100)
        odds_str = odds_str.replace(',', '.')
        odds = float(odds_str)
        if '.' in odds_str and len(odds_str.split('.')[1]) > 2:
            flash('Odds must have at most 2 decimal places.', 'danger')
            return redirect(url_for('dashboard'))
    except ValueError:
        flash('Stake and odds must be valid positive numbers.', 'danger')
        return redirect(url_for('dashboard'))

    if new_stake_cents < 1:
        flash('Stake must be at least €0.01.', 'danger')
        return redirect(url_for('dashboard'))

    if odds <= 1:
        flash('Odds must be greater than 1.00.', 'danger')
        return redirect(url_for('dashboard'))

    # check balance delta
    old_stake_cents = bet['stake']
    extra_needed = new_stake_cents - old_stake_cents
    if extra_needed > 0:
        bal = user_balance(bet['user_id'])
        if bal < extra_needed:
            flash(f'Insufficient balance. Need €{extra_needed/100:.2f} more, have €{bal/100:.2f}.', 'danger')
            return redirect(url_for('dashboard'))

    # look up round from the selected fixture
    round_ = ''
    if match_number:
        row = query(f'SELECT round FROM fixtures WHERE match_number = {p()}', (match_number,)).fetchone()
        round_ = row['round'] if row else ''

    query(
        f'UPDATE bets SET match_info = {p()}, pick = {p()}, match_number = {p()}, stake = {p()}, odds = {p()}, round = {p()} WHERE id = {p()}',
        (match_info, pick, match_number, new_stake_cents, odds, round_, bet_id)
    )
    db.commit()

    # record balance change
    try:
        bal = user_balance(bet['user_id'])
        query(
            f'INSERT INTO balance_records (user_id, balance, notes) VALUES ({p()}, {p()}, {p()})',
            (bet['user_id'], bal, f"Bet edited: {match_info}")
        )
        db.commit()
    except Exception:
        db.rollback()

    flash('Bet updated!', 'success')
    return redirect(url_for('dashboard'))


@app.route('/admin', methods=['GET', 'POST'])
@login_required
@admin_required
def admin_panel():
    db = get_db()
    users = query('SELECT id, username FROM users ORDER BY username').fetchall()

    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'delete_user':
            uid = request.form.get('user_id', type=int)
            if uid:
                query('DELETE FROM bets WHERE user_id = %s' if is_pg() else 'DELETE FROM bets WHERE user_id = ?', (uid,))
                query('DELETE FROM balance_records WHERE user_id = %s' if is_pg() else 'DELETE FROM balance_records WHERE user_id = ?', (uid,))
                query('DELETE FROM users WHERE id = %s' if is_pg() else 'DELETE FROM users WHERE id = ?', (uid,))
                db.commit()
                flash('User and all their data deleted.', 'success')

        elif action == 'reset_balance':
            uid = request.form.get('user_id', type=int)
            if uid:
                query('DELETE FROM balance_records WHERE user_id = %s' if is_pg() else 'DELETE FROM balance_records WHERE user_id = ?', (uid,))
                db.commit()
                flash(f'Balance records wiped for user {uid}. They will be re-credited on next bet.', 'success')

        elif action == 'delete_all_bets':
            uid = request.form.get('user_id', type=int)
            if uid:
                query('DELETE FROM bets WHERE user_id = %s' if is_pg() else 'DELETE FROM bets WHERE user_id = ?', (uid,))
                query('DELETE FROM balance_records WHERE user_id = %s' if is_pg() else 'DELETE FROM balance_records WHERE user_id = ?', (uid,))
                db.commit()
                flash(f'All bets and balance records deleted for user {uid}.', 'success')

        elif action == 'sync_fixtures':
            ok = sync_fixtures_from_api()
            if ok:
                flash('Fixtures synced from wheniskickoff.com!', 'success')
            else:
                flash('Failed to sync fixtures.', 'danger')

        elif action == 'reseed_fixtures':
            seed_fixtures()
            flash('Fixtures re-seeded successfully.', 'success')

        return redirect(url_for('admin_panel'))

    user_data = []
    for u in users:
        bal = user_balance(u['id'])
        stats = user_bet_stats(u['id'])
        bet_count = query(
            f'SELECT count(*) as cnt FROM bets WHERE user_id = {p()}', (u['id'],)
        ).fetchone()['cnt']
        user_data.append({
            'id': u['id'],
            'username': u['username'],
            'balance': bal / 100.0,
            'bet_count': bet_count,
            'wins': stats.get('wins', 0),
            'total': stats.get('total', 0),
            'staked': stats.get('staked', 0),
        })

    return render_template('admin_panel.html', users=user_data)


@app.route('/fixtures')
@login_required
def fixtures():
    db = get_db()
    try:
        rows = query(
            'SELECT * FROM fixtures ORDER BY match_number'
        ).fetchall()
        fixtures_list = [dict(r) for r in rows]
        # bet totals per match
        bet_data = query(
            f'SELECT match_number, COUNT(*) as bet_count, SUM(stake) as total_staked '
            f'FROM bets WHERE match_number IS NOT NULL GROUP BY match_number'
        ).fetchall()
        bet_map = {}
        for b in bet_data:
            bet_map[b['match_number']] = {
                'count': b['bet_count'],
                'total': (b['total_staked'] or 0) / 100.0,
            }
        for f in fixtures_list:
            info = bet_map.get(f['match_number'])
            f['bet_count'] = info['count'] if info else 0
            f['bet_total'] = info['total'] if info else 0

        grouped = {}
        for f in fixtures_list:
            grouped.setdefault(f['round'], []).append(f)
        for round_name, round_fixtures in grouped.items():
            if round_name == 'Group Stage':
                by_group = {}
                for f in round_fixtures:
                    by_group.setdefault(f['group_name'], []).append(f)
                grouped[round_name] = by_group
    except Exception as e:
        logging.error(f"fixtures error: {e}")
        raise
    return render_template('fixtures.html', fixtures=grouped, rounds=ROUNDS_FIXTURES)


GROUP_NAMES = ['A','B','C','D','E','F','G','H','I','J','K','L']

@app.route('/standings')
@login_required
def standings():
    db = get_db()
    groups = {}
    for g in GROUP_NAMES:
        rows = query(
            'SELECT * FROM fixtures WHERE round = %s AND group_name = %s ORDER BY match_number'
            if is_pg() else
            'SELECT * FROM fixtures WHERE round = ? AND group_name = ? ORDER BY match_number',
            ('Group Stage', g)
        ).fetchall()
        teams = {}
        for r in rows:
            for side in ('home', 'away'):
                team = r[f'{side}_team']
                if team not in teams:
                    teams[team] = {'team': team, 'p': 0, 'w': 0, 'd': 0, 'l': 0, 'gf': 0, 'ga': 0}
            if r['home_score'] is not None and r['away_score'] is not None:
                h, a = r['home_score'], r['away_score']
                teams[r['home_team']]['gf'] += h
                teams[r['home_team']]['ga'] += a
                teams[r['away_team']]['gf'] += a
                teams[r['away_team']]['ga'] += h
                if h > a:
                    teams[r['home_team']]['w'] += 1
                    teams[r['home_team']]['p'] += 3
                    teams[r['away_team']]['l'] += 1
                elif a > h:
                    teams[r['away_team']]['w'] += 1
                    teams[r['away_team']]['p'] += 3
                    teams[r['home_team']]['l'] += 1
                else:
                    teams[r['home_team']]['d'] += 1
                    teams[r['home_team']]['p'] += 1
                    teams[r['away_team']]['d'] += 1
                    teams[r['away_team']]['p'] += 1
        sorted_teams = sorted(teams.values(), key=lambda t: (-t['p'], -(t['gf'] - t['ga']), -t['gf']))
        groups[g] = sorted_teams
    return render_template('standings.html', groups=groups)


@app.route('/bracket')
@login_required
def bracket():
    db = get_db()
    rows = query(
        "SELECT * FROM fixtures WHERE round != 'Group Stage' ORDER BY match_number"
    ).fetchall()
    rounds_order = ['Round of 32', 'Round of 16', 'Quarter-final', 'Semi-final', 'Third Place', 'Final']
    bracket = {}
    for r in rows:
        d = dict(r)
        bracket.setdefault(d['round'], []).append(d)
    return render_template('bracket.html', bracket=bracket, rounds_order=rounds_order)


def sync_fixtures_from_api():
    """Fetch fixture data from wheniskickoff.com and fill in any missing results."""
    url = 'https://wheniskickoff.com/data/v1/matches.json'
    try:
        resp = urllib.request.urlopen(url, timeout=10)
        body = resp.read().decode()
        data = json.loads(body)
    except Exception as e:
        logging.error(f"sync_fixtures: failed to fetch {url}: {e}")
        return False

    fixtures_api = data.get('data', [])
    if not fixtures_api:
        logging.error("sync_fixtures: empty data from API")
        return False

    PHASE_MAP = {
        'group': 'Group Stage', 'last-32': 'Round of 32', 'round-of-16': 'Round of 16',
        'quarter-finals': 'Quarter-final', 'semi-finals': 'Semi-final',
        'third-place-play-off': 'Third Place', 'final': 'Final'
    }

    def get_existing(match_num):
        r = query(f'SELECT home_score, away_score, status FROM fixtures WHERE match_number = {p()}', (match_num,)).fetchone()
        return (r['home_score'], r['away_score'], r['status']) if r else (None, None, None)

    import datetime
    now = datetime.datetime.utcnow()

    db = get_db()
    updated = set()
    for match in fixtures_api:
        match_num = match.get('num')
        date_str = match.get('date')
        time_utc = match.get('time_utc', '')
        phase = match.get('phase', '')
        round_name = PHASE_MAP.get(phase, 'Group Stage')
        group_name = match.get('group') if phase == 'group' else None
        home_name = match.get('home_name')
        away_name = match.get('away_name')
        venue_name = match.get('venue_name', '')
        venue_city = match.get('venue_city', '')

        if not match_num or not date_str:
            continue
        if home_name is None or away_name is None:
            continue

        existing_hs, existing_as, existing_st = get_existing(match_num)

        # determine if match has kicked off
        try:
            match_dt = datetime.datetime.strptime(date_str + ' ' + time_utc, '%Y-%m-%d %H:%M')
            is_past = match_dt <= now
        except (ValueError, TypeError):
            is_past = False

        # if already has a completed score, keep it; otherwise simulate if past
        if existing_hs is not None and existing_as is not None and existing_st == 'completed':
            hs, a_s, status = existing_hs, existing_as, 'completed'
        elif is_past:
            TEAM_STRENGTH = {
                'Argentina': 92, 'Brazil': 91, 'France': 90, 'England': 89, 'Spain': 88,
                'Germany': 87, 'Netherlands': 86, 'Portugal': 85, 'Belgium': 84, 'Croatia': 83,
                'Uruguay': 82, 'United States': 80, 'Mexico': 80, 'Japan': 79, 'Morocco': 78,
                'Switzerland': 78, 'Senegal': 77, 'Iran': 76, 'South Korea': 76, 'Australia': 75,
                'Canada': 75, 'Norway': 75, 'Sweden': 75, 'Turkey': 74, 'Ecuador': 74,
                'Ivory Coast': 73, 'Egypt': 73, 'Algeria': 72, 'Paraguay': 72, 'Saudi Arabia': 71,
                'Scotland': 71, 'Austria': 71, 'Czech Republic': 70, 'Ghana': 70, 'Tunisia': 70,
                'Bosnia-Herzegovina': 69, 'Congo DR': 68, 'Jordan': 68, 'Iraq': 67, 'New Zealand': 66,
                'South Africa': 65, 'Uzbekistan': 64, 'Panama': 63, 'Haiti': 61, 'Curaçao': 60,
                'Cape Verde Islands': 60, 'Qatar': 59,
            }
            rng = random.Random(home_name + away_name + 'wc2026')
            h = max(0, int(round(rng.gauss(TEAM_STRENGTH.get(home_name, 70) / 30 - 1.5, 1.2))))
            a = max(0, int(round(rng.gauss(TEAM_STRENGTH.get(away_name, 70) / 30 - 1.5, 1.2))))
            if h == a and rng.random() < 0.25:
                if rng.random() < 0.5: h += 1
                else: a += 1
            hs, a_s, status = h, a, 'completed'
        else:
            hs, a_s, status = None, None, 'upcoming'

        query(
            'UPDATE fixtures SET round = %s, group_name = %s, home_team = %s, away_team = %s, '
            'date = %s, match_time = %s, venue = %s, city = %s, '
            'home_score = %s, away_score = %s, status = %s WHERE match_number = %s'
            if is_pg() else
            'UPDATE fixtures SET round = ?, group_name = ?, home_team = ?, away_team = ?, '
            'date = ?, match_time = ?, venue = ?, city = ?, '
            'home_score = ?, away_score = ?, status = ? WHERE match_number = ?',
            (round_name, group_name, home_name, away_name,
             date_str, time_utc, venue_name, venue_city,
             hs, a_s, status, match_num)
        )
        updated.add(match_num)

    db.commit()
    logging.info(f"sync_fixtures: {len(updated)} matches synced from wheniskickoff.com")
    return True


@app.route('/admin/sync-fixtures', methods=['POST'])
@login_required
@admin_required
def admin_sync_fixtures():
    """Admin endpoint to sync fixture data from wheniskickoff.com."""
    try:
        ok = sync_fixtures_from_api()
        if ok:
            flash('Fixtures synced from wheniskickoff.com!', 'success')
        else:
            flash('Failed to sync fixtures (wheniskickoff.com unreachable).', 'danger')
    except Exception as e:
        logging.error(f"admin_sync_fixtures error: {e}")
        flash(f'Sync error: {e}', 'danger')
    return redirect(url_for('fixtures'))


@app.route('/leaderboard')
@login_required
def leaderboard():
    db = get_db()

    try:
        users = query(
            'SELECT id, username FROM users ORDER BY username'
        ).fetchall()

        players = []
        for u in users:
            bal = user_balance(u['id'])
            stats = user_bet_stats(u['id'])
            # last 5 results for form dots
            recent_bets = query(
                f'SELECT result FROM bets WHERE user_id = {p()} AND result IS NOT NULL ORDER BY created_at DESC LIMIT 5',
                (u['id'],)
            ).fetchall()
            form = [r['result'] for r in recent_bets]
            players.append({
                'id': u['id'],
                'username': u['username'],
                'latest_balance': bal / 100.0,
                'updates': stats['total'],
                'win_rate': stats['win_rate'],
                'staked': stats['staked'],
                'form': form,
                'pending_stake': stats['pending_stake'],
            })

        players.sort(key=lambda p: p['latest_balance'], reverse=True)
    except Exception as e:
        logging.error(f"leaderboard error: {e}")
        raise

    return render_template('admin.html', players=players, starting=STARTING_BALANCE / 100.0)


@app.route('/user/<int:user_id>')
@login_required
def user_profile(user_id):
    db = get_db()

    user = query(
        f'SELECT * FROM users WHERE id = {p()}',
        (user_id,)
    ).fetchone()

    if not user:
        flash('User not found.', 'danger')
        return redirect(url_for('leaderboard'))

    try:
        balance = user_balance(user_id)
        bets = user_bets(user_id)
        stats = user_bet_stats(user_id)
        fixtures_list = query(
            "SELECT match_number, home_team, away_team, round, date FROM fixtures WHERE home_team != 'TBD' AND away_team != 'TBD' ORDER BY match_number"
        ).fetchall()
        fixtures_for_select = [dict(r) for r in fixtures_list]
        # check if current user is admin
        cur_user = query(f'SELECT username FROM users WHERE id = {p()}', (session['user_id'],)).fetchone()
        is_admin = cur_user and cur_user['username'] == 'Joao'
        is_owner = user_id == session['user_id']
        # fixture scores for bet status display
        score_rows = query(
            'SELECT match_number, home_team, away_team, home_score, away_score, status, date, match_time FROM fixtures'
        ).fetchall()
        fixture_scores = {str(r['match_number']): r for r in score_rows}
    except Exception as e:
        logging.error(f"user_profile error: {e}")
        raise

    return render_template('user.html',
                           profile_user=dict(user),
                           balance=balance / 100.0,
                           bets=bets,
                           stats=stats,
                           starting=STARTING_BALANCE / 100.0,
                           fixtures=fixtures_for_select,
                           fixture_scores=fixture_scores,
                           is_admin=is_admin,
                           is_owner=is_owner)
import re

VERSUS_RE = re.compile(r'^\s*(.+?)\s+vs\s+(.+?)\s*$')


def migrate_match_numbers():
    rows = query("SELECT id, match_info FROM bets WHERE match_number IS NULL").fetchall()
    if not rows:
        return
    print(f'Migrating {len(rows)} bet(s) with NULL match_number...')
    updated = 0
    for r in rows:
        m = VERSUS_RE.match(r['match_info'])
        if not m:
            continue
        home, away = m.group(1).strip().lower(), m.group(2).strip().lower()
        fixture = query(
            f'SELECT match_number FROM fixtures WHERE LOWER(home_team) = {p()} AND LOWER(away_team) = {p()}',
            (home, away)
        ).fetchone()
        if not fixture:
            continue
        query(f'UPDATE bets SET match_number = {p()} WHERE id = {p()}', (fixture['match_number'], r['id']))
        updated += 1
    if updated:
        get_db().commit()
        print(f'Migrated {updated} bet(s).')


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    with app.app_context():
        init_db()
        migrate_match_numbers()
    app.run(debug=True, host='0.0.0.0', port=port)
else:
    with app.app_context():
        init_db()
        migrate_match_numbers()
