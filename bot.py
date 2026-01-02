import os
import sqlite3
from datetime import datetime, date
from dotenv import load_dotenv

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes,
    filters, ConversationHandler
)

from flask import Flask
import threading

# --------------------------
# Load env
# --------------------------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

# --------------------------
# Simple health server for UptimeRobot
# --------------------------
app = Flask(__name__)

@app.get("/health")
def health():
    return "OK", 200

def run_health_server():
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

# --------------------------
# Database
# --------------------------
DB_PATH = "attendance.db"

def db_init():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        full_name TEXT,
        role TEXT DEFAULT 'employee'
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS attendance (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        att_date TEXT,
        checkin_time TEXT,
        checkout_time TEXT,
        note TEXT,
        shift TEXT,
        created_at TEXT,
        FOREIGN KEY(user_id) REFERENCES users(user_id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS admin_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        shift TEXT,
        message TEXT,
        created_at TEXT
    )
    """)

    conn.commit()
    conn.close()

def db_execute(query, params=()):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(query, params)
    conn.commit()
    conn.close()

def db_fetchone(query, params=()):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(query, params)
    row = cur.fetchone()
    conn.close()
    return row

def db_fetchall(query, params=()):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(query, params)
    rows = cur.fetchall()
    conn.close()
    return rows

# --------------------------
# Access control
# --------------------------
# ✅ اینجا فقط ID خودت فعلاً هست
# بعداً ID مدیر دوم و ۷ کارمند رو اضافه می‌کنیم
ALLOWED_USERS = {6017492841}
ADMINS = {6017492841}

def is_allowed(user_id: int) -> bool:
    return user_id in ALLOWED_USERS

def is_admin(user_id: int) -> bool:
    return user_id in ADMINS

# --------------------------
# UI Keyboards
# --------------------------
BTN_EMPLOYEE = "👤 کارمند"
BTN_ADMIN = "👑 مدیر"
BTN_BACK = "⬅️ بازگشت"
BTN_CANCEL = "❌ لغو"

employee_kb = ReplyKeyboardMarkup(
    [
        [KeyboardButton("✅ ورود"), KeyboardButton("❌ خروج")],
        [KeyboardButton("📍 وضعیت امروز"), KeyboardButton("📝 ثبت توضیح")],
        [KeyboardButton("ℹ️ راهنما"), KeyboardButton(BTN_BACK)]
    ],
    resize_keyboard=True
)

admin_kb = ReplyKeyboardMarkup(
    [
        [KeyboardButton("📊 گزارش امروز"), KeyboardButton("📝 پیام مدیر برای شیفت")],
        [KeyboardButton("📌 دیدن توضیحات پرسنل"), KeyboardButton("ℹ️ راهنما")],
        [KeyboardButton(BTN_BACK)]
    ],
    resize_keyboard=True
)

role_kb = ReplyKeyboardMarkup(
    [[KeyboardButton(BTN_EMPLOYEE), KeyboardButton(BTN_ADMIN)]],
    resize_keyboard=True,
    one_time_keyboard=True
)

# Conversation states
ASK_NOTE = 1
ASK_ADMIN_MESSAGE_SHIFT = 2
ASK_ADMIN_MESSAGE_TEXT = 3

# --------------------------
# Helper: shifts (1/2/3)
# --------------------------
def get_current_shift(now: datetime) -> str:
    h = now.hour
    if 8 <= h < 16:
        return "1"
    elif 16 <= h < 24:
        return "2"
    else:
        return "3"

def get_shift_label(shift_code: str) -> str:
    if shift_code == "1": return "شیفت 1 (08:00–16:00)"
    if shift_code == "2": return "شیفت 2 (16:00–24:00)"
    return "شیفت 3 (00:00–08:00)"

def get_shift_start(shift_code: str):
    # hour, minute
    return {"1": (8, 0), "2": (16, 0), "3": (0, 0)}[shift_code]

# --------------------------
# Global helpers: cancel / back
# --------------------------
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ عملیات لغو شد.", reply_markup=role_kb)
    return ConversationHandler.END

async def go_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⬅️ بازگشت به منوی انتخاب نقش.", reply_markup=role_kb)
    return ConversationHandler.END

# --------------------------
# Commands
# --------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    row = db_fetchone("SELECT user_id FROM users WHERE user_id=?", (user.id,))
    if not row:
        db_execute(
            "INSERT INTO users(user_id, username, full_name, role) VALUES(?,?,?,?)",
            (user.id, user.username or "", user.full_name or "", "employee")
        )

    welcome = (
        "👋 سلام!\n"
        "به ربات حضور و غیاب خوش آمدید.\n\n"
        "ابتدا نقش خود را انتخاب کنید:"
    )
    await update.message.reply_text(welcome, reply_markup=role_kb)

async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.message.reply_text(
        f"🆔 Telegram User ID شما:\n`{user.id}`",
        parse_mode="Markdown"
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (
        "ℹ️ راهنما:\n"
        "/start شروع\n"
        "/myid نمایش ID\n"
        "/cancel لغو عملیات\n\n"
        "اگر در لیست مجاز نباشید، امکان ثبت حضور ندارید."
    )
    await update.message.reply_text(txt)

# --------------------------
# Role selection
# --------------------------
async def role_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text

    if text == BTN_EMPLOYEE:
        if not is_allowed(user_id):
            await update.message.reply_text(
                "⛔ شما مجاز به استفاده از امکانات حضور و غیاب نیستید.\n"
                "برای فعال‌سازی، ID خود را با دستور /myid برای مدیر ارسال کنید."
            )
            return
        await update.message.reply_text("✅ منوی کارمند فعال شد.", reply_markup=employee_kb)
        return

    if text == BTN_ADMIN:
        if not is_admin(user_id):
            await update.message.reply_text(
                "⛔ شما مدیر نیستید.\n"
                "اگر فکر می‌کنید اشتباه است، ID خود را با /myid ارسال کنید."
            )
            return
        await update.message.reply_text("✅ پنل مدیر فعال شد.", reply_markup=admin_kb)
        return

# --------------------------
# Employee actions
# --------------------------
async def checkin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_allowed(user.id):
        await update.message.reply_text("⛔ شما مجاز نیستید. /myid")
        return

    today = date.today().isoformat()
    now = datetime.now()
    shift = get_current_shift(now)

    row = db_fetchone("SELECT id, checkin_time FROM attendance WHERE user_id=? AND att_date=?", (user.id, today))
    if row and row[1]:
        await update.message.reply_text("⚠️ شما امروز قبلاً ورود زده‌اید.")
        return

    # محاسبه تاخیر
    sh, sm = get_shift_start(shift)
    start_time = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
    delay_minutes = max(0, int((now - start_time).total_seconds() // 60))
    delay_text = "✅ به موقع" if delay_minutes == 0 else f"⏰ تاخیر: {delay_minutes} دقیقه"

    db_execute("""
        INSERT INTO attendance(user_id, att_date, checkin_time, checkout_time, note, shift, created_at)
        VALUES(?,?,?,?,?,?,?)
    """, (user.id, today, now.strftime("%H:%M:%S"), None, None, shift, now.isoformat()))

    msg_row = db_fetchone("SELECT message FROM admin_messages WHERE shift=? ORDER BY id DESC LIMIT 1", (shift,))
    admin_msg = msg_row[0] if msg_row else None

    response = (
        f"✅ ورود ثبت شد.\n"
        f"⏰ ساعت: {now.strftime('%H:%M')}\n"
        f"🧩 {get_shift_label(shift)}\n"
        f"{delay_text}"
    )

    if admin_msg:
        response += f"\n\n📌 پیام مدیر برای این شیفت:\n{admin_msg}"

    await update.message.reply_text(response)

    # ارسال تاخیر برای مدیرها
    if delay_minutes > 0:
        for admin_id in ADMINS:
            try:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=(
                        f"🚨 تاخیر ثبت شد!\n"
                        f"👤 {user.full_name} (@{user.username})\n"
                        f"🧩 {get_shift_label(shift)}\n"
                        f"⏰ ورود: {now.strftime('%H:%M')}\n"
                        f"⏰ تاخیر: {delay_minutes} دقیقه"
                    )
                )
            except:
                pass

async def checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_allowed(user.id):
        await update.message.reply_text("⛔ شما مجاز نیستید. /myid")
        return

    today = date.today().isoformat()
    now = datetime.now()

    row = db_fetchone("SELECT id, checkout_time FROM attendance WHERE user_id=? AND att_date=?", (user.id, today))
    if not row:
        await update.message.reply_text("⚠️ شما امروز هنوز ورود نزده‌اید.")
        return
    if row[1]:
        await update.message.reply_text("⚠️ شما امروز قبلاً خروج زده‌اید.")
        return

    db_execute("UPDATE attendance SET checkout_time=? WHERE user_id=? AND att_date=?",
               (now.strftime("%H:%M:%S"), user.id, today))

    await update.message.reply_text(f"❌ خروج ثبت شد.\n⏰ ساعت: {now.strftime('%H:%M')}")

async def today_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_allowed(user.id):
        await update.message.reply_text("⛔ شما مجاز نیستید. /myid")
        return

    today = date.today().isoformat()
    row = db_fetchone("""
        SELECT checkin_time, checkout_time, note, shift
        FROM attendance
        WHERE user_id=? AND att_date=?
    """, (user.id, today))

    if not row:
        await update.message.reply_text("📍 امروز هنوز ورود/خروج ثبت نشده است.")
        return

    checkin_t, checkout_t, note, shift = row
    txt = (
        f"📍 وضعیت امروز:\n"
        f"🧩 {get_shift_label(shift)}\n"
        f"✅ ورود: {checkin_t or '-'}\n"
        f"❌ خروج: {checkout_t or '-'}\n"
        f"📝 توضیحات: {note or '-'}"
    )
    await update.message.reply_text(txt)

async def ask_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_allowed(user.id):
        await update.message.reply_text("⛔ شما مجاز نیستید. /myid")
        return ConversationHandler.END

    await update.message.reply_text("📝 لطفاً توضیحات خود را برای امروز/شیفت‌های بعد بنویسید:\n\n(برای لغو /cancel)")
    return ASK_NOTE

async def save_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    note = update.message.text.strip()
    today = date.today().isoformat()

    if note == BTN_BACK:
        await go_back(update, context)
        return ConversationHandler.END

    if note == BTN_CANCEL:
        await cancel(update, context)
        return ConversationHandler.END

    row = db_fetchone("SELECT id FROM attendance WHERE user_id=? AND att_date=?", (user.id, today))
    if not row:
        now = datetime.now()
        shift = get_current_shift(now)
        db_execute("""
            INSERT INTO attendance(user_id, att_date, checkin_time, checkout_time, note, shift, created_at)
            VALUES(?,?,?,?,?,?,?)
        """, (user.id, today, None, None, note, shift, now.isoformat()))
    else:
        db_execute("UPDATE attendance SET note=? WHERE user_id=? AND att_date=?",
                   (note, user.id, today))

    await update.message.reply_text("✅ توضیحات ثبت شد.", reply_markup=employee_kb)
    return ConversationHandler.END

# --------------------------
# Admin actions
# --------------------------
async def report_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("⛔ دسترسی مدیر ندارید.")
        return

    today = date.today().isoformat()
    rows = db_fetchall("""
        SELECT u.full_name, u.username, a.shift, a.checkin_time, a.checkout_time
        FROM attendance a
        JOIN users u ON u.user_id = a.user_id
        WHERE a.att_date=?
        ORDER BY a.shift, u.full_name
    """, (today,))

    if not rows:
        await update.message.reply_text("📊 امروز هنوز هیچ رکوردی ثبت نشده است.")
        return

    lines = ["📊 گزارش امروز:\n"]
    for full_name, username, shift, cin, cout in rows:
        nm = full_name or (f"@{username}" if username else "بدون نام")
        lines.append(
            f"• {nm} | {get_shift_label(shift)} | ورود: {cin or '-'} | خروج: {cout or '-'}"
        )

    await update.message.reply_text("\n".join(lines))

async def view_notes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("⛔ دسترسی مدیر ندارید.")
        return

    today = date.today().isoformat()
    rows = db_fetchall("""
        SELECT u.full_name, u.username, a.shift, a.note
        FROM attendance a
        JOIN users u ON u.user_id = a.user_id
        WHERE a.att_date=? AND a.note IS NOT NULL AND a.note <> ''
        ORDER BY a.shift, u.full_name
    """, (today,))

    if not rows:
        await update.message.reply_text("📌 امروز هیچ توضیحی ثبت نشده است.")
        return

    lines = ["📌 توضیحات پرسنل امروز:\n"]
    for full_name, username, shift, note in rows:
        nm = full_name or (f"@{username}" if username else "بدون نام")
        lines.append(f"• {nm} | {get_shift_label(shift)}\n   📝 {note}\n")

    await update.message.reply_text("\n".join(lines))

async def admin_message_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("⛔ دسترسی مدیر ندارید.")
        return ConversationHandler.END

    await update.message.reply_text(
        "📝 برای کدام شیفت پیام ثبت شود؟\n"
        "فقط عدد را بفرست:\n"
        "1) شیفت 1 (08:00–16:00)\n"
        "2) شیفت 2 (16:00–24:00)\n"
        "3) شیفت 3 (00:00–08:00)\n\n"
        "برای لغو /cancel"
    )
    return ASK_ADMIN_MESSAGE_SHIFT

async def admin_message_shift(update: Update, context: ContextTypes.DEFAULT_TYPE):
    shift = update.message.text.strip()

    if shift in (BTN_BACK, BTN_CANCEL):
        await cancel(update, context)
        return ConversationHandler.END

    if shift not in ("1", "2", "3"):
        await update.message.reply_text("⚠️ مقدار شیفت نامعتبر است. فقط 1 یا 2 یا 3 ارسال کن.")
        return ASK_ADMIN_MESSAGE_SHIFT

    context.user_data["admin_shift"] = shift
    await update.message.reply_text(f"✅ خوب. حالا متن پیام مدیر برای {get_shift_label(shift)} را ارسال کن:")
    return ASK_ADMIN_MESSAGE_TEXT

async def admin_message_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    shift = context.user_data.get("admin_shift")
    text = update.message.text.strip()
    now = datetime.now().isoformat()

    if text in (BTN_BACK, BTN_CANCEL):
        await cancel(update, context)
        return ConversationHandler.END

    db_execute("INSERT INTO admin_messages(shift, message, created_at) VALUES(?,?,?)", (shift, text, now))
    await update.message.reply_text("✅ پیام مدیر ذخیره شد.", reply_markup=admin_kb)
    return ConversationHandler.END

# --------------------------
# Router
# --------------------------
async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if text == BTN_BACK:
        await go_back(update, context)
        return

    if text == BTN_CANCEL:
        await cancel(update, context)
        return

    if text == "✅ ورود":
        await checkin(update, context)
        return
    if text == "❌ خروج":
        await checkout(update, context)
        return
    if text == "📍 وضعیت امروز":
        await today_status(update, context)
        return
    if text == "📝 ثبت توضیح":
        await ask_note(update, context)
        return
    if text == "📊 گزارش امروز":
        await report_today(update, context)
        return
    if text == "📌 دیدن توضیحات پرسنل":
        await view_notes(update, context)
        return
    if text == "📝 پیام مدیر برای شیفت":
        # handled by conversation handler entry point
        return
    if text == "ℹ️ راهنما":
        await help_cmd(update, context)
        return

    if text in (BTN_EMPLOYEE, BTN_ADMIN):
        await role_selected(update, context)
        return

    await update.message.repl
