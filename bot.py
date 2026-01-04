import os
import sqlite3
import asyncio
from datetime import datetime
from dotenv import load_dotenv
from flask import Flask, request

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
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

RENDER_URL = os.getenv("RENDER_URL")
if not RENDER_URL:
    raise ValueError("❌ RENDER_URL missing! Add RENDER_URL in Render Environment Variables.")

PORT = int(os.getenv("PORT", 10000))
DB_NAME = "attendance.db"

# =============================================================================
# Flask app
# =============================================================================
app = Flask(__name__)

@app.get("/")
def home():
    return "✅ Bot is running (Render keep-alive OK)", 200


# =============================================================================
# DB init
# =============================================================================
def init_db():
    conn = sqlite3.connect(DB_NAME)
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

    conn.commit()
    conn.close()


# =============================================================================
# Telegram Keyboards
# =============================================================================
def kb_main():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("👤 پنل کارمند")],
            [KeyboardButton("👨‍💼 پنل مدیر")],
            [KeyboardButton("ℹ️ راهنما")]
        ],
        resize_keyboard=True
    )


WELCOME_TEXT = (
    "👋 سلام!\n\n"
    "به سیستم مدیریت شیفت خوش آمدید ✅\n"
    "لطفاً یکی از گزینه‌های زیر را انتخاب کنید:"
)

HELP_TEXT = (
    "ℹ️ راهنما:\n\n"
    "این نسخه فقط برای تست webhook است ✅\n"
    "بعد از اینکه مطمئن شدیم تلگرام جواب میده، نسخه کامل رو می‌ذاریم."
)


# =============================================================================
# Handlers
# =============================================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME_TEXT, reply_markup=kb_main())


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, reply_markup=kb_main())


async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if text == "👤 پنل کارمند":
        await update.message.reply_text("✅ پنل کارمند (فعلاً تست)", reply_markup=kb_main())
        return

    if text == "👨‍💼 پنل مدیر":
        await update.message.reply_text("✅ پنل مدیر (فعلاً تست)", reply_markup=kb_main())
        return

    if text == "ℹ️ راهنما":
        await help_cmd(update, context)
        return

    await update.message.reply_text("❓ از دکمه‌ها استفاده کن.", reply_markup=kb_main())


# =============================================================================
# Telegram Application Global
# =============================================================================
bot_app = Application.builder().token(BOT_TOKEN).build()
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

# Register handlers
bot_app.add_handler(CommandHandler("start", start))
bot_app.add_handler(CommandHandler("help", help_cmd))
bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_buttons))


# =============================================================================
# WEBHOOK endpoint (SYNC)
# =============================================================================
@app.post("/webhook")
def webhook():
    data = request.get_json(force=True)
    update = Update.de_json(data, bot_app.bot)

    # push update into async loop safely
    asyncio.run_coroutine_threadsafe(bot_app.process_update(update), loop)
    return "ok", 200


# =============================================================================
# STARTUP
# =============================================================================
def main():
    init_db()

    async def startup():
        await bot_app.initialize()
        await bot_app.start()

        webhook_url = f"{RENDER_URL}/webhook"
        await bot_app.bot.set_webhook(webhook_url)
        print(f"✅ Webhook set to: {webhook_url}")

    loop.create_task(startup())
    loop.run_forever()


if __name__ == "__main__":
    import threading
    threading.Thread(target=main, daemon=True).start()
    print(f"✅ Flask running on PORT={PORT}")
    app.run(host="0.0.0.0", port=PORT)
