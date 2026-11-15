from __future__ import annotations

import os
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("DEPLOY_DATA_DIR", ROOT / "data")).resolve()
DATABASE = Path(os.getenv("DEPLOY_DATABASE", DATA_DIR / "platform.db")).resolve()
RELEASES_DIR = Path(os.getenv("DEPLOY_RELEASES_DIR", DATA_DIR / "releases")).resolve()

GITHUB_CLIENT_ID = os.getenv("GITHUB_CLIENT_ID", "")
GITHUB_CLIENT_SECRET = os.getenv("GITHUB_CLIENT_SECRET", "")
GITHUB_REDIRECT_URI = os.getenv(
    "GITHUB_REDIRECT_URI", "http://127.0.0.1:8000/auth/github/callback"
)
SECRET_KEY = os.getenv("DEPLOY_SECRET_KEY", "development-only-change-me")

