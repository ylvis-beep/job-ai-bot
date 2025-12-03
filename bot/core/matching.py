import os
import logging
from typing import Any, Dict, List, Optional, Tuple, cast  # <<< ADDED
import re  # <<< ADDED
from urllib.parse import urlparse  # <<< ADDED
import requests  # <<< ADDED
from bs4 import BeautifulSoup  # <<< ADDED
from io import BytesIO  # <<< ADDED
from PyPDF2 import PdfReader  # <<< ADDED

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from openai import OpenAI
from openai.types.chat import ChatCompletionMessageParam  # <<< ADDED

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize OpenAI client
# the newest OpenAI model is "gpt-4.1-mini" which was released June 2024.  # <<< CHANGED
# do not change this unless explicitly requested by the user
openai_client = None

DEFAULT_HEADERS = {  # <<< ADDED
    "User-Agent": (  # <<< ADDED
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "  # <<< ADDED
        "AppleWebKit/537.36 (KHTML, like Gecko) "  # <<< ADDED
        "Chrome/120.0.0.0 Safari/537.36"  # <<< ADDED
    )  # <<< ADDED
}  # <<< ADDED

MIN_MEANINGFUL_TEXT_LENGTH = 400  # <<< ADDED


def load_system_prompt() -> str:  # <<< ADDED
    """Load the system prompt from system_prompt.txt if it exists."""  # <<< ADDED
    try:  # <<< ADDED
        base_dir = os.path.dirname(os.path.abspath(__file__))  # <<< ADDED
        file_path = os.path.join(base_dir, "system_prompt.txt")  # <<< ADDED
        with open(file_path, "r", encoding="utf-8") as f:  # <<< ADDED
            return f.read()  # <<< ADDED
    except FileNotFoundError:  # <<< ADDED
        return "You are a helpful assistant."  # <<< ADDED
    except Exception as e:  # <<< ADDED
        logger.warning(f"Failed to load system_prompt.txt: {e}")  # <<< ADDED
        return "You are a helpful assistant."  # <<< ADDED


# =========================
# Парсинг текста / ссылок / PDF  # <<< ADDED
# =========================

def clean_text(raw: str) -> str:  # <<< ADDED
    """Приводит текст в аккуратный вид."""  # <<< ADDED
    if not raw:  # <<< ADDED
        return ""  # <<< ADDED
    text = raw.replace("\r\n", "\n")  # <<< ADDED
    lines = [line.strip() for line in text.split("\n")]  # <<< ADDED
    text = "\n".join(lines)  # <<< ADDED
    text = re.sub(r"\n{3,}", "\n\n", text)  # <<< ADDED
    return text.strip()  # <<< ADDED


def is_url(text: str) -> bool:  # <<< ADDED
    """Проверяем, похожа ли строка на URL."""  # <<< ADDED
    if not text:  # <<< ADDED
        return False  # <<< ADDED
    text = text.strip()  # <<< ADDED
    try:  # <<< ADDED
        parsed = urlparse(text)  # <<< ADDED
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)  # <<< ADDED
    except ValueError:  # <<< ADDED
        return False  # <<< ADDED


def extract_text_from_pdf_bytes(data: bytes) -> str:  # <<< ADDED
    """Достаём текст из PDF по сырым байтам."""  # <<< ADDED
    try:  # <<< ADDED
        reader = PdfReader(BytesIO(data))  # <<< ADDED
        pages_text: List[str] = []  # <<< ADDED
        for page in reader.pages:  # <<< ADDED
            page_text = page.extract_text() or ""  # <<< ADDED
            pages_text.append(page_text)  # <<< ADDED
        return clean_text("\n\n".join(pages_text))  # <<< ADDED
    except Exception as e:  # <<< ADDED
        logger.error(f"Error while extracting text from PDF bytes: {e}")  # <<< ADDED
        return ""  # <<< ADDED


def _fetch_url_content(url: str) -> Tuple[Optional[str], str, bytes]:  # <<< ADDED
    """Возвращает текст, content-type и байты ответа или (None, "", b"")."""  # <<< ADDED
    try:  # <<< ADDED
        resp = requests.get(url, headers=DEFAULT_HEADERS, timeout=20)  # <<< ADDED
        resp.raise_for_status()  # <<< ADDED
        return resp.text, resp.headers.get("Content-Type", ""), resp.content  # <<< ADDED
    except Exception as exc:  # <<< ADDED
        logger.warning(f"Failed to fetch {url}: {exc}")  # <<< ADDED
        return None, "", b""  # <<< ADDED


def _html_to_text(html: str) -> str:  # <<< ADDED
    soup = BeautifulSoup(html, "html.parser")  # <<< ADDED
    body = soup.body or soup  # <<< ADDED
    text = body.get_text(separator="\n")  # <<< ADDED
    return clean_text(text)  # <<< ADDED


def _is_meaningful(text: str) -> bool:  # <<< ADDED
    return len(text) >= MIN_MEANINGFUL_TEXT_LENGTH  # <<< ADDED


def _jina_reader(url: str) -> str:  # <<< ADDED
    jina_url = f"https://r.jina.ai/{url}"  # <<< ADDED
    html, content_type, content = _fetch_url_content(jina_url)  # <<< ADDED
    if not html:  # <<< ADDED
        return ""  # <<< ADDED
    if "pdf" in content_type.lower():  # <<< ADDED
        return extract_text_from_pdf_bytes(content)  # <<< ADDED
    return clean_text(html)  # <<< ADDED


def extract_text_from_url(url: str) -> str:  # <<< ADDED
    """Скачиваем страницу/файл по ссылке и вытаскиваем текст."""  # <<< ADDED
    html, content_type, content = _fetch_url_content(url)  # <<< ADDED
    parsed = urlparse(url)  # <<< ADDED
    host = parsed.netloc.lower()  # <<< ADDED
    if host.startswith("www."):  # <<< ADDED
        host = host[4:]  # <<< ADDED

    if "pdf" in content_type.lower():  # <<< ADDED
        return extract_text_from_pdf_bytes(content)  # <<< ADDED

    primary_text = _html_to_text(html) if html else ""  # <<< ADDED
    candidates: List[str] = []  # <<< ADDED
    if primary_text:  # <<< ADDED
        candidates.append(primary_text)  # <<< ADDED

    # Tochka защищается от простых запросов, поэтому сразу используем Jina Reader.  # <<< ADDED
    if host == "tochka.com":  # <<< ADDED
        jina_text = _jina_reader(url)  # <<< ADDED
        if jina_text:  # <<< ADDED
            logger.info(
                "Using Jina Reader fallback for tochka.com (len=%s)", len(jina_text)
            )  # <<< ADDED
            candidates.insert(0, jina_text)  # <<< ADDED
    elif not _is_meaningful(primary_text):  # <<< ADDED
        jina_text = _jina_reader(url)  # <<< ADDED
        if jina_text:  # <<< ADDED
            logger.info(
                "Primary fetch too short (len=%s), using Jina Reader", len(primary_text)
            )  # <<< ADDED
            candidates.append(jina_text)  # <<< ADDED

    for text in candidates:  # <<< ADDED
        if _is_meaningful(text):  # <<< ADDED
            return text  # <<< ADDED

    return candidates[0] if candidates else ""  # <<< ADDED


def prepare_input_text(raw: str) -> str:  # <<< ADDED
    """
    Универсальная функция:
    - если ссылка — скачиваем и чистим,
    - если текст — просто чистим.
    """  # <<< ADDED
    if not raw:  # <<< ADDED
        return ""  # <<< ADDED
    raw = raw.strip()  # <<< ADDED
    if is_url(raw):  # <<< ADDED
        return extract_text_from_url(raw)  # <<< ADDED
    return clean_text(raw)  # <<< ADDED


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a message when the command /start is issued."""
    message = update.message  # <<< ADDED
    if message is None:  # <<< ADDED
        logger.warning("Received /start update without message")  # <<< ADDED
        return  # <<< ADDED

    user = update.effective_user  # <<< ADDED
    if user is None:  # <<< ADDED
        await message.reply_text(  # <<< ADDED
            "Привет! Я твой помощник в поиске работы и по сопроводительным письмам. "
            "Отправь резюме или ссылку на него, а я подберу формулировки и соберу письма под нужные вакансии"
        )
        return  # <<< ADDED

    await message.reply_html(  # <<< CHANGED
        f"Привет {user.mention_html()}! Я твой помощник в поиске работы и по сопроводительным письмам. "
        f"Отправь резюме или ссылку на него, а я подберу формулировки и соберу письма под нужные вакансии"
    )


async def help_command(update: Update,
                       context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a message when the command /help is issued."""
    message = update.message  # <<< ADDED
    if message is None:  # <<< ADDED
        logger.warning("Received /help update without message")  # <<< ADDED
        return  # <<< ADDED

    help_text = """
Available commands:
/start - Start the bot
/help - Show this help message.
/update_resume - Загрузите новые файлы с описанием вашего опыта

Чтобы я работал точнее, сначала пришли полное описание своих навыков, опыта и достижений или резюме.
Потом отправляй вакансии, а я буду присылать:

* главные требования для резюме
* таблицу совпадений и процент совпадения
* пункты, которые лучше подсветить при отклике
* готовое сопроводительное письмо

    """
    await message.reply_text(help_text)  # <<< CHANGED


async def update_resume(update: Update,
                        context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /update_resume command."""
    message = update.message  # <<< ADDED
    if message is None:  # <<< ADDED
        logger.warning("Received /update_resume update without message")  # <<< ADDED
        return  # <<< ADDED

    user_data = cast(Dict[str, Any], context.user_data)  # <<< ADDED
    user_data['awaiting_resume'] = True  # <<< CHANGED
    user_data.pop('resume', None)  # <<< CHANGED
    await message.reply_text("Загрузите новые файлы с описанием вашего опыта")  # <<< CHANGED


async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle user messages with OpenAI."""
    message = update.message  # <<< ADDED
    if message is None:  # <<< ADDED
        logger.warning("Received text update without message in chat handler")  # <<< ADDED
        return  # <<< ADDED

    if not openai_client:
        await message.reply_text(
            "Sorry, OpenAI is not configured. Please set the OPENAI_API_KEY environment variable."
        )
        return

    user_data = cast(Dict[str, Any], context.user_data)  # <<< ADDED

    # Определяем, откуда брать текст: PDF или текст  # <<< ADDED
    user_message: str  # <<< ADDED

    if message.document is not None:  # <<< ADDED
        doc = message.document  # <<< ADDED
        is_pdf = (
            doc.mime_type == "application/pdf"
            or (doc.file_name and doc.file_name.lower().endswith(".pdf"))
        )

        if not is_pdf:  # <<< ADDED
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
        except Exception as e:
            logger.error(f"Error while processing input text or URL: {e}")
            await message.reply_text(
                "Не удалось обработать текст или ссылку. Попробуйте другой формат."
            )
            return

    # Если ожидаем новое резюме после /update_resume — сохраняем его и не вызываем OpenAI
    if user_data.get("awaiting_resume"):
        user_data["resume"] = user_message
        user_data["awaiting_resume"] = False
        await message.reply_text(
            "Спасибо! Я обновил информацию о вашем опыте. Теперь отправьте вакансию или вопрос, "
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

        # >>> ВОТ ЗДЕСЬ БЫЛА ОШИБКА ТИПОВ <<<
        if history:
            messages.extend(history[-max_history_messages:])  # type: ignore[arg-type]  # <<< CHANGED

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
            "ERROR: Please set your TELEGRAM_BOT_TOKEN environment variable.")
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

