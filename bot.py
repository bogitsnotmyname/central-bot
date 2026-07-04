"""
Бот для записи на тренировки
=============================
Клиенты бронируют сеансы на конкретную дату и время из фиксированного
расписания (14:00–22:00, сеанс — 1,5 часа). На один слот может
записаться только один человек или одна пара (максимум 2 человека).
Как только слот забронирован, он сразу становится недоступен для других.

Администратор получает уведомление о каждой записи, видит живое
расписание и может посмотреть полный список записей скрытой командой
/admin.

Запуск:
    pip install -r requirements.txt
    cp .env.example .env   # затем заполните своими значениями
    python bot.py

Полная инструкция — в README.md.
"""

import logging
import os
import sqlite3
from datetime import datetime, timedelta, date as date_cls
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "Europe/Amsterdam"))
REMINDER_MINUTES_BEFORE = int(os.getenv("REMINDER_MINUTES_BEFORE", "60"))
DB_PATH = os.getenv("DB_PATH", "bookings.db")

WORK_START = os.getenv("WORK_START", "14:00")
WORK_END = os.getenv("WORK_END", "22:00")
SESSION_MINUTES = int(os.getenv("SESSION_MINUTES", "90"))
BOOKING_DAYS_AHEAD = int(os.getenv("BOOKING_DAYS_AHEAD", "14"))
SCHEDULE_DAYS_AHEAD = int(os.getenv("SCHEDULE_DAYS_AHEAD", "7"))
DATES_PER_PAGE = 8

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

MONTHS_RU = {
    1: "января", 2: "февраля", 3: "марта", 4: "апреля",
    5: "мая", 6: "июня", 7: "июля", 8: "августа",
    9: "сентября", 10: "октября", 11: "ноября", 12: "декабря",
}
WEEKDAYS_RU = {
    0: "пн", 1: "вт", 2: "ср", 3: "чт", 4: "пт", 5: "сб", 6: "вс",
}

BTN_BOOK = "📅 Записаться"
BTN_SCHEDULE = "📋 Свободные места"
BTN_CANCEL = "❌ Отменить запись"
BTN_ABOUT = "ℹ️ О боте"

MAIN_MENU = ReplyKeyboardMarkup(
    [[BTN_BOOK], [BTN_SCHEDULE], [BTN_CANCEL], [BTN_ABOUT]],
    resize_keyboard=True,
)

WELCOME_TEXT = (
    '👋 Вас приветствует студия шелкографии <a href="https://t.me/centralstudioo">central.studio</a>!\n\n'
    "⏰ Мы работаем с 14:00 до 22:00.\n"
    "Один сеанс длится 1,5 часа.\n"
    "📅 Бронирование доступно на конкретную дату и время.\n\n"
    "👥 Важно: забронировать сеанс может один человек или одна пара.\n"
    "Не более двух человек на сеанс — количество мест строго ограничено.\n\n"
    "Используйте кнопки ниже 👇"
)

ABOUT_TEXT = (
    "🕐 Мы работаем с 14:00 до 22:00.\n"
    "Сеанс — 1,5 часа.\n"
    "Один сеанс — 1 человек или пара.\n"
    "📅 Бронирование на конкретную дату (например, 16 июля).\n"
    "Свободные места отображаются в расписании.\n"
    "Администратор видит все записи с датой и временем мгновенно."
)


def format_date_ru(d: date_cls) -> str:
    return f"{d.day} {MONTHS_RU[d.month]}"


def generate_slot_templates():
    start = datetime.strptime(WORK_START, "%H:%M")
    end = datetime.strptime(WORK_END, "%H:%M")
    slots = []
    cur = start
    while cur + timedelta(minutes=SESSION_MINUTES) <= end:
        slot_end = cur + timedelta(minutes=SESSION_MINUTES)
        slots.append((cur.strftime("%H:%M"), slot_end.strftime("%H:%M")))
        cur = slot_end
    return slots


SLOT_TEMPLATES = generate_slot_templates()  # [("14:00","15:30"), ("15:30","17:00"), ...]


# ---------------------------------------------------------------------------
# База данных
# ---------------------------------------------------------------------------

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            booking_date TEXT NOT NULL,     -- YYYY-MM-DD
            slot_start TEXT NOT NULL,       -- HH:MM
            slot_end TEXT NOT NULL,         -- HH:MM
            user_id INTEGER NOT NULL,
            client_name TEXT NOT NULL,
            contact TEXT,
            phone TEXT,
            type TEXT NOT NULL,             -- 'single' or 'couple'
            reminder_sent INTEGER NOT NULL DEFAULT 0,
            UNIQUE(booking_date, slot_start)
        )
        """
    )
    # миграция: если база создана до появления поля phone — добавляем его
    cols = [row[1] for row in conn.execute("PRAGMA table_info(bookings)").fetchall()]
    if "phone" not in cols:
        conn.execute("ALTER TABLE bookings ADD COLUMN phone TEXT")
    conn.commit()
    conn.close()


async def notify_admin(context: ContextTypes.DEFAULT_TYPE, text: str):
    if ADMIN_ID:
        try:
            await context.bot.send_message(ADMIN_ID, text)
        except Exception as e:
            logger.warning("Не удалось уведомить администратора: %s", e)


def get_contact(user) -> str:
    return f"@{user.username}" if user.username else f"не указан (id {user.id})"


# ---------------------------------------------------------------------------
# Дата: выбор дня для бронирования
# ---------------------------------------------------------------------------

def upcoming_dates_with_free_slots(limit_days: int):
    """Даты в пределах limit_days, у которых есть хотя бы один свободный слот."""
    today = datetime.now(TIMEZONE).date()
    conn = get_conn()
    result = []
    for i in range(limit_days):
        d = today + timedelta(days=i)
        d_str = d.isoformat()
        taken = {
            r["slot_start"]
            for r in conn.execute(
                "SELECT slot_start FROM bookings WHERE booking_date=?", (d_str,)
            ).fetchall()
        }
        now = datetime.now(TIMEZONE)
        free_exists = False
        for start, _ in SLOT_TEMPLATES:
            if start in taken:
                continue
            slot_dt = datetime.strptime(f"{d_str} {start}", "%Y-%m-%d %H:%M").replace(tzinfo=TIMEZONE)
            if slot_dt > now:
                free_exists = True
                break
        if free_exists:
            result.append(d)
    conn.close()
    return result


async def show_date_picker(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0, edit=False):
    dates = upcoming_dates_with_free_slots(BOOKING_DAYS_AHEAD)

    if not dates:
        text = "Свободных дат сейчас нет — загляните позже!"
        if edit:
            await update.callback_query.edit_message_text(text)
        else:
            await update.message.reply_text(text, reply_markup=MAIN_MENU)
        return

    start_i = page * DATES_PER_PAGE
    page_dates = dates[start_i:start_i + DATES_PER_PAGE]

    rows = []
    for i in range(0, len(page_dates), 2):
        row = []
        for d in page_dates[i:i + 2]:
            label = f"{format_date_ru(d)} ({WEEKDAYS_RU[d.weekday()]})"
            row.append(InlineKeyboardButton(label, callback_data=f"date|{d.isoformat()}"))
        rows.append(row)

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"datepage|{page-1}"))
    if start_i + DATES_PER_PAGE < len(dates):
        nav.append(InlineKeyboardButton("➡️ Далее", callback_data=f"datepage|{page+1}"))
    if nav:
        rows.append(nav)

    text = "Пожалуйста, выберите дату для тренировки:"
    markup = InlineKeyboardMarkup(rows)
    if edit:
        await update.callback_query.edit_message_text(text, reply_markup=markup)
    else:
        await update.message.reply_text(text, reply_markup=markup)


async def datepage_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    page = int(query.data.split("|")[1])
    await show_date_picker(update, context, page=page, edit=True)


# ---------------------------------------------------------------------------
# Время: выбор слота на выбранную дату
# ---------------------------------------------------------------------------

async def show_time_picker(update: Update, context: ContextTypes.DEFAULT_TYPE, d_str: str):
    query = update.callback_query
    conn = get_conn()
    rows_db = conn.execute(
        "SELECT * FROM bookings WHERE booking_date=?", (d_str,)
    ).fetchall()
    conn.close()
    taken = {r["slot_start"]: r for r in rows_db}

    now = datetime.now(TIMEZONE)
    buttons = []
    for start, end in SLOT_TEMPLATES:
        slot_dt = datetime.strptime(f"{d_str} {start}", "%Y-%m-%d %H:%M").replace(tzinfo=TIMEZONE)
        if slot_dt <= now:
            continue  # прошедшее время
        if start in taken:
            label = f"🔴 {start}–{end} занято"
            buttons.append([InlineKeyboardButton(label, callback_data="taken")])
        else:
            label = f"🟢 {start}–{end}"
            buttons.append([InlineKeyboardButton(label, callback_data=f"slot|{d_str}|{start}")])

    if not buttons:
        await query.edit_message_text("На эту дату больше нет свободных слотов. Выберите другую дату через «📅 Записаться».")
        return

    d = date_cls.fromisoformat(d_str)
    await query.edit_message_text(
        f"Выберите время на {format_date_ru(d)}:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def date_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    d_str = query.data.split("|", 1)[1]
    await show_time_picker(update, context, d_str)


async def taken_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Этот слот уже занят, выберите другой.", show_alert=True)


# ---------------------------------------------------------------------------
# Тип: один / пара, затем подтверждение записи
# ---------------------------------------------------------------------------

async def slot_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, d_str, start = query.data.split("|")

    buttons = [
        [
            InlineKeyboardButton("👤 Один", callback_data=f"type|single|{d_str}|{start}"),
            InlineKeyboardButton("👥 Пара", callback_data=f"type|couple|{d_str}|{start}"),
        ]
    ]
    d = date_cls.fromisoformat(d_str)
    slot_end = next(end for s, end in SLOT_TEMPLATES if s == start)
    await query.edit_message_text(
        f"Вы выбрали {format_date_ru(d)}, {start}–{slot_end}.\nЭто индивидуальная тренировка или для пары?",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def type_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, kind, d_str, start = query.data.split("|")
    slot_end = next(end for s, end in SLOT_TEMPLATES if s == start)

    conn = get_conn()
    existing = conn.execute(
        "SELECT * FROM bookings WHERE booking_date=? AND slot_start=?", (d_str, start)
    ).fetchone()
    conn.close()
    if existing:
        await query.answer("Извините, этот слот только что заняли.", show_alert=True)
        await show_time_picker(update, context, d_str)
        return

    await query.answer()

    # запоминаем выбор и переходим к сбору имени/телефона
    context.user_data["pending"] = {
        "date": d_str,
        "start": start,
        "end": slot_end,
        "kind": kind,
        "stage": "name",
    }

    d = date_cls.fromisoformat(d_str)
    kind_ru = "один человек" if kind == "single" else "пара (2 человека)"
    await query.edit_message_text(
        f"Вы выбрали {format_date_ru(d)}, {start}–{slot_end} ({kind_ru}).\n\n"
        "Пожалуйста, напишите ваше имя:"
    )


async def handle_pending_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Обрабатывает ввод имени/телефона во время оформления записи.
    Возвращает True, если сообщение было обработано здесь."""
    pending = context.user_data.get("pending")
    if not pending:
        return False

    text = update.message.text.strip()

    # если во время оформления записи нажали кнопку меню — прерываем оформление
    if text in (BTN_BOOK, BTN_SCHEDULE, BTN_CANCEL, BTN_ABOUT):
        context.user_data.pop("pending", None)
        return False

    if pending["stage"] == "name":
        pending["name"] = text
        pending["stage"] = "phone"
        await update.message.reply_text("Спасибо! Теперь, пожалуйста, напишите ваш номер телефона:")
        return True

    # stage == "phone"
    pending["phone"] = text
    d_str, start, slot_end, kind = pending["date"], pending["start"], pending["end"], pending["kind"]
    user = update.effective_user

    conn = get_conn()
    existing = conn.execute(
        "SELECT * FROM bookings WHERE booking_date=? AND slot_start=?", (d_str, start)
    ).fetchone()
    if existing:
        await update.message.reply_text(
            "Извините, этот слот только что заняли. Попробуйте выбрать другой через «📅 Записаться».",
            reply_markup=MAIN_MENU,
        )
        conn.close()
        context.user_data.pop("pending", None)
        return True

    contact = get_contact(user)
    try:
        conn.execute(
            """
            INSERT INTO bookings (booking_date, slot_start, slot_end, user_id, client_name, contact, phone, type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (d_str, start, slot_end, user.id, pending["name"], contact, pending["phone"], kind),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        await update.message.reply_text(
            "Извините, этот слот только что заняли. Попробуйте выбрать другой через «📅 Записаться».",
            reply_markup=MAIN_MENU,
        )
        conn.close()
        context.user_data.pop("pending", None)
        return True
    conn.close()

    d = date_cls.fromisoformat(d_str)
    kind_ru = "один человек" if kind == "single" else "пара (2 человека)"
    await update.message.reply_text(
        f"✅ Вы записаны на {format_date_ru(d)}, {start}–{slot_end}.\n"
        f"Тип: {kind_ru}.\nДо встречи!",
        reply_markup=MAIN_MENU,
    )

    kind_label = "один" if kind == "single" else "пара (2 человека)"
    await notify_admin(
        context,
        "📩 Новая запись!\n"
        f"👤 Имя: {pending['name']}\n"
        f"📞 Телефон: {pending['phone']}\n"
        f"📅 Дата: {format_date_ru(d)}\n"
        f"🕐 Время: {start}–{slot_end}\n"
        f"👥 Тип: {kind_label}\n"
        f"💬 Telegram: {contact}\n"
        "✅ Слот заблокирован для других.",
    )

    context.user_data.pop("pending", None)
    return True


# ---------------------------------------------------------------------------
# Расписание (только просмотр)
# ---------------------------------------------------------------------------

async def show_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = datetime.now(TIMEZONE).date()
    conn = get_conn()

    lines = []
    for i in range(SCHEDULE_DAYS_AHEAD):
        d = today + timedelta(days=i)
        d_str = d.isoformat()
        rows_db = conn.execute(
            "SELECT * FROM bookings WHERE booking_date=?", (d_str,)
        ).fetchall()
        taken = {r["slot_start"]: r for r in rows_db}

        lines.append(f"📅 {format_date_ru(d)} ({WEEKDAYS_RU[d.weekday()]})")
        for start, end in SLOT_TEMPLATES:
            if start in taken:
                r = taken[start]
                kind_ru = "один" if r["type"] == "single" else "пара"
                lines.append(f"🔴 {start}–{end} — {r['client_name']} ({kind_ru})")
            else:
                lines.append(f"🟢 {start}–{end} — свободно")
        lines.append("")  # пустая строка между днями

    conn.close()
    text = "\n".join(lines).strip()
    # Telegram limits messages to 4096 characters; split if needed
    for i in range(0, len(text), 4000):
        await update.message.reply_text(text[i:i + 4000] or "Нет данных.", reply_markup=MAIN_MENU if i + 4000 >= len(text) else None)


# ---------------------------------------------------------------------------
# Мои записи / отмена
# ---------------------------------------------------------------------------

async def mybookings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    today_str = datetime.now(TIMEZONE).date().isoformat()
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM bookings WHERE user_id=? AND booking_date >= ? ORDER BY booking_date, slot_start",
        (user_id, today_str),
    ).fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text(
            "У вас пока нет предстоящих записей. Нажмите «📅 Записаться», чтобы выбрать время.",
            reply_markup=MAIN_MENU,
        )
        return

    for r in rows:
        d = date_cls.fromisoformat(r["booking_date"])
        text = f"🗓 {format_date_ru(d)}, {r['slot_start']}–{r['slot_end']}"
        buttons = [[InlineKeyboardButton("❌ Отменить", callback_data=f"cancelbk|{r['id']}")]]
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons))


async def cancel_booking_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    booking_id = int(query.data.split("|")[1])

    conn = get_conn()
    row = conn.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()

    if not row or row["user_id"] != query.from_user.id:
        await query.edit_message_text("Эту запись нельзя отменить здесь.")
        conn.close()
        return

    conn.execute("DELETE FROM bookings WHERE id=?", (booking_id,))
    conn.commit()
    conn.close()

    d = date_cls.fromisoformat(row["booking_date"])
    text = f"{format_date_ru(d)}, {row['slot_start']}–{row['slot_end']}"
    await query.edit_message_text(f"Запись на {text} отменена.")

    await notify_admin(context, f"⚠️ {query.from_user.full_name} отменил запись на {text}.")


# ---------------------------------------------------------------------------
# Команды администратора
# ---------------------------------------------------------------------------

def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


async def admin_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Скрытая команда /admin — полный список всех предстоящих записей с возможностью отмены."""
    if not is_admin(update.effective_user.id):
        return  # молча игнорируем, чтобы не выдавать существование команды

    today_str = datetime.now(TIMEZONE).date().isoformat()
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM bookings WHERE booking_date >= ? ORDER BY booking_date, slot_start",
        (today_str,),
    ).fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("Пока нет ни одной записи.")
        return

    for r in rows:
        d = date_cls.fromisoformat(r["booking_date"])
        kind_ru = "один" if r["type"] == "single" else "пара"
        text = (
            f"#{r['id']} — {format_date_ru(d)}, {r['slot_start']}–{r['slot_end']}\n"
            f"👤 {r['client_name']} ({kind_ru})\n"
            f"📞 {r['phone'] or '—'}\n"
            f"💬 {r['contact']}"
        )
        buttons = [[InlineKeyboardButton("❌ Отменить эту запись", callback_data=f"admincancel|{r['id']}")]]
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons))


async def admin_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer()
        return

    await query.answer()
    booking_id = int(query.data.split("|")[1])

    conn = get_conn()
    row = conn.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()

    if not row:
        await query.edit_message_text("Эта запись уже была отменена или не найдена.")
        conn.close()
        return

    conn.execute("DELETE FROM bookings WHERE id=?", (booking_id,))
    conn.commit()
    conn.close()

    d = date_cls.fromisoformat(row["booking_date"])
    text = f"{format_date_ru(d)}, {row['slot_start']}–{row['slot_end']}"
    await query.edit_message_text(f"✅ Запись #{booking_id} ({row['client_name']}, {text}) отменена.")

    try:
        await context.bot.send_message(
            row["user_id"],
            f"⚠️ Администратор отменил вашу тренировку {text}. Пожалуйста, выберите другое время через «📅 Записаться».",
        )
    except Exception as e:
        logger.warning("Не удалось уведомить клиента об отмене администратором: %s", e)


# ---------------------------------------------------------------------------
# Главное меню и /start
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME_TEXT, reply_markup=MAIN_MENU, parse_mode="HTML")
    await notify_admin(context, f"👤 {update.effective_user.full_name} открыл бота.")


async def menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await handle_pending_text(update, context):
        return

    text = update.message.text
    if text == BTN_BOOK:
        await show_date_picker(update, context)
    elif text == BTN_SCHEDULE:
        await show_schedule(update, context)
    elif text == BTN_CANCEL:
        await mybookings(update, context)
    elif text == BTN_ABOUT:
        await update.message.reply_text(ABOUT_TEXT, reply_markup=MAIN_MENU)
    else:
        await update.message.reply_text(
            "Не понимаю эту команду. Используйте кнопки меню ниже.", reply_markup=MAIN_MENU
        )


# ---------------------------------------------------------------------------
# Напоминания
# ---------------------------------------------------------------------------

async def send_reminders(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(TIMEZONE)
    window_start = now + timedelta(minutes=REMINDER_MINUTES_BEFORE - 5)
    window_end = now + timedelta(minutes=REMINDER_MINUTES_BEFORE + 5)

    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM bookings WHERE reminder_sent=0 AND booking_date >= ?",
        (now.date().isoformat(),),
    ).fetchall()

    for r in rows:
        slot_dt = datetime.strptime(
            f"{r['booking_date']} {r['slot_start']}", "%Y-%m-%d %H:%M"
        ).replace(tzinfo=TIMEZONE)
        if not (window_start <= slot_dt <= window_end):
            continue
        try:
            await context.bot.send_message(
                r["user_id"],
                f"⏰ Напоминание: ваша тренировка сегодня в {r['slot_start']}!",
            )
            conn.execute("UPDATE bookings SET reminder_sent=1 WHERE id=?", (r["id"],))
            conn.commit()
        except Exception as e:
            logger.warning("Не удалось отправить напоминание %s: %s", r["user_id"], e)

    conn.close()


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN не задан. Скопируйте .env.example в .env и заполните его.")
    if not ADMIN_ID:
        logger.warning("ADMIN_ID не задан — уведомления и /admin будут недоступны.")

    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_list))  # скрытая команда, не в меню
    app.add_handler(CallbackQueryHandler(datepage_callback, pattern=r"^datepage\|"))
    app.add_handler(CallbackQueryHandler(date_callback, pattern=r"^date\|"))
    app.add_handler(CallbackQueryHandler(slot_callback, pattern=r"^slot\|"))
    app.add_handler(CallbackQueryHandler(taken_callback, pattern=r"^taken$"))
    app.add_handler(CallbackQueryHandler(type_callback, pattern=r"^type\|"))
    app.add_handler(CallbackQueryHandler(cancel_booking_callback, pattern=r"^cancelbk\|"))
    app.add_handler(CallbackQueryHandler(admin_cancel_callback, pattern=r"^admincancel\|"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, menu_router))

    app.job_queue.run_repeating(send_reminders, interval=300, first=10)

    logger.info("Бот запускается... Слоты: %s", SLOT_TEMPLATES)
    app.run_polling()


if __name__ == "__main__":
    main()
