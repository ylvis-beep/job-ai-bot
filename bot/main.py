import os
import logging
from typing import Any, Dict, List, cast
import re
import time
import random
import json
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

# =========================
# Улучшенные функции парсинга
# =========================

def get_rotating_user_agent() -> str:
    """Возвращает случайный User-Agent."""
    user_agents = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Safari/605.1.15",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36 Edg/121.0.0.0",
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
    """Проверяем, похожа ли строка на URL."""
    if not text:
        return False
    text = text.strip()
    
    # Быстрая проверка по паттернам
    url_patterns = [
        r'^https?://\S+$',
        r'^www\.\S+\.\S+$',
        r'^\S+\.(com|ru|net|org|io|me)\S*$'
    ]
    
    for pattern in url_patterns:
        if re.match(pattern, text, re.IGNORECASE):
            return True
    
    try:
        parsed = urlparse(text)
        if parsed.scheme in ('http', 'https', ''):
            if parsed.netloc and '.' in parsed.netloc:
                return True
    except:
        pass
    
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


def _extract_json_ld(soup: BeautifulSoup) -> str:
    """Извлекаем структурированные данные из JSON-LD."""
    json_ld_data = []
    scripts = soup.find_all('script', type='application/ld+json')
    
    for script in scripts:
        try:
            data = json.loads(script.string)
            # Ищем данные о вакансиях
            if isinstance(data, dict):
                if data.get('@type') in ['JobPosting', 'JobPosting']:
                    job_info = []
                    for key in ['title', 'description', 'responsibilities', 'requirements', 
                                'qualifications', 'skills', 'experienceRequirements']:
                        if key in data:
                            value = data[key]
                            if isinstance(value, str):
                                job_info.append(f"{key}: {value}")
                            elif isinstance(value, list):
                                job_info.append(f"{key}: " + ", ".join(str(v) for v in value))
                    if job_info:
                        json_ld_data.append("\n".join(job_info))
        except:
            continue
    
    return "\n\n".join(json_ld_data) if json_ld_data else ""


def _extract_meta_content(soup: BeautifulSoup) -> str:
    """Извлекаем контент из мета-тегов."""
    meta_content = []
    
    # Meta description
    meta_desc = soup.find('meta', attrs={'name': 'description'})
    if meta_desc and meta_desc.get('content'):
        meta_content.append(f"Описание: {meta_desc['content']}")
    
    # Open Graph
    og_desc = soup.find('meta', attrs={'property': 'og:description'})
    if og_desc and og_desc.get('content'):
        meta_content.append(f"OG Описание: {og_desc['content']}")
    
    # Title
    title = soup.find('title')
    if title and title.string:
        meta_content.append(f"Заголовок: {title.string}")
    
    # H1
    h1_tags = soup.find_all('h1')
    for h1 in h1_tags[:2]:  # Берем первые 2 h1
        if h1 and h1.get_text(strip=True):
            meta_content.append(f"Заголовок H1: {h1.get_text(strip=True)}")
    
    return "\n".join(meta_content)


def _try_advanced_request(url: str) -> str:
    """Продвинутый запрос с обходом блокировок."""
    session = requests.Session()
    
    headers = {
        'User-Agent': get_rotating_user_agent(),
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
        'Accept-Encoding': 'gzip, deflate',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
        'Cache-Control': 'max-age=0',
    }
    
    # Добавляем случайный реферер
    referers = [
        'https://www.google.com/',
        'https://yandex.ru/',
        'https://www.bing.com/',
    ]
    headers['Referer'] = random.choice(referers)
    
    session.headers.update(headers)
    
    try:
        # Сначала идем на главную страницу для установки куков
        parsed = urlparse(url)
        base_url = f"{parsed.scheme}://{parsed.netloc}"
        
        try:
            session.get(base_url, timeout=5)
            time.sleep(1)
        except:
            pass
        
        # Ждем перед основным запросом
        time.sleep(random.uniform(1, 2))
        
        # Основной запрос
        response = session.get(url, timeout=20, allow_redirects=True)
        logger.info(f"Advanced request to {url}: {response.status_code}")
        
        if response.status_code != 200:
            # Пробуем еще раз с другими заголовками
            time.sleep(2)
            headers['User-Agent'] = get_rotating_user_agent()
            response = session.get(url, headers=headers, timeout=20)
            
        if response.status_code != 200:
            response.raise_for_status()
        
        # Парсим HTML
        soup = BeautifulSoup(response.content, 'html.parser')
        
        # Удаляем ненужные элементы
        for tag in soup(['script', 'style', 'noscript', 'iframe', 'svg']):
            tag.decompose()
        
        # Пробуем разные стратегии извлечения
        content_sources = []
        
        # 1. JSON-LD данные
        json_ld = _extract_json_ld(soup)
        if json_ld:
            content_sources.append(f"Структурированные данные:\n{json_ld}")
        
        # 2. Мета информация
        meta_info = _extract_meta_content(soup)
        if meta_info:
            content_sources.append(f"Мета информация:\n{meta_info}")
        
        # 3. Ищем основной контент
        content_selectors = [
            'article', 'main', 
            '[class*="vacancy"]', '[class*="job"]', '[class*="description"]',
            '[class*="content"]', '[class*="text"]', '[class*="body"]',
            '.container', '.wrapper', '.page-content',
        ]
        
        main_content = ""
        for selector in content_selectors:
            try:
                elements = soup.select(selector)
                for elem in elements:
                    text = elem.get_text(separator='\n', strip=True)
                    if len(text) > 200:  # Достаточно длинный текст
                        main_content = text
                        break
                if main_content:
                    break
            except:
                continue
        
        if main_content:
            content_sources.append(f"Основной контент:\n{main_content}")
        else:
            # Весь текст как запасной вариант
            body = soup.find('body') or soup
            full_text = body.get_text(separator='\n', strip=True)
            if len(full_text) > 100:
                content_sources.append(f"Весь текст:\n{full_text}")
        
        # Объединяем все источники
        if content_sources:
            combined = "\n\n".join(content_sources)
            return clean_text(combined)
        else:
            raise RuntimeError("NO_CONTENT")
            
    except requests.RequestException as e:
        logger.error(f"Request error for {url}: {e}")
        raise RuntimeError("REQUEST_FAILED")
    except Exception as e:
        logger.error(f"Parsing error for {url}: {e}")
        raise RuntimeError("PARSING_FAILED")
    finally:
        session.close()


def _try_simple_request(url: str) -> str:
    """Простой запрос как запасной вариант."""
    headers = {
        'User-Agent': get_rotating_user_agent(),
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    }
    
    try:
        response = requests.get(url, headers=headers, timeout=15, allow_redirects=True)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.content, 'html.parser')
        
        # Удаляем скрипты и стили
        for tag in soup(['script', 'style']):
            tag.decompose()
        
        text = soup.get_text(separator='\n', strip=True)
        return clean_text(text)
        
    except requests.RequestException as e:
        logger.error(f"Simple request failed for {url}: {e}")
        raise RuntimeError("SIMPLE_REQUEST_FAILED")


def extract_text_from_url(url: str) -> str:
    """
    Основная функция для извлечения текста из URL.
    Пробует несколько стратегий.
    """
    logger.info(f"Attempting to parse URL: {url}")
    
    # Нормализуем URL
    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url
    
    strategies = [
        _try_advanced_request,
        _try_simple_request,
    ]
    
    for strategy in strategies:
        try:
            result = strategy(url)
            if result and len(result.strip()) > 100:
                logger.info(f"Strategy {strategy.__name__} succeeded for {url}")
                return result
        except Exception as e:
            logger.debug(f"Strategy {strategy.__name__} failed: {e}")
            time.sleep(1)
            continue
    
    logger.warning(f"All parsing strategies failed for {url}")
    raise RuntimeError("UNABLE_TO_PARSE")


def prepare_input_text(raw: str) -> str:
    """
    Универсальная функция для обработки ввода.
    """
    if not raw:
        return ""
    
    raw = raw.strip()
    
    # Если это не ссылка, просто чистим текст
    if not is_url(raw):
        return clean_text(raw)
    
    # Если это ссылка - пробуем парсить
    logger.info(f"Detected URL message: {raw}")
    
    try:
        text = extract_text_from_url(raw)
        if text and len(text.strip()) > 100:
            return text
        else:
            # Если текст слишком короткий
            return clean_text(f"Вакансия по ссылке: {raw}")
    except RuntimeError:
        # В случае ошибки парсинга возвращаем ссылку как текст
        return clean_text(f"Вакансия по ссылке: {raw}")
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        return clean_text(f"Вакансия по ссылке: {raw}")


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

Я помогу вам с:
- Анализом вакансий
- Сопоставлением с вашим резюме
- Составлением сопроводительных писем
- Подготовкой к собеседованиям

Просто отправьте мне:
1. Ваше резюме (текстом или PDF)
2. Ссылку на интересующую вакансию
3. Или описание вакансии текстом

Я работаю с большинством сайтов вакансий!
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
        
        # Показываем, что бот работает
        await message.chat.send_action(action="typing")
        
        try:
            user_message = prepare_input_text(raw_text)
        except Exception as e:
            logger.error(f"Error while processing input text or URL: {e}")
            # В тихом режиме не показываем ошибки
            user_message = clean_text(raw_text)

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
            messages.extend(history[-max_history_messages:])

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
            "Произошла ошибка при обработке запроса. Пожалуйста, попробуйте ещё раз."
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
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("update_resume", update_resume))

    # Register message handler for текст + PDF
    application.add_handler(
        MessageHandler(
            (filters.TEXT | filters.Document.PDF) & ~filters.COMMAND,
            chat,
        )
    )

    # Register error handler
    application.add_error_handler(error_handler)

    # Start the bot
    logger.info("Bot is starting...")
    print("Bot is running! Press Ctrl+C to stop.")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
