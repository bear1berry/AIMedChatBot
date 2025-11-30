import os
from dotenv import load_dotenv

# Загружаем .env
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
MODEL_NAME = os.getenv("MODEL_NAME", "llama3-8b-8192")

if not BOT_TOKEN:
    raise ValueError("Не указан BOT_TOKEN в .env")

if not GROQ_API_KEY:
    raise ValueError("Не указан GROQ_API_KEY в .env")
