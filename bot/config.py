import os

# Токен телеграм-бота
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# Ключ OpenAI
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# RU-прокси для парсинга вакансий
PROXY_URL = os.getenv("PROXY_URL")

# Настройки Selenium
SELENIUM_ENABLED = os.getenv("SELENIUM_ENABLED", "true").lower() == "true"
SELENIUM_TIMEOUT = int(os.getenv("SELENIUM_TIMEOUT", "30"))
SELENIUM_HEADLESS = os.getenv("SELENIUM_HEADLESS", "true").lower() == "true"

# Минимальная длина текста вакансии
MIN_MEANINGFUL_TEXT_LENGTH = int(os.getenv("MIN_MEANINGFUL_TEXT_LENGTH", "400"))
