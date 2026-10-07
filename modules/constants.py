# MediaCatalog
# Author: GlacialPigeon
# GitHub: https://github.com/GlacialPigeon

from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "MediaCatalog"
PROGRAM_VERSION = "3.0.2"
APP_AUTHOR = "GlacialPigeon"
APP_AUTHOR_URL = "https://github.com/GlacialPigeon"

APP_ROOT = Path(os.path.abspath(__file__)).parent.parent
DATA_DIR = APP_ROOT / "data"
DB_PATH = DATA_DIR / "MediaCatalog.db"
CONFIG_PATH = DATA_DIR / "config.json"
BACKUP_DIR = DATA_DIR / "backups"
REPORTS_DIR = APP_ROOT / "reports"
LOGS_DIR = APP_ROOT / "logs"
PATHLISTS_DIR = APP_ROOT / "pathlists"
LOCALES_DIR = APP_ROOT / "locales"
