from __future__ import annotations

import os
from pathlib import Path


from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent.parent
# Read .env beside the code. Variables already set, e.g. by systemd EnvironmentFile, take precedence.
load_dotenv(ROOT / ".env")
DATA_DIR = Path(os.getenv("DEPLOY_DATA_DIR", ROOT / "data")).resolve()
DATABASE = Path(os.getenv("DEPLOY_DATABASE", DATA_DIR / "platform.db")).resolve()

try:
    KEEP_RELEASES = max(1, int(os.getenv("DEPLOY_KEEP_RELEASES", "5")))
except ValueError:
    KEEP_RELEASES = 5

