#!/usr/bin/env python3
"""
Telegram Timer Bot - บอทจับเวลา ไปห้องน้ำ / ตลาด / สูบบุหรี่
เก็บข้อมูลลง Google Sheets + ทำงาน 24 ชม.
"""

import os
import json
import logging
import base64
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Dict, Any, Optional

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)
from telegram.constants import ParseMode
import gspread
from google.oauth2.service_account import Credentials

# ==================== CONFIG ====================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
SHEET_NAME = os.getenv("SHEET_NAME", "BreakLogs")
TIMEZONE = ZoneInfo("Asia/Bangkok")

ACTIONS = {
    "bathroom": "ไปห้องน้ำ",
    "market": "ไปตลาด",
    "smoke": "ไปสูบบุหรี่",
}

active_sessions: Dict[int, Dict[str, Any]] = {}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ==================== GOOGLE SHEETS ====================
def get_gspread_client():
    """สร้าง client จาก credentials (รองรับ Base64 + env var + ไฟล์)"""
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    # วิธีที่ 1: จาก Base64 (แนะนำสำหรับ Railway)
    creds_b64 = os.getenv("GOOGLE_CREDENTIALS_BASE64")
    if creds_b64:
        info = json.loads(base64.b64decode(creds_b64).decode("utf-8"))
        credentials = Credentials.from_service_account_info(info, scopes=scopes)
        return gspread.authorize(credentials)

    # วิธีที่ 2: จาก env var ธรรมดา
    creds_json = os.getenv("GOOGLE_CREDENTIALS")
    if creds_json:
        info = json.loads(creds_json)
        credentials = Credentials.from_service_account_info(info, scopes=scopes)
        return gspread.authorize(credentials)

    # วิธีที่ 3: จากไฟล์ credentials.json (สำหรับ local)
    if os.path.exists("credentials.json"):
        credentials = Credentials.from_service_account_file(
            "credentials.json", scopes=scopes
        )
        return gspread.authorize(credentials)

    raise RuntimeError(
        "ไม่พบ Google credentials! ตั้งค่า GOOGLE_CREDENTIALS_BASE64 หรือ GOOGLE_CREDENTIALS หรือวางไฟล์ credentials.json"
    )


def get_worksheet():
    client = get_gspread_client()
    spreadsheet = client.open_by_key(SPREADSHEET_ID)
    try:
        return spreadsheet.worksheet(SHEET_NAME)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title=SHEET_NAME, rows=1000, cols=10)
        headers = [
            "Start_Time", "User_ID", "Name", "Username",
            "Action", "End_Time", "Duration_Minutes", "Status",
        ]
        worksheet.append_row(headers)
        worksheet.format("A1:H1", {"textFormat": {"bold": True}})
        return worksheet


def append_start_log(user_id: int, name: str, username: str, action: str, start_time: datetime) -> int:
    worksheet = get_worksheet()
    start_str = start_time.strftime("%d/%m/%Y %H:%M:%S")
    row = [start_str, str(user_id), name, username or "", action, "", "", "Out"]
    worksheet.append_row(row, value_input_option="USER_ENTERED")
    return len(worksheet.get_all_values())


def update_end_log(user_id: int, end_time: datetime, duration_minutes: float) -> bool:
    worksheet = get_worksheet()
    records = worksheet.get_all_records()
    end_str = end_time.strftime("%d/%m/%Y %H:%M:%S")

    for idx, row in enumerate(records, start=2):
        if str(row.get("User_ID")) == str(user_id) and row.get("Status") == "Out":
            worksheet.update_cell(idx, 6, end_str)
            worksheet.update_cell(idx, 7, round(duration_minutes, 1))
            worksheet.update_cell(idx, 8, "Done")
            return True
    return False


def load_active_sessions_from_sheet():
    global active_sessions
    try:
        worksheet = get_worksheet()
        records = worksheet.get_all_records()
        loaded = 0

        for row in records:
            if row.get("Status") != "Out":
                continue

            try:
                user_id = int(row["User_ID"])
            except (KeyError, TypeError, ValueError):
                logger.warning(f"Skip invalid active-session row: {row}")
                continue

            try:
                start = datetime.strptime(
                    str(row.get("Start_Time", "")),
                    "%d/%m/%Y %H:%M:%S",
                ).replace(tzinfo=TIMEZONE)
            except (TypeError, ValueError):
                start = datetime.now(TIMEZONE)

            active_sessions[user_id] = {
                "action": str(row.get("Action") or "ออกไป"),
                "start": start,
                "name": str(row.get("Name") or f"User {user_id}"),
                "username": str(row.get("Username") or ""),
                "control_msg_id": None,
                # ชีตเดิมไม่ได้เก็บ chat_id/thread_id จึงยังเตือนอัตโนมัติ
                # สำหรับ session ที่กู้คืนหลังรีสตาร์ทไม่ได้
                "chat_id": None,
                "thread_id": None,
                "reminded": False,
            }
            loaded += 1

        logger.info(f"Loaded {loaded} active sessions from Google Sheet")
    except Exception as e:
        logger.error(f"Failed to load active sessions: {e}")


# ==================== KEYBOARDS ====================
def get_main_menu_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("🚽 1.ไปห้องน้ำ", callback_data="go_bathroom")],
        [InlineKeyboardButton("🛒 2.ไปตลาด", callback_data="go_market")],
        [InlineKeyboardButton("🚬 3.ไปสูบบุหรี่", callback_data="go_smoke")],
    ]
    return InlineKeyboardMarkup(keyboard)


def get_back_keyboard(user_id: int) -> InlineKeyboardMarkup:
    keyboard = [[InlineKeyboardButton("✅ กลับมาแล้ว", callback_data=f"back_{user_id}")]]
    return InlineKeyboardMarkup(keyboard)


# ==================== HANDLERS ====================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 <b>(Timer Bot Bank)</b>\n\n"
        "เพื่อบันทึกเวลาที่ออกไป:\n"
        "• ไปห้องน้ำ\n"
        "• ไปตลาด\n"
        "• ไปสูบบุหรี่\n\n"
        "📌 <b>วิธีใช้</b>\n"
        "1. พิมพ์ /menu เพื่อเปิดเมนู 3 ช่อง\n"
        "2. กดเลือกสิ่งที่ต้องการไป\n"
        "3. เมื่อกลับมากดปุ่ม <b>กลับมาแล้ว</b>"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "⌛️ <b>จะไปไหน ?</b> ⌛️\n\n"
        
        "กดปุ่มด้านล่างได้เลย"
    )
    thread_id = update.message.message_thread_id if update.message else None
    await update.message.reply_text(
        text,
        reply_markup=get_main_menu_keyboard(),
        parse_mode=ParseMode.HTML,
        message_thread_id=thread_id,
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not active_sessions:
        await update.message.reply_text("ตอนนี้ไม่มีใครออกไปไหนเลย 👍")
        return

    lines = ["📊 <b>คนที่กำลังออกอยู่ตอนนี้:</b>\n"]
    now = datetime.now(TIMEZONE)
    for uid, data in active_sessions.items():
        elapsed = (now - data["start"]).total_seconds() / 60
        lines.append(
            f"• {data['name']} → {data['action']} "
            f"(ไปแล้ว {round(elapsed, 1)} นาที)"
        )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def back_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id

    if user_id not in active_sessions:
        await update.message.reply_text("คุณไม่ได้บันทึกว่าออกไปไหนอยู่ครับ")
        return

    await process_return(update, context, user_id, from_command=True)


async def process_return(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    from_command: bool = False,
):
    data = active_sessions.pop(user_id)
    end_time = datetime.now(TIMEZONE)
    duration_sec = int((end_time - data["start"]).total_seconds())
    hours = duration_sec // 3600
    minutes = (duration_sec % 3600) // 60
    seconds = duration_sec % 60
    duration_str = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    duration_minutes = round(duration_sec / 60, 1)

    start_time_only = data["start"].strftime("%H:%M:%S")
    end_time_only = end_time.strftime("%H:%M:%S")

    action = data["action"]
    if "ห้องน้ำ" in action:
        action_emoji = "🚻 ไปห้องน้ำ"
    elif "ตลาด" in action:
        action_emoji = "🛒 ไปตลาด"
    else:
        action_emoji = "🚬 ไปสูบบุหรี่"

    notify_text = (
        f"<b>🔔 กลับมาแล้ว</b>\n"
        f"{action_emoji}\n\n"
        f"Name: <b>{data['name']}</b>\n\n"
        f"🟢 start: {start_time_only}\n"
        f"🔴 end: {end_time_only}\n\n"
        f"<b>⏱ ใช้เวลา ({duration_str})</b>"
    )

    chat_id = update.effective_chat.id
    thread_id = None
    if update.callback_query and update.callback_query.message:
        thread_id = update.callback_query.message.message_thread_id
    elif update.message:
        thread_id = update.message.message_thread_id

    await context.bot.send_message(
        chat_id=chat_id,
        text=notify_text,
        parse_mode=ParseMode.HTML,
        message_thread_id=thread_id,
    )

    # ส่งเมนูใหม่หลังกลับมา
    menu_text = (
        "⌛️ <b>จะไปไหน ?</b> ⌛️\n\n"
        
        "กดปุ่มด้านล่างได้เลย"
    )
    await context.bot.send_message(
        chat_id=chat_id,
        text=menu_text,
        reply_markup=get_main_menu_keyboard(),
        parse_mode=ParseMode.HTML,
        message_thread_id=thread_id,
    )

    try:
        updated = update_end_log(user_id, end_time, duration_minutes)
        if not updated:
            logger.warning(f"No open row found for user {user_id}")
    except Exception as e:
        logger.error(f"Failed to update sheet: {e}")

    if not from_command and update.callback_query:
        try:
            control_msg_id = data.get("control_msg_id")
            if control_msg_id:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=control_msg_id,
                    text=(
                        f"✅ <b>{data['name']}</b> กลับมาแล้วจาก{data['action']}\n"
                        f"<b>⏱ ใช้เวลา ({duration_str})</b>"
                    ),
                    parse_mode=ParseMode.HTML,
                    reply_markup=None,
                )
        except Exception as e:
            logger.debug(f"Could not edit control message: {e}")

    if update.callback_query:
        await update.callback_query.answer(f"กลับมาแล้ว! ใช้เวลา {duration_str}")


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user = query.from_user
    user_id = user.id
    name = user.full_name
    username = user.username or ""
    data = query.data
    chat_id = query.message.chat_id
    thread_id = query.message.message_thread_id

    if data.startswith("go_"):
        action_key = data[3:]
        if action_key not in ACTIONS:
            return

        action = ACTIONS[action_key]

        if user_id in active_sessions:
            await query.answer(
                "คุณยังไม่ได้กด 'กลับมาแล้ว' กรุณากดกลับก่อนนะ",
                show_alert=True,
            )
            return

        start_time = datetime.now(TIMEZONE)

        active_sessions[user_id] = {
            "action": action,
            "start": start_time,
            "name": name,
            "username": username,
            "control_msg_id": None,
            "chat_id": chat_id,
            "thread_id": thread_id,
            "reminded": False,
        }

        if action_key == "bathroom":
            action_emoji = "🚻 ไปห้องน้ำ"
        elif action_key == "market":
            action_emoji = "🛒 ไปตลาด"
        else:
            action_emoji = "🚬 ไปสูบบุหรี่"

        time_only = start_time.strftime("%H:%M:%S")

        notify = (
            f"📝 Name: <b>{name}</b>\n"
            f"{action_emoji}\n\n"
            f"✅ ช่วงเวลา: {time_only}"
        )
        await context.bot.send_message(
            chat_id=chat_id,
            text=notify,
            parse_mode=ParseMode.HTML,
            message_thread_id=thread_id,
        )

        control_text = (
            f"⏳ <b>{name}</b> กำลัง{action}...\n"
            f"กดปุ่มด้านล่างเมื่อกลับมาแล้ว"
        )
        control_msg = await context.bot.send_message(
            chat_id=chat_id,
            text=control_text,
            reply_markup=get_back_keyboard(user_id),
            parse_mode=ParseMode.HTML,
            message_thread_id=thread_id,
        )
        active_sessions[user_id]["control_msg_id"] = control_msg.message_id

        try:
            append_start_log(user_id, name, username, action, start_time)
        except Exception as e:
            logger.error(f"Failed to append start log: {e}")

    elif data.startswith("back_"):
        try:
            target_uid = int(data.split("_")[1])
        except (IndexError, ValueError):
            await query.answer("ปุ่มไม่ถูกต้อง", show_alert=True)
            return

        if user_id != target_uid:
            await query.answer("นี่ไม่ใช่ปุ่มของคุณ!", show_alert=True)
            return

        if user_id not in active_sessions:
            await query.answer(
                "ไม่พบข้อมูลการออกของคุณ (อาจเป็นเพราะบอทรีสตาร์ท)\n"
                "ลองพิมพ์ /back แทนได้ครับ",
                show_alert=True,
            )
            return

        await process_return(update, context, user_id, from_command=False)

async def check_overdue_sessions(context: ContextTypes.DEFAULT_TYPE):
    """แท็กแจ้งเตือนเพียงครั้งเดียว เมื่อผู้ใช้ครบ 20 นาที"""
    now = datetime.now(TIMEZONE)

    for user_id, data in list(active_sessions.items()):
        # เคยแจ้งเตือนแล้ว ไม่แจ้งซ้ำ
        if data.get("reminded", False):
            continue

        elapsed_min = (now - data["start"]).total_seconds() / 60

        # ยังไม่ครบ 20 นาที
        if elapsed_min < 20:
            continue

        chat_id = data.get("chat_id")
        thread_id = data.get("thread_id")
        name = data.get("name", "ผู้ใช้งาน")

        if not chat_id:
            logger.warning(
                f"Cannot remind user {user_id}: chat_id is missing"
            )
            continue

        # แท็กผู้ใช้งานด้วย Telegram user ID
        mention = f'<a href="tg://user?id={user_id}">{name}</a>'
        text = (
            f"⚠️ {mention}\n"
            f"ครบเวลา <b>20 นาที</b> แล้วครับ\n"
            f"กรุณากดปุ่ม <b>กลับมาแล้ว</b>"
        )

        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                message_thread_id=thread_id,
            )

            # ตั้งเป็น True หลังส่งสำเร็จ เพื่อไม่ให้แจ้งซ้ำ
            active_sessions[user_id]["reminded"] = True

            logger.info(
                f"One-time 20-minute reminder sent to "
                f"{user_id} ({name})"
            )
        except Exception as e:
            logger.error(
                f"Failed to remind user {user_id}: {e}"
            )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📖 <b>คำสั่งที่ใช้ได้</b>\n\n"
        "/menu - เปิดเมนู 3 ช่อง\n"
        "/status - ดูว่าตอนนี้ใครกำลังออกอยู่\n"
        "/back - กลับมา (กรณีปุ่มหาย)\n"
        "/start - แนะนำบอท\n"
        "/help - แสดงข้อความนี้"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Exception while handling update: {context.error}", exc_info=context.error)


def main():
    if not BOT_TOKEN:
        raise RuntimeError("กรุณาตั้งค่า BOT_TOKEN ใน environment variable")
    if not SPREADSHEET_ID:
        raise RuntimeError("กรุณาตั้งค่า SPREADSHEET_ID ใน environment variable")

    # ต้องสร้าง app ทุกครั้ง ไม่ควรอยู่ภายใน except
    app = Application.builder().token(BOT_TOKEN).build()

    load_active_sessions_from_sheet()

    # เช็คคนลืมกดกลับมาทุก 1 นาที
    if app.job_queue is not None:
        app.job_queue.run_repeating(
            check_overdue_sessions,
            interval=60,
            first=30,
        )
    else:
        logger.warning(
            "JobQueue is unavailable. Install: "
            'python-telegram-bot[job-queue]==22.3'
        )

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("menu", menu_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("back", back_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_error_handler(error_handler)

    logger.info("Bot is starting... (24/7 ready)")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
