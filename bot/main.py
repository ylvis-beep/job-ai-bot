import os
import logging
from typing import Any, Dict, List, cast
import re
import time
import random
from urllib.parse import urlparse
import requests
from bs4 import BeautifulSoup
from io import BytesIO
from PyPDF2 import PdfReader

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from openai import OpenAI
from openai.types.chat import ChatCompletionMessageParam

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Initialize OpenAI client
openai_client = None


def get_random_user_agent() -> str:
    """Возвращает случайный User-Agent из списка."""
    user_agents = [
        # Chrome на Windows
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.6045.159 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",
        
        # Chrome на Mac
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_14_6) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
        
        # Firefox
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:121.0) Gecko/20100101 Firefox/121.0",
        "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0",
        
        # Safari
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_14_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.1.2 Safari/605.1.15",
        
        # Edge
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36 Edg/119.0.0.0",
    ]
    
    return random.choice(user_agents)


def load_system_prompt() -> str:
    """Load the system prompt from system_prompt.txt if it exists."""
    try:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        file_path = os.path.join(base_dir, "system_prompt.txt")
        with open(file_path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return "You are a helpful assistant."
    except Exception as e:
        logger.warning(f"Failed to load system_prompt.txt: {e}")
        return "You are a helpful assistant."


# =========================
# Парсинг текста / ссылок / PDF
# =========================

def clean_text(raw: str) -> str:
    """Приводит текст в аккуратный вид."""
    if not raw:
        return ""
    text = raw.replace("\r\n", "\n")
    lines = [line.strip() for line in text.split("\n")]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def is_url(text: str) -> bool:
    """Проверяем, похожа ли строка на URL (если сообщение целиком — URL)."""
    if not text:
        return False
    text = text.strip()
    try:
        parsed = urlparse(text)
        # Более строгая проверка для URL
        return (parsed.scheme in ("http", "https") and 
                bool(parsed.netloc) and 
                '.' in parsed.netloc and
                len(parsed.netloc) > 3)
    except ValueError:
        return False


def extract_text_from_pdf_bytes(data: bytes) -> str:
    """Достаём текст из PDF по сырым байтам."""
    try:
        reader = PdfReader(BytesIO(data))
        pages_text: List[str] = []
        for page in reader.pages:
            page_text = page.extract_text() or ""
            pages_text.append(page_text)
        return clean_text("\n\n".join(pages_text))
    except Exception as e:
        logger.error(f"Error while extracting text from PDF bytes: {e}")
        return ""


def extract_text_from_url(url: str) -> str:
    """
    Скачиваем страницу/файл по ссылке и вытаскиваем текст.
    
    УЛУЧШЕННЫЙ ВАРИАНТ с обходом блокировок:
    - Случайный User-Agent
    - Полный набор заголовков браузера
    - Задержка перед запросом
    - Поддержка редиректов
    - Обработка кодировки
    """
    # Случайный User-Agent для каждого запроса
    headers = {
        "User-Agent": get_random_user_agent(),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;"
            "q=0.9,image/webp,image/apng,*/*;q=0.8,"
            "application/signed-exchange;v=b3;q=0.7"
        ),
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7,uk;q=0.6,de;q=0.5",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Cache-Control": "max-age=0",
        "Pragma": "no-cache",
        "Referer": "https://www.google.com/",  # Добавляем реферер
    }
    
    # Параметры для обхода блокировок
    request_params = {
        "headers": headers,
        "timeout": 15,  # Увеличиваем таймаут
        "allow_redirects": True,  # Разрешаем редиректы
        "verify": True,  # Проверяем SSL
        "stream": False,  # Не использовать stream для простых запросов
    }
    
    try:
        # Небольшая случайная задержка (имитация поведения человека)
        time.sleep(random.uniform(0.5, 2.0))
        
        logger.info(f"Fetching URL: {url} with User-Agent: {headers['User-Agent'][:50]}...")
        
        resp = requests.get(url, **request_params)
        logger.info(f"Fetched URL {url} with status {resp.status_code}")
        
        # Если статус уже не 2xx — сразу обрабатываем
        if resp.status_code == 403:
            logger.warning(f"Forbidden (403) while fetching {url}")
            raise RuntimeError("REMOTE_FORBIDDEN")
        
        if resp.status_code == 429:
            logger.warning(f"Too Many Requests (429) while fetching {url}")
            raise RuntimeError("REMOTE_HTTP_ERROR_429")

        if resp.status_code < 200 or resp.status_code >= 300:
            code = resp.status_code
            logger.error(f"Non-OK HTTP status {code} while fetching {url}")
            raise RuntimeError(f"REMOTE_HTTP_ERROR_{code}")
    
    except requests.Timeout as e:
        logger.error(f"Timeout while fetching {url}: {e}")
        raise RuntimeError("NETWORK_ERROR") from e
    except requests.TooManyRedirects as e:
        logger.error(f"Too many redirects for {url}: {e}")
        raise RuntimeError("NETWORK_ERROR") from e
    except requests.RequestException as e:
        # Любые другие сетевые ошибки
        logger.error(f"Network error while fetching {url}: {e}")
        raise RuntimeError("NETWORK_ERROR") from e

    # Если мы здесь — статус 2xx, можно парсить
    content_type = resp.headers.get("Content-Type", "").lower()
    
    # Пробуем определить кодировку если не указана
    if not resp.encoding:
        try:
            # Простая проверка на UTF-8
            resp.content.decode('utf-8')
            resp.encoding = 'utf-8'
        except UnicodeDecodeError:
            # Пробуем другие кодировки
            try:
                resp.content.decode('cp1251')
                resp.encoding = 'cp1251'
            except:
                resp.encoding = 'utf-8'  # По умолчанию
    
    # Обработка PDF
    if "pdf" in content_type:
        return extract_text_from_pdf_bytes(resp.content)
    
    # Обработка HTML
    try:
        # Используем 'html.parser' для надежности (не требует lxml)
        soup = BeautifulSoup(resp.content, "html.parser", from_encoding=resp.encoding)
        
        # Удаляем ненужные элементы
        for element in soup(["script", "style", "nav", "footer", "header", 
                           "aside", "form", "iframe", "noscript"]):
            element.decompose()
        
        # Получаем основной контент
        # Сначала ищем article, main, или div с контентом
        main_content = soup.find('article') or soup.find('main') or soup.find('div', class_=re.compile(r'content|post|article|text', re.I))
        
        if main_content:
            body = main_content
        else:
            body = soup.body or soup
        
        # Получаем текст с сохранением структуры
        text = body.get_text(separator="\n", strip=True)
        
        # Дополнительная очистка
        text = re.sub(r'\s*\n\s*\n\s*', '\n\n', text)  # Убираем лишние пустые строки
        text = re.sub(r'[ \t]+', ' ', text)  # Заменяем множественные пробелы
        
        return clean_text(text)
    
    except Exception as e:
        logger.error(f"Error parsing HTML from {url}: {e}")
        # В случае ошибки парсинга, пробуем просто получить весь текст
        try:
            return clean_text(resp.text)
        except:
            raise RuntimeError("PARSING_ERROR")


def prepare_input_text(raw: str) -> str:
    """
    Универсальная функция:
    - если строка целиком — ссылка, скачиваем и чистим,
    - если текст — просто чистим.
    """
    if not raw:
        return ""
    raw = raw.strip()
    if is_url(raw):
        logger.info(f"Detected URL message: {raw}")
        try:
            return extract_text_from_url(raw)
        except RuntimeError as e:
            # Пробуем добавить https:// если его нет
            if not raw.startswith(('http://', 'https://')):
                try:
                    return extract_text_from_url('https://' + raw)
                except:
                    raise e
            else:
                raise e
    return clean_text(raw)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a message when the command /start is issued."""
    message = update.message
    if message is None:
        logger.warning("Received /start update without message")
        return

    user = update.effective_user
    if user is None:
        await message.reply_text(
            "Привет! Я твой помощник в поиске работы и по сопроводительным письмам. "
            "Отправь резюме или ссылку на него, а я подберу формулировки и соберу письма под нужные вакансии"
        )
        return

    await message.reply_html(
        rf"Привет {user.mention_html()}! Я твой помощник в поиске работы и по сопроводительным письмам. "
        rf"Отправь резюме или ссылку на него, а я подберу формулировки и соберу письма под нужные вакансии"
    )


async def help_command(update: Update,
                       context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a message when the command /help is issued."""
    message = update.message
    if message is None:
        logger.warning("Received /help update without message")
        return

    help_text = """
Доступные команды:
/start - Запустить бота
/help - Показать это сообщение помощи
/update_resume - Загрузите новые файлы с описанием вашего опыта

Чтобы я работал точнее, сначала пришли полное описание своих навыков, опыта и достижений или резюме.
Потом отправляй вакансии, а я буду присылать:

* главные требования для резюме
* таблицу совпадений и процент совпадения
* пункты, которые лучше подсветить при отклике
* готовое сопроводительное письмо

Поддерживаемые форматы:
- Текст (просто отправьте текст)
- PDF файлы
- Ссылки на вакансии (большинство сайтов)

    """
    await message.reply_text(help_text)


async def update_resume(update: Update,
                        context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /update_resume command."""
    message = update.message
    if message is None:
        logger.warning("Received /update_resume update without message")
        return

    user_data = cast(Dict[str, Any], context.user_data)
    user_data['awaiting_resume'] = True
    user_data.pop('resume', None)
    await message.reply_text("Загрузите новые файлы с описанием вашего опыта")


async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle user messages with OpenAI."""
    message = update.message
    if message is None:
        logger.warning("Received text update without message in chat handler")
        return

    if not openai_client:
        await message.reply_text(
            "Sorry, OpenAI is not configured. Please set the OPENAI_API_KEY environment variable."
        )
        return

    user_data = cast(Dict[str, Any], context.user_data)

    # Определяем, откуда брать текст: PDF или текст
    user_message: str

    if message.document is not None:
        doc = message.document
        is_pdf = (
            doc.mime_type == "application/pdf"
            or (doc.file_name and doc.file_name.lower().endswith(".pdf"))
        )

        if not is_pdf:
            await message.reply_text(
                "Сейчас я умею обрабатывать только PDF-файлы и текстовые сообщения."
            )
            return

        try:
            file = await doc.get_file()
            bio = BytesIO()
            await file.download_to_memory(out=bio)
            pdf_bytes = bio.getvalue()
            extracted = extract_text_from_pdf_bytes(pdf_bytes)
            if not extracted:
                await message.reply_text(
                    "Не удалось извлечь текст из PDF. Попробуйте другой файл или отправьте текст."
                )
                return
            user_message = extracted
        except Exception as e:
            logger.error(f"Error while downloading/reading PDF: {e}")
            await message.reply_text(
                "Произошла ошибка при чтении PDF-файла. Попробуйте ещё раз или отправьте текст."
            )
            return

    else:  # текстовое сообщение
        raw_text = message.text
        if raw_text is None:
            await message.reply_text("Я могу обрабатывать только текст или PDF-файлы.")
            return
        try:
            user_message = prepare_input_text(raw_text)
        except RuntimeError as e:
            code = str(e)
            if code == "REMOTE_FORBIDDEN":
                await message.reply_text(
                    "⚠️ Сайт заблокировал доступ с моего сервера (ошибка 403 Forbidden).\n\n"
                    "🔹 **Что можно сделать:**\n"
                    "1. Скопируйте текст вакансии и пришлите его сюда текстом\n"
                    "2. Попробуйте другую ссылку\n"
                    "3. Используйте PDF версию вакансии\n\n"
                    "Некоторые сайты (особенно с hh.ru, rabota.ru) защищают свои страницы от автоматического парсинга."
                )
                return
            elif code == "REMOTE_HTTP_ERROR_429":
                await message.reply_text(
                    "⚠️ Сайт ограничил количество запросов (Too Many Requests).\n\n"
                    "Пожалуйста, подождите 1-2 минуты и попробуйте снова, "
                    "или скопируйте текст вакансии вручную."
                )
                return
            elif code == "NETWORK_ERROR":
                await message.reply_text(
                    "⚠️ Не удалось подключиться к сайту по ссылке.\n\n"
                    "🔹 **Возможные причины:**\n"
                    "1. Сайт временно недоступен\n"
                    "2. Проблемы с сетью\n"
                    "3. Неверная ссылка\n\n"
                    "Попробуйте ещё раз позже или пришлите текст вакансии вручную."
                )
                return
            elif code.startswith("REMOTE_HTTP_ERROR_"):
                status = code.split("_")[-1]
                await message.reply_text(
                    f"⚠️ Сайт вернул ошибку {status}.\n\n"
                    "Пожалуйста, скопируйте текст вакансии и отправьте его сюда текстом, "
                    "или проверьте правильность ссылки."
                )
                return
            elif code == "PARSING_ERROR":
                await message.reply_text(
                    "⚠️ Не удалось обработать содержимое страницы.\n\n"
                    "Пожалуйста, скопируйте текст вакансии и отправьте его сюда."
                )
                return
            else:
                logger.error(f"Runtime error while processing input text or URL: {e}")
                await message.reply_text(
                    "❌ Не удалось обработать текст или ссылку. Попробуйте другой формат."
                )
                return
        except Exception as e:
            logger.error(f"Error while processing input text or URL: {e}")
            await message.reply_text(
                "❌ Не удалось обработать текст или ссылку. Попробуйте другой формат."
            )
            return

    # Если ожидаем новое резюме после /update_resume — сохраняем его и не вызываем OpenAI
    if user_data.get("awaiting_resume"):
        user_data["resume"] = user_message
        user_data["awaiting_resume"] = False
        await message.reply_text(
            "✅ Спасибо! Я обновил информацию о вашем опыте.\n\n"
            "Теперь отправьте вакансию или вопрос, "
            "и я буду использовать это резюме для анализа."
        )
        return

    try:
        # Send typing action to show the bot is processing
        await message.chat.send_action(action="typing")

        system_prompt = load_system_prompt()

        # История диалога по пользователю/чату
        history = cast(List[Dict[str, str]], user_data.get("history", []))
        max_history_messages = 10

        messages: List[ChatCompletionMessageParam] = [
            {
                "role": "system",
                "content": system_prompt
            }
        ]

        if history:
            messages.extend(history[-max_history_messages:])  # type: ignore[arg-type]

        # Если сохранено резюме — добавляем его как отдельное сообщение-контекст
        resume = user_data.get("resume")
        if resume:
            messages.append({
                "role": "user",
                "content": (
                    "Это резюме пользователя. Используй его как основной контекст "
                    "при анализе вакансий, составлении таблиц совпадений и подготовке сопроводительных писем:\n\n"
                    f"{resume}"
                ),
            })

        # Текущее сообщение
        messages.append({
            "role": "user",
            "content": user_message
        })

        # Call OpenAI API
        response = openai_client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=messages,
            max_completion_tokens=2048
        )

        ai_response = response.choices[0].message.content or (
            "Извините, не удалось сформировать ответ."
        )
        await message.reply_text(ai_response)

        # Обновляем историю: user → assistant
        if ai_response:
            history.append({"role": "user", "content": user_message})
            history.append({"role": "assistant", "content": ai_response})
            user_data["history"] = history[-max_history_messages:]

    except Exception as e:
        logger.error(f"Error calling OpenAI API: {e}")
        await message.reply_text(
            "Sorry, I encountered an error processing your message. Please try again."
        )


async def error_handler(update: Update,
                        context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log errors caused by updates."""
    logger.error(f"Update {update} caused error {context.error}")


def main() -> None:
    """Start the bot."""
    global openai_client

    # Get the bot token from environment variable
    token = os.getenv('TELEGRAM_BOT_TOKEN')
    openai_api_key = os.getenv('OPENAI_API_KEY')

    if not token:
        logger.error("TELEGRAM_BOT_TOKEN not found in environment variables!")
        print(
            "ERROR: Please set your TELEGRAM_BOT_TOKEN environment variable."
        )
        print("You can get a token from @BotFather on Telegram.")
        return

    if not openai_api_key:
        logger.warning("OPENAI_API_KEY not found in environment variables!")
        print(
            "WARNING: OpenAI API key not set. The bot will run but AI features won't work."
        )
        print("Please set your OPENAI_API_KEY to enable AI responses.")
    else:
        # Initialize OpenAI client
        openai_client = OpenAI(api_key=openai_api_key)
        logger.info("OpenAI client initialized successfully")

    # Create the Application
    application = Application.builder().token(token).build()

    # Register command handlers
    application.add_handler(CommandHandler("start", start))  # type: ignore[arg-type]
    application.add_handler(CommandHandler("help", help_command))  # type: ignore[arg-type]
    application.add_handler(CommandHandler("update_resume", update_resume))  # type: ignore[arg-type]

    # Register message handler for текст + PDF
    application.add_handler(
        MessageHandler(
            (filters.TEXT | filters.Document.PDF) & ~filters.COMMAND,
            chat,  # type: ignore[arg-type]
        )
    )

    # Register error handler
    application.add_error_handler(error_handler)  # type: ignore[arg-type]

    # Start the bot
    logger.info("Bot is starting...")
    print("Bot is running! Press Ctrl+C to stop.")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
