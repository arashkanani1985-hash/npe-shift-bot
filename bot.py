import os
import time
import sqlite3
import threading
from datetime import datetime

from dotenv import load_dotenv
from flask import Flask
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# --------------------------
# Load env
# --------------------------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN is missing! Set it in Render Environment Variables.")

# --------------------------
# Flask app (Render needs a web port)
# --------------------------
app = Flask(__name__)

@app.get("/")
def home():
    return "✅ Bot is running!", 200

PORT = int(os.getenv("PORT", "10000"))

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

    c.execute("""
        CREATE TABLE IF NOT EXISTS attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            shift_id INTEGER,
            delay_minutes INTEGER,
            timestamp TEXT
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

def get_shifts():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT shift_id, shift_name, start_time FROM shifts ORDER BY shift_id")
    data = c.fetchall()
    conn.close()
    return data

# --------------------------
# Managers
# --------------------------
MANAGERS = {6017492841, 97965212, 1035761242}

# --------------------------
# Conversation states
# --------------------------
SHIFT_SELECT, DELAY_INPUT = range(2)

# --------------------------
# Keyboards
# --------------------------
def kb_shifts():
    shifts = get_shifts()
    keyboard = []
    row = []
    for sid, _, _ in shifts:
        row.append(KeyboardButton(str(sid)))
        if len(row) == 3:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    keyboard.append([KeyboardButton("⬅️ بازگشت"), KeyboardButton("/cancel")])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def kb_back():
    return ReplyKeyboardMarkup(
        [[KeyboardButton("⬅️ بازگشت"), KeyboardButton("/cancel")]],
        resize_keyboard=True
    )

# --------------------------
# Handlers
# --------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if user_id not in MANAGERS:
        await update.message.reply_text("❌ فعلاً فقط مدیران اجازه ورود دارند.")
        return ConversationHandler.END

    await update.message.reply_text(
        "✅ خوش آمدید مدیر 🌟\n\n"
        "برای ثبت ورود، شماره شیفت را انتخاب کنید:\n"
        "1️⃣ شیفت 1 (08:00-16:00)\n"
        "2️⃣ شیفت 2 (16:00-24:00)\n"
        "3️⃣ شیفت 3 (00:00-08:00)\n\n"
        "👇 فقط عدد 1 یا 2 یا 3 را ارسال کنید.",
        reply_markup=kb_shifts(),
    )
    return SHIFT_SELECT

async def shift_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()

    if text == "⬅️ بازگشت":
        return await start(update, context)

    if text not in ("1", "2", "3"):
        await update.message.reply_text(
            "❌ مقدار شیفت نامعتبر است. فقط 1 یا 2 یا 3 بفرست.",
            reply_markup=kb_shifts(),
        )
        return SHIFT_SELECT

    context.user_data["shift_id"] = int(text)

    await update.message.reply_text(
        "✅ خیلی خوب!\n\n"
        "⏱️ حالا میزان تاخیر را به دقیقه وارد کن (مثلاً 10):",
        reply_markup=kb_back(),
    )
    return DELAY_INPUT

async def delay_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()

    if text == "⬅️ بازگشت":
        await update.message.reply_text("⬅️ برگشتیم به انتخاب شیفت.", reply_markup=kb_shifts())
        return SHIFT_SELECT

    if text == "/cancel":
        await update.message.reply_text(
            "✅ عملیات کنسل شد.",
            reply_markup=ReplyKeyboardMarkup([["/start"]], resize_keyboard=True),
        )
        return ConversationHandler.END

    if not text.isdigit():
        await update.message.reply_text(
            "❌ لطفاً فقط عدد دقیقه وارد کن (مثلاً 5 یا 10).",
            reply_markup=kb_back(),
        )
        return DELAY_INPUT

    delay = int(text)
    shift_id = context.user_data.get("shift_id")
    user = update.effective_user
    username = user.full_name

    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(
        "INSERT INTO attendance (user_id, username, shift_id, delay_minutes, timestamp) VALUES (?, ?, ?, ?, ?)",
        (user.id, username, shift_id, delay, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()

      await update.message.reply_text(
        f"✅ ورود ثبت شد!\n\n"
        f"👤 {username}\n"
        f"🕒 شیفت: {shift_id}\n"
        f"⏱️ تاخیر: {delay} دقیقه",
       reply_markup=ReplyKeyboardMarkup([["/start"]], resize_keyboard=True)
      )

    msg = f"📢 گزارش ورود:\n\n👤 {username}\n🕒 شیفت {shift_id}\n⏱️ {delay} دقیقه تاخیر"
    for manager_id in MANAGERS:
        try:
            await context.bot.send_message(chat_id=manager_id, text=msg)
        except:
            pass

    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✅ عملیات کنسل شد.",
        reply_markup=ReplyKeyboardMarkup([["/start"]], resize_keyboard=True),
    )
    return ConversationHandler.END


# --------------------------
# Bot runner (NO Updater)
# --------------------------
def run_bot():
    init_db()
    seed_shifts()

    application = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            SHIFT_SELECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, shift_select)],
            DELAY_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, delay_input)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    application.add_handler(conv)

    print("✅ Telegram bot polling started!")
    application.run_polling()


# --------------------------
# Main
# --------------------------
if __name__ == "__main__":
    threading.Thread(target=run_bot, daemon=True).start()

    print(f"✅ Flask running on PORT={PORT}")
    app.run(host="0.0.0.0", port=PORT)


