# config.py
import os

# Токен телеграм-бота
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# Ключ OpenAI
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# RU-прокси для парсинга вакансий
# Пример для Bright Data:
# PROXY_URL = "http://user:pass@brd.superproxy.io:33335"
PROXY_URL = os.getenv("PROXY_URL")

# Минимальная длина текста вакансии, чтобы считать её “осмысленной”
MIN_MEANINGFUL_TEXT_LENGTH = 400
