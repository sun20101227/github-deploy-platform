"""Admin accounts for the web console: scrypt passwords, server-side sessions, login throttling.

The console signs in with a username and password and keeps an HttpOnly session cookie.
DEPLOY_ADMIN_TOKEN still works as a Bearer token for scripts and CI (see docs/API.md).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

from . import db

COOKIE = 'deploy_session'
SESSION_DAYS = 7
USERNAME = re.compile(r'[A-Za-z0-9_.-]{3,32}')
MIN_PASSWORD, MAX_PASSWORD = 10, 128

# scrypt: memory-hard and in the standard library, so a self update never has to build a native wheel.
_N, _R, _P = 2**15, 8, 1
_MAXMEM = 64 * 1024 * 1024


def init() -> None:
    db.init()  # the admins and admin_sessions tables are created with the rest of the schema


# ---- passwords ----

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, maxmem=_MAXMEM)
    return 'scrypt${}${}${}${}${}'.format(_N, _R, _P, base64.b64encode(salt).decode(), base64.b64encode(digest).decode())


def verify_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, digest = stored.split('$')
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                                maxmem=_MAXMEM, dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


# Checked against when the username does not exist, so both cases take the same time.
_DUMMY = hash_password(secrets.token_urlsafe(16))


def check_new_credentials(username: str, password: str) -> None:
    if not USERNAME.fullmatch(username or ''):
        raise ValueError('用户名需要 3–32 个字符，只能用英文字母、数字、点、横线和下划线')
    if len(password or '') < MIN_PASSWORD:
        raise ValueError(f'密码至少 {MIN_PASSWORD} 个字符')
    if len(password) > MAX_PASSWORD:
        raise ValueError(f'密码最多 {MAX_PASSWORD} 个字符')


# ---- accounts ----

def admin_exists() -> bool:
    with db.connect() as conn:
        return conn.execute('SELECT 1 FROM admins').fetchone() is not None


def set_admin(username: str, password: str) -> int:
    """Create the admin, or set a new password for an existing one. Ends that admin's sessions."""
    check_new_credentials(username, password)
    with db.connect() as conn:
        row = conn.execute('SELECT id FROM admins WHERE username=?', (username,)).fetchone()
        if row:
            conn.execute('UPDATE admins SET password_hash=?, updated_at=? WHERE id=?', (hash_password(password), db.now(), row['id']))
            conn.execute('DELETE FROM admin_sessions WHERE admin_id=?', (row['id'],))
            return row['id']
        return conn.execute('INSERT INTO admins(username,password_hash,created_at,updated_at) VALUES (?,?,?,?)',
                            (username, hash_password(password), db.now(), db.now())).lastrowid


def authenticate(username: str, password: str):
    with db.connect() as conn:
        row = conn.execute('SELECT id, username, password_hash FROM admins WHERE username=?', (username or '',)).fetchone()
    if not row:
        verify_password(password or '', _DUMMY)
        return None
    return dict(id=row['id'], username=row['username']) if verify_password(password or '', row['password_hash']) else None


def change_password(admin_id: int, current: str, new: str, keep_token: str | None = None) -> None:
    with db.connect() as conn:
        row = conn.execute('SELECT username, password_hash FROM admins WHERE id=?', (admin_id,)).fetchone()
    if not row or not verify_password(current or '', row['password_hash']):
        raise PermissionError('当前密码不正确')
    check_new_credentials(row['username'], new)
    with db.connect() as conn:
        conn.execute('UPDATE admins SET password_hash=?, updated_at=? WHERE id=?', (hash_password(new), db.now(), admin_id))
        # Sign out everywhere else; the browser that changed it stays signed in.
        conn.execute('DELETE FROM admin_sessions WHERE admin_id=? AND token_hash<>?', (admin_id, _digest(keep_token or '')))


# ---- sessions ----

def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(admin_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    with db.connect() as conn:
        conn.execute('DELETE FROM admin_sessions WHERE expires_at < ?', (now.isoformat(),))
        conn.execute('INSERT INTO admin_sessions(token_hash,admin_id,created_at,expires_at) VALUES (?,?,?,?)',
                     (_digest(token), admin_id, now.isoformat(), (now + timedelta(days=SESSION_DAYS)).isoformat()))
    return token


def session_admin(token: str | None):
    if not token:
        return None
    with db.connect() as conn:
        row = conn.execute('SELECT a.id, a.username FROM admin_sessions s JOIN admins a ON a.id=s.admin_id '
                           'WHERE s.token_hash=? AND s.expires_at > ?', (_digest(token), db.now())).fetchone()
    return dict(row) if row else None


def end_session(token: str | None) -> None:
    if token:
        with db.connect() as conn:
            conn.execute('DELETE FROM admin_sessions WHERE token_hash=?', (_digest(token),))


# ---- login throttling ----

class Throttle:
    """Lock a client after repeated failures, and a username after many failures from anywhere."""
    WINDOW = 15 * 60
    PER_CLIENT = 5
    PER_USER = 20

    def __init__(self):
        self._lock = threading.Lock()
        self._fails: dict[str, list[float]] = {}

    def _recent(self, key, now):
        hits = [t for t in self._fails.get(key, []) if now - t < self.WINDOW]
        self._fails[key] = hits
        return hits

    def wait_seconds(self, client: str, username: str) -> int:
        now = time.monotonic()
        with self._lock:
            waits = [int(self.WINDOW - (now - hits[0])) + 1
                     for key, limit in ((f'ip:{client}', self.PER_CLIENT), (f'user:{username.lower()}', self.PER_USER))
                     if len(hits := self._recent(key, now)) >= limit]
        return max(waits, default=0)

    def failed(self, client: str, username: str) -> None:
        now = time.monotonic()
        with self._lock:
            for key in (f'ip:{client}', f'user:{username.lower()}'):
                self._recent(key, now).append(now)

    def succeeded(self, client: str, username: str) -> None:
        with self._lock:
            self._fails.pop(f'ip:{client}', None)
            self._fails.pop(f'user:{username.lower()}', None)


throttle = Throttle()
