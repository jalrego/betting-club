import os
import sys
import sqlite3
import json
import logging
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

        # Seed fixtures if empty
        try:
            existing = query('SELECT COUNT(*) as cnt FROM fixtures').fetchone()
            if existing['cnt'] == 0:
                seed_fixtures()
                db.commit()
        except Exception:
            db.rollback()
    except Exception as e:
        logging.error(f"init_db failed: {e}")
        raise


ROUNDS_FIXTURES = ['Group Stage', 'Round of 32', 'Round of 16', 'Quarter-final', 'Semi-final', 'Third Place', 'Final']


def seed_fixtures():
    fixtures = [
        # Group Stage - Matchday 1
        (1, 'Group Stage', 'A', 'Mexico', 'South Africa', '2026-06-11', 'Estadio Azteca', 'Mexico City'),
        (2, 'Group Stage', 'A', 'South Korea', 'Czechia', '2026-06-11', 'Estadio Akron', 'Guadalajara'),
        (3, 'Group Stage', 'B', 'Canada', 'TBD', '2026-06-12', 'BMO Field', 'Toronto'),
        (4, 'Group Stage', 'D', 'USA', 'Paraguay', '2026-06-12', 'SoFi Stadium', 'Los Angeles'),
        (5, 'Group Stage', 'C', 'Haiti', 'Scotland', '2026-06-13', 'Gillette Stadium', 'Boston'),
        (6, 'Group Stage', 'D', 'Australia', 'TBD', '2026-06-13', 'BC Place', 'Vancouver'),
        (7, 'Group Stage', 'C', 'Brazil', 'Morocco', '2026-06-13', 'MetLife Stadium', 'New York/New Jersey'),
        (8, 'Group Stage', 'B', 'Qatar', 'Switzerland', '2026-06-13', "Levi's Stadium", 'San Francisco'),
        (9, 'Group Stage', 'E', "Cote d'Ivoire", 'Ecuador', '2026-06-14', 'Lincoln Financial Field', 'Philadelphia'),
        (10, 'Group Stage', 'E', 'Germany', 'Curacao', '2026-06-14', 'NRG Stadium', 'Houston'),
        (11, 'Group Stage', 'F', 'Netherlands', 'Japan', '2026-06-14', "AT&T Stadium", 'Dallas'),
        (12, 'Group Stage', 'F', 'Tunisia', 'TBD', '2026-06-14', 'Estadio BBVA', 'Monterrey'),
        (13, 'Group Stage', 'G', 'Belgium', 'Egypt', '2026-06-15', 'Lumen Field', 'Seattle'),
        (14, 'Group Stage', 'G', 'Iran', 'New Zealand', '2026-06-15', 'SoFi Stadium', 'Los Angeles'),
        (15, 'Group Stage', 'H', 'Spain', 'Cape Verde', '2026-06-15', 'Mercedes-Benz Stadium', 'Atlanta'),
        (16, 'Group Stage', 'H', 'Saudi Arabia', 'Uruguay', '2026-06-15', 'Hard Rock Stadium', 'Miami'),
        (17, 'Group Stage', 'I', 'France', 'Senegal', '2026-06-16', 'MetLife Stadium', 'New York/New Jersey'),
        (18, 'Group Stage', 'I', 'Norway', 'TBD', '2026-06-16', 'Gillette Stadium', 'Boston'),
        (19, 'Group Stage', 'J', 'Argentina', 'Algeria', '2026-06-16', 'Arrowhead Stadium', 'Kansas City'),
        (20, 'Group Stage', 'K', 'Portugal', 'TBD', '2026-06-17', 'NRG Stadium', 'Houston'),
        (21, 'Group Stage', 'K', 'Uzbekistan', 'Colombia', '2026-06-17', 'Estadio Azteca', 'Mexico City'),
        (22, 'Group Stage', 'L', 'England', 'Croatia', '2026-06-17', "AT&T Stadium", 'Dallas'),
        (23, 'Group Stage', 'L', 'Ghana', 'Panama', '2026-06-17', 'BMO Field', 'Toronto'),
        (24, 'Group Stage', 'A', 'Czechia', 'South Africa', '2026-06-18', 'Mercedes-Benz Stadium', 'Atlanta'),
        (25, 'Group Stage', 'A', 'Mexico', 'South Korea', '2026-06-18', 'Estadio Akron', 'Guadalajara'),
        (26, 'Group Stage', 'B', 'Switzerland', 'TBD', '2026-06-18', 'SoFi Stadium', 'Los Angeles'),
        (27, 'Group Stage', 'B', 'Canada', 'Qatar', '2026-06-18', 'BC Place', 'Vancouver'),
        (28, 'Group Stage', 'C', 'Scotland', 'Morocco', '2026-06-19', 'Gillette Stadium', 'Boston'),
        (29, 'Group Stage', 'C', 'Brazil', 'Haiti', '2026-06-19', 'Lincoln Financial Field', 'Philadelphia'),
        (30, 'Group Stage', 'D', 'USA', 'Australia', '2026-06-19', 'Lumen Field', 'Seattle'),
        (31, 'Group Stage', 'F', 'Tunisia', 'Japan', '2026-06-19', 'Estadio BBVA', 'Monterrey'),
        (32, 'Group Stage', 'E', 'Germany', "Cote d'Ivoire", '2026-06-20', 'BMO Field', 'Toronto'),
        (33, 'Group Stage', 'E', 'Ecuador', 'Curacao', '2026-06-20', 'Arrowhead Stadium', 'Kansas City'),
        (34, 'Group Stage', 'F', 'Netherlands', 'TBD', '2026-06-20', 'NRG Stadium', 'Houston'),
        (35, 'Group Stage', 'G', 'Belgium', 'Iran', '2026-06-21', 'SoFi Stadium', 'Los Angeles'),
        (36, 'Group Stage', 'G', 'New Zealand', 'Egypt', '2026-06-21', 'BC Place', 'Vancouver'),
        (37, 'Group Stage', 'H', 'Spain', 'Saudi Arabia', '2026-06-21', 'Mercedes-Benz Stadium', 'Atlanta'),
        (38, 'Group Stage', 'H', 'Uruguay', 'Cape Verde', '2026-06-21', 'Hard Rock Stadium', 'Miami'),
        (39, 'Group Stage', 'I', 'France', 'TBD', '2026-06-22', 'Lincoln Financial Field', 'Philadelphia'),
        (40, 'Group Stage', 'I', 'Norway', 'Senegal', '2026-06-22', 'MetLife Stadium', 'New York/New Jersey'),
        (41, 'Group Stage', 'J', 'Argentina', 'Austria', '2026-06-22', "AT&T Stadium", 'Dallas'),
        (42, 'Group Stage', 'J', 'Jordan', 'Algeria', '2026-06-22', "Levi's Stadium", 'San Francisco'),
        (43, 'Group Stage', 'K', 'Portugal', 'Uzbekistan', '2026-06-23', 'NRG Stadium', 'Houston'),
        (44, 'Group Stage', 'K', 'Colombia', 'TBD', '2026-06-23', 'Estadio Akron', 'Guadalajara'),
        (45, 'Group Stage', 'L', 'England', 'Ghana', '2026-06-23', 'Gillette Stadium', 'Boston'),
        (46, 'Group Stage', 'L', 'Panama', 'Croatia', '2026-06-23', 'BMO Field', 'Toronto'),
        (47, 'Group Stage', 'A', 'South Africa', 'South Korea', '2026-06-24', 'Estadio Akron', 'Guadalajara'),
        (48, 'Group Stage', 'A', 'Czechia', 'Mexico', '2026-06-24', 'Estadio Azteca', 'Mexico City'),
        (49, 'Group Stage', 'B', 'Switzerland', 'Canada', '2026-06-24', 'BC Place', 'Vancouver'),
        (50, 'Group Stage', 'B', 'TBD', 'Qatar', '2026-06-24', 'SoFi Stadium', 'Los Angeles'),
        (51, 'Group Stage', 'C', 'Morocco', 'Haiti', '2026-06-25', 'Gillette Stadium', 'Boston'),
        (52, 'Group Stage', 'C', 'Scotland', 'Brazil', '2026-06-25', 'MetLife Stadium', 'New York/New Jersey'),
        (53, 'Group Stage', 'D', 'Paraguay', 'Australia', '2026-06-25', 'Lumen Field', 'Seattle'),
        (54, 'Group Stage', 'D', 'TBD', 'USA', '2026-06-25', "Levi's Stadium", 'San Francisco'),
        (55, 'Group Stage', 'E', 'Curacao', "Cote d'Ivoire", '2026-06-25', 'NRG Stadium', 'Houston'),
        (56, 'Group Stage', 'E', 'Ecuador', 'Germany', '2026-06-25', 'Lincoln Financial Field', 'Philadelphia'),
        (57, 'Group Stage', 'F', 'Japan', 'TBD', '2026-06-26', 'Estadio BBVA', 'Monterrey'),
        (58, 'Group Stage', 'F', 'TBD', 'Netherlands', '2026-06-26', "AT&T Stadium", 'Dallas'),
        (59, 'Group Stage', 'G', 'Egypt', 'Iran', '2026-06-26', 'BC Place', 'Vancouver'),
        (60, 'Group Stage', 'G', 'New Zealand', 'Belgium', '2026-06-26', 'Lumen Field', 'Seattle'),
        (61, 'Group Stage', 'H', 'Cape Verde', 'Saudi Arabia', '2026-06-26', 'NRG Stadium', 'Houston'),
        (62, 'Group Stage', 'H', 'Uruguay', 'Spain', '2026-06-26', 'Estadio Akron', 'Guadalajara'),
        (63, 'Group Stage', 'I', 'Senegal', 'TBD', '2026-06-27', 'MetLife Stadium', 'New York/New Jersey'),
        (64, 'Group Stage', 'I', 'TBD', 'France', '2026-06-27', 'Hard Rock Stadium', 'Miami'),
        (65, 'Group Stage', 'J', 'Austria', 'Algeria', '2026-06-27', 'Arrowhead Stadium', 'Kansas City'),
        (66, 'Group Stage', 'J', 'Jordan', 'Argentina', '2026-06-27', "AT&T Stadium", 'Dallas'),
        (67, 'Group Stage', 'K', 'TBD', 'Uzbekistan', '2026-06-27', 'Estadio Azteca', 'Mexico City'),
        (68, 'Group Stage', 'K', 'Colombia', 'Portugal', '2026-06-27', 'Hard Rock Stadium', 'Miami'),
        (69, 'Group Stage', 'L', 'Croatia', 'Ghana', '2026-06-27', 'BMO Field', 'Toronto'),
        (70, 'Group Stage', 'L', 'Panama', 'England', '2026-06-27', 'MetLife Stadium', 'New York/New Jersey'),
        # Knockout Stage
        (71, 'Round of 32', None, 'TBD', 'TBD', '2026-06-28', 'TBD', 'TBD'),
        (72, 'Round of 32', None, 'TBD', 'TBD', '2026-06-29', 'TBD', 'TBD'),
        (73, 'Round of 32', None, 'TBD', 'TBD', '2026-06-29', 'TBD', 'TBD'),
        (74, 'Round of 32', None, 'TBD', 'TBD', '2026-06-29', 'TBD', 'TBD'),
        (75, 'Round of 32', None, 'TBD', 'TBD', '2026-06-30', 'TBD', 'TBD'),
        (76, 'Round of 32', None, 'TBD', 'TBD', '2026-06-30', 'TBD', 'TBD'),
        (77, 'Round of 32', None, 'TBD', 'TBD', '2026-06-30', 'TBD', 'TBD'),
        (78, 'Round of 32', None, 'TBD', 'TBD', '2026-07-01', 'TBD', 'TBD'),
        (79, 'Round of 32', None, 'TBD', 'TBD', '2026-07-01', 'TBD', 'TBD'),
        (80, 'Round of 32', None, 'TBD', 'TBD', '2026-07-01', 'TBD', 'TBD'),
        (81, 'Round of 32', None, 'TBD', 'TBD', '2026-07-02', 'TBD', 'TBD'),
        (82, 'Round of 32', None, 'TBD', 'TBD', '2026-07-02', 'TBD', 'TBD'),
        (83, 'Round of 32', None, 'TBD', 'TBD', '2026-07-02', 'TBD', 'TBD'),
        (84, 'Round of 32', None, 'TBD', 'TBD', '2026-07-03', 'TBD', 'TBD'),
        (85, 'Round of 32', None, 'TBD', 'TBD', '2026-07-03', 'TBD', 'TBD'),
        (86, 'Round of 32', None, 'TBD', 'TBD', '2026-07-03', 'TBD', 'TBD'),
        (87, 'Round of 16', None, 'TBD', 'TBD', '2026-07-04', 'TBD', 'TBD'),
        (88, 'Round of 16', None, 'TBD', 'TBD', '2026-07-04', 'TBD', 'TBD'),
        (89, 'Round of 16', None, 'TBD', 'TBD', '2026-07-05', 'TBD', 'TBD'),
        (90, 'Round of 16', None, 'TBD', 'TBD', '2026-07-05', 'TBD', 'TBD'),
        (91, 'Round of 16', None, 'TBD', 'TBD', '2026-07-06', 'TBD', 'TBD'),
        (92, 'Round of 16', None, 'TBD', 'TBD', '2026-07-06', 'TBD', 'TBD'),
        (93, 'Round of 16', None, 'TBD', 'TBD', '2026-07-07', 'TBD', 'TBD'),
        (94, 'Round of 16', None, 'TBD', 'TBD', '2026-07-07', 'TBD', 'TBD'),
        (95, 'Quarter-final', None, 'TBD', 'TBD', '2026-07-09', 'TBD', 'TBD'),
        (96, 'Quarter-final', None, 'TBD', 'TBD', '2026-07-10', 'TBD', 'TBD'),
        (97, 'Quarter-final', None, 'TBD', 'TBD', '2026-07-11', 'TBD', 'TBD'),
        (98, 'Quarter-final', None, 'TBD', 'TBD', '2026-07-11', 'TBD', 'TBD'),
        (99, 'Semi-final', None, 'TBD', 'TBD', '2026-07-14', 'TBD', 'TBD'),
        (100, 'Semi-final', None, 'TBD', 'TBD', '2026-07-15', 'TBD', 'TBD'),
        (101, 'Third Place', None, 'TBD', 'TBD', '2026-07-18', 'TBD', 'TBD'),
        (102, 'Final', None, 'TBD', 'TBD', '2026-07-19', 'MetLife Stadium', 'New York/New Jersey'),
    ]
    for f in fixtures:
        query(
            'INSERT INTO fixtures (match_number, round, group_name, home_team, away_team, date, venue, city) '
            f'VALUES ({p()}, {p()}, {p()}, {p()}, {p()}, {p()}, {p()}, {p()})',
            f
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
    # calculate win rate
    settled = stats['won'] + stats['lost']
    stats['win_rate'] = round(stats['won'] / settled * 100) if settled > 0 else 0
    stats['staked'] = stats['staked'] / 100.0
    return stats


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            flash('Please log in first.', 'warning')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


@app.teardown_appcontext
def teardown(exception):
    close_db()


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


@app.route('/dashboard')
@login_required
def dashboard():
    db = get_db()
    user_id = session['user_id']

    try:
        balance = user_balance(user_id)
        bets = user_bets(user_id)
        stats = user_bet_stats(user_id)
    except Exception as e:
        logging.error(f"dashboard error: {e}")
        raise

    return render_template('dashboard.html',
                           balance=balance / 100.0,
                           bets=bets,
                           stats=stats,
                           rounds=ROUNDS,
                           starting=STARTING_BALANCE / 100.0)


@app.route('/bet/new', methods=['POST'])
@login_required
def new_bet():
    bet_type = request.form.get('bet_type', 'single')
    round_ = request.form.get('round', '').strip()
    stake_str = request.form.get('stake', '').strip()
    odds_str = request.form.get('odds', '').strip()

    if bet_type == 'multi':
        matches = request.form.getlist('multi_match[]')
        picks = request.form.getlist('multi_pick[]')
        if not matches or not picks or not round_ or not stake_str or not odds_str:
            flash('All bet fields are required.', 'danger')
            return redirect(url_for('dashboard'))
        selections = [{"match": m.strip(), "pick": p.strip()} for m, p in zip(matches, picks) if m.strip() and p.strip()]
        if len(selections) < 2:
            flash('Multi-bet must have at least 2 selections.', 'danger')
            return redirect(url_for('dashboard'))
        match_info = ' / '.join(s['match'] for s in selections)
        pick = ' / '.join(s['pick'] for s in selections)
        selections_json = json.dumps(selections)
    else:
        match_info = request.form.get('match_info', '').strip()
        pick = request.form.get('pick', '').strip()
        if not all([match_info, round_, stake_str, odds_str, pick]):
            flash('All bet fields are required.', 'danger')
            return redirect(url_for('dashboard'))
        selections_json = None
        bet_type = 'single'

    try:
        stake = round(float(stake_str) * 100)
        odds = float(odds_str)
    except ValueError:
        flash('Stake and odds must be numbers.', 'danger')
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
        'INSERT INTO bets (user_id, match_info, round, stake, odds, pick, bet_type, selections) '
        f'VALUES ({p()}, {p()}, {p()}, {p()}, {p()}, {p()}, {p()}, {p()})',
        (session['user_id'], match_info, round_, stake, odds, pick, bet_type, selections_json)
    )
    db.commit()

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


@app.route('/admin')
@login_required
def admin():
    db = get_db()

    try:
        rows = query("""
            SELECT
                u.id,
                u.username,
                u.created_at AS joined_at,
                (SELECT balance FROM balance_records
                 WHERE user_id = u.id ORDER BY created_at DESC LIMIT 1
                ) AS latest_balance,
                (SELECT created_at FROM balance_records
                  WHERE user_id = u.id ORDER BY created_at DESC LIMIT 1
                ) AS last_updated,
                (SELECT COUNT(*) FROM balance_records WHERE user_id = u.id
                ) AS updates
            FROM users u
            ORDER BY u.username
        """).fetchall()

        players = []
        for r in rows:
            d = dict(r)
            if d['latest_balance'] is not None:
                d['latest_balance'] = d['latest_balance'] / 100.0
            players.append(d)
    except Exception as e:
        logging.error(f"admin error: {e}")
        raise

    return render_template('admin.html', players=players, starting=STARTING_BALANCE / 100.0)


@app.route('/fixtures')
@login_required
def fixtures():
    db = get_db()
    try:
        rows = query(
            'SELECT * FROM fixtures ORDER BY match_number'
        ).fetchall()
        fixtures_list = [dict(r) for r in rows]
        grouped = {}
        for f in fixtures_list:
            grouped.setdefault(f['round'], []).append(f)
        # also group group-stage by group_name
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
            players.append({
                'id': u['id'],
                'username': u['username'],
                'latest_balance': bal / 100.0,
                'updates': stats['total'],
                'win_rate': stats['win_rate'],
                'staked': stats['staked'],
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
    except Exception as e:
        logging.error(f"user_profile error: {e}")
        raise

    return render_template('user.html',
                           profile_user=dict(user),
                           balance=balance / 100.0,
                           bets=bets,
                           stats=stats,
                           starting=STARTING_BALANCE / 100.0)


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(debug=True, host='0.0.0.0', port=port)
else:
    with app.app_context():
        init_db()
