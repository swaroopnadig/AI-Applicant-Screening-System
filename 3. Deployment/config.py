import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
project_root = BASE_DIR.parent
for env_path in (BASE_DIR / ".env", project_root / ".env"):
    if env_path.exists():
        load_dotenv(env_path)

SECRET_KEY = os.getenv("SECRET_KEY")
if not SECRET_KEY:
    raise RuntimeError("SECRET_KEY must be set in the environment before starting the application")


def normalize_database_url(value):
    if not value:
        return "sqlite:///app.db"
    normalized = value.strip()
    if normalized.startswith("postgres://"):
        normalized = "postgresql://" + normalized[len("postgres://"):]
    return normalized


class Config:
    SECRET_KEY = SECRET_KEY
    MAX_CONTENT_LENGTH = 5 * 1024 * 1024
    UPLOAD_FOLDER = str(BASE_DIR / "uploads")
    SQLALCHEMY_DATABASE_URI = normalize_database_url(os.getenv("DATABASE_URL")) or "sqlite:///app.db"
    SQLALCHEMY_TRACK_MODIFICATIONS = False
