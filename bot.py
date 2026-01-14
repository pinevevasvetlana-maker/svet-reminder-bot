import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Optional

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

DB_PATH = os.environ.get("REMINDER_DB", "reminders.db")
DATETIME_FORMAT = "%Y-%m-%d %H:%M"
MOSCOW_TZ = ZoneInfo("Europe/Moscow")
ADMIN_CHAT_ID = int(os.environ.get("ADMIN_CHAT_ID", "0"))


@dataclass
class Reminder:
    reminder_id: int
    creator_chat_id: int
    target_chat_id: int
    remind_at: datetime
    message: str
    repeat_interval_minutes: Optional[int] = None


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS contacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER UNIQUE NOT NULL,
                name TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                creator_chat_id INTEGER NOT NULL,
                target_chat_id INTEGER NOT NULL,
                remind_at TEXT NOT NULL,
                message TEXT NOT NULL,
                repeat_interval_minutes INTEGER
            )
            """
        )
        ensure_column(conn, "reminders", "repeat_interval_minutes", "INTEGER")


def ensure_column(conn: sqlite3.Connection, table: str, column: str, column_type: str) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")


def is_admin(chat_id: int) -> bool:
    return ADMIN_CHAT_ID != 0 and chat_id == ADMIN_CHAT_ID


def upsert_contact(chat_id: int, name: str) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO contacts (chat_id, name)
            VALUES (?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET name = excluded.name
            """,
            (chat_id, name),
        )


def get_contact_by_name(name: str) -> Optional[tuple[int, str]]:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT chat_id, name FROM contacts WHERE lower(name) = lower(?)",
            (name,),
        ).fetchone()
    return row if row else None


def list_contacts() -> list[tuple[str, int]]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT name, chat_id FROM contacts ORDER BY lower(name)"
        ).fetchall()
    return rows


def add_reminder(
    creator_chat_id: int,
    target_chat_id: int,
    remind_at: datetime,
    message: str,
    repeat_interval_minutes: Optional[int],
) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            """
            INSERT INTO reminders (
                creator_chat_id,
                target_chat_id,
                remind_at,
                message,
                repeat_interval_minutes
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                creator_chat_id,
                target_chat_id,
                remind_at.isoformat(),
                message,
                repeat_interval_minutes,
            ),
        )
        return cursor.lastrowid


def update_reminder_time(reminder_id: int, remind_at: datetime) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE reminders SET remind_at = ? WHERE id = ?",
            (remind_at.isoformat(), reminder_id),
        )


def delete_reminder(reminder_id: int) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))


def load_future_reminders(now: datetime) -> list[Reminder]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT id, creator_chat_id, target_chat_id, remind_at, message, repeat_interval_minutes
            FROM reminders
            WHERE remind_at > ?
            ORDER BY remind_at
            """,
            (now.isoformat(),),
        ).fetchall()

    reminders: list[Reminder] = []
    for row in rows:
        remind_at = datetime.fromisoformat(row[3])
        reminders.append(
            Reminder(
                reminder_id=row[0],
                creator_chat_id=row[1],
                target_chat_id=row[2],
                remind_at=remind_at,
                message=row[4],
                repeat_interval_minutes=row[5],
            )
        )
    return reminders


def load_user_reminders(chat_id: int) -> list[Reminder]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT id, creator_chat_id, target_chat_id, remind_at, message, repeat_interval_minutes
            FROM reminders
            WHERE creator_chat_id = ?
            ORDER BY remind_at
            """,
            (chat_id,),
        ).fetchall()

    reminders: list[Reminder] = []
    for row in rows:
        remind_at = datetime.fromisoformat(row[3])
        reminders.append(
            Reminder(
                reminder_id=row[0],
                creator_chat_id=row[1],
                target_chat_id=row[2],
                remind_at=remind_at,
                message=row[4],
                repeat_interval_minutes=row[5],
            )
        )
    return reminders


def load_all_reminders() -> list[Reminder]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT id, creator_chat_id, target_chat_id, remind_at, message, repeat_interval_minutes
            FROM reminders
            ORDER BY remind_at
            """,
        ).fetchall()

    reminders: list[Reminder] = []
    for row in rows:
        remind_at = datetime.fromisoformat(row[3])
        reminders.append(
            Reminder(
                reminder_id=row[0],
                creator_chat_id=row[1],
                target_chat_id=row[2],
                remind_at=remind_at,
                message=row[4],
                repeat_interval_minutes=row[5],
            )
        )
    return reminders


def get_reminder(reminder_id: int) -> Optional[Reminder]:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            """
            SELECT id, creator_chat_id, target_chat_id, remind_at, message, repeat_interval_minutes
            FROM reminders
            WHERE id = ?
            """,
            (reminder_id,),
        ).fetchone()

    if not row:
        return None

    return Reminder(
        reminder_id=row[0],
        creator_chat_id=row[1],
        target_chat_id=row[2],
        remind_at=datetime.fromisoformat(row[3]),
        message=row[4],
        repeat_interval_minutes=row[5],
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message:
        return
    chat_id = update.effective_chat.id
    display_name = update.effective_user.full_name if update.effective_user else str(chat_id)
    upsert_contact(chat_id, display_name)
    await update.message.reply_text(
        "Привет! Я напоминалка.\n"
        "Ты можешь создавать напоминания только себе через /remindme и /repeatme.\n"
        "Администратор управляет контактами и напоминаниями других пользователей.",
    )


async def setname(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message:
        return
    if not is_admin(update.effective_chat.id):
        await update.message.reply_text("Эта команда доступна только администратору.")
        return
    if len(context.args) < 2:
        await update.message.reply_text("Формат: /setname <chat_id> <имя>")
        return
    chat_id = int(context.args[0])
    name = " ".join(context.args[1:]).strip()
    if not name:
        await update.message.reply_text("Имя не может быть пустым.")
        return
    upsert_contact(chat_id, name)
    await update.message.reply_text(f"Сохранил имя: {name} (id: {chat_id})")


async def contacts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_chat:
        return
    if not is_admin(update.effective_chat.id):
        await update.message.reply_text("Эта команда доступна только администратору.")
        return
    rows = list_contacts()
    if not rows:
        await update.message.reply_text("Контактов пока нет. Используй /setname.")
        return
    lines = ["Доступные контакты:"]
    for name, chat_id in rows:
        lines.append(f"• {name} (id: {chat_id})")
    await update.message.reply_text("\n".join(lines))


def parse_datetime(date_part: str, time_part: str) -> Optional[datetime]:
    try:
        local_dt = datetime.strptime(f"{date_part} {time_part}", DATETIME_FORMAT)
        return local_dt.replace(tzinfo=MOSCOW_TZ).astimezone(timezone.utc)
    except ValueError:
        return None


def validate_future(remind_at: datetime) -> bool:
    return remind_at > datetime.now(timezone.utc)


def ensure_message(message: str) -> Optional[str]:
    cleaned = message.strip()
    return cleaned if cleaned else None


def schedule_reminder_job(application: Application, reminder: Reminder) -> None:
    delay = (reminder.remind_at - datetime.now(timezone.utc)).total_seconds()
    if delay <= 0:
        return
    application.job_queue.run_once(
        send_reminder,
        when=delay,
        data=reminder,
        name=f"reminder-{reminder.reminder_id}",
    )


def build_reminder_line(reminder: Reminder) -> str:
    repeat = (
        f" (повтор каждые {reminder.repeat_interval_minutes} мин.)"
        if reminder.repeat_interval_minutes
        else ""
    )
    local_time = reminder.remind_at.astimezone(MOSCOW_TZ)
    return (
        f"#{reminder.reminder_id} на {local_time.strftime(DATETIME_FORMAT)} МСК"
        f" — {reminder.message}{repeat}"
    )


def create_reminder(
    creator_chat_id: int,
    target_chat_id: int,
    remind_at: datetime,
    message: str,
    repeat_interval_minutes: Optional[int],
    application: Application,
) -> int:
    reminder_id = add_reminder(
        creator_chat_id=creator_chat_id,
        target_chat_id=target_chat_id,
        remind_at=remind_at,
        message=message,
        repeat_interval_minutes=repeat_interval_minutes,
    )

    schedule_reminder_job(
        application,
        Reminder(
            reminder_id=reminder_id,
            creator_chat_id=creator_chat_id,
            target_chat_id=target_chat_id,
            remind_at=remind_at,
            message=message,
            repeat_interval_minutes=repeat_interval_minutes,
        ),
    )
    return reminder_id


async def remindme(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message:
        return
    if len(context.args) < 3:
        await update.message.reply_text(
            "Формат: /remindme <YYYY-MM-DD> <HH:MM> <текст>"
        )
        return

    date_part = context.args[0]
    time_part = context.args[1]
    message = ensure_message(" ".join(context.args[2:]))
    if not message:
        await update.message.reply_text("Текст напоминания не может быть пустым.")
        return

    remind_at = parse_datetime(date_part, time_part)
    if not remind_at:
        await update.message.reply_text(
            "Не могу разобрать дату. Используй формат YYYY-MM-DD HH:MM (МСК)."
        )
        return

    if not validate_future(remind_at):
        await update.message.reply_text("Дата должна быть в будущем.")
        return

    create_reminder(
        creator_chat_id=update.effective_chat.id,
        target_chat_id=update.effective_chat.id,
        remind_at=remind_at,
        message=message,
        repeat_interval_minutes=None,
        application=context.application,
    )

    await update.message.reply_text(
        f"Напоминание запланировано на {remind_at.astimezone(MOSCOW_TZ).strftime(DATETIME_FORMAT)} МСК."
    )


async def repeatme(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message:
        return
    if len(context.args) < 4:
        await update.message.reply_text(
            "Формат: /repeatme <YYYY-MM-DD> <HH:MM> <интервал_мин> <текст>"
        )
        return

    date_part = context.args[0]
    time_part = context.args[1]
    try:
        interval_minutes = int(context.args[2])
    except ValueError:
        await update.message.reply_text("Интервал должен быть числом (в минутах).")
        return
    if interval_minutes <= 0:
        await update.message.reply_text("Интервал должен быть больше нуля.")
        return

    message = ensure_message(" ".join(context.args[3:]))
    if not message:
        await update.message.reply_text("Текст напоминания не может быть пустым.")
        return

    remind_at = parse_datetime(date_part, time_part)
    if not remind_at:
        await update.message.reply_text(
            "Не могу разобрать дату. Используй формат YYYY-MM-DD HH:MM (МСК)."
        )
        return

    if not validate_future(remind_at):
        await update.message.reply_text("Дата должна быть в будущем.")
        return

    create_reminder(
        creator_chat_id=update.effective_chat.id,
        target_chat_id=update.effective_chat.id,
        remind_at=remind_at,
        message=message,
        repeat_interval_minutes=interval_minutes,
        application=context.application,
    )

    await update.message.reply_text(
        "Повторяющееся напоминание создано. "
        f"Старт: {remind_at.astimezone(MOSCOW_TZ).strftime(DATETIME_FORMAT)} МСК, "
        f"интервал: {interval_minutes} мин."
    )


async def my(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message:
        return
    reminders = load_user_reminders(update.effective_chat.id)
    if not reminders:
        await update.message.reply_text("У тебя нет активных напоминаний.")
        return
    lines = ["Твои напоминания:"]
    for reminder in reminders:
        lines.append(build_reminder_line(reminder))
    await update.message.reply_text("\n".join(lines))


async def cancelme(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message:
        return
    if not context.args:
        await update.message.reply_text("Формат: /cancelme <id>")
        return
    try:
        reminder_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("ID должен быть числом.")
        return

    reminder = get_reminder(reminder_id)
    if not reminder or reminder.creator_chat_id != update.effective_chat.id:
        await update.message.reply_text("Напоминание не найдено.")
        return

    delete_reminder(reminder_id)
    await update.message.reply_text(f"Напоминание #{reminder_id} отменено.")


async def remind(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message:
        return
    if not is_admin(update.effective_chat.id):
        await update.message.reply_text("Эта команда доступна только администратору.")
        return
    if len(context.args) < 4:
        await update.message.reply_text(
            "Формат: /remind <имя> <YYYY-MM-DD> <HH:MM> <текст>\n"
            "Пример: /remind Иван 2024-12-31 09:00 Позвонить"
        )
        return

    name = context.args[0]
    date_part = context.args[1]
    time_part = context.args[2]
    message = ensure_message(" ".join(context.args[3:]))
    if not message:
        await update.message.reply_text("Текст напоминания не может быть пустым.")
        return

    contact = get_contact_by_name(name)
    if not contact:
        await update.message.reply_text(
            "Контакт не найден. Проверь /contacts или попроси человека сделать /start."
        )
        return

    remind_at = parse_datetime(date_part, time_part)
    if not remind_at:
        await update.message.reply_text(
            "Не могу разобрать дату. Используй формат YYYY-MM-DD HH:MM (МСК)."
        )
        return

    if not validate_future(remind_at):
        await update.message.reply_text("Дата должна быть в будущем.")
        return

    create_reminder(
        creator_chat_id=update.effective_chat.id,
        target_chat_id=contact[0],
        remind_at=remind_at,
        message=message,
        repeat_interval_minutes=None,
        application=context.application,
    )

    await update.message.reply_text(
        f"Напоминание для {contact[1]} запланировано на "
        f"{remind_at.astimezone(MOSCOW_TZ).strftime(DATETIME_FORMAT)} МСК."
    )


async def repeat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message:
        return
    if not is_admin(update.effective_chat.id):
        await update.message.reply_text("Эта команда доступна только администратору.")
        return
    if len(context.args) < 5:
        await update.message.reply_text(
            "Формат: /repeat <имя> <YYYY-MM-DD> <HH:MM> <интервал_мин> <текст>"
        )
        return

    name = context.args[0]
    date_part = context.args[1]
    time_part = context.args[2]
    try:
        interval_minutes = int(context.args[3])
    except ValueError:
        await update.message.reply_text("Интервал должен быть числом (в минутах).")
        return
    if interval_minutes <= 0:
        await update.message.reply_text("Интервал должен быть больше нуля.")
        return

    message = ensure_message(" ".join(context.args[4:]))
    if not message:
        await update.message.reply_text("Текст напоминания не может быть пустым.")
        return

    contact = get_contact_by_name(name)
    if not contact:
        await update.message.reply_text("Контакт не найден. Проверь /contacts.")
        return

    remind_at = parse_datetime(date_part, time_part)
    if not remind_at:
        await update.message.reply_text(
            "Не могу разобрать дату. Используй формат YYYY-MM-DD HH:MM (МСК)."
        )
        return

    if not validate_future(remind_at):
        await update.message.reply_text("Дата должна быть в будущем.")
        return

    create_reminder(
        creator_chat_id=update.effective_chat.id,
        target_chat_id=contact[0],
        remind_at=remind_at,
        message=message,
        repeat_interval_minutes=interval_minutes,
        application=context.application,
    )

    await update.message.reply_text(
        "Повторяющееся напоминание создано для "
        f"{contact[1]} с интервалом {interval_minutes} мин."
    )


async def list_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message:
        return
    if not is_admin(update.effective_chat.id):
        await update.message.reply_text("Эта команда доступна только администратору.")
        return
    reminders = load_all_reminders()
    if not reminders:
        await update.message.reply_text("Напоминаний нет.")
        return
    lines = ["Все напоминания:"]
    for reminder in reminders:
        lines.append(
            f"{build_reminder_line(reminder)} (creator: {reminder.creator_chat_id}, "
            f"target: {reminder.target_chat_id})"
        )
    await update.message.reply_text("\n".join(lines))


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message:
        return
    if not is_admin(update.effective_chat.id):
        await update.message.reply_text("Эта команда доступна только администратору.")
        return
    if not context.args:
        await update.message.reply_text("Формат: /cancel <id>")
        return
    try:
        reminder_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("ID должен быть числом.")
        return

    reminder = get_reminder(reminder_id)
    if not reminder:
        await update.message.reply_text("Напоминание не найдено.")
        return

    delete_reminder(reminder_id)
    await update.message.reply_text(f"Напоминание #{reminder_id} отменено.")


async def send_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    reminder: Reminder = context.job.data
    await context.bot.send_message(
        chat_id=reminder.target_chat_id,
        text=f"Напоминание: {reminder.message}",
    )

    if reminder.repeat_interval_minutes:
        next_time = reminder.remind_at + timedelta(minutes=reminder.repeat_interval_minutes)
        update_reminder_time(reminder.reminder_id, next_time)
        schedule_reminder_job(
            context.application,
            Reminder(
                reminder_id=reminder.reminder_id,
                creator_chat_id=reminder.creator_chat_id,
                target_chat_id=reminder.target_chat_id,
                remind_at=next_time,
                message=reminder.message,
                repeat_interval_minutes=reminder.repeat_interval_minutes,
            ),
        )
        return

    delete_reminder(reminder.reminder_id)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text(
        (
            "Команды для всех пользователей:\n"
            "/remindme <YYYY-MM-DD> <HH:MM> <текст>\n"
            "/repeatme <YYYY-MM-DD> <HH:MM> <интервал_мин> <текст>\n"
            "/my — список своих напоминаний\n"
            "/cancelme <id> — отменить своё напоминание\n\n"
            "Команды администратора:\n"
            "/contacts — список контактов\n"
            "/setname <chat_id> <имя> — задать имя контакта\n"
            "/remind <имя> <YYYY-MM-DD> <HH:MM> <текст>\n"
            "/repeat <имя> <YYYY-MM-DD> <HH:MM> <интервал_мин> <текст>\n"
            "/list — список всех напоминаний\n"
            "/cancel <id> — отменить напоминание\n\n"
            "Время указывается в МСК."
        ),
        parse_mode=ParseMode.HTML,
    )


async def on_startup(application: Application) -> None:
    now = datetime.now(timezone.utc)
    for reminder in load_future_reminders(now):
        schedule_reminder_job(application, reminder)


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

    logging.basicConfig(level=logging.INFO)
    init_db()

    application = Application.builder().token(token).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("setname", setname))
    application.add_handler(CommandHandler("contacts", contacts))
    application.add_handler(CommandHandler("remindme", remindme))
    application.add_handler(CommandHandler("repeatme", repeatme))
    application.add_handler(CommandHandler("my", my))
    application.add_handler(CommandHandler("cancelme", cancelme))
    application.add_handler(CommandHandler("remind", remind))
    application.add_handler(CommandHandler("repeat", repeat))
    application.add_handler(CommandHandler("list", list_reminders))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(CommandHandler("help", help_command))

    application.post_init = on_startup

    application.run_polling()


if __name__ == "__main__":
    main()
