import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent


def normalize_database_url(value):
    if not value:
        return "sqlite:///app.db"
    normalized = value.strip()
    if normalized.startswith("postgres://"):
        normalized = "postgresql://" + normalized[len("postgres://"):]
    return normalized


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "change-me-in-production")
    MAX_CONTENT_LENGTH = 5 * 1024 * 1024
    UPLOAD_FOLDER = str(BASE_DIR / "uploads")
    SQLALCHEMY_DATABASE_URI = normalize_database_url(os.getenv("DATABASE_URL")) or "sqlite:///app.db"
    SQLALCHEMY_TRACK_MODIFICATIONS = False
