import os
import logging
from typing import Any, Dict, List, cast
import re
import time
import random
from urllib.parse import urlparse, urljoin
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


def get_rotating_user_agent() -> str:
    """Возвращает User-Agent с ротацией для обхода блокировок."""
    user_agents = [
        # Chrome последние версии
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        
        # Firefox
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:122.0) Gecko/20100101 Firefox/122.0",
        
        # Safari
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Safari/605.1.15",
        
        # Edge
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


# =========================
# Парсинг текста / ссылок / PDF - УЛУЧШЕННЫЙ
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
        # Более строгая проверка
        has_scheme = parsed.scheme in ("http", "https", "")
        has_netloc = bool(parsed.netloc)
        has_dot = '.' in parsed.netloc if parsed.netloc else False
        return has_scheme and has_netloc and has_dot
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


def extract_text_with_proxies(url: str) -> str:
    """Пытаемся получить текст через разные методы с обходом блокировок."""
    
    strategies = [
        _try_stealth_request,
        _try_with_session,
        _try_mobile_headers,
        _try_with_referer_chain,
    ]
    
    for strategy in strategies:
        try:
            result = strategy(url)
            if result and len(result.strip()) > 100:  # Проверяем, что получили достаточно текста
                logger.info(f"Strategy {strategy.__name__} succeeded for {url}")
                return result
        except Exception as e:
            logger.debug(f"Strategy {strategy.__name__} failed: {e}")
            continue
    
    # Если все стратегии провалились, пробуем простое извлечение
    try:
        return _try_simple_request(url)
    except:
        raise RuntimeError("UNABLE_TO_PARSE")


def _try_stealth_request(url: str) -> str:
    """Стелс-запрос с полным набором заголовков браузера."""
    headers = {
        "User-Agent": get_rotating_user_agent(),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7,uk;q=0.6",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Cache-Control": "max-age=0",
        "Pragma": "no-cache",
    }
    
    # Добавляем заголовки для обхода Cloudflare
    headers.update({
        "sec-ch-ua": '"Google Chrome";v="121", "Not A Brand";v="99", "Chromium";v="121"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
    })
    
    # Случайный реферер
    referers = [
        "https://www.google.com/",
        "https://yandex.ru/",
        "https://www.bing.com/",
        f"https://{urlparse(url).netloc}/"
    ]
    headers["Referer"] = random.choice(referers)
    
    time.sleep(random.uniform(1, 3))
    
    session = requests.Session()
    session.headers.update(headers)
    
    try:
        response = session.get(url, timeout=25, allow_redirects=True, verify=True)
        
        if response.status_code == 200:
            return _extract_content(response, url)
        elif response.status_code == 403:
            # Пробуем с другими заголовками
            time.sleep(2)
            alt_headers = headers.copy()
            alt_headers["User-Agent"] = get_rotating_user_agent()
            alt_headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
            
            response2 = session.get(url, headers=alt_headers, timeout=25)
            if response2.status_code == 200:
                return _extract_content(response2, url)
        
        response.raise_for_status()
        return _extract_content(response, url)
        
    finally:
        session.close()


def _try_with_session(url: str) -> str:
    """Использование сессии с куками."""
    session = requests.Session()
    
    # Инициализируем сессию
    init_headers = {
        "User-Agent": get_rotating_user_agent(),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    
    session.headers.update(init_headers)
    
    try:
        # Сначала получаем главную страницу для установки куков
        parsed = urlparse(url)
        base_url = f"{parsed.scheme}://{parsed.netloc}"
        
        try:
            session.get(base_url, timeout=10, allow_redirects=True)
            time.sleep(1)
        except:
            pass  # Игнорируем ошибки при получении главной страницы
        
        # Теперь запрашиваем нужную страницу
        time.sleep(random.uniform(0.5, 2))
        
        headers = {
            "Referer": base_url,
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        }
        
        response = session.get(url, headers=headers, timeout=20, allow_redirects=True)
        
        if response.status_code == 200:
            return _extract_content(response, url)
        
        response.raise_for_status()
        return _extract_content(response, url)
        
    finally:
        session.close()


def _try_mobile_headers(url: str) -> str:
    """Использование мобильных заголовков."""
    mobile_agents = [
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
        "Mozilla/5.0 (Linux; Android 13; SM-S901B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/112.0.0.0 Mobile Safari/537.36",
        "Mozilla/5.0 (Linux; Android 13; SM-G991B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/112.0.0.0 Mobile Safari/537.36",
    ]
    
    headers = {
        "User-Agent": random.choice(mobile_agents),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "X-Requested-With": "XMLHttpRequest",
    }
    
    time.sleep(random.uniform(1, 2))
    
    response = requests.get(url, headers=headers, timeout=20, allow_redirects=True)
    
    if response.status_code == 200:
        return _extract_content(response, url)
    
    response.raise_for_status()
    return _extract_content(response, url)


def _try_with_referer_chain(url: str) -> str:
    """Цепочка запросов с реферерами."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": get_rotating_user_agent(),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })
    
    try:
        # Имитируем навигацию
        referer_chain = [
            "https://www.google.com/search?q=" + "+".join(urlparse(url).netloc.split('.')[:2]),
            f"https://{urlparse(url).netloc}/",
        ]
        
        for referer in referer_chain:
            try:
                session.get(referer, timeout=5)
                time.sleep(random.uniform(0.5, 1.5))
            except:
                pass
        
        time.sleep(random.uniform(1, 2))
        
        response = session.get(url, timeout=20, allow_redirects=True)
        
        if response.status_code == 200:
            return _extract_content(response, url)
        
        response.raise_for_status()
        return _extract_content(response, url)
        
    finally:
        session.close()


def _try_simple_request(url: str) -> str:
    """Простой запрос как запасной вариант."""
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    
    response = requests.get(url, headers=headers, timeout=15, allow_redirects=True)
    response.raise_for_status()
    return _extract_content(response, url)


def _extract_content(response, url: str) -> str:
    """Извлечение контента из ответа."""
    content_type = response.headers.get("Content-Type", "").lower()
    
    # PDF
    if "pdf" in content_type:
        return extract_text_from_pdf_bytes(response.content)
    
    # HTML
    try:
        # Пробуем разные кодировки
        encodings_to_try = ['utf-8', 'cp1251', 'koi8-r', 'iso-8859-1']
        
        for encoding in encodings_to_try:
            try:
                soup = BeautifulSoup(response.content.decode(encoding, errors='ignore'), 'html.parser')
                break
            except:
                continue
        else:
            soup = BeautifulSoup(response.content, 'html.parser')
        
        # Удаляем ненужные элементы
        for element in soup(["script", "style", "noscript", "iframe", "svg", "meta", "link"]):
            element.decompose()
        
        # Ищем основной контент
        content_selectors = [
            # Вакансии
            '[class*="vacancy"]', '[class*="job"]', '[class*="description"]',
            'article', 'main', '.content', '.post-content',
            '[class*="content"]', '[class*="text"]', '[class*="body"]',
            # Общие
            '#content', '.main-content', '.page-content',
            '.entry-content', '.post-body', '.article-body'
        ]
        
        content_element = None
        for selector in content_selectors:
            try:
                element = soup.select_one(selector)
                if element and len(element.get_text(strip=True)) > 200:
                    content_element = element
                    break
            except:
                continue
        
        if not content_element:
            content_element = soup.find('body') or soup
        
        # Получаем текст
        text = content_element.get_text(separator='\n', strip=True)
        
        # Очистка
        text = re.sub(r'\n\s*\n\s*\n+', '\n\n', text)
        text = re.sub(r'[ \t]{2,}', ' ', text)
        
        # Удаляем короткие строки если их много
        lines = [line.strip() for line in text.split('\n') if line.strip()]
        if len(lines) > 30:
            lines = [line for line in lines if len(line) > 20]
        
        text = '\n'.join(lines)
        
        if len(text) < 100:
            logger.warning(f"Extracted text too short from {url}: {len(text)} chars")
        
        return clean_text(text)
        
    except Exception as e:
        logger.error(f"Error extracting content from {url}: {e}")
        raise


def extract_text_from_url(url: str) -> str:
    """
    Основная функция для извлечения текста из URL.
    Пробует несколько стратегий обхода блокировок.
    """
    logger.info(f"Attempting to parse URL: {url}")
    
    # Нормализуем URL
    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url
    
    try:
        return extract_text_with_proxies(url)
    except Exception as e:
        logger.warning(f"All parsing strategies failed for {url}: {e}")
        raise RuntimeError("UNABLE_TO_PARSE")


def prepare_input_text(raw: str) -> str:
    """
    Универсальная функция:
    - если строка целиком — ссылка, скачиваем и чистим,
    - если текст — просто чистим.
    
    В случае ошибок парсинга - возвращаем исходный текст (ссылку),
    чтобы GPT сам мог попробовать понять, что это за вакансия.
    """
    if not raw:
        return ""
    
    raw = raw.strip()
    
    # Если это явно не ссылка, просто чистим текст
    if not is_url(raw):
        return clean_text(raw)
    
    # Если это ссылка - пробуем парсить
    logger.info(f"Detected URL message: {raw}")
    
    try:
        text = extract_text_from_url(raw)
        if text and len(text.strip()) > 100:
            return text
        else:
            # Если текст слишком короткий, возможно, парсинг не удался
            logger.warning(f"Parsed text too short, returning original URL")
            return clean_text(raw)  # Возвращаем очищенную ссылку
    except RuntimeError as e:
        if str(e) == "UNABLE_TO_PARSE":
            logger.info(f"Could not parse URL {raw}, returning URL as text")
            # Возвращаем ссылку как текст - GPT сам попробует понять
            return clean_text(f"Ссылка на вакансию: {raw}")
        else:
            raise e
    except Exception as e:
        logger.error(f"Unexpected error parsing URL {raw}: {e}")
        # В случае любой другой ошибки возвращаем ссылку как текст
        return clean_text(f"Ссылка на вакансию: {raw}")


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
- Ссылки на вакансии (я постараюсь их обработать)

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
            # В тихом режиме не показываем ошибки, просто используем исходный текст
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
        # В тихом режиме показываем общее сообщение
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
