# main.py
import logging
import asyncio
from io import BytesIO

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from config import TELEGRAM_BOT_TOKEN
from parsing import (
    extract_text_from_pdf_bytes,
    looks_like_url,
    normalize_url,
    clean_text,
    fetch_url_text_via_proxy,
)
from ai_service import analyze_vacancy

# =========================
# Настройка логирования
# =========================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


# =========================
# TELEGRAM BOT ФУНКЦИИ
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /start: просим резюме (если нет) или вакансию (если резюме уже есть)."""
    user = update.effective_user
    user_data = context.user_data

    # Если резюме уже есть в памяти — сразу просим вакансию
    if 'resume' in user_data and user_data['resume']:
        user_data['awaiting_resume'] = False
        await update.message.reply_html(
            f"👋 Привет, {user.mention_html()}!\n\n"
            f"✅ Я уже помню твоё резюме.\n"
            f"Теперь пришли <b>вакансию</b> (ссылку или текст) — и я составлю сопроводительное письмо.\n\n"
            f"Если хочешь заменить резюме — нажми /update_resume."
        )
        return

    # Иначе — просим резюме
    user_data['awaiting_resume'] = True
    await update.message.reply_html(
        f"👋 Привет, {user.mention_html()}!\n\n"
        f"Я помогу составить идеальное сопроводительное письмо.\n\n"
        f"📝 Пришли <b>резюме</b> одним из способов:\n"
        f"• PDF\n"
        f"• ссылка\n"
        f"• текст\n\n"
        f"После резюме я попрошу <b>текст вакансии</b> (или ссылку) и подготовлю письмо."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /help"""
    help_text = """
📋 <b>Доступные команды:</b>
/start - Начать работу
/help - Помощь
/update_resume - Обновить резюме

📝 <b>Как использовать:</b>
1) Нажми /start и отправь резюме (PDF/ссылка/текст)
2) Потом отправь вакансию (ссылка или текст)
3) Я составлю сопроводительное письмо
"""
    await update.message.reply_text(help_text, parse_mode='HTML')


async def update_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обновление резюме"""
    context.user_data['awaiting_resume'] = True
    await update.message.reply_text(
        "📝 Отправь новое резюме одним из способов:\n\n"
        "• PDF файл\n"
        "• Текст резюме\n"
        "• Ссылка на резюме\n\n"
        "Я сохраню его и дальше буду использовать для анализа вакансий."
    )


async def process_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Основная функция обработки сообщений.
    Логика:
    - PDF → локальный парсер
    - Ссылка → парсим через RU-прокси
    - Текст → используем как есть

    ⚠️ Важно: при включенной параллельной обработке апдейтов мы
    делаем лок "по пользователю", чтобы сообщения одного пользователя
    обрабатывались строго последовательно.
    """
    message = update.message
    if not message:
        return

    user_data = context.user_data
    user_id = message.from_user.id

    # ✅ ЛОК НА ПОЛЬЗОВАТЕЛЯ: последовательно внутри user_id, параллельно между разными user_id
    locks = context.application.bot_data.setdefault("user_locks", {})
    lock = locks.setdefault(user_id, asyncio.Lock())

    async with lock:
        try:
            text_content = ""

            # 1) ОПРЕДЕЛЯЕМ ТИП СООБЩЕНИЯ
            if message.document and message.document.mime_type == "application/pdf":
                # 📄 PDF
                logger.info(f"📄 Обработка PDF от пользователя {user_id}")

                file = await message.document.get_file()
                bio = BytesIO()
                await file.download_to_memory(out=bio)

                text_content = extract_text_from_pdf_bytes(bio.getvalue())
                logger.info(f"✅ PDF обработан: {len(text_content)} символов")

            elif message.text:
                input_text = message.text.strip()

                if looks_like_url(input_text):
                    # 🔗 ССЫЛКА
                    url = normalize_url(input_text)
                    logger.info(f"🔗 Обработка ссылки: {input_text} -> {url}")

                    await message.chat.send_action(action="typing")

                    # ✅ чтобы не блокировать обработку других апдейтов
                    text_content = await asyncio.to_thread(fetch_url_text_via_proxy, url)

                else:
                    # 📝 ТЕКСТ
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

            # 2) ПРОВЕРЯЕМ КОНТЕКСТ (резюме или вакансия)
            if user_data.get('awaiting_resume'):
                # 📋 СОХРАНЯЕМ РЕЗЮМЕ В ПАМЯТИ ЧАТА
                user_data['resume'] = text_content
                user_data['awaiting_resume'] = False

                await message.reply_text(
                    f"✅ <b>Резюме сохранено!</b>\n\n"
                    f"📊 Получено: {len(text_content)} символов\n\n"
                    f"Теперь отправь <b>вакансию</b> (ссылку или текст),\n"
                    f"и я составлю сопроводительное письмо!",
                    parse_mode='HTML'
                )
                return

            elif 'resume' in user_data:
                # 🎯 АНАЛИЗИРУЕМ ВАКАНСИЮ
                await analyze_vacancy(message, user_data['resume'], text_content)

            else:
                await message.reply_text(
                    "📝 Сначала отправь <b>резюме</b> командой /start или /update_resume,\n"
                    "а потом — вакансию для анализа.",
                    parse_mode='HTML'
                )

        except ValueError as e:
            await message.reply_text(f"⚠️ {str(e)}")

        except Exception as e:
            logger.error(f"❌ Критическая ошибка: {str(e)}", exc_info=True)
            await message.reply_text(
                "❌ Произошла непредвиденная ошибка.\n"
                "Попробуйте еще раз или свяжитесь с поддержкой."
            )


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка ошибок"""
    logger.error(f"Ошибка в боте: {context.error}", exc_info=True)

    if update and update.message:
        await update.message.reply_text(
            "⚠️ Произошла техническая ошибка.\n"
            "Попробуйте позже."
        )


def main() -> None:
    """Запуск бота"""
    if not TELEGRAM_BOT_TOKEN:
        print("❌ ОШИБКА: TELEGRAM_BOT_TOKEN не найден!")
        print("Передайте TELEGRAM_BOT_TOKEN как переменную окружения в Render.")
        return

    # ✅ Включаем параллельную обработку апдейтов
    # True = дефолтный лимит (обычно до 256 конкурентных апдейтов)
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).concurrent_updates(True).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("update_resume", update_resume))

    # ✅ Только текст и PDF
    app.add_handler(MessageHandler(
        filters.TEXT | filters.Document.PDF,
        process_message
    ))

    app.add_error_handler(error_handler)

    logger.info("🤖 Бот запускается...")
    print("=" * 50)
    print("✅ Бот успешно запущен!")
    print("Отправьте /start в Telegram для начала работы")
    print("=" * 50)

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
