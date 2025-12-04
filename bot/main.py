import os
import logging
import re
import time
from typing import Any, Dict, List, Optional, cast
from urllib.parse import urlparse
from io import BytesIO

import requests
from bs4 import BeautifulSoup
from PyPDF2 import PdfReader
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from openai import OpenAI
from openai.types.chat import ChatCompletionMessageParam
from dotenv import load_dotenv  # ✅ ДОБАВЛЕНО: загрузка .env

# =========================
# ЗАГРУЗКА ПЕРЕМЕННЫХ ОКРУЖЕНИЯ
# =========================
load_dotenv()  # ✅ ДОБАВЛЕНО: теперь os.getenv увидит значения из .env

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# Инициализация клиентов
openai_client = None

# Константы
MIN_MEANINGFUL_TEXT_LENGTH = 400

# =========================
# SCRAPINGBEE API - ТОЛЬКО ДЛЯ ССЫЛОК
# =========================

SCRAPINGBEE_API_KEY = os.getenv("SCRAPINGBEE_API_KEY")

def parse_url_with_scrapingbee(url: str) -> str:
    """
    Парсинг веб-страниц через ScrapingBee API
    РАБОТАЕТ ТОЛЬКО ДЛЯ ССЫЛОК!
    """
    if not SCRAPINGBEE_API_KEY:
        raise ValueError(
            "❌ SCRAPINGBEE_API_KEY не установлен!\n"
            "Получите бесплатный ключ: https://www.scrapingbee.com/\n"
            "Добавьте в .env файл: SCRAPINGBEE_API_KEY=ваш_ключ"
        )
    
    api_endpoint = "https://app.scrapingbee.com/api/v1"
    
    # Правильные параметры для обхода блокировок
    params = {
        "api_key": SCRAPINGBEE_API_KEY,
        "url": url,
        "render_js": "true",           # Обязательно для современных сайтов
        "premium_proxy": "true",       # Используем резидентские прокси
        "country_code": "ru",          # Российские IP
        "wait": "3000",                # Ждем 3 секунды для загрузки JS
        "block_resources": "false",    # Не блокируем ресурсы
        "timeout": "30000",            # Таймаут 30 секунд
    }
    
    try:
        logger.info(f"🔗 Парсим ссылку через ScrapingBee: {url}")
        
        response = requests.get(
            api_endpoint,
            params=params,
            timeout=35,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            }
        )

        logger.info(f"ScrapingBee статус: {response.status_code}")  # ✅ немного больше логов
        logger.debug(f"ScrapingBee ответ (фрагмент): {response.text[:300]}")

        if response.status_code == 200:
            html = response.text
            
            # Проверяем, не вернулась ли капча/блокировка
            html_lower = html.lower()
            if any(marker in html_lower for marker in ["captcha", "cloudflare", "access denied"]):
                logger.error("⚠️ Сайт вернул капчу/блокировку")
                raise ValueError("Сайт заблокировал запрос. Попробуйте скопировать текст вручную.")
            
            if len(html) < 500:
                logger.warning(f"⚠️ ScrapingBee вернул короткий ответ ({len(html)} символов)")
                raise ValueError("Не удалось получить контент с сайта.")
            
            logger.info(f"✅ Успешно получено {len(html)} символов с {url}")
            return html
            
        elif response.status_code == 403:
            logger.error("❌ Доступ запрещен (проверьте API ключ)")
            raise PermissionError("Неверный API ключ ScrapingBee")
            
        elif response.status_code == 429:
            logger.error("❌ Превышен лимит запросов")
            raise RuntimeError("Лимит ScrapingBee исчерпан. Подождите или обновите тариф.")
            
        else:
            logger.error(f"❌ Ошибка ScrapingBee: {response.status_code}")
            response.raise_for_status()
            return ""
            
    except requests.exceptions.Timeout:
        logger.error(f"❌ Таймаут при парсинге {url}")
        raise TimeoutError("Сайт не отвечает. Попробуйте позже.")
        
    except Exception as e:
        logger.error(f"❌ Ошибка парсинга {url}: {str(e)}")
        raise ValueError(f"Ошибка при обработке ссылки: {str(e)}")

# =========================
# PDF ПАРСЕР - ТОЛЬКО ДЛЯ PDF
# =========================

def extract_text_from_pdf_bytes(data: bytes) -> str:
    """
    Извлечение текста из PDF файла
    РАБОТАЕТ ТОЛЬКО ДЛЯ PDF!
    """
    try:
        reader = PdfReader(BytesIO(data))
        pages_text = []
        
        for page in reader.pages:
            page_text = page.extract_text()
            if page_text:
                pages_text.append(page_text)
        
        text = "\n\n".join(pages_text)
        
        # Очистка текста
        text = clean_text(text)
        
        if not text or len(text) < 50:
            raise ValueError("PDF файл пуст или не содержит читаемого текста")
        
        logger.info(f"✅ Извлечено {len(text)} символов из PDF")
        return text
        
    except Exception as e:
        logger.error(f"❌ Ошибка чтения PDF: {str(e)}")
        raise ValueError(f"Не удалось прочитать PDF файл: {str(e)}")

# =========================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# =========================

def clean_text(raw: str) -> str:
    """Очистка текста"""
    if not raw:
        return ""
    
    text = raw.replace('\r\n', '\n').replace('\r', '\n')
    lines = [line.strip() for line in text.split('\n')]
    text = '\n'.join(lines)
    text = re.sub(r'\n{3,}', '\n\n', text)
    
    return text.strip()

# --- НОВАЯ ЛОГИКА РАБОТЫ СО ССЫЛКАМИ ---

URL_REGEX = re.compile(
    r'^(https?://)?([a-z0-9.-]+\.[a-z]{2,})(/.*)?$',
    re.IGNORECASE
)

def looks_like_url(text: str) -> bool:
    """
    Более мягкая проверка – похоже ли на URL.
    Поддерживает варианты:
    - https://hh.ru/vacancy/123
    - http://example.com
    - hh.ru/vacancy/123
    - www.hh.ru/vacancy/123
    """
    if not text:
        return False
    text = text.strip()
    return bool(URL_REGEX.match(text))

def normalize_url(text: str) -> str:
    """
    Гарантирует, что URL начинается с http/https.
    'hh.ru/vacancy/123' -> 'https://hh.ru/vacancy/123'
    """
    text = text.strip()
    if not text.startswith(("http://", "https://")):
        return "https://" + text
    return text

# (старую is_url можно оставить, но она больше не используется
#  – можно удалить, если хочешь полностью очистить код)

def html_to_text(html: str) -> str:
    """Извлечение текста из HTML"""
    try:
        soup = BeautifulSoup(html, 'html.parser')
        
        # Удаляем ненужные элементы
        for element in soup(["script", "style", "nav", "footer", "header"]):
            element.decompose()
        
        text = soup.get_text(separator='\n')
        return clean_text(text)
        
    except Exception as e:
        logger.error(f"❌ Ошибка извлечения текста из HTML: {str(e)}")
        return ""

def load_system_prompt() -> str:
    """Загрузка системного промпта"""
    try:
        with open("system_prompt.txt", "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        # Промпт для анализа резюме и вакансий
        return """Ты помощник для составления сопроводительных писем к резюме. 
Пользователь отправляет тебе:
1. Сначала свое резюме (текст или PDF)
2. Потом вакансию (ссылку или текст)

Твоя задача:
1. Проанализировать соответствие резюме вакансии
2. Выделить ключевые совпадения навыков
3. Составить сопроводительное письмо
4. Дать рекомендации по улучшению резюме

Будь конкретным, деловым и полезным."""

# =========================
# TELEGRAM BOT ФУНКЦИИ
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /start"""
    user = update.effective_user
    await update.message.reply_html(
        f"👋 Привет {user.mention_html()}!\n\n"
        f"Я помогу составить идеальное сопроводительное письмо.\n\n"
        f"📝 <b>Как это работает:</b>\n"
        f"1. Отправь мне свое <b>резюме</b> (текст или PDF)\n"
        f"2. Потом отправь <b>вакансию</b> (ссылку или текст)\n"
        f"3. Я проанализирую и составлю письмо\n\n"
        f"🔗 <b>Поддерживаю:</b> hh.ru, tochka.com, habr.com и другие сайты\n"
        f"📄 <b>Форматы:</b> PDF, текст, ссылки"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /help"""
    help_text = """
📋 <b>Доступные команды:</b>
/start - Начать работу
/help - Помощь
/update_resume - Обновить резюме

📝 <b>Как использовать:</b>
1. Сначала отправь резюме командой /update_resume
2. Потом отправляй вакансии
3. Я составлю сопроводительное письмо

🔗 <b>Примеры:</b>
- Отправь PDF с резюме
- Отправь ссылку на hh.ru/vacancy/123
- Отправь текст вакансии

💡 <b>Совет:</b> Чем подробнее резюме, тем лучше результат!
"""
    await update.message.reply_text(help_text, parse_mode='HTML')

async def update_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обновление резюме"""
    context.user_data['awaiting_resume'] = True
    await update.message.reply_text(
        "📝 Отправь свое резюме одним из способов:\n\n"
        "• PDF файл с резюме\n"
        "• Текст резюме\n"
        "• Ссылку на резюме\n\n"
        "Я сохраню его для последующего анализа вакансий."
    )

async def process_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Основная функция обработки сообщений
    Четкая логика: PDF → локальный парсер, Ссылка → ScrapingBee, Текст → как есть
    """
    message = update.message
    if not message:
        return
    
    user_data = context.user_data
    
    try:
        # 1. ОПРЕДЕЛЯЕМ ТИП СООБЩЕНИЯ
        text_content = ""
        
        if message.document and message.document.mime_type == "application/pdf":
            # 📄 PDF ФАЙЛ - парсим локально
            logger.info(f"📄 Обработка PDF от пользователя {message.from_user.id}")
            
            file = await message.document.get_file()
            bio = BytesIO()
            await file.download_to_memory(out=bio)
            
            text_content = extract_text_from_pdf_bytes(bio.getvalue())
            logger.info(f"✅ PDF обработан: {len(text_content)} символов")
            
        elif message.text:
            input_text = message.text.strip()
            
            # ✅ НОВАЯ ЛОГИКА: сначала проверяем, похож ли текст на ссылку
            if looks_like_url(input_text):
                # 🔗 ССЫЛКА - парсим через ScrapingBee
                url = normalize_url(input_text)
                logger.info(f"🔗 Обработка ссылки: {input_text} -> {url}")
                
                # Показываем, что бот работает
                await message.chat.send_action(action="typing")
                
                # Парсим через ScrapingBee
                html = parse_url_with_scrapingbee(url)
                
                # Извлекаем текст из HTML
                text_content = html_to_text(html)
                
                if not text_content or len(text_content) < MIN_MEANINGFUL_TEXT_LENGTH:
                    raise ValueError(
                        f"Не удалось получить текст с сайта.\n"
                        f"Попробуйте скопировать текст вакансии вручную."
                    )
                
                logger.info(f"✅ Ссылка обработана: {len(text_content)} символов")
                
            else:
                # 📝 ОБЫЧНЫЙ ТЕКСТ - используем как есть
                text_content = clean_text(input_text)
                logger.info(f"📝 Обработка текста: {len(text_content)} символов")
        
        else:
            await message.reply_text(
                "❌ Поддерживаются только:\n"
                "• PDF файлы\n"
                "• Текст\n"
                "• Ссылки на сайты"
            )
            return
        
        # 2. ПРОВЕРЯЕМ КОНТЕКСТ (резюме или вакансия)
        if user_data.get('awaiting_resume'):
            # 📋 СОХРАНЯЕМ РЕЗЮМЕ
            user_data['resume'] = text_content
            user_data['awaiting_resume'] = False
            
            await message.reply_text(
                f"✅ <b>Резюме сохранено!</b>\n\n"
                f"📊 Получено: {len(text_content)} символов\n\n"
                f"Теперь отправь <b>вакансию</b> (ссылку или текст),\n"
                f"и я составлю сопроводительное письмо!",
                parse_mode='HTML'
            )
            
        elif 'resume' in user_data:
            # 🎯 АНАЛИЗИРУЕМ ВАКАНСИЮ
            await analyze_vacancy(message, user_data['resume'], text_content)
            
        else:
            # ❓ НЕТ РЕЗЮМЕ - просим сначала его
            await message.reply_text(
                "📝 Сначала отправь свое <b>резюме</b> командой /update_resume,\n"
                "а потом - вакансию для анализа.",
                parse_mode='HTML'
            )
            
    except ValueError as e:
        # Ошибки пользовательского ввода
        await message.reply_text(f"⚠️ {str(e)}")
        
    except Exception as e:
        # Неожиданные ошибки
        logger.error(f"❌ Критическая ошибка: {str(e)}", exc_info=True)
        await message.reply_text(
            "❌ Произошла непредвиденная ошибка.\n"
            "Попробуйте еще раз или свяжитесь с поддержкой."
        )

async def analyze_vacancy(message, resume_text: str, vacancy_text: str) -> None:
    """
    Анализ вакансии с использованием OpenAI
    """
    if not openai_client:
        await message.reply_text("❌ OpenAI не настроен. Проверьте OPENAI_API_KEY")
        return
    
    # Показываем, что бот работает
    await message.chat.send_action(action="typing")
    
    try:
        system_prompt = load_system_prompt()
        
        # Формируем промпт для анализа
        prompt = f"""
АНАЛИЗ СОПРОВОДИТЕЛЬНОГО ПИСЬМА

РЕЗЮМЕ КАНДИДАТА:
{resume_text[:3000]}  # Ограничиваем размер

ТЕКСТ ВАКАНСИИ:
{vacancy_text[:3000]}  # Ограничиваем размер

ЗАДАЧА:
1. Проанализировать соответствие резюме требованиям вакансии
2. Выделить 3-5 ключевых совпадений навыков и опыта
3. Составить профессиональное сопроводительное письмо
4. Дать рекомендации по подаче (что подчеркнуть в резюме)

ФОРМАТ ОТВЕТА:
📊 Анализ соответствия
✅ Ключевые совпадения
📝 Готовое сопроводительное письмо
💡 Рекомендации по подаче

Будь конкретным, деловым и полезным.
"""
        
        response = openai_client.chat.completions.create(
            model="gpt-4.1-mini",  # Идеально для этой задачи
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt}
            ],
            temperature=0.7,
            max_tokens=2000
        )
        
        ai_response = response.choices[0].message.content or "❌ Не удалось сформировать ответ"
        
        # Отправляем ответ пользователю
        if len(ai_response) > 4000:
            # Разбиваем длинные сообщения
            parts = [ai_response[i:i+4000] for i in range(0, len(ai_response), 4000)]
            for i, part in enumerate(parts, 1):
                await message.reply_text(f"📄 Часть {i}/{len(parts)}:\n\n{part}")
        else:
            await message.reply_text(ai_response)
            
        logger.info(f"✅ OpenAI ответ сгенерирован: {len(ai_response)} символов")
        
    except Exception as e:
        logger.error(f"❌ OpenAI ошибка: {str(e)}")
        await message.reply_text(
            "❌ Ошибка при анализе через AI.\n"
            "Попробуйте еще раз или проверьте подключение."
        )

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка ошибок"""
    logger.error(f"Ошибка в боте: {context.error}", exc_info=True)
    
    if update and update.message:
        await update.message.reply_text(
            "⚠️ Произошла техническая ошибка.\n"
            "Разработчики уже уведомлены. Попробуйте позже."
        )

def main() -> None:
    """Запуск бота"""
    global openai_client
    
    # Проверка обязательных переменных
    token = os.getenv('TELEGRAM_BOT_TOKEN')
    openai_key = os.getenv('OPENAI_API_KEY')
    scrapingbee_key = os.getenv('SCRAPINGBEE_API_KEY')
    
    if not token:
        print("❌ ОШИБКА: TELEGRAM_BOT_TOKEN не найден!")
        print("Добавьте в .env файл: TELEGRAM_BOT_TOKEN=ваш_токен")
        print("Получите токен у @BotFather в Telegram")
        return
    
    if not openai_key:
        print("⚠️ ВНИМАНИЕ: OPENAI_API_KEY не найден")
        print("AI функции не будут работать")
        print("Добавьте в .env: OPENAI_API_KEY=ваш_ключ")
    
    if not scrapingbee_key:
        print("⚠️ ВНИМАНИЕ: SCRAPINGBEE_API_KEY не найден")
        print("Парсинг ссылок не будет работать")
        print("Получите бесплатный ключ: https://www.scrapingbee.com/")
        print("Добавьте в .env: SCRAPINGBEE_API_KEY=ваш_ключ")
    
    # Инициализация клиентов
    if openai_key:
        try:
            openai_client = OpenAI(api_key=openai_key)
            logger.info("✅ OpenAI клиент инициализирован")
        except Exception as e:
            logger.error(f"❌ Ошибка инициализации OpenAI: {e}")
    
    # Создание приложения
    app = Application.builder().token(token).build()
    
    # Регистрация обработчиков
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("update_resume", update_resume))
    
    # Обработчик всех сообщений
    app.add_handler(MessageHandler(
        filters.TEXT | filters.Document.PDF,
        process_message
    ))
    
    # Обработчик ошибок
    app.add_error_handler(error_handler)
    
    # Запуск бота
    logger.info("🤖 Бот запускается...")
    print("=" * 50)
    print("✅ Бот успешно запущен!")
    print("Отправьте /start в Telegram для начала работы")
    print("=" * 50)
    
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
