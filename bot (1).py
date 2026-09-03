import os
import psycopg2
from contextlib import contextmanager
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo

from aiohttp import web
from dotenv import load_dotenv
from telegram import Bot, BotCommand, Update
from telegram.ext import Application, CommandHandler, ContextTypes

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
GROUP_CHAT_ID = int(os.getenv("GROUP_CHAT_ID", "0"))
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

# https://ваш-бот.onrender.com (без / в конце). Обязателен на Render.
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").rstrip("/")
# Любая случайная строка. Рекомендуется, но не обязательна.
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
PORT = int(os.getenv("PORT", "10000"))

KYIV_TZ = ZoneInfo("Europe/Kyiv")
DATABASE_URL = os.getenv("DATABASE_URL", "")

WEEK_PERIODS = [
    ("2026-09-01", "2026-09-04", "Верхній"),
    ("2026-09-07", "2026-09-12", "Нижній"),
    ("2026-09-14", "2026-09-19", "Верхній"),
    ("2026-09-21", "2026-09-26", "Нижній"),
    ("2026-09-28", "2026-10-03", "Верхній"),
    ("2026-10-05", "2026-10-10", "Нижній"),
    ("2026-10-12", "2026-10-17", "Верхній"),
    ("2026-10-19", "2026-10-24", "Нижній"),
    ("2026-10-26", "2026-10-31", "Верхній"),
    ("2026-11-02", "2026-11-07", "Нижній"),
    ("2026-11-09", "2026-11-14", "Верхній"),
    ("2026-11-16", "2026-11-20", "Нижній"),
]

WEEKDAY_NAMES = [
    "Понеділок",
    "Вівторок",
    "Середа",
    "Четвер",
    "П’ятниця",
    "Субота",
    "Неділя",
]

POSSIBLE_SATURDAYS = {
    "2026-09-12": 1,
    "2026-09-19": 2,
    "2026-09-26": 3,
    "2026-10-03": 0,
}


@contextmanager
def get_connection():
    conn = psycopg2.connect(DATABASE_URL)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def execute(conn, query, params=()):
    cur = conn.cursor()
    cur.execute(query, params)
    return cur


def init_db():
    with get_connection() as conn:
        execute(conn, """
            CREATE TABLE IF NOT EXISTS recurring_lessons (
                id SERIAL PRIMARY KEY,
                week_type TEXT NOT NULL,
                weekday INTEGER NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                subject TEXT NOT NULL,
                details TEXT NOT NULL
            )
        """)
        execute(conn, """
            CREATE TABLE IF NOT EXISTS date_changes (
                id SERIAL PRIMARY KEY,
                lesson_date TEXT NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                subject TEXT NOT NULL,
                details TEXT NOT NULL
            )
        """)
        execute(conn, """
            CREATE TABLE IF NOT EXISTS homework (
                id SERIAL PRIMARY KEY,
                subject TEXT NOT NULL,
                task TEXT NOT NULL,
                deadline TEXT NOT NULL,
                is_done INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)
        # Нужна, чтобы не отправлять одно и то же авто-сообщение
        # повторно при каждом внешнем пинге /check-schedule.
        execute(conn, """
            CREATE TABLE IF NOT EXISTS sent_log (
                job_name TEXT PRIMARY KEY,
                last_date TEXT NOT NULL
            )
        """)


def already_sent_today(job_name, today_text):
    with get_connection() as conn:
        row = execute(conn, 
            "SELECT last_date FROM sent_log WHERE job_name = %s", (job_name,)
        ).fetchone()
    return row is not None and row[0] == today_text


def mark_sent(job_name, today_text):
    with get_connection() as conn:
        execute(conn, """
            INSERT INTO sent_log (job_name, last_date) VALUES (%s, %s)
            ON CONFLICT(job_name) DO UPDATE SET last_date = excluded.last_date
        """, (job_name, today_text))


def is_admin(update: Update) -> bool:
    return update.effective_user is not None and update.effective_user.id == ADMIN_ID


def parse_date(date_text):
    return datetime.strptime(date_text, "%d.%m.%Y").date()


def week_type_for_date(target_date):
    for start_text, end_text, week_type in WEEK_PERIODS:
        start_date = datetime.fromisoformat(start_text).date()
        end_date = datetime.fromisoformat(end_text).date()
        if start_date <= target_date <= end_date:
            return week_type
    return "Невідомий"


def possible_study_day_for_date(target_date):
    return POSSIBLE_SATURDAYS.get(target_date.isoformat())


def get_regular_lessons(week_type, weekday):
    with get_connection() as conn:
        return execute(conn, """
            SELECT id, start_time, end_time, subject, details, 'regular'
            FROM recurring_lessons
            WHERE week_type = %s AND weekday = %s
        """, (week_type, weekday)).fetchall()


def get_date_lessons(target_date):
    with get_connection() as conn:
        return execute(conn, """
            SELECT id, start_time, end_time, subject, details, 'date'
            FROM date_changes
            WHERE lesson_date = %s
        """, (target_date.isoformat(),)).fetchall()


def get_lessons_for_date(target_date):
    week_type = week_type_for_date(target_date)
    copied_weekday = possible_study_day_for_date(target_date)
    weekday = copied_weekday if copied_weekday is not None else target_date.weekday()

    recurring = get_regular_lessons(week_type, weekday)
    date_lessons = get_date_lessons(target_date)
    return sorted(recurring + date_lessons, key=lambda lesson: lesson[1])


def copied_day_name(target_date):
    copied_weekday = possible_study_day_for_date(target_date)
    if copied_weekday is None:
        return None
    return WEEKDAY_NAMES[copied_weekday]


def format_lessons(lessons):
    text = ""
    for number, (_, start_time, end_time, subject, details, _) in enumerate(lessons, start=1):
        text += (
            f"{number}. {start_time}–{end_time}\n"
            f"📚 {subject}\n"
            f"📍 {details}\n\n"
        )
    return text.strip()


def format_schedule(target_date):
    week_type = week_type_for_date(target_date)
    lessons = get_lessons_for_date(target_date)
    weekday_name = WEEKDAY_NAMES[target_date.weekday()]
    copied_name = copied_day_name(target_date)

    text = (
        f"📅 Розклад на {target_date.strftime('%d.%m.%Y')} ({weekday_name})\n"
        f"🔄 {week_type} тиждень\n"
    )

    if copied_name:
        text += f"📌 Можливе відпрацювання за {copied_name.lower()}\n"

    text += "\n"

    if target_date.weekday() == 5 and copied_name:
        if lessons:
            return (
                text
                + "⚠️ Уточніть у старости або викладача, чи підтверджені пари.\n\n"
                + format_lessons(lessons)
            )
        return (
            text
            + "⚠️ Це можлива дата відпрацювання. Поки що пар у розкладі немає.\n"
            + "Уточніть у старости або викладача."
        )

    if not lessons:
        return text + "🎉 Пар немає."

    return text + format_lessons(lessons)


def parse_lesson_command(raw_text):
    parts = [part.strip() for part in raw_text.split("|")]
    if len(parts) != 3:
        return None

    timing, subject, details = parts
    timing_parts = timing.split()
    if len(timing_parts) != 2:
        return None

    start_time, end_time = timing_parts
    datetime.strptime(start_time, "%H:%M")
    datetime.strptime(end_time, "%H:%M")
    return start_time, end_time, subject, details


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await help_command(update, context)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📖 Бот-розклад магістратури\n\n"
        "Команди для всіх:\n"
        "/today — розклад на сьогодні\n"
        "/tomorrow — розклад на завтра\n"
        "/date ДД.ММ.РРРР — розклад на обрану дату\n"
        "/week — розклад на найближчі 7 днів\n"
        "/weektype — верхній чи нижній тиждень сьогодні\n"
        "/hw — актуальні домашні завдання\n"
        "/help — ця довідка\n\n"
        "Автоматично в групі:\n"
        "• Пн–Чт о 09:00 — повідомлення лише якщо є пари\n"
        "• Пн–Чт о 20:00 — розклад на завтра лише якщо є пари\n"
        "• Пт о 20:00 — нагадування про вихідні або можливе відпрацювання\n"
        "• Можлива субота о 09:00 — попередження про можливі пари\n"
        "• Нд о 18:00 — розклад на понеділок, якщо є пари\n\n"
        "🌿 Якщо пар немає, автоматичні повідомлення не надсилаються."
    )

    if is_admin(update):
        text += (
            "\n\n🔐 Команди адміністратора:\n"
            "/addregular — додати постійну пару\n"
            "/adddate — додати разову пару на точну дату\n"
            "/listregular — список постійних пар\n"
            "/listdates — список разових пар\n"
            "/delete R1 або /delete D1 — видалити пару\n"
            "/addhw — додати домашнє завдання\n"
            "/donehw НОМЕР — позначити ДЗ виконаним\n"
            "/deletehw НОМЕР — видалити ДЗ\n"
            "/allhw — усі домашні завдання\n"
            "/sendtomorrow — тестово надіслати розклад на завтра\n"
            "/sendmorning — тестово надіслати ранкове повідомлення\n"
            "/myid — показати твій Telegram ID\n\n"
            "Формат постійної пари:\n"
            "/addregular Верхній 2 14:40 16:00 | Предмет | ауд. 101\n\n"
            "Дні: 0 — Пн, 1 — Вт, 2 — Ср, 3 — Чт, 4 — Пт, 5 — Сб, 6 — Нд.\n\n"
            "Формат разової пари:\n"
            "/adddate 15.09.2026 14:40 16:00 | Предмет | ауд. 101\n\n"
            "Формат домашнього завдання:\n"
            "/addhw 15.09.2026 | Предмет | Опис завдання"
        )

    await update.message.reply_text(text)


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Твій Telegram ID:\n{update.effective_user.id}")


async def weektype(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today_date = datetime.now(KYIV_TZ).date()
    await update.message.reply_text(
        f"Сьогодні {today_date.strftime('%d.%m.%Y')} — {week_type_for_date(today_date).lower()} тиждень."
    )


async def add_regular_lesson(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("Лише адміністратор може додавати пари.")
        return

    raw_text = update.message.text.replace("/addregular", "", 1).strip()
    command_parts = raw_text.split(maxsplit=2)

    if len(command_parts) < 3:
        await update.message.reply_text(
            "Формат:\n\n"
            "/addregular Верхній або Нижній НОМЕР_ДНЯ ЧАС_ПОЧАТКУ ЧАС_КІНЦЯ | Предмет | Аудиторія\n\n"
            "Приклад:\n"
            "/addregular Верхній 2 14:40 16:00 | Методологія | ауд. 312"
        )
        return

    week_type = command_parts[0].capitalize()
    if week_type not in ("Верхній", "Нижній"):
        await update.message.reply_text("Напиши тільки: Верхній або Нижній.")
        return

    try:
        weekday = int(command_parts[1])
        if weekday < 0 or weekday > 6:
            raise ValueError
        parsed = parse_lesson_command(command_parts[2])
        if parsed is None:
            raise ValueError
        start_time, end_time, subject, details = parsed
    except ValueError:
        await update.message.reply_text(
            "Не можу прочитати команду.\n\n"
            "Приклад:\n"
            "/addregular Верхній 2 14:40 16:00 | Методологія | ауд. 312"
        )
        return

    with get_connection() as conn:
        cursor = execute(conn, """
            INSERT INTO recurring_lessons (
                week_type, weekday, start_time, end_time, subject, details
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (week_type, weekday, start_time, end_time, subject, details))
        lesson_id = cursor.fetchone()[0]

    await update.message.reply_text(
        "✅ Постійне заняття додано.\n\n"
        f"Номер: R{lesson_id}\n"
        f"Тиждень: {week_type}\n"
        f"День: {WEEKDAY_NAMES[weekday]}\n"
        f"Час: {start_time}–{end_time}\n"
        f"Предмет: {subject}\n"
        f"Де/примітка: {details}"
    )


async def add_date_lesson(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("Лише адміністратор може додавати пари.")
        return

    raw_text = update.message.text.replace("/adddate", "", 1).strip()
    command_parts = raw_text.split(maxsplit=1)

    if len(command_parts) < 2:
        await update.message.reply_text(
            "Формат:\n\n"
            "/adddate ДД.ММ.РРРР ЧАС_ПОЧАТКУ ЧАС_КІНЦЯ | Предмет | Аудиторія\n\n"
            "Приклад:\n"
            "/adddate 15.09.2026 14:40 16:00 | Гостьова лекція | ауд. 101"
        )
        return

    try:
        lesson_date = parse_date(command_parts[0])
        parsed = parse_lesson_command(command_parts[1])
        if parsed is None:
            raise ValueError
        start_time, end_time, subject, details = parsed
    except ValueError:
        await update.message.reply_text(
            "Не можу прочитати команду.\n\n"
            "Приклад:\n"
            "/adddate 15.09.2026 14:40 16:00 | Гостьова лекція | ауд. 101"
        )
        return

    with get_connection() as conn:
        cursor = execute(conn, """
            INSERT INTO date_changes (
                lesson_date, start_time, end_time, subject, details
            )
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
        """, (lesson_date.isoformat(), start_time, end_time, subject, details))
        lesson_id = cursor.fetchone()[0]

    await update.message.reply_text(
        "✅ Разову пару за датою додано.\n\n"
        f"Номер: D{lesson_id}\n"
        f"Дата: {lesson_date.strftime('%d.%m.%Y')}\n"
        f"Тиждень: {week_type_for_date(lesson_date)}\n"
        f"Час: {start_time}–{end_time}\n"
        f"Предмет: {subject}\n"
        f"Де/примітка: {details}"
    )


async def today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(format_schedule(datetime.now(KYIV_TZ).date()))


async def tomorrow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target_date = datetime.now(KYIV_TZ).date() + timedelta(days=1)
    await update.message.reply_text(format_schedule(target_date))


async def show_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Приклад: /date 15.09.2026")
        return

    try:
        target_date = parse_date(context.args[0])
    except ValueError:
        await update.message.reply_text("Формат дати: ДД.ММ.РРРР\nПриклад: /date 15.09.2026")
        return

    await update.message.reply_text(format_schedule(target_date))


async def week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    start_date = datetime.now(KYIV_TZ).date()
    text = "📚 Розклад на найближчі 7 днів\n\n"

    for offset in range(7):
        target_date = start_date + timedelta(days=offset)
        text += format_schedule(target_date) + "\n\n"

    await update.message.reply_text(text.strip())


async def list_regular(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("Лише адміністратор може бачити список для редагування.")
        return

    with get_connection() as conn:
        rows = execute(conn, """
            SELECT id, week_type, weekday, start_time, end_time, subject, details
            FROM recurring_lessons
            ORDER BY week_type, weekday, start_time
        """).fetchall()

    if not rows:
        await update.message.reply_text("Постійних пар поки немає.")
        return

    text = "📋 Постійний розклад:\n\n"
    for lesson_id, week_type, weekday, start_time, end_time, subject, details in rows:
        text += (
            f"R{lesson_id} — {week_type}, {WEEKDAY_NAMES[weekday]}\n"
            f"{start_time}–{end_time} — {subject}\n"
            f"{details}\n\n"
        )

    await update.message.reply_text(text.strip())


async def list_dates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("Лише адміністратор може бачити список для редагування.")
        return

    today_text = datetime.now(KYIV_TZ).date().isoformat()
    with get_connection() as conn:
        rows = execute(conn, """
            SELECT id, lesson_date, start_time, end_time, subject, details
            FROM date_changes
            WHERE lesson_date >= %s
            ORDER BY lesson_date, start_time
        """, (today_text,)).fetchall()

    if not rows:
        await update.message.reply_text("Разових пар за датами поки немає.")
        return

    text = "📋 Разові пари за датами:\n\n"
    for lesson_id, lesson_date, start_time, end_time, subject, details in rows:
        date_value = datetime.fromisoformat(lesson_date).date()
        text += (
            f"D{lesson_id} — {date_value.strftime('%d.%m.%Y')}, {start_time}–{end_time}\n"
            f"{subject}\n{details}\n\n"
        )

    await update.message.reply_text(text.strip())


async def delete_lesson(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("Лише адміністратор може видаляти пари.")
        return

    if not context.args:
        await update.message.reply_text(
            "Приклад:\n/delete R3 — видалити постійну пару\n/delete D2 — видалити разову пару"
        )
        return

    code = context.args[0].upper().strip()
    if len(code) < 2 or code[0] not in ("R", "D") or not code[1:].isdigit():
        await update.message.reply_text("Формат: /delete R3 або /delete D2")
        return

    lesson_id = int(code[1:])
    table_name = "recurring_lessons" if code[0] == "R" else "date_changes"

    with get_connection() as conn:
        cursor = execute(conn, f"DELETE FROM {table_name} WHERE id = %s", (lesson_id,))

    if cursor.rowcount == 0:
        await update.message.reply_text(f"Запис {code} не знайдено.")
        return

    await update.message.reply_text(f"🗑 Запис {code} видалено.")


async def add_homework(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("Лише адміністратор може додавати домашні завдання.")
        return

    raw_text = update.message.text.replace("/addhw", "", 1).strip()
    parts = [part.strip() for part in raw_text.split("|")]

    if len(parts) != 3:
        await update.message.reply_text(
            "Формат:\n\n"
            "/addhw ДД.ММ.РРРР | Предмет | Опис завдання\n\n"
            "Приклад:\n"
            "/addhw 15.09.2026 | Сучасна релігійна філософія | "
            "Прочитати статтю та підготувати 3 тези"
        )
        return

    deadline_text, subject, task = parts

    try:
        deadline = parse_date(deadline_text)
    except ValueError:
        await update.message.reply_text(
            "Неправильна дата. Формат: ДД.ММ.РРРР\n"
            "Приклад: 15.09.2026"
        )
        return

    with get_connection() as conn:
        cursor = execute(conn, """
            INSERT INTO homework (subject, task, deadline, is_done, created_at)
            VALUES (%s, %s, %s, 0, %s)
            RETURNING id
        """, (subject, task, deadline.isoformat(), datetime.now(KYIV_TZ).isoformat()))
        homework_id = cursor.fetchone()[0]

    await update.message.reply_text(
        "✅ Домашнє завдання додано.\n\n"
        f"Номер: {homework_id}\n"
        f"📚 Предмет: {subject}\n"
        f"📝 Завдання: {task}\n"
        f"📅 Дедлайн: {deadline.strftime('%d.%m.%Y')}"
    )


async def show_homework(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today_date = datetime.now(KYIV_TZ).date()

    with get_connection() as conn:
        rows = execute(conn, """
            SELECT id, subject, task, deadline
            FROM homework
            WHERE is_done = 0
            ORDER BY deadline, id
        """).fetchall()

    if not rows:
        await update.message.reply_text("🎉 Активних домашніх завдань немає.")
        return

    text = "📚 Актуальні домашні завдання\n\n"
    for homework_id, subject, task, deadline_text in rows:
        deadline = datetime.fromisoformat(deadline_text).date()
        days_left = (deadline - today_date).days

        if days_left < 0:
            status = f"❗ Прострочено на {-days_left} дн."
        elif days_left == 0:
            status = "🚨 Дедлайн сьогодні"
        elif days_left == 1:
            status = "⚠️ Дедлайн завтра"
        else:
            status = f"⏳ Залишилось: {days_left} дн."

        text += (
            f"#{homework_id} — {subject}\n"
            f"📝 {task}\n"
            f"📅 До {deadline.strftime('%d.%m.%Y')} — {status}\n\n"
        )

    await update.message.reply_text(text.strip())


async def done_homework(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("Лише адміністратор може змінювати домашні завдання.")
        return

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Приклад: /donehw 3")
        return

    homework_id = int(context.args[0])
    with get_connection() as conn:
        cursor = execute(conn, """
            UPDATE homework
            SET is_done = 1
            WHERE id = %s
        """, (homework_id,))

    if cursor.rowcount == 0:
        await update.message.reply_text("Такого завдання не знайдено.")
        return

    await update.message.reply_text(f"✅ Домашнє завдання #{homework_id} позначено виконаним.")


async def delete_homework(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("Лише адміністратор може видаляти домашні завдання.")
        return

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Приклад: /deletehw 3")
        return

    homework_id = int(context.args[0])
    with get_connection() as conn:
        cursor = execute(conn, "DELETE FROM homework WHERE id = %s", (homework_id,))

    if cursor.rowcount == 0:
        await update.message.reply_text("Такого завдання не знайдено.")
        return

    await update.message.reply_text(f"🗑 Домашнє завдання #{homework_id} видалено.")


async def all_homework(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("Лише адміністратор може переглядати повний список.")
        return

    with get_connection() as conn:
        rows = execute(conn, """
            SELECT id, subject, task, deadline, is_done
            FROM homework
            ORDER BY is_done, deadline, id
        """).fetchall()

    if not rows:
        await update.message.reply_text("Домашніх завдань поки немає.")
        return

    text = "📚 Усі домашні завдання\n\n"
    for homework_id, subject, task, deadline_text, is_done in rows:
        mark = "✅ Виконано" if is_done else "⏳ Активне"
        deadline = datetime.fromisoformat(deadline_text).date()
        text += (
            f"#{homework_id} — {mark}\n"
            f"📚 {subject}\n"
            f"📝 {task}\n"
            f"📅 До {deadline.strftime('%d.%m.%Y')}\n\n"
        )

    await update.message.reply_text(text.strip())


async def build_morning_message():
    today_date = datetime.now(KYIV_TZ).date()
    lessons = get_lessons_for_date(today_date)
    week_type = week_type_for_date(today_date)
    copied_name = copied_day_name(today_date)
    count = len(lessons)

    if today_date.weekday() == 5 and copied_name:
        if count == 0:
            return (
                "☀️ Доброго ранку!\n\n"
                f"Сьогодні, {today_date.strftime('%d.%m')}, {week_type.lower()} тиждень.\n"
                f"⚠️ Можливе відпрацювання за {copied_name.lower()}.\n"
                "Пари поки не підтверджені — уточніть у старости або викладача."
            )

        first_lesson_time = lessons[0][1]
        lesson_word = "пара" if count == 1 else "пари"
        return (
            "☀️ Доброго ранку!\n\n"
            f"Сьогодні, {today_date.strftime('%d.%m')}, {week_type.lower()} тиждень.\n"
            f"⚠️ Можливе відпрацювання за {copied_name.lower()}.\n"
            f"За звичайним розкладом: {count} {lesson_word}, перша — о {first_lesson_time}.\n"
            "Будь ласка, уточніть, чи пари підтверджені."
        )

    first_lesson_time = lessons[0][1]
    lesson_word = "пара" if count == 1 else "пари"
    return (
        "☀️ Доброго ранку!\n\n"
        f"Сьогодні, {today_date.strftime('%d.%m')}, {week_type.lower()} тиждень.\n"
        f"У вас {count} {lesson_word}.\n"
        f"Перша пара — о {first_lesson_time}."
    )


# --- Автоматичні розсилки: тепер приймають Bot напряму, а не JobQueue-контекст ---

async def send_morning_count(bot: Bot):
    today_date = datetime.now(KYIV_TZ).date()
    lessons = get_lessons_for_date(today_date)
    if not lessons:
        return
    await bot.send_message(chat_id=GROUP_CHAT_ID, text=await build_morning_message())


async def send_tomorrow_to_group(bot: Bot):
    target_date = datetime.now(KYIV_TZ).date() + timedelta(days=1)
    lessons = get_lessons_for_date(target_date)
    if not lessons:
        return
    text = "🔔 Нагадування на завтра\n\n" + format_schedule(target_date)
    await bot.send_message(chat_id=GROUP_CHAT_ID, text=text)


async def send_saturday_morning(bot: Bot):
    today_date = datetime.now(KYIV_TZ).date()
    if copied_day_name(today_date) is None:
        return
    await bot.send_message(chat_id=GROUP_CHAT_ID, text=await build_morning_message())


async def send_friday_message(bot: Bot):
    tomorrow_date = datetime.now(KYIV_TZ).date() + timedelta(days=1)
    copied_name = copied_day_name(tomorrow_date)

    if copied_name:
        text = (
            "⚠️ Нагадування на суботу\n\n"
            f"Завтра, {tomorrow_date.strftime('%d.%m')}, можливе відпрацювання за "
            f"{copied_name.lower()}.\n\n"
            "Перевірте повідомлення старости або викладачів: пари можуть бути підтверджені окремо."
        )
    else:
        text = (
            "🌿 Початок вихідних!\n\n"
            "У суботу бот не надсилатиме повідомлень.\n"
            "Перевірте домашні завдання та дедлайни на наступний тиждень.\n\n"
            "У неділю о 18:00 надійде розклад на понеділок, якщо є пари."
        )

    await bot.send_message(chat_id=GROUP_CHAT_ID, text=text)


async def send_monday_schedule(bot: Bot):
    today_date = datetime.now(KYIV_TZ).date()
    monday_date = today_date + timedelta(days=1)
    lessons = get_lessons_for_date(monday_date)
    if not lessons:
        return
    text = "🔔 Нагадування на понеділок\n\n" + format_schedule(monday_date)
    await bot.send_message(chat_id=GROUP_CHAT_ID, text=text)


async def send_tomorrow_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("Лише адміністратор може запускати тестову відправку.")
        return

    target_date = datetime.now(KYIV_TZ).date() + timedelta(days=1)
    text = "🔔 Тестове надсилання на завтра\n\n" + format_schedule(target_date)
    await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=text)
    await update.message.reply_text("✅ Розклад на завтра надіслано в групу.")


async def send_morning_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("Лише адміністратор може запускати тестову відправку.")
        return

    message = await build_morning_message()
    test_message = "☀️ Тестове ранкове повідомлення\n\n" + message.replace("☀️ Доброго ранку!\n\n", "")
    await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=test_message)
    await update.message.reply_text("✅ Ранкове повідомлення надіслано в групу.")


# --- Розклад автосповіщень: python-варіант weekday() (Пн=0 ... Нд=6) ---
# job_name, дні тижня коли спрацьовує, час Kyiv, функція
SCHEDULED_JOBS = [
    ("weekday_morning_count", {0, 1, 2, 3}, time(9, 0), send_morning_count),
    ("weekday_tomorrow_schedule", {0, 1, 2, 3}, time(20, 0), send_tomorrow_to_group),
    ("friday_message", {4}, time(20, 0), send_friday_message),
    ("possible_saturday_morning", {5}, time(9, 0), send_saturday_morning),
    ("sunday_monday_schedule", {6}, time(18, 0), send_monday_schedule),
]


async def run_scheduled_jobs(bot: Bot):
    """Викликається зовнішнім cron-пінгом (замінює JobQueue.run_daily)."""
    now = datetime.now(KYIV_TZ)
    today_text = now.date().isoformat()

    for job_name, weekdays, target_time, job_func in SCHEDULED_JOBS:
        if now.weekday() not in weekdays:
            continue
        if now.time() < target_time:
            continue
        if already_sent_today(job_name, today_text):
            continue

        await job_func(bot)
        mark_sent(job_name, today_text)


async def post_init(application: Application):
    init_db()
    await application.bot.set_my_commands([
        BotCommand("today", "розклад на сьогодні"),
        BotCommand("tomorrow", "розклад на завтра"),
        BotCommand("date", "розклад на обрану дату"),
        BotCommand("week", "розклад на найближчі 7 днів"),
        BotCommand("weektype", "верхній або нижній тиждень"),
        BotCommand("hw", "актуальні домашні завдання"),
        BotCommand("help", "довідка про команди"),
    ])


def build_application() -> Application:
    app = Application.builder().token(TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(CommandHandler("weektype", weektype))
    app.add_handler(CommandHandler("addregular", add_regular_lesson))
    app.add_handler(CommandHandler("adddate", add_date_lesson))
    app.add_handler(CommandHandler("today", today))
    app.add_handler(CommandHandler("tomorrow", tomorrow))
    app.add_handler(CommandHandler("date", show_date))
    app.add_handler(CommandHandler("week", week))
    app.add_handler(CommandHandler("listregular", list_regular))
    app.add_handler(CommandHandler("listdates", list_dates))
    app.add_handler(CommandHandler("delete", delete_lesson))
    app.add_handler(CommandHandler("addhw", add_homework))
    app.add_handler(CommandHandler("hw", show_homework))
    app.add_handler(CommandHandler("donehw", done_homework))
    app.add_handler(CommandHandler("deletehw", delete_homework))
    app.add_handler(CommandHandler("allhw", all_homework))
    app.add_handler(CommandHandler("sendtomorrow", send_tomorrow_now))
    app.add_handler(CommandHandler("sendmorning", send_morning_now))

    return app


# --- aiohttp-сервер: приймає webhook від Telegram і зовнішній пінг cron-job.org ---

application: Application = None  # ініціалізується в on_startup


async def telegram_webhook(request: web.Request):
    if WEBHOOK_SECRET:
        header = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if header != WEBHOOK_SECRET:
            return web.Response(status=401, text="unauthorized")

    data = await request.json()
    update = Update.de_json(data, application.bot)
    await application.process_update(update)
    return web.Response(text="ok")


async def check_schedule(request: web.Request):
    await run_scheduled_jobs(application.bot)
    return web.Response(text="ok")


async def health(request: web.Request):
    return web.Response(text="alive")


async def on_startup(web_app: web.Application):
    global application
    application = build_application()
    await application.initialize()
    await post_init(application)  # тут создаётся init_db() — не вызывается сам по себе без run_polling/run_webhook
    await application.start()

    if WEBHOOK_URL:
        await application.bot.set_webhook(
            url=f"{WEBHOOK_URL}/webhook",
            secret_token=WEBHOOK_SECRET or None,
        )
        print(f"Webhook встановлено: {WEBHOOK_URL}/webhook")
    else:
        print("УВАГА: WEBHOOK_URL не задано — webhook не зареєстровано автоматично.")


async def on_cleanup(web_app: web.Application):
    await application.stop()
    await application.shutdown()


def create_web_app() -> web.Application:
    if not TOKEN:
        raise ValueError("Не знайдено BOT_TOKEN.")
    if GROUP_CHAT_ID == 0:
        raise ValueError("Не знайдено GROUP_CHAT_ID.")
    if ADMIN_ID == 0:
        raise ValueError("Не знайдено ADMIN_ID.")

    web_app = web.Application()
    web_app.router.add_post("/webhook", telegram_webhook)
    web_app.router.add_get("/check-schedule", check_schedule)
    web_app.router.add_get("/", health)
    web_app.on_startup.append(on_startup)
    web_app.on_cleanup.append(on_cleanup)
    return web_app


if __name__ == "__main__":
    web.run_app(create_web_app(), port=PORT)
