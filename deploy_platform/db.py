from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from . import config


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect():
    config.DATABASE.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DATABASE)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS github_accounts (
                id INTEGER PRIMARY KEY,
                github_id TEXT NOT NULL UNIQUE,
                login TEXT NOT NULL,
                access_token TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY,
                account_id INTEGER NOT NULL REFERENCES github_accounts(id),
                repository TEXT NOT NULL,
                branch TEXT NOT NULL DEFAULT 'main',
                deploy_root TEXT NOT NULL,
                build_command TEXT NOT NULL DEFAULT '',
                start_command TEXT NOT NULL,
                healthcheck_url TEXT NOT NULL DEFAULT '',
                webhook_secret TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(account_id, repository, deploy_root)
            );
            CREATE TABLE IF NOT EXISTS deployments (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id),
                action TEXT NOT NULL,
                status TEXT NOT NULL,
                commit_sha TEXT NOT NULL DEFAULT '',
                release_dir TEXT NOT NULL DEFAULT '',
                previous_release TEXT NOT NULL DEFAULT '',
                message TEXT NOT NULL DEFAULT '',
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL DEFAULT ''
            );
            -- Console sign-in (auth.py). Sessions are stored as SHA-256 of the cookie value.
            CREATE TABLE IF NOT EXISTS admins (
                id INTEGER PRIMARY KEY,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS admin_sessions (
                token_hash TEXT PRIMARY KEY,
                admin_id INTEGER NOT NULL REFERENCES admins(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            """
        )
        # Keep development databases created by the first scaffold usable.
        try:
            conn.execute("ALTER TABLE projects ADD COLUMN webhook_secret TEXT NOT NULL DEFAULT ''")
        except sqlite3.OperationalError:
            pass
        for table, name, definition in (
            ('projects', 'mode', "TEXT NOT NULL DEFAULT 'release'"),
            ('projects', 'runtime', "TEXT NOT NULL DEFAULT 'command'"),
            ('projects', 'runtime_config', "TEXT NOT NULL DEFAULT '{}'"),
            ('deployments', 'previous_commit', "TEXT NOT NULL DEFAULT ''"),
            ('projects', 'healthcheck_timeout', "INTEGER NOT NULL DEFAULT 45"),
        ):
            columns = {row[1] for row in conn.execute(f'PRAGMA table_info({table})')}
            if name not in columns:
                conn.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')
        conn.execute('CREATE INDEX IF NOT EXISTS deployments_by_project ON deployments(project_id, id)')


def recover_interrupted() -> int:
    """Jobs run in threads of this process, so none can outlive a restart."""
    with connect() as conn:
        return conn.execute(
            "UPDATE deployments SET status='failed', finished_at=?, message=message || ? "
            "WHERE status IN ('queued','running')",
            (now(), '平台重启导致任务中断；服务可能停在中间状态，请检查后重新部署\n'),
        ).rowcount

