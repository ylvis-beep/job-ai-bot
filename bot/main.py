# main.py
import logging

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
    Основная функция обработки сообщений.
    Логика:
    - PDF → локальный парсер
    - Ссылка → парсим через RU-прокси (Bright Data и т.п.)
    - Текст → используем как есть
    """
    message = update.message
    if not message:
        return

    user_data = context.user_data

    try:
        text_content = ""

        # 1. ОПРЕДЕЛЯЕМ ТИП СООБЩЕНИЯ
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

            if looks_like_url(input_text):
                # 🔗 ССЫЛКА - парсим через RU-прокси
                url = normalize_url(input_text)
                logger.info(f"🔗 Обработка ссылки: {input_text} -> {url}")

                await message.chat.send_action(action="typing")

                text_content = fetch_url_text_via_proxy(url)

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
            await message.reply_text(
                "📝 Сначала отправь свое <b>резюме</b> командой /update_resume,\n"
                "а потом - вакансию для анализа.",
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
            "Разработчики уже уведомлены. Попробуйте позже."
        )


def main() -> None:
    """Запуск бота"""
    if not TELEGRAM_BOT_TOKEN:
        print("❌ ОШИБКА: TELEGRAM_BOT_TOKEN не найден!")
        print("Передайте TELEGRAM_BOT_TOKEN как переменную окружения в Render.")
        return

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("update_resume", update_resume))

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
