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

# ===== НОВЫЕ НАСТРОЙКИ ДЛЯ ПАРСЕРА =====

# Включен ли Cloudscraper (обход защиты через браузероподобные запросы)
CLOUDSCRAPER_ENABLED = os.getenv("CLOUDSCRAPER_ENABLED", "true").lower() == "true"

# Для hh.ru предпочитаем мобильную версию сайта
FORCE_MOBILE_HH = os.getenv("FORCE_MOBILE_HH", "true").lower() == "true"

# Количество общих попыток парсинга (циклы по методам)
RETRY_COUNT = int(os.getenv("RETRY_COUNT", "3"))

# Флаг запуска на Render (можно выставлять через env или скрипт старта)
IS_RENDER = os.getenv("IS_RENDER", "false").lower() == "true"
