# main.py
import logging
import asyncio
import os
from io import BytesIO
from datetime import datetime, timedelta, timezone, time as dtime
from zoneinfo import ZoneInfo

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
# ADMIN / STATS SETTINGS (из окружения)
# =========================
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))  # ты уже добавил(а) в окружение
STATS_TZ = ZoneInfo("Europe/Helsinki")
STATS_DAILY_TIME = dtime(hour=9, minute=0, tzinfo=STATS_TZ)  # каждый день в 09:00 (Хельсинки)


# =========================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ СТАТИСТИКИ (без БД, в памяти)
# =========================
def _ensure_stats_structures(context: ContextTypes.DEFAULT_TYPE) -> None:
    bd = context.application.bot_data
    bd.setdefault("users", {})  # user_id -> {"last_seen": iso, "username": str, "first_seen": iso}
    bd.setdefault("counters", {
        "messages": 0,
        "resumes_saved": 0,
        "vacancies_processed": 0,
        "errors": 0,
        "new_users": 0,
    })


def touch_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отмечаем пользователя и увеличиваем общий счётчик сообщений."""
    _ensure_stats_structures(context)

    user = update.effective_user
    if not user:
        return

    users = context.application.bot_data["users"]
    counters = context.application.bot_data["counters"]

    now = datetime.now(timezone.utc).isoformat()
    is_new = user.id not in users

    users[user.id] = {
        "last_seen": now,
        "first_seen": users.get(user.id, {}).get("first_seen", now),
        "username": user.username or "",
    }

    counters["messages"] += 1
    if is_new:
        counters["new_users"] += 1


def inc_counter(context: ContextTypes.DEFAULT_TYPE, key: str, amount: int = 1) -> None:
    _ensure_stats_structures(context)
    context.application.bot_data["counters"][key] += amount


def build_stats_text(context: ContextTypes.DEFAULT_TYPE) -> str:
    _ensure_stats_structures(context)

    users = context.application.bot_data["users"]
    counters = context.application.bot_data["counters"]

    now = datetime.now(timezone.utc)
    day_ago = now - timedelta(hours=24)

    active_24h = 0
    for info in users.values():
        try:
            last_seen = datetime.fromisoformat(info["last_seen"])
            if last_seen >= day_ago:
                active_24h += 1
        except Exception:
            continue

    total_users = len(users)

    return (
        "📊 Статистика бота (в памяти, с момента последнего запуска)\n\n"
        f"• Всего уникальных пользователей: {total_users}\n"
        f"• Активных за 24 часа: {active_24h}\n"
        f"• Новых пользователей за запуск: {counters.get('new_users', 0)}\n\n"
        f"• Сообщений обработано: {counters.get('messages', 0)}\n"
        f"• Резюме сохранено: {counters.get('resumes_saved', 0)}\n"
        f"• Вакансий обработано: {counters.get('vacancies_processed', 0)}\n"
        f"• Ошибок: {counters.get('errors', 0)}\n\n"
        f"🕒 Отчёт: {datetime.now(STATS_TZ).strftime('%Y-%m-%d %H:%M')} ({STATS_TZ.key})"
    )


async def send_daily_stats(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ежедневная отправка статистики админу (без команды в Telegram)."""
    if ADMIN_ID <= 0:
        return

    text = build_stats_text(context)
    try:
        await context.bot.send_message(chat_id=ADMIN_ID, text=text)
    except Exception as e:
        # Не делаем raise, чтобы job не падал постоянно
        logger.error(f"Не удалось отправить ежедневную статистику админу: {e}", exc_info=True)


# =========================
# TELEGRAM BOT ФУНКЦИИ
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /start: просим резюме (если нет) или вакансию (если резюме уже есть)."""
    touch_user(update, context)

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
    touch_user(update, context)

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
    touch_user(update, context)

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

    При параллельной обработке апдейтов делаем лок "по пользователю",
    чтобы сообщения одного пользователя обрабатывались последовательно.
    """
    touch_user(update, context)

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

                    # ✅ чтобы не блокировать event loop (и других пользователей)
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
                inc_counter(context, "resumes_saved", 1)

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
                inc_counter(context, "vacancies_processed", 1)
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
            inc_counter(context, "errors", 1)
            logger.error(f"❌ Критическая ошибка: {str(e)}", exc_info=True)
            await message.reply_text(
                "❌ Произошла непредвиденная ошибка.\n"
                "Попробуйте еще раз или свяжитесь с поддержкой."
            )


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка ошибок уровня приложения"""
    inc_counter(context, "errors", 1)
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

    # ✅ Ежедневный отчёт админу (если ADMIN_ID задан)
    if ADMIN_ID > 0:
        app.job_queue.run_daily(send_daily_stats, time=STATS_DAILY_TIME)
        logger.info(f"📈 Ежедневная статистика включена: admin={ADMIN_ID}, time={STATS_DAILY_TIME}")

    logger.info("🤖 Бот запускается...")
    print("=" * 50)
    print("✅ Бот успешно запущен!")
    print("Отправьте /start в Telegram для начала работы")
    print("=" * 50)

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
