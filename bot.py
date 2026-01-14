import asyncio
import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
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


@dataclass
class Reminder:
    reminder_id: int
    creator_chat_id: int
    target_chat_id: int
    remind_at: datetime
    message: str


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
                message TEXT NOT NULL
            )
            """
        )


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


def add_reminder(creator_chat_id: int, target_chat_id: int, remind_at: datetime, message: str) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            """
            INSERT INTO reminders (creator_chat_id, target_chat_id, remind_at, message)
            VALUES (?, ?, ?, ?)
            """,
            (creator_chat_id, target_chat_id, remind_at.isoformat(), message),
        )
        return cursor.lastrowid


def delete_reminder(reminder_id: int) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))


def load_future_reminders(now: datetime) -> list[Reminder]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT id, creator_chat_id, target_chat_id, remind_at, message
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
            )
        )
    return reminders


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message:
        return
    chat_id = update.effective_chat.id
    display_name = update.effective_user.full_name if update.effective_user else str(chat_id)
    upsert_contact(chat_id, display_name)
    await update.message.reply_text(
        "Привет! Я напоминалка.\n"
        "Сначала задай имя через /setname, чтобы другие могли выбрать тебя как контакт.\n"
        "Формат напоминания: /remind <имя> <YYYY-MM-DD> <HH:MM> <текст>\n"
        "Время указывается в UTC. Команда /contacts показывает список контактов.",
    )


async def setname(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message:
        return
    if not context.args:
        await update.message.reply_text("Укажи имя: /setname Иван")
        return
    name = " ".join(context.args).strip()
    if not name:
        await update.message.reply_text("Имя не может быть пустым.")
        return
    upsert_contact(update.effective_chat.id, name)
    await update.message.reply_text(f"Сохранил имя: {name}")


async def contacts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    rows = list_contacts()
    if not rows:
        await update.message.reply_text("Контактов пока нет. Используй /setname.")
        return
    lines = ["Доступные контакты:"]
    for name, chat_id in rows:
        lines.append(f"• {name} (id: {chat_id})")
    await update.message.reply_text("\n".join(lines))


async def remind(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message:
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
    message = " ".join(context.args[3:]).strip()

    contact = get_contact_by_name(name)
    if not contact:
        await update.message.reply_text(
            "Контакт не найден. Проверь /contacts или попроси человека сделать /start и /setname."
        )
        return

    try:
        remind_at = datetime.strptime(f"{date_part} {time_part}", DATETIME_FORMAT).replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        await update.message.reply_text(
            "Не могу разобрать дату. Используй формат YYYY-MM-DD HH:MM (UTC)."
        )
        return

    if remind_at <= datetime.now(timezone.utc):
        await update.message.reply_text("Дата должна быть в будущем.")
        return

    reminder_id = add_reminder(
        creator_chat_id=update.effective_chat.id,
        target_chat_id=contact[0],
        remind_at=remind_at,
        message=message,
    )

    schedule_reminder_job(context.application, Reminder(
        reminder_id=reminder_id,
        creator_chat_id=update.effective_chat.id,
        target_chat_id=contact[0],
        remind_at=remind_at,
        message=message,
    ))

    await update.message.reply_text(
        f"Напоминание для {contact[1]} запланировано на {remind_at.strftime(DATETIME_FORMAT)} UTC."
    )


async def send_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    reminder: Reminder = context.job.data
    await context.bot.send_message(
        chat_id=reminder.target_chat_id,
        text=f"Напоминание: {reminder.message}",
    )
    await context.bot.send_message(
        chat_id=reminder.creator_chat_id,
        text=(
            f"Напоминание отправлено контакту (id: {reminder.target_chat_id})."
        ),
    )
    delete_reminder(reminder.reminder_id)


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


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text(
        (
            "Команды:\n"
            "/start — зарегистрироваться как контакт\n"
            "/setname <имя> — задать отображаемое имя\n"
            "/contacts — список контактов\n"
            "/remind <имя> <YYYY-MM-DD> <HH:MM> <текст> — создать напоминание (UTC)"
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
    application.add_handler(CommandHandler("remind", remind))
    application.add_handler(CommandHandler("help", help_command))

    application.post_init = on_startup

    application.run_polling()


if __name__ == "__main__":
    main()
