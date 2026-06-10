import os
import sys
import sqlite3
import logging
from datetime import datetime
from functools import wraps
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from flask import (
    Flask, g, render_template, request, redirect, url_for,
    flash, session, send_from_directory
)

logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                    format='%(asctime)s %(levelname)s %(message)s')

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'change-this-to-a-random-secret')
app.config['UPLOAD_FOLDER'] = os.path.join(app.static_folder, 'uploads')
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
DATABASE = os.path.join(app.root_path, 'betting.db')

STARTING_BALANCE = 1000  # €10.00 in cents


def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
    return g.db


def close_db(e=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def init_db():
    db = get_db()
    try:
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
                screenshot TEXT,
                notes TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_balance_user ON balance_records(user_id);
            CREATE INDEX IF NOT EXISTS idx_balance_created ON balance_records(created_at);
        """)
        db.commit()

        col_type = db.execute("PRAGMA table_info(balance_records)").fetchall()
        for col in col_type:
            if col[1] == 'balance' and col[2].upper() == 'REAL':
                db.execute("UPDATE balance_records SET balance = CAST(balance * 100 AS INTEGER)")
                db.commit()
                break
    except Exception as e:
        logging.error(f"init_db failed: {e}")
        raise


def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


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
        existing = db.execute(
            'SELECT id FROM users WHERE username = ?', (username,)
        ).fetchone()
        if existing:
            flash('Username already taken.', 'danger')
            return render_template('register.html')

        pw_hash = generate_password_hash(password, method='pbkdf2:sha256')
        db.execute(
            'INSERT INTO users (username, password_hash) VALUES (?, ?)',
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
        user = db.execute(
            'SELECT * FROM users WHERE username = ?', (username,)
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
        rows = db.execute(
            'SELECT * FROM balance_records WHERE user_id = ? ORDER BY created_at DESC',
            (user_id,)
        ).fetchall()

        records = []
        for r in rows:
            d = dict(r)
            d['balance'] = d['balance'] / 100.0
            records.append(d)

        latest = records[0] if records else None
    except Exception as e:
        logging.error(f"dashboard error: {e}")
        raise

    return render_template('dashboard.html',
                           records=records,
                           latest=latest,
                           starting=STARTING_BALANCE / 100.0)


@app.route('/upload', methods=['POST'])
@login_required
def upload():
    balance_str = request.form.get('balance', '').strip()
    notes = request.form.get('notes', '').strip()
    file = request.files.get('screenshot')

    if not balance_str:
        flash('Please enter your current balance.', 'danger')
        return redirect(url_for('dashboard'))

    try:
        balance = round(float(balance_str) * 100)
    except ValueError:
        flash('Balance must be a number.', 'danger')
        return redirect(url_for('dashboard'))

    filename = None
    if file and file.filename:
        if not allowed_file(file.filename):
            flash('File type not allowed. Use PNG, JPG, GIF, or WebP.', 'danger')
            return redirect(url_for('dashboard'))
        filename = secure_filename(
            f"{session['user_id']}_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_"
            f"{file.filename}"
        )
        file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))

    db = get_db()
    db.execute(
        'INSERT INTO balance_records (user_id, balance, screenshot, notes) '
        'VALUES (?, ?, ?, ?)',
        (session['user_id'], balance, filename, notes)
    )
    db.commit()

    flash('Balance recorded!', 'success')
    return redirect(url_for('dashboard'))


@app.route('/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)


@app.route('/admin')
@login_required
def admin():
    db = get_db()

    try:
        rows = db.execute("""
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


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(debug=True, host='0.0.0.0', port=port)
else:
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    with app.app_context():
        init_db()
