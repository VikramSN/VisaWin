import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent

# Load .env if present
env_file = BASE_DIR / ".env"
if env_file.exists():
    load_dotenv(env_file)
else:
    load_dotenv()

class Config:
    BASE_DIR = BASE_DIR
    SECRET_KEY = os.getenv("SECRET_KEY", "us-visa-scheduler-godaddy-secret-key-2026")
    API_KEY = os.getenv("API_KEY", "visabot-secure-api-key-2026")
    ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
    ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")
    
    DATA_DIR = BASE_DIR / "data"
    DATABASE_PATH = DATA_DIR / "visabot.db"
    ACCOUNTS_BACKUP_JSON = DATA_DIR / "accounts.json"
    
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
    TELEGRAM_CHAT_IDS = os.getenv("TELEGRAM_CHAT_IDS", "")
