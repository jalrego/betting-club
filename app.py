import os
import sys
import sqlite3
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
                    result TEXT NOT NULL DEFAULT 'pending',
                    settled_at TIMESTAMP,
                    created_at TIMESTAMP NOT NULL DEFAULT NOW()
                )
            """)
            query("""
                CREATE INDEX IF NOT EXISTS idx_bets_user ON bets(user_id)
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
                    result TEXT NOT NULL DEFAULT 'pending',
                    settled_at TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_bets_user ON bets(user_id);
            """)
        db.commit()
    except Exception as e:
        logging.error(f"init_db failed: {e}")
        raise


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
    match_info = request.form.get('match_info', '').strip()
    round_ = request.form.get('round', '').strip()
    stake_str = request.form.get('stake', '').strip()
    odds_str = request.form.get('odds', '').strip()
    pick = request.form.get('pick', '').strip()

    if not all([match_info, round_, stake_str, odds_str, pick]):
        flash('All bet fields are required.', 'danger')
        return redirect(url_for('dashboard'))

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
        'INSERT INTO bets (user_id, match_info, round, stake, odds, pick) '
        f'VALUES ({p()}, {p()}, {p()}, {p()}, {p()}, {p()})',
        (session['user_id'], match_info, round_, stake, odds, pick)
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


@app.route('/reset', methods=['POST'])
@login_required
def reset_db():
    db = get_db()
    try:
        if is_pg():
            query('DELETE FROM bets')
            query('DELETE FROM balance_records')
            query('DELETE FROM users')
            query("ALTER SEQUENCE bets_id_seq RESTART WITH 1")
            query("ALTER SEQUENCE balance_records_id_seq RESTART WITH 1")
            query("ALTER SEQUENCE users_id_seq RESTART WITH 1")
        else:
            query('DELETE FROM bets')
            query('DELETE FROM balance_records')
            query('DELETE FROM users')
            query("DELETE FROM sqlite_sequence")
        db.commit()
        flash('Database wiped clean. Ready for the challenge! ⚽🏆', 'success')
    except Exception as e:
        db.rollback()
        logging.error(f"reset error: {e}")
        flash(f'Error: {e}', 'danger')
    return redirect(url_for('dashboard'))


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(debug=True, host='0.0.0.0', port=port)
else:
    with app.app_context():
        init_db()
