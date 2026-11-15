from __future__ import annotations

import json
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
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                account_id INTEGER NOT NULL REFERENCES github_accounts(id),
                expires_at INTEGER NOT NULL
            );
            """
        )
        # Keep development databases created by the first scaffold usable.
        try:
            conn.execute("ALTER TABLE projects ADD COLUMN webhook_secret TEXT NOT NULL DEFAULT ''")
        except sqlite3.OperationalError:
            pass


def row_to_dict(row):
    return dict(row) if row else None


def json_text(value) -> str:
    return json.dumps(value, ensure_ascii=False)

