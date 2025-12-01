import os
import logging
from typing import Any, Dict, List, cast
import re
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
# the newest OpenAI model is "gpt-4.1-mini" which was released June 2024.
# do not change this unless explicitly requested by the user
openai_client = None


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
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
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

    Если сайт отвечает 403 (Forbidden) — поднимаем специальную ошибка REMOTE_FORBIDDEN,
    чтобы выше по стеку показать пользователю понятное сообщение.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }

    try:
        resp = requests.get(url, headers=headers, timeout=10)
        logger.info(f"Fetched URL {url} with status {resp.status_code}")
    except requests.RequestException as e:
        logger.error(f"Network error while fetching {url}: {e}")
        # даём понять наверх, что проблема с сетью/подключением
        raise RuntimeError("NETWORK_ERROR") from e

    # сайт сознательно не даёт читать себя с нашего сервера
    if resp.status_code == 403:
        logger.warning(f"Forbidden (403) while fetching {url}")
        raise RuntimeError("REMOTE_FORBIDDEN")

    # остальные коды ошибок пусть превращаются в обычные HTTPError
    try:
        resp.raise_for_status()
    except requests.HTTPError as e:
        logger.error(f"HTTP error while fetching {url}: {e}")
        raise

    content_type = resp.headers.get("Content-Type", "")
    if "pdf" in content_type.lower():
        return extract_text_from_pdf_bytes(resp.content)

    soup = BeautifulSoup(resp.text, "html.parser")
    body = soup.body or soup
    text = body.get_text(separator="\n")
    return clean_text(text)


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
        return extract_text_from_url(raw)
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
        f"Привет {user.mention_html()}! Я твой помощник в поиске работы и по сопроводительным письмам. "
        f"Отправь резюме или ссылку на него, а я подберу формулировки и соберу письма под нужные вакансии"
    )


async def help_command(update: Update,
                       context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a message when the command /help is issued."""
    message = update.message
    if message is None:
        logger.warning("Received /help update without message")
        return

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
            # наши спец-ошибки из extract_text_from_url
            code = str(e)
            if code == "REMOTE_FORBIDDEN":
                await message.reply_text(
                    "Сайт по этой ссылке не разрешает автоматически считывать содержимое "
                    "с моего сервера (отдаёт 403 Forbidden).\n\n"
                    "Пожалуйста, скопируйте текст вакансии и пришлите его сюда текстом."
                )
                return
            elif code == "NETWORK_ERROR":
                await message.reply_text(
                    "Не удалось подключиться к сайту по ссылке. "
                    "Попробуйте ещё раз позже или пришлите текст вакансии вручную."
                )
                return
            else:
                logger.error(f"Runtime error while processing input text or URL: {e}")
                await message.reply_text(
                    "Не удалось обработать текст или ссылку. Попробуйте другой формат."
                )
                return
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
