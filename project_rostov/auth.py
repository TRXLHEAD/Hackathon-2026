"""Пользователи, вход и журнал действий. Всё хранится в той же SQLite-базе, что и данные."""
import logging
import os
import re
from logging.handlers import RotatingFileHandler
import secrets
import time
from datetime import datetime

from flask import session, request
from werkzeug.security import generate_password_hash, check_password_hash

import db

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SECRET_PATH = os.path.join(BASE_DIR, 'secret.key')

USERNAME_RE = re.compile(r'^[\w.\-@]{2,32}$')
MIN_PASSWORD = 4

# защита от подбора пароля: 5 неудачных попыток -> пауза на минуту
_fails = {}
MAX_FAILS, LOCK_SECONDS = 5, 60


def secret_key():
    """Ключ для подписи cookie. Создаётся один раз и лежит рядом с приложением."""
    if os.path.exists(SECRET_PATH):
        with open(SECRET_PATH, 'r', encoding='utf-8') as f:
            return f.read().strip()
    key = secrets.token_hex(32)
    with open(SECRET_PATH, 'w', encoding='utf-8') as f:
        f.write(key)
    return key


def init():
    with db.connect() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE COLLATE NOCASE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user',
            created_at TEXT NOT NULL,
            last_login TEXT)""")
        conn.execute("UPDATE users SET role = 'owner' WHERE role != 'owner'")


def now():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def client_ip():
    try:
        return request.remote_addr or ''
    except RuntimeError:
        return ''


def current_user():
    name = session.get('user')
    return {'username': name, 'role': session.get('role', 'user')} if name else None


# ---------- журнал (только в файле, для разработчика) ----------

LOG_DIR = os.path.join(BASE_DIR, 'logs')
os.makedirs(LOG_DIR, exist_ok=True)
_logger = logging.getLogger('proekt_rostov')
if not _logger.handlers:
    _h = RotatingFileHandler(os.path.join(LOG_DIR, 'app.log'), maxBytes=2 * 1024 * 1024,
                             backupCount=5, encoding='utf-8')
    _h.setFormatter(logging.Formatter('%(asctime)s | %(levelname)-7s | %(message)s'))
    _logger.addHandler(_h)
    _logger.setLevel(logging.INFO)
    _logger.propagate = False

_LEVELS = {'info': logging.INFO, 'warning': logging.WARNING, 'error': logging.ERROR}


def log(action, details='', level='info', username=None):
    """Записать событие в logs/app.log. Журнал никогда не должен ронять приложение."""
    try:
        if username is None:
            try:
                username = session.get('user')
            except RuntimeError:
                username = None
        _logger.log(_LEVELS.get(level, logging.INFO), '%s | %s | %s | ip=%s',
                    username or '-', action, str(details).replace('\n', ' ')[:2000], client_ip() or '-')
    except Exception:
        pass


# ---------- пользователи ----------

def _locked(key):
    rec = _fails.get(key)
    if not rec:
        return 0
    count, until = rec
    if count >= MAX_FAILS and until > time.time():
        return int(until - time.time()) + 1
    if until <= time.time():
        _fails.pop(key, None)
    return 0


def register(username, password):
    username = (username or '').strip()
    if not USERNAME_RE.match(username):
        raise ValueError('Логин: от 2 до 32 символов — буквы, цифры, точка, дефис, подчёркивание, @.')
    if len(password or '') < MIN_PASSWORD:
        raise ValueError(f'Пароль должен быть не короче {MIN_PASSWORD} символов.')
    with db.connect() as conn:
        role = 'owner'  # все пользователи — владельцы
        try:
            conn.execute('INSERT INTO users (username, password_hash, role, created_at) VALUES (?,?,?,?)',
                         (username, generate_password_hash(password), role, now()))
        except Exception:
            raise ValueError('Такой логин уже занят.')
    return username, role


def login(username, password):
    username = (username or '').strip()
    key = (client_ip(), username.lower())
    wait = _locked(key)
    if wait:
        raise ValueError(f'Слишком много неудачных попыток. Подождите {wait} с.')
    with db.connect() as conn:
        row = conn.execute('SELECT username, password_hash, role FROM users WHERE username = ?',
                           (username,)).fetchone()
        if not row or not check_password_hash(row[1], password or ''):
            count = _fails.get(key, (0, 0))[0] + 1
            _fails[key] = (count, time.time() + LOCK_SECONDS)
            log('login_failed', f'Неверный логин или пароль для «{username}»', 'warning', username=username or None)
            raise ValueError('Неверный логин или пароль.')
        conn.execute('UPDATE users SET last_login = ? WHERE username = ?', (now(), row[0]))
    _fails.pop(key, None)
    return row[0], row[2]


def start_session(username, role):
    session.clear()
    session['user'] = username
    session['role'] = role
    session.permanent = True


def user_count():
    with db.connect() as conn:
        return conn.execute('SELECT COUNT(*) FROM users').fetchone()[0]
