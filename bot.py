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
    ContextTypes,
    CallbackQueryHandler,
    filters,
)

# =============================================================================
# CONFIG
# =============================================================================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN is missing! Set it in Render Environment Variables.")

RENDER_URL = os.getenv("RENDER_URL", "").strip().rstrip("/")
if not RENDER_URL:
    raise ValueError("❌ RENDER_URL is missing! Set it in Render Environment Variables.")

PORT = int(os.getenv("PORT", 10000))
DB_NAME = "attendance.db"

# ---------------------------
# Roles
# ---------------------------
REAL_MANAGERS = {97965212, 1035761242}      # Parham + Tohiid
SUPERUSER = {6017492841}                   # You (Full Access)
ADMIN_USERS = REAL_MANAGERS | SUPERUSER

# ---------------------------
# Shift constants (fixed hours)
# ---------------------------
SHIFTS = [
    (1, "شیفت 1", "08:00", "16:00"),
    (2, "شیفت 2", "16:00", "24:00"),
    (3, "شیفت 3", "00:00", "08:00"),
]

# ---------------------------
# Reminders / Reports
# ---------------------------
REMINDER_MINUTES_BEFORE_SHIFT = 15
LATE_ALERT_MINUTES_AFTER_SHIFT_START = 5

NIGHTLY_REPORT_HOUR = 23
NIGHTLY_REPORT_MINUTE = 59

# =============================================================================
# Flask app (Webhook + Keepalive)
# =============================================================================
app = Flask(__name__)

@app.get("/")
def home():
    return "✅ Bot is running (Render keep-alive OK)", 200

# =============================================================================
# Database
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
# Keyboards
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
    role = "سوپر یوزر" if user_id in SUPERUSER else "مدیر"
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

def ikb_leave_approve_reject(req_id: int):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ تایید", callback_data=f"leave_approve:{req_id}"),
            InlineKeyboardButton("❌ رد", callback_data=f"leave_reject:{req_id}"),
        ]
    ])

# =============================================================================
# Conversation states
# =============================================================================
REG_FULLNAME, EMP_NOTE, LEAVE_REASON, MANAGER_NOTE, ASSIGN_SHIFT_USER, ASSIGN_SHIFT_SHIFT = range(6)

# =============================================================================
# Helpers
# =============================================================================
async def notify_admins(context: ContextTypes.DEFAULT_TYPE, text: str):
    for mid in ADMIN_USERS:
        try:
            await context.bot.send_message(chat_id=mid, text=text)
        except:
            pass

async def notify_real_managers(context: ContextTypes.DEFAULT_TYPE, text: str):
    for mid in REAL_MANAGERS:
        try:
            await context.bot.send_message(chat_id=mid, text=text)
        except:
            pass

async def check_employee_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    if user.id in ADMIN_USERS:
        return True

    status = get_employee_status(user.id)
    if status != "approved":
        await update.message.reply_text("⛔ هنوز تایید نشده‌ای. ابتدا «ثبت‌نام کارمند» را انجام بده.", reply_markup=kb_employee(user.id))
        return False
    return True

# =============================================================================
# Start / Help
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
    "✅ نکته: کارمند جدید فقط یکبار باید «ثبت‌نام کارمند» را انجام دهد."
)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME_TEXT, reply_markup=kb_main(update.effective_user.id))

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, reply_markup=kb_main(update.effective_user.id))

# =============================================================================
# Panels
# =============================================================================
async def employee_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.message.reply_text("👤 پنل کارمند", reply_markup=kb_employee(user.id))

async def manager_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_USERS:
        await update.message.reply_text("❌ فقط مدیر دسترسی دارد.", reply_markup=kb_main(user.id))
        return
    role = "سوپر یوزر" if user.id in SUPERUSER else "مدیر"
    await update.message.reply_text(f"👨‍💼 پنل {role}", reply_markup=kb_manager(user.id))

# =============================================================================
# Employee Registration
# =============================================================================
async def register_employee_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id in ADMIN_USERS:
        await update.message.reply_text("✅ شما مدیر هستید و نیازی به ثبت‌نام ندارید.", reply_markup=kb_employee(user.id))
        return ConversationHandler.END

    status = get_employee_status(user.id)
    if status == "approved":
        await update.message.reply_text("✅ شما قبلاً تایید شده‌اید.", reply_markup=kb_employee(user.id))
        return ConversationHandler.END

    await update.message.reply_text("📝 لطفاً نام و نام خانوادگی خود را وارد کن (مثلاً: علی رضایی):")
    return REG_FULLNAME

async def register_employee_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    full_name = update.message.text.strip()

    upsert_employee(
        user_id=user.id,
        username=user.username or "",
        full_name=full_name,
        status="pending"
    )

    await update.message.reply_text("✅ ثبت‌نام انجام شد. منتظر تایید مدیر باشید.", reply_markup=kb_employee(user.id))

    msg = (
        "👤 درخواست ثبت‌نام کارمند\n\n"
        f"نام: {full_name}\n"
        f"ID: {user.id}\n"
    )
    if user.username:
        msg += f"یوزرنیم: @{user.username}\n"
    msg += "\n✅ تایید / ❌ رد ؟"

    for mid in ADMIN_USERS:
        try:
            await context.bot.send_message(chat_id=mid, text=msg, reply_markup=ikb_approve_reject(user.id))
        except:
            pass

    return ConversationHandler.END

async def approve_reject_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.from_user.id not in ADMIN_USERS:
        await query.edit_message_text("❌ فقط مدیر اجازه تایید/رد دارد.")
        return

    action, emp_id_str = query.data.split(":")
    emp_id = int(emp_id_str)

    if action == "approve":
        set_employee_status(emp_id, "approved")
        await query.edit_message_text("✅ تایید شد.")
        try:
            await context.bot.send_message(chat_id=emp_id, text="✅ حساب شما تایید شد. خوش آمدید 🌟")
        except:
            pass

    elif action == "reject":
        set_employee_status(emp_id, "rejected")
        await query.edit_message_text("❌ رد شد.")
        try:
            await context.bot.send_message(chat_id=emp_id, text="❌ درخواست شما تایید نشد.")
        except:
            pass

# =============================================================================
# Manager Features
# =============================================================================
async def manager_pending_employees(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_USERS:
        return

    pendings = list_pending_employees()
    if not pendings:
        await update.message.reply_text("✅ هیچ درخواست در انتظار تایید نداریم.", reply_markup=kb_manager(update.effective_user.id))
        return

    await update.message.reply_text("🔔 درخواست‌های در انتظار تایید:", reply_markup=kb_manager(update.effective_user.id))
    for emp_id, username, full_name in pendings:
        msg = f"👤 {full_name}\nID: {emp_id}"
        if username:
            msg += f"\n@{username}"
        await update.message.reply_text(msg, reply_markup=ikb_approve_reject(emp_id))

async def list_employees(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_USERS:
        return

    emps = list_approved_employees()
    if not emps:
        await update.message.reply_text("❌ هنوز کارمندی تایید نشده.", reply_markup=kb_manager(update.effective_user.id))
        return

    text = "🧾 لیست کارمندهای تایید شده:\n\n"
    for uid, username, full_name in emps:
        text += f"• {full_name} | ID: {uid}"
        if username:
            text += f" | @{username}"
        shift_id = get_employee_shift(uid)
        if shift_id:
            s = get_shift_by_id(shift_id)
            text += f" | {s[1]} ({s[2]}-{s[3]})"
        text += "\n"

    await update.message.reply_text(text, reply_markup=kb_manager(update.effective_user.id))

# =============================================================================
# Shift Assignment
# =============================================================================
async def assign_shift_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_USERS:
        return ConversationHandler.END

    emps = list_approved_employees()
    if not emps:
        await update.message.reply_text("❌ کارمندی تایید نشده.", reply_markup=kb_manager(update.effective_user.id))
        return ConversationHandler.END

    text = "🗓️ تعیین/تغییر شیفت کارمند\n\nیک کارمند را با ID ارسال کن:\n\n"
    for uid, username, full_name in emps:
        text += f"• {full_name} | ID: {uid}\n"
    text += "\n(مثلاً: 123456789)"

    await update.message.reply_text(text, reply_markup=kb_manager(update.effective_user.id))
    return ASSIGN_SHIFT_USER

async def assign_shift_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    if not txt.isdigit():
        await update.message.reply_text("❌ لطفاً فقط ID عددی بفرست.", reply_markup=kb_manager(update.effective_user.id))
        return ASSIGN_SHIFT_USER

    context.user_data["assign_user_id"] = int(txt)

    kb = ReplyKeyboardMarkup(
        [[KeyboardButton("1"), KeyboardButton("2"), KeyboardButton("3")],
         [KeyboardButton("⬅️ بازگشت")]],
        resize_keyboard=True
    )

    await update.message.reply_text("شماره شیفت را انتخاب کن (1/2/3):", reply_markup=kb)
    return ASSIGN_SHIFT_SHIFT

async def assign_shift_shift(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    if txt == "⬅️ بازگشت":
        await update.message.reply_text("بازگشت به پنل مدیر.", reply_markup=kb_manager(update.effective_user.id))
        return ConversationHandler.END

    if txt not in ["1", "2", "3"]:
        await update.message.reply_text("❌ فقط 1 یا 2 یا 3 بفرست.", reply_markup=kb_manager(update.effective_user.id))
        return ASSIGN_SHIFT_SHIFT

    emp_id = context.user_data.get("assign_user_id")
    shift_id = int(txt)

    set_employee_shift(emp_id, shift_id)
    s = get_shift_by_id(shift_id)

    await update.message.reply_text(
        f"✅ شیفت کارمند تنظیم شد: {s[1]} ({s[2]}-{s[3]})",
        reply_markup=kb_manager(update.effective_user.id)
    )

    try:
        await context.bot.send_message(chat_id=emp_id, text=f"📌 شیفت شما تغییر کرد:\n\n{s[1]} ({s[2]}-{s[3]}) ✅")
    except:
        pass

    return ConversationHandler.END

# =============================================================================
# Employee My Shift
# =============================================================================
async def my_shift(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_employee_access(update, context):
        return

    user = update.effective_user
    shift_id = get_employee_shift(user.id)

    if not shift_id:
        await update.message.reply_text("❌ هنوز شیفت شما توسط مدیر تنظیم نشده.", reply_markup=kb_employee(user.id))
        return

    s = get_shift_by_id(shift_id)
    yday = (datetime.now().date() - timedelta(days=1)).isoformat()

    conn = db()
    c = conn.cursor()

    c.execute("""
        SELECT full_name, note
        FROM shift_notes
        WHERE date=?
        ORDER BY id DESC LIMIT 1
    """, (yday,))
    prev_note = c.fetchone()

    c.execute("""
        SELECT note
        FROM manager_notes
        WHERE date=?
        ORDER BY id DESC LIMIT 1
    """, (yday,))
    mgr_note = c.fetchone()

    conn.close()

    text = (
        f"🕒 شیفت شما:\n\n"
        f"✅ {s[1]}\n"
        f"⏰ ساعت: {s[2]} تا {s[3]}\n\n"
        "📜 توضیح شیفت قبلی:\n"
    )

    if prev_note:
        text += f"👤 {prev_note[0]}\n{prev_note[1]}\n\n"
    else:
        text += "— موردی ثبت نشده.\n\n"

    text += "📝 پیام مدیر:\n"
    text += mgr_note[0] if mgr_note else "— پیامی ثبت نشده."

    await update.message.reply_text(text, reply_markup=kb_employee(user.id))

# =============================================================================
# Attendance
# =============================================================================
def get_today_attendance(user_id: int, date_str: str):
    conn = db()
    c = conn.cursor()
    c.execute("""
        SELECT id, shift_id, check_in_time, check_out_time, delay_minutes
        FROM attendance
        WHERE date=? AND user_id=?
        ORDER BY id DESC LIMIT 1
    """, (date_str, user_id))
    row = c.fetchone()
    conn.close()
    return row

async def employee_check_in(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_employee_access(update, context):
        return

    user = update.effective_user
    date_str = get_today_str()
    shift_id = get_employee_shift(user.id)

    if not shift_id:
        await update.message.reply_text("❌ شیفت شما هنوز تنظیم نشده. با مدیر تماس بگیرید.", reply_markup=kb_employee(user.id))
        return

    existing = get_today_attendance(user.id, date_str)
    if existing and existing[2]:
        await update.message.reply_text("✅ ورود شما قبلاً ثبت شده است.", reply_markup=kb_employee(user.id))
        return

    shift = get_shift_by_id(shift_id)
    now = datetime.now()
    shift_start_dt = datetime.combine(now.date(), parse_hhmm(shift[2]))
    delay = max(0, int((now - shift_start_dt).total_seconds() // 60))

    conn = db()
    c = conn.cursor()
    c.execute("""
        INSERT INTO attendance (date, user_id, full_name, shift_id, check_in_time, delay_minutes)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (date_str, user.id, get_employee_full_name(user.id) or user.full_name, shift_id,
          now.isoformat(timespec="seconds"), delay))
    conn.commit()
    conn.close()

    await update.message.reply_text(
        f"✅ ورود ثبت شد!\n\n"
        f"👤 {get_employee_full_name(user.id) or user.full_name}\n"
        f"🕒 {shift[1]} ({shift[2]}-{shift[3]})\n"
        f"⏱️ تاخیر: {delay} دقیقه",
        reply_markup=kb_employee(user.id)
    )

    await notify_real_managers(
        context,
        f"📌 ثبت ورود\n\n👤 {get_employee_full_name(user.id) or user.full_name}\n🗓️ {date_str}\n🕒 {shift[1]}\n⏱️ تاخیر: {delay} دقیقه"
    )

async def employee_check_out(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_employee_access(update, context):
        return

    user = update.effective_user
    date_str = get_today_str()

    row = get_today_attendance(user.id, date_str)
    if not row or not row[2]:
        await update.message.reply_text("❌ هنوز ورود ثبت نکرده‌اید.", reply_markup=kb_employee(user.id))
        return
    if row[3]:
        await update.message.reply_text("✅ خروج شما قبلاً ثبت شده است.", reply_markup=kb_employee(user.id))
        return

    now = datetime.now()

    conn = db()
    c = conn.cursor()
    c.execute("UPDATE attendance SET check_out_time=? WHERE id=?", (now.isoformat(timespec="seconds"), row[0]))
    conn.commit()
    conn.close()

    await update.message.reply_text("✅ خروج ثبت شد. خسته نباشی 🌟", reply_markup=kb_employee(user.id))
    await notify_real_managers(
        context,
        f"✅ ثبت خروج\n\n👤 {get_employee_full_name(user.id) or user.full_name}\n🗓️ {date_str}\n🕒 ساعت: {now.strftime('%H:%M')}"
    )

async def employee_status_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_employee_access(update, context):
        return

    user = update.effective_user
    date_str = get_today_str()
    shift_id = get_employee_shift(user.id)

    att = get_today_attendance(user.id, date_str)

    text = f"📍 وضعیت امروز ({date_str})\n\n"
    if shift_id:
        s = get_shift_by_id(shift_id)
        text += f"🕒 شیفت: {s[1]} ({s[2]}-{s[3]})\n\n"
    else:
        text += "🕒 شیفت: تعیین نشده\n\n"

    if att:
        text += f"✅ ورود: {att[2]}\n"
        text += f"❌ خروج: {att[3] or 'ثبت نشده'}\n"
        text += f"⏱️ تاخیر: {att[4]} دقیقه\n"
    else:
        text += "❌ ورود ثبت نشده.\n"

    await update.message.reply_text(text, reply_markup=kb_employee(user.id))

# =============================================================================
# Notes (handover)
# =============================================================================
async def employee_note_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_employee_access(update, context):
        return ConversationHandler.END
    await update.message.reply_text("✍️ توضیح خود را برای شیفت بعد بنویس:")
    return EMP_NOTE

async def employee_note_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text.strip()
    date_str = get_today_str()
    shift_id = get_employee_shift(user.id)

    conn = db()
    c = conn.cursor()
    c.execute("""
        INSERT INTO shift_notes (date, user_id, full_name, shift_id, note, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (date_str, user.id, get_employee_full_name(user.id) or user.full_name,
          shift_id or 0, text, datetime.now().isoformat(timespec="seconds")))
    conn.commit()
    conn.close()

    await update.message.reply_text("✅ توضیح ثبت شد و به مدیر ارسال شد.", reply_markup=kb_employee(user.id))
    await notify_real_managers(context, f"📝 توضیح شیفت بعد\n\n👤 {get_employee_full_name(user.id) or user.full_name}\n🗓️ {date_str}\n\n{text}")

    return ConversationHandler.END

async def previous_shift_notes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_employee_access(update, context):
        return

    yday = (datetime.now().date() - timedelta(days=1)).isoformat()

    conn = db()
    c = conn.cursor()

    c.execute("""
        SELECT full_name, shift_id, note
        FROM shift_notes
        WHERE date=?
        ORDER BY id DESC LIMIT 1
    """, (yday,))
    row = c.fetchone()

    c.execute("""
        SELECT note
        FROM manager_notes
        WHERE date=?
        ORDER BY id DESC LIMIT 1
    """, (yday,))
    mgr = c.fetchone()

    conn.close()

    text = "📜 توضیحات شیفت قبلی:\n\n"
    if row:
        text += f"👤 {row[0]} | شیفت {row[1]}\n\n{row[2]}\n\n"
    else:
        text += "— موردی ثبت نشده.\n\n"

    text += "📝 پیام مدیر:\n\n"
    text += mgr[0] if mgr else "— پیامی ثبت نشده."

    await update.message.reply_text(text, reply_markup=kb_employee(update.effective_user.id))

# =============================================================================
# Leave
# =============================================================================
async def leave_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_employee_access(update, context):
        return ConversationHandler.END
    await update.message.reply_text("🏖️ دلیل مرخصی را بنویس:")
    return LEAVE_REASON

async def leave_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    reason = update.message.text.strip()
    date_str = get_today_str()

    conn = db()
    c = conn.cursor()
    c.execute("""
        INSERT INTO leave_requests (date, user_id, full_name, reason, status, created_at)
        VALUES (?, ?, ?, ?, 'pending', ?)
    """, (date_str, user.id, get_employee_full_name(user.id) or user.full_name, reason,
          datetime.now().isoformat(timespec="seconds")))
    req_id = c.lastrowid
    conn.commit()
    conn.close()

    await update.message.reply_text("✅ درخواست مرخصی ثبت شد و به مدیر ارسال شد.", reply_markup=kb_employee(user.id))

    msg = f"🏖️ درخواست مرخصی\n\n👤 {get_employee_full_name(user.id) or user.full_name}\n🗓️ {date_str}\n\n📌 دلیل:\n{reason}"
    for mid in ADMIN_USERS:
        try:
            await context.bot.send_message(chat_id=mid, text=msg, reply_markup=ikb_leave_approve_reject(req_id))
        except:
            pass

    return ConversationHandler.END

async def leave_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.from_user.id not in ADMIN_USERS:
        await query.edit_message_text("❌ فقط مدیر اجازه دارد.")
        return

    action, req_id_str = query.data.split(":")
    req_id = int(req_id_str)

    conn = db()
    c = conn.cursor()
    c.execute("SELECT user_id, full_name, date FROM leave_requests WHERE id=?", (req_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        await query.edit_message_text("❌ درخواست پیدا نشد.")
        return

    emp_id, full_name, date_str = row

    if action == "leave_approve":
        c.execute("UPDATE leave_requests SET status='approved' WHERE id=?", (req_id,))
        conn.commit()
        conn.close()
        await query.edit_message_text("✅ مرخصی تایید شد.")
        try:
            await context.bot.send_message(chat_id=emp_id, text=f"✅ مرخصی شما برای {date_str} تایید شد.")
        except:
            pass

    elif action == "leave_reject":
        c.execute("UPDATE leave_requests SET status='rejected' WHERE id=?", (req_id,))
        conn.commit()
        conn.close()
        await query.edit_message_text("❌ مرخصی رد شد.")
        try:
            await context.bot.send_message(chat_id=emp_id, text=f"❌ مرخصی شما برای {date_str} رد شد.")
        except:
            pass

# =============================================================================
# Manager note + report
# =============================================================================
async def manager_note_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_USERS:
        return ConversationHandler.END
    await update.message.reply_text("📝 پیام مدیر را بنویس:")
    return MANAGER_NOTE

async def manager_note_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    date_str = get_today_str()

    conn = db()
    c = conn.cursor()
    c.execute("""
        INSERT INTO manager_notes (date, note, created_at)
        VALUES (?, ?, ?)
    """, (date_str, text, datetime.now().isoformat(timespec="seconds")))
    conn.commit()
    conn.close()

    await update.message.reply_text("✅ پیام مدیر ثبت شد.", reply_markup=kb_manager(update.effective_user.id))
    await notify_admins(context, f"📝 پیام مدیر ثبت شد:\n\n{text}")
    return ConversationHandler.END

async def manager_report_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_USERS:
        return

    date_str = get_today_str()

    conn = db()
    c = conn.cursor()
    c.execute("""
        SELECT full_name, shift_id, check_in_time, check_out_time, delay_minutes
        FROM attendance
        WHERE date=?
        ORDER BY shift_id, full_name
    """, (date_str,))
    rows = c.fetchall()

    c.execute("""
        SELECT full_name, reason, status
        FROM leave_requests
        WHERE date=?
        ORDER BY created_at DESC
    """, (date_str,))
    leaves = c.fetchall()
    conn.close()

    text = f"📊 گزارش امروز ({date_str})\n\n"
    if rows:
        text += "✅ حضور و غیاب:\n"
        for full_name, shift_id, cin, cout, delay in rows:
            cin_t = cin.split("T")[-1] if cin else "—"
            cout_t = cout.split("T")[-1] if cout else "—"
            text += f"• {full_name} | شیفت {shift_id} | ورود: {cin_t} | خروج: {cout_t} | تاخیر: {delay}m\n"
    else:
        text += "— هنوز ورود/خروج ثبت نشده.\n"

    text += "\n🏖️ مرخصی‌ها:\n"
    if leaves:
        for full_name, reason, status in leaves:
            text += f"• {full_name} | {status} | {reason}\n"
    else:
        text += "— موردی ثبت نشده.\n"

    await update.message.reply_text(text, reply_markup=kb_manager(update.effective_user.id))

# =============================================================================
# Jobs: reminders + late alert + nightly report
# =============================================================================
async def job_shift_reminder(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now()
    for shift_id, shift_name, start_hhmm, end_hhmm in SHIFTS:
        start_dt = datetime.combine(now.date(), parse_hhmm(start_hhmm))
        remind_dt = start_dt - timedelta(minutes=REMINDER_MINUTES_BEFORE_SHIFT)

        if abs((now - remind_dt).total_seconds()) < 60:
            conn = db()
            c = conn.cursor()
            c.execute("SELECT user_id FROM employee_shifts WHERE shift_id=?", (shift_id,))
            targets = [r[0] for r in c.fetchall()]
            conn.close()

            for uid in targets:
                name = get_employee_full_name(uid) or ""
                try:
                    await context.bot.send_message(
                        chat_id=uid,
                        text=(
                            "⏰ یادآوری شروع شیفت\n\n"
                            f"سلام {name} 🌟\n"
                            f"تا {REMINDER_MINUTES_BEFORE_SHIFT} دقیقه دیگر شیفت شما شروع می‌شود:\n"
                            f"🕒 {shift_name} ({start_hhmm}-{end_hhmm})\n\n"
                            "لطفاً در زمان شروع شیفت «ثبت ورود» را انجام دهید ✅"
                        )
                    )
                except:
                    pass

async def job_late_alert(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now()
    date_str = get_today_str()

    for shift_id, shift_name, start_hhmm, _ in SHIFTS:
        start_dt = datetime.combine(now.date(), parse_hhmm(start_hhmm))
        alert_dt = start_dt + timedelta(minutes=LATE_ALERT_MINUTES_AFTER_SHIFT_START)

        if abs((now - alert_dt).total_seconds()) < 60:
            conn = db()
            c = conn.cursor()

            c.execute("SELECT user_id FROM employee_shifts WHERE shift_id=?", (shift_id,))
            assigned = [r[0] for r in c.fetchall()]

            c.execute("""
                SELECT user_id
                FROM attendance
                WHERE date=? AND shift_id=? AND check_in_time IS NOT NULL
            """, (date_str, shift_id))
            checked = {r[0] for r in c.fetchall()}
            conn.close()

            late_people = [uid for uid in assigned if uid not in checked]
            if late_people:
                names = [get_employee_full_name(uid) or str(uid) for uid in late_people]

                await notify_real_managers(
                    context,
                    f"⚠️ هشدار عدم ثبت ورود\n\n"
                    f"شیفت: {shift_name}\n"
                    f"تا {LATE_ALERT_MINUTES_AFTER_SHIFT_START} دقیقه بعد از شروع شیفت، ورود ثبت نشده برای:\n"
                    + "\n".join([f"• {n}" for n in names])
                )

async def job_nightly_report(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now()
    if now.hour != NIGHTLY_REPORT_HOUR or now.minute != NIGHTLY_REPORT_MINUTE:
        return

    date_str = get_today_str()
    conn = db()
    c = conn.cursor()
    c.execute("""
        SELECT full_name, shift_id, check_in_time, check_out_time, delay_minutes
        FROM attendance
        WHERE date=?
        ORDER BY shift_id, full_name
    """, (date_str,))
    rows = c.fetchall()

    c.execute("""
        SELECT full_name, reason, status
        FROM leave_requests
        WHERE date=?
        ORDER BY created_at DESC
    """, (date_str,))
    leaves = c.fetchall()
    conn.close()

    text = f"📌 گزارش شبانه ({date_str})\n\n"
    if rows:
        text += "✅ حضور و غیاب:\n"
        for full_name, shift_id, cin, cout, delay in rows:
            cin_t = cin.split("T")[-1] if cin else "—"
            cout_t = cout.split("T")[-1] if cout else "—"
            text += f"• {full_name} | شیفت {shift_id} | ورود: {cin_t} | خروج: {cout_t} | تاخیر: {delay}m\n"
    else:
        text += "— هیچ ورودی ثبت نشده.\n"

    text += "\n🏖️ مرخصی‌ها:\n"
    if leaves:
        for full_name, reason, status in leaves:
            text += f"• {full_name} | {status} | {reason}\n"
    else:
        text += "— موردی ثبت نشده.\n"

    await notify_real_managers(context, text)

# =============================================================================
# Router
# =============================================================================
async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    user_id = update.effective_user.id

    if text == "👨‍💼 پنل مدیر":
        return await manager_panel(update, context)

    if text == "👤 پنل کارمند":
        return await employee_panel(update, context)

    if text == "ℹ️ راهنما":
        return await help_cmd(update, context)

    if text == "📌 ثبت‌نام کارمند":
        return await register_employee_start(update, context)

    if text == "🕒 شیفت من":
        return await my_shift(update, context)

    if text == "✅ ثبت ورود":
        return await employee_check_in(update, context)

    if text == "❌ ثبت خروج":
        return await employee_check_out(update, context)

    if text == "📍 وضعیت امروز":
        return await employee_status_today(update, context)

    if text == "✍️ ثبت توضیح برای شیفت بعد":
        return await employee_note_start(update, context)

    if text == "📜 توضیحات شیفت قبلی":
        return await previous_shift_notes(update, context)

    if text == "🏖️ درخواست مرخصی":
        return await leave_start(update, context)

    if text == "👥 تایید کارمندها":
        return await manager_pending_employees(update, context)

    if text == "🧾 لیست کارمندها":
        return await list_employees(update, context)

    if text == "🗓️ تعیین/تغییر شیفت کارمند":
        return await assign_shift_start(update, context)

    if text == "📝 پیام مدیر":
        return await manager_note_start(update, context)

    if text == "📊 گزارش امروز":
        return await manager_report_today(update, context)

    if text == "🏖️ مرخصی‌ها":
        await update.message.reply_text("✅ درخواست‌های مرخصی با دکمه تایید/رد مدیریت می‌شوند.", reply_markup=kb_manager(user_id))
        return

    if text == "⬅️ بازگشت به منوی اصلی":
        await update.message.reply_text("✅ منوی اصلی", reply_markup=kb_main(user_id))
        return

    await update.message.reply_text("❓ متوجه نشدم. از دکمه‌ها استفاده کن.", reply_markup=kb_main(user_id))

# =============================================================================
# Telegram webhook setup
# =============================================================================
async def set_webhook(application: Application):
    webhook_url = f"{RENDER_URL}/webhook"
    await application.bot.set_webhook(webhook_url)
    print(f"✅ Webhook set to: {webhook_url}")

# =============================================================================
# Main app
# =============================================================================
application = Application.builder().token(BOT_TOKEN).build()

# Commands
application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("help", help_cmd))

# Callbacks
application.add_handler(CallbackQueryHandler(approve_reject_callback, pattern=r"^(approve|reject):"))
application.add_handler(CallbackQueryHandler(leave_callback, pattern=r"^(leave_approve|leave_reject):"))

# Conversations
application.add_handler(ConversationHandler(
    entry_points=[MessageHandler(filters.Regex("^📌 ثبت‌نام کارمند$"), register_employee_start)],
    states={REG_FULLNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_employee_save)]},
    fallbacks=[],
))

application.add_handler(ConversationHandler(
    entry_points=[MessageHandler(filters.Regex("^✍️ ثبت توضیح برای شیفت بعد$"), employee_note_start)],
    states={EMP_NOTE: [MessageHandler(filters.TEXT & ~filters.COMMAND, employee_note_save)]},
    fallbacks=[],
))

application.add_handler(ConversationHandler(
    entry_points=[MessageHandler(filters.Regex("^🏖️ درخواست مرخصی$"), leave_start)],
    states={LEAVE_REASON: [MessageHandler(filters.TEXT & ~filters.COMMAND, leave_save)]},
    fallbacks=[],
))

application.add_handler(ConversationHandler(
    entry_points=[MessageHandler(filters.Regex("^📝 پیام مدیر$"), manager_note_start)],
    states={MANAGER_NOTE: [MessageHandler(filters.TEXT & ~filters.COMMAND, manager_note_save)]},
    fallbacks=[],
))

application.add_handler(ConversationHandler(
    entry_points=[MessageHandler(filters.Regex("^🗓️ تعیین/تغییر شیفت کارمند$"), assign_shift_start)],
    states={
        ASSIGN_SHIFT_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, assign_shift_user)],
        ASSIGN_SHIFT_SHIFT: [MessageHandler(filters.TEXT & ~filters.COMMAND, assign_shift_shift)],
    },
    fallbacks=[],
))

# Router
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_buttons))

# Jobs
application.job_queue.run_repeating(job_shift_reminder, interval=60, first=10)
application.job_queue.run_repeating(job_late_alert, interval=60, first=20)
application.job_queue.run_repeating(job_nightly_report, interval=60, first=30)

# Flask webhook endpoint
@app.post("/webhook")
def webhook():
    data = request.get_json(force=True)
    asyncio.run(application.update_queue.put(Update.de_json(data, application.bot)))
    return "ok", 200

def run():
    init_db()
    asyncio.get_event_loop().run_until_complete(application.initialize())
    asyncio.get_event_loop().run_until_complete(set_webhook(application))
    asyncio.get_event_loop().run_until_complete(application.start())
    print("✅ Bot started in webhook mode (NO POLLING).")
    app.run(host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    run()
