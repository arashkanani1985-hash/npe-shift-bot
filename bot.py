import os
import time
import threading
import sqlite3
from datetime import datetime

from dotenv import load_dotenv
from flask import Flask
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ConversationHandler,
    ContextTypes, filters
)

# --------------------------
# Load env
# --------------------------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN is missing! Set it in Render Environment Variables.")

# --------------------------
# Flask app (Render needs this)
# --------------------------
app = Flask(__name__)

@app.get("/")
def home():
    return "OK", 200

PORT = int(os.getenv("PORT", 10000))

# --------------------------
# DB
# --------------------------
DB_NAME = "attendance.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS shifts (
            shift_id INTEGER PRIMARY KEY,
            shift_name TEXT,
            start_time TEXT,
            end_time TEXT
        )
    """)

    conn.commit()
    conn.close()

def seed_shifts():
    shifts = [
        (1, "شیفت 1 (08:00-16:00)", "08:00", "16:00"),
        (2, "شیفت 2 (16:00-24:00)", "16:00", "24:00"),
        (3, "شیفت 3 (00:00-08:00)", "00:00", "08:00"),
    ]

    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM shifts")
    if c.fetchone()[0] == 0:
        c.executemany("INSERT INTO shifts VALUES (?, ?, ?, ?)", shifts)
        conn.commit()
    conn.close()

# --------------------------
# Users (temporary)
# --------------------------
MANAGERS = {6017492841}
EMPLOYEES = {6017492841}

# --------------------------
# Conversation states
# --------------------------
ROLE_SELECT, MENU = range(2)

# --------------------------
# Keyboards
# --------------------------
def kb_role():
    return ReplyKeyboardMarkup(
        [[KeyboardButton("👑 مدیر"), KeyboardButton("👤 کارمند")]],
        resize_keyboard=True
    )

def kb_manager_menu():
    return ReplyKeyboardMarkup([["⬅️ بازگشت"]], resize_keyboard=True)

def kb_employee_menu():
    return ReplyKeyboardMarkup([["⬅️ بازگشت"]], resize_keyboard=True)

# --------------------------
# Bot handlers
# --------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "سلام! خوش آمدید 🌟\nنقش خود را انتخاب کنید:",
        reply_markup=kb_role()
    )
    return ROLE_SELECT

async def role_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_id = update.effective_user.id

    if text == "👑 مدیر":
        if user_id not in MANAGERS:
            await update.message.reply_text("شما مدیر نیستید ❌", reply_markup=kb_role())
            return ROLE_SELECT
        await update.message.reply_text("منوی مدیر 👇", reply_markup=kb_manager_menu())
        return MENU

    if text == "👤 کارمند":
        if user_id not in EMPLOYEES:
            await update.message.reply_text("شما در لیست پرسنل نیستید ❌", reply_markup=kb_role())
            return ROLE_SELECT
        await update.message.reply_text("منوی کارمند 👇", reply_markup=kb_employee_menu())
        return MENU

    await update.message.reply_text("لطفاً یکی از گزینه‌ها را انتخاب کنید.", reply_markup=kb_role())
    return ROLE_SELECT

async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("فعلاً فقط تست آنلاین شدن ربات ✅")
    return MENU

# --------------------------
# Run bot in background thread
# --------------------------
def run_bot():
    init_db()
    seed_shifts()

    application = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ROLE_SELECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, role_select)],
            MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, menu)],
        },
        fallbacks=[],
    )

    application.add_handler(conv)

    while True:
        try:
            print("✅ Bot is running...")
            application.run_polling()
        except Exception as e:
            print("⚠️ Bot crashed, retry in 5s:", e)
            time.sleep(5)

# --------------------------
# Main
# --------------------------
if __name__ == "__main__":
    threading.Thread(target=run_bot, daemon=True).start()

    print(f"✅ Flask running on PORT={PORT}")
    app.run(host="0.0.0.0", port=PORT)
