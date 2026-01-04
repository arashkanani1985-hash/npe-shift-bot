import os
import sqlite3
import asyncio
from datetime import datetime, timedelta, time as dtime
from dotenv import load_dotenv

from flask import Flask, request

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# =============================================================================
# CONFIG
# =============================================================================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN missing! Add BOT_TOKEN in Render Environment Variables.")

RENDER_URL = os.getenv("RENDER_URL")  # Example: https://xxxx.onrender.com
if not RENDER_URL:
    raise ValueError("❌ RENDER_URL missing! Add RENDER_URL in Render Environment Variables.")

PORT = int(os.getenv("PORT", 10000))
DB_NAME = "attendance.db"

# ---------------------------
# Roles
# ---------------------------
REAL_MANAGERS = {97965212, 1035761242}       # Parham + Tohid
SUPERUSER = {6017492841}                    # YOU (full access)
ADMIN_USERS = REAL_MANAGERS | SUPERUSER     # all admins

# =============================================================================
# Shift constants (fixed hours)
# =============================================================================
SHIFTS = [
    (1, "شیفت 1", "08:00", "16:00"),
    (2, "شیفت 2", "16:00", "24:00"),
    (3, "شیفت 3", "00:00", "08:00"),
]

REMINDER_MINUTES_BEFORE_SHIFT = 15
LATE_ALERT_MINUTES_AFTER_SHIFT_START = 5

NIGHTLY_REPORT_HOUR = 23
NIGHTLY_REPORT_MINUTE = 59

# =============================================================================
# FLASK APP (Render Keep alive + Telegram webhook)
# =============================================================================
app = Flask(__name__)

@app.get("/")
def home():
    return "✅ Bot is running (Render keep-alive OK)", 200

@app.post("/webhook")
async def webhook():
    data = request.get_json(force=True)
    update = Update.de_json(data, bot_app.bot)
    await bot_app.process_update(update)
    return "ok", 200

# =============================================================================
# DATABASE
# =============================================================================
def db():
    return sqlite3.connect(DB_NAME)

def init_db():
    conn = db()
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS employees (
            user_id INTEGER PRIMARY KEY,
            telegram_username TEXT,
            full_name TEXT,
            status TEXT DEFAULT 'pending',
            created_at TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS employee_shifts (
            user_id INTEGER PRIMARY KEY,
            shift_id INTEGER,
            updated_at TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT,
            user_id INTEGER,
            full_name TEXT,
            shift_id INTEGER,
            check_in_time TEXT,
            check_out_time TEXT,
            delay_minutes INTEGER DEFAULT 0
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS shift_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT,
            user_id INTEGER,
            full_name TEXT,
            shift_id INTEGER,
            note TEXT,
            created_at TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS manager_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT,
            note TEXT,
            created_at TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS leave_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT,
            user_id INTEGER,
            full_name TEXT,
            reason TEXT,
            status TEXT DEFAULT 'pending',
            created_at TEXT
        )
    """)

    conn.commit()
    conn.close()

def get_today_str():
    return datetime.now().date().isoformat()

def parse_hhmm(hhmm: str) -> dtime:
    h, m = hhmm.split(":")
    return dtime(int(h), int(m))

def get_shift_by_id(shift_id: int):
    for s in SHIFTS:
        if s[0] == shift_id:
            return s
    return None

def get_employee_status(user_id: int):
    conn = db()
    c = conn.cursor()
    c.execute("SELECT status FROM employees WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def get_employee_full_name(user_id: int):
    conn = db()
    c = conn.cursor()
    c.execute("SELECT full_name FROM employees WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def upsert_employee(user_id: int, username: str, full_name: str, status="pending"):
    conn = db()
    c = conn.cursor()
    c.execute("""
        INSERT INTO employees (user_id, telegram_username, full_name, status, created_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            telegram_username=excluded.telegram_username,
            full_name=excluded.full_name,
            status=excluded.status
    """, (user_id, username, full_name, status, datetime.now().isoformat(timespec="seconds")))
    conn.commit()
    conn.close()

def set_employee_status(user_id: int, status: str):
    conn = db()
    c = conn.cursor()
    c.execute("UPDATE employees SET status=? WHERE user_id=?", (status, user_id))
    conn.commit()
    conn.close()

def list_pending_employees():
    conn = db()
    c = conn.cursor()
    c.execute("SELECT user_id, telegram_username, full_name FROM employees WHERE status='pending'")
    rows = c.fetchall()
    conn.close()
    return rows

def list_approved_employees():
    conn = db()
    c = conn.cursor()
    c.execute("SELECT user_id, telegram_username, full_name FROM employees WHERE status='approved'")
    rows = c.fetchall()
    conn.close()
    return rows

def set_employee_shift(user_id: int, shift_id: int):
    conn = db()
    c = conn.cursor()
    c.execute("""
        INSERT INTO employee_shifts (user_id, shift_id, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            shift_id=excluded.shift_id,
            updated_at=excluded.updated_at
    """, (user_id, shift_id, datetime.now().isoformat(timespec="seconds")))
    conn.commit()
    conn.close()

def get_employee_shift(user_id: int):
    conn = db()
    c = conn.cursor()
    c.execute("SELECT shift_id FROM employee_shifts WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

# =============================================================================
# KEYBOARDS
# =============================================================================
def kb_main(user_id: int):
    buttons = []
    if user_id in ADMIN_USERS:
        buttons.append([KeyboardButton("👨‍💼 پنل مدیر")])
    buttons.append([KeyboardButton("👤 پنل کارمند")])
    buttons.append([KeyboardButton("ℹ️ راهنما")])
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)

def kb_employee(user_id: int):
    rows = [
        [KeyboardButton("🕒 شیفت من"), KeyboardButton("✅ ثبت ورود"), KeyboardButton("❌ ثبت خروج")],
        [KeyboardButton("✍️ ثبت توضیح برای شیفت بعد"), KeyboardButton("📜 توضیحات شیفت قبلی")],
        [KeyboardButton("🏖️ درخواست مرخصی"), KeyboardButton("📍 وضعیت امروز")],
        [KeyboardButton("⬅️ بازگشت به منوی اصلی")],
    ]
    status = get_employee_status(user_id)
    if user_id not in ADMIN_USERS and status in (None, "pending"):
        rows.insert(0, [KeyboardButton("📌 ثبت‌نام کارمند")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

def kb_manager(user_id: int):
    rows = [
        [KeyboardButton("👥 تایید کارمندها"), KeyboardButton("🧾 لیست کارمندها")],
        [KeyboardButton("🗓️ تعیین/تغییر شیفت کارمند"), KeyboardButton("📝 پیام مدیر")],
        [KeyboardButton("📊 گزارش امروز"), KeyboardButton("🏖️ مرخصی‌ها")],
        [KeyboardButton("⬅️ بازگشت به منوی اصلی")],
    ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

def ikb_approve_reject(user_id: int):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ تایید", callback_data=f"approve:{user_id}"),
            InlineKeyboardButton("❌ رد", callback_data=f"reject:{user_id}"),
        ]
    ])

# =============================================================================
# TEXTS
# =============================================================================
WELCOME_TEXT = (
    "👋 سلام!\n\n"
    "به سیستم مدیریت شیفت خوش آمدید ✅\n"
    "لطفاً نقش خود را انتخاب کنید:"
)

HELP_TEXT = (
    "ℹ️ راهنما:\n\n"
    "👤 پنل کارمند:\n"
    "• شیفت من\n"
    "• ثبت ورود/خروج\n"
    "• توضیحات شیفت قبلی و ثبت توضیح برای شیفت بعد\n"
    "• درخواست مرخصی\n\n"
    "👨‍💼 پنل مدیر:\n"
    "• تایید کارمندها\n"
    "• تعیین/تغییر شیفت کارمندها\n"
    "• گزارش امروز + مرخصی‌ها + پیام مدیر\n\n"
    "✅ نکته: کارمند جدید باید «ثبت‌نام کارمند» را فقط یکبار انجام دهد."
)

# =============================================================================
# HANDLERS
# =============================================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME_TEXT, reply_markup=kb_main(update.effective_user.id))

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, reply_markup=kb_main(update.effective_user.id))

async def employee_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👤 پنل کارمند", reply_markup=kb_employee(update.effective_user.id))

async def manager_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_USERS:
        await update.message.reply_text("❌ فقط مدیر دسترسی دارد.", reply_markup=kb_main(update.effective_user.id))
        return
    await update.message.reply_text("👨‍💼 پنل مدیر", reply_markup=kb_manager(update.effective_user.id))

# =============================================================================
# MAIN ROUTER
# =============================================================================
async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if text == "👨‍💼 پنل مدیر":
        return await manager_panel(update, context)
    if text == "👤 پنل کارمند":
        return await employee_panel(update, context)
    if text == "ℹ️ راهنما":
        return await help_cmd(update, context)

    await update.message.reply_text("❓ از دکمه‌ها استفاده کن.", reply_markup=kb_main(update.effective_user.id))

# =============================================================================
# BOT SETUP
# =============================================================================
bot_app: Application = None

async def build_app():
    global bot_app
    init_db()

    bot_app = Application.builder().token(BOT_TOKEN).build()

    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CommandHandler("help", help_cmd))
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_buttons))

    await bot_app.initialize()
    await bot_app.start()

    # Set webhook
    webhook_url = f"{RENDER_URL}/webhook"
    await bot_app.bot.set_webhook(webhook_url)
    print(f"✅ Webhook set to: {webhook_url}")

async def run():
    await build_app()
    app.run(host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    asyncio.run(run())
