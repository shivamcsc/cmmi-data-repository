"""Application settings, loaded from the .env file in the project root."""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _get(key, default=""):
    return os.getenv(key, default).strip()


DB_HOST = _get("DB_HOST", "127.0.0.1")
DB_PORT = int(_get("DB_PORT", "3306") or 3306)
DB_USER = _get("DB_USER", "root")
DB_PASSWORD = _get("DB_PASSWORD", "")
DB_NAME = _get("DB_NAME", "cmmi_data")

APP_HOST = _get("APP_HOST", "127.0.0.1")
APP_PORT = int(_get("APP_PORT", "8000") or 8000)

UPLOAD_DIR = BASE_DIR / _get("UPLOAD_DIR", "storage/uploads")
ORGANOGRAM_DIR = BASE_DIR / "storage" / "organogram"
MAX_UPLOAD_MB = int(_get("MAX_UPLOAD_MB", "100") or 100)

ALLOWED_EXTENSIONS = {".xlsx", ".xlsm", ".xls", ".csv"}


def _quote(value):
    from urllib.parse import quote_plus

    return quote_plus(value)


def database_url(include_db=True):
    """SQLAlchemy URL. Set include_db=False to connect before the schema exists."""
    name = DB_NAME if include_db else ""
    return (
        "mysql+pymysql://%s:%s@%s:%d/%s?charset=utf8mb4"
        % (_quote(DB_USER), _quote(DB_PASSWORD), DB_HOST, DB_PORT, name)
    )
