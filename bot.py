import os
import sqlite3
import asyncio
import threading
from datetime import datetime, timedelta, time as dtime

from dotenv import load_dotenv
from flask import Flask

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

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN is missing! Set it in Render Environment Variables.")

PORT = int(os.getenv("PORT", 10000))

# ---- Role Model (Professional) ----
# Real managers (official)
REAL_MANAGERS = {97965212, 1035761242}

# Superuser (you, creator): has admin access but not considered a real manager
SUPERUSER = {6017492841}

# Admins are those who can access manager panel
ADMIN_USERS = REAL_MANAGERS | SUPERUSER

# ---- DB ----
DB_NAME = "attendance.db"

# ---- Scheduling defaults ----
REMINDER_MINUTES_BEFORE_SHIFT = 15
LATE_ALERT_MINUTES_AFTER_SHIFT_START = 5

# Nightly report (server local time)
NIGHTLY_REPORT_HOUR = 23
NIGHTLY_REPORT_MINUTE = 59

# ---- Shifts ----
SHIFTS = [
    (1, "شیفت 1 (08:00-16:00)", "08:00", "16:00"),
    (2, "شیفت 2 (16:00-24:00)", "16:00", "24:00"),
    (3, "شیفت 3 (00:00-08:00)", "00:00", "08:00"),
]


# =============================================================================
# Flask app (Render keep-alive)
# =============================================================================
app = Flask(__name__)

@app.get("/")
def home():
    return "✅ Bot is running (Render keep-alive OK)", 200


# =============================================================================
# DB helpers
# =============================================================================
def db():
    return sqlite3.connect(DB_NAME)

def init_db():
    conn = db()
    c = conn.cursor()

    # employees: approved / pending / rejected
    c.execute("""
        CREATE TABLE IF NOT EXISTS employees (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            status TEXT DEFAULT 'pending',
            created_at TEXT
        )
    """)

    # shifts master
    c.execute("""
        CREATE TABLE IF NOT EXISTS shifts (
            shift_id INTEGER PRIMARY KEY,
            shift_name TEXT,
            start_time TEXT,
            end_time TEXT
        )
    """)

    # shift assignments
    c.execute("""
        CREATE TABLE IF NOT EXISTS shift_assignments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT,
            user_id INTEGER,
            shift_id INTEGER,
            UNIQUE(date, user_id)
        )
    """)

    # attendance: check-in / check-out
    c.execute("""
        CREATE TABLE IF NOT EXISTS attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT,
            user_id INTEGER,
            username TEXT,
            full_name TEXT,
            shift_id INTEGER,
            check_in_time TEXT,
            check_out_time TEXT,
            delay_minutes INTEGER DEFAULT 0,
            note TEXT DEFAULT ''
        )
    """)

    # shift notes (handover)
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

    # manager notes
    c.execute("""
        CREATE TABLE IF NOT EXISTS manager_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT,
            shift_id INTEGER,
            note TEXT,
            created_at TEXT
        )
    """)

    # leave requests
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

def seed_shifts():
    conn = db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM shifts")
    if c.fetchone()[0] == 0:
        c.executemany("INSERT INTO shifts VALUES (?, ?, ?, ?)", SHIFTS)
        conn.commit()
    conn.close()

def get_employee_status(user_id: int):
    conn = db()
    c = conn.cursor()
    c.execute("SELECT status FROM employees WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def upsert_employee(user_id: int, username: str, full_name: str, status="pending"):
    conn = db()
    c = conn.cursor()
    c.execute("""
        INSERT INTO employees (user_id, username, full_name, status, created_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username=excluded.username,
            full_name=excluded.full_name,
            status=excluded.status
    """, (user_id, username, full_name, status, datetime.now().isoformat()))
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
    c.execute("SELECT user_id, username, full_name FROM employees WHERE status='pending'")
    rows = c.fetchall()
    conn.close()
    return rows

def list_approved_employees():
    conn = db()
    c = conn.cursor()
    c.execute("SELECT user_id, username, full_name FROM employees WHERE status='approved'")
    rows = c.fetchall()
    conn.close()
    return rows

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

def get_assigned_shift(user_id: int, date_str: str):
    conn = db()
    c = conn.cursor()
    c.execute("SELECT shift_id FROM shift_assignments WHERE date=? AND user_id=?", (date_str, user_id))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def assign_shift(user_id: int, shift_id: int, date_str: str):
    conn = db()
    c = conn.cursor()
    c.execute("""
        INSERT OR REPLACE INTO shift_assignments (date, user_id, shift_id)
        VALUES (?, ?, ?)
    """, (date_str, user_id, shift_id))
    conn.commit()
    conn.close()


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
        [KeyboardButton("✅ ثبت ورود"), KeyboardButton("❌ ثبت خروج")],
        [KeyboardButton("✍️ ثبت توضیح برای شیفت بعد"), KeyboardButton("📜 توضیحات شیفت قبلی")],
        [KeyboardButton("🏖️ درخواست مرخصی"), KeyboardButton("📍 وضعیت امروز")],
        [KeyboardButton("⬅️ بازگشت به منوی اصلی")],
    ]
    status = get_employee_status(user_id)
    if user_id not in ADMIN_USERS and status in (None, "pending"):
        rows.insert(0, [KeyboardButton("📌 ارسال ID برای فعال‌سازی")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

def kb_manager(user_id: int):
    # show a badge to superuser
    badge = " (سوپر یوزر)" if user_id in SUPERUSER else ""
    rows = [
        [KeyboardButton("👥 تایید کارمندها"), KeyboardButton("🗓️ تعیین شیفت امروز")],
        [KeyboardButton("📝 پیام مدیر برای شیفت"), KeyboardButton("📊 گزارش امروز")],
        [KeyboardButton("🏖️ مرخصی‌ها"), KeyboardButton("🧾 لیست کارمندها")],
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
EMP_NOTE, LEAVE_REASON, MANAGER_NOTE, ASSIGN_SHIFT_USER, ASSIGN_SHIFT_SHIFT = range(5)


# =============================================================================
# Messaging helpers
# =============================================================================
async def notify_real_managers(context: ContextTypes.DEFAULT_TYPE, text: str):
    """Notify only real managers (not superuser)"""
    for mid in REAL_MANAGERS:
        try:
            await context.bot.send_message(chat_id=mid, text=text)
        except:
            pass

async def notify_admins(context: ContextTypes.DEFAULT_TYPE, text: str):
    """Notify all admins (real managers + superuser)"""
    for mid in ADMIN_USERS:
        try:
            await context.bot.send_message(chat_id=mid, text=text)
        except:
            pass


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
    "• ثبت ورود/خروج\n"
    "• ثبت توضیح برای شیفت بعد\n"
    "• مشاهده توضیحات شیفت قبلی\n"
    "• درخواست مرخصی\n\n"
    "👨‍💼 پنل مدیر:\n"
    "• تایید کارمندها\n"
    "• تعیین شیفت امروز\n"
    "• پیام مدیر برای شیفت‌ها\n"
    "• گزارش‌ها و مرخصی‌ها\n\n"
    "✅ نکته: کارمند جدید باید «ارسال ID» بزند تا مدیر تایید کند."
)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await update.message.reply_text(WELCOME_TEXT, reply_markup=kb_main(user_id))

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await update.message.reply_text(HELP_TEXT, reply_markup=kb_main(user_id))


# =============================================================================
# Employee Registration & Approval
# =============================================================================
async def employee_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    status = get_employee_status(user.id)

    if user.id not in ADMIN_USERS and status == "rejected":
        await update.message.reply_text("❌ دسترسی شما تایید نشده است.", reply_markup=kb_main(user.id))
        return

    await update.message.reply_text("👤 پنل کارمند", reply_markup=kb_employee(user.id))

    if user.id not in ADMIN_USERS and status is None:
        await update.message.reply_text(
            "✅ برای فعال‌سازی حساب کاربری، روی دکمه زیر بزن:\n\n📌 ارسال ID برای فعال‌سازی",
            reply_markup=kb_employee(user.id)
        )

async def send_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    status = get_employee_status(user.id)

    if user.id in ADMIN_USERS:
        await update.message.reply_text("✅ شما دسترسی مدیر دارید و نیازی به ارسال ID ندارید.", reply_markup=kb_employee(user.id))
        return

    if status == "approved":
        await update.message.reply_text("✅ شما قبلاً تایید شده‌اید.", reply_markup=kb_employee(user.id))
        return

    upsert_employee(
        user_id=user.id,
        username=user.username or "",
        full_name=user.full_name,
        status="pending"
    )

    await update.message.reply_text("✅ درخواست شما ثبت شد. منتظر تایید مدیر باشید.", reply_markup=kb_employee(user.id))

    # Notify ONLY real managers (so approval flow is owned by real managers)
    msg = "👤 درخواست ثبت‌نام کارمند\n\n"
    msg += f"نام: {user.full_name}\n"
    if user.username:
        msg += f"یوزرنیم: @{user.username}\n"
    msg += f"ID: {user.id}\n\n✅ تایید / ❌ رد ؟"

    for mid in REAL_MANAGERS:
        try:
            await context.bot.send_message(chat_id=mid, text=msg, reply_markup=ikb_approve_reject(user.id))
        except:
            pass

async def approve_reject_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if user_id not in REAL_MANAGERS:
        await query.edit_message_text("❌ فقط مدیر واقعی اجازه تایید/رد دارد.")
        return

    action, emp_id_str = query.data.split(":")
    emp_id = int(emp_id_str)

    if action == "approve":
        set_employee_status(emp_id, "approved")
        await query.edit_message_text(f"✅ کارمند {emp_id} تایید شد.")
        try:
            await context.bot.send_message(chat_id=emp_id, text="✅ حساب شما تایید شد. خوش آمدید 🌟")
        except:
            pass

    elif action == "reject":
        set_employee_status(emp_id, "rejected")
        await query.edit_message_text(f"❌ کارمند {emp_id} رد شد.")
        try:
            await context.bot.send_message(chat_id=emp_id, text="❌ درخواست شما تایید نشد.")
        except:
            pass


# =============================================================================
# Manager Panel
# =============================================================================
async def manager_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_USERS:
        await update.message.reply_text("❌ فقط مدیر دسترسی دارد.", reply_markup=kb_main(user_id))
        return
    role = "سوپر یوزر" if user_id in SUPERUSER else "مدیر"
    await update.message.reply_text(f"👨‍💼 پنل {role}", reply_markup=kb_manager(user_id))

async def manager_pending_employees(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_USERS:
        return

    pendings = list_pending_employees()
    if not pendings:
        await update.message.reply_text("✅ هیچ درخواست در انتظار تایید نداریم.", reply_markup=kb_manager(update.effective_user.id))
        return

    await update.message.reply_text(f"🔔 {len(pendings)} درخواست در انتظار تایید:", reply_markup=kb_manager(update.effective_user.id))
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
        text += "\n"
    await update.message.reply_text(text, reply_markup=kb_manager(update.effective_user.id))


# =============================================================================
# Shift assignment (Manager)
# =============================================================================
async def assign_shift_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_USERS:
        return ConversationHandler.END

    emps = list_approved_employees()
    if not emps:
        await update.message.reply_text("❌ کارمندی تایید نشده.", reply_markup=kb_manager(update.effective_user.id))
        return ConversationHandler.END

    text = "🗓️ تعیین شیفت امروز\n\nیک کارمند را با ID انتخاب کنید و بفرستید:\n\n"
    for uid, username, full_name in emps:
        text += f"• {full_name} | ID: {uid}\n"
    text += "\n(مثلاً: 123456789)"

    await update.message.reply_text(text, reply_markup=kb_manager(update.effective_user.id))
    return ASSIGN_SHIFT_USER

async def assign_shift_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_USERS:
        return ConversationHandler.END

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
    if update.effective_user.id not in ADMIN_USERS:
        return ConversationHandler.END

    txt = update.message.text.strip()
    if txt == "⬅️ بازگشت":
        await update.message.reply_text("بازگشت به پنل مدیر.", reply_markup=kb_manager(update.effective_user.id))
        return ConversationHandler.END

    if txt not in ["1", "2", "3"]:
        await update.message.reply_text("❌ فقط 1 یا 2 یا 3 بفرست.", reply_markup=kb_manager(update.effective_user.id))
        return ASSIGN_SHIFT_SHIFT

    emp_id = context.user_data.get("assign_user_id")
    shift_id = int(txt)
    date_str = get_today_str()

    assign_shift(emp_id, shift_id, date_str)

    s = get_shift_by_id(shift_id)
    await update.message.reply_text(
        f"✅ شیفت امروز برای {emp_id} تنظیم شد:\n{s[1]}",
        reply_markup=kb_manager(update.effective_user.id)
    )

    try:
        await context.bot.send_message(
            chat_id=emp_id,
            text=f"📌 شیفت امروز شما تنظیم شد:\n\n{s[1]}"
        )
    except:
        pass

    return ConversationHandler.END


# =============================================================================
# Employee access control
# =============================================================================
async def check_access_employee(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    if user.id in ADMIN_USERS:
        return True

    status = get_employee_status(user.id)
    if status != "approved":
        await update.message.reply_text("⛔ هنوز تایید نشده‌ای. ابتدا «ارسال ID» بزن.", reply_markup=kb_employee(user.id))
        return False
    return True


# =============================================================================
# Attendance helpers
# =============================================================================
def get_today_attendance(user_id: int, date_str: str):
    conn = db()
    c = conn.cursor()
    c.execute("""
        SELECT id, shift_id, check_in_time, check_out_time, delay_minutes, note
        FROM attendance
        WHERE date=? AND user_id=?
        ORDER BY id DESC LIMIT 1
    """, (date_str, user_id))
    row = c.fetchone()
    conn.close()
    return row


# =============================================================================
# Employee actions
# =============================================================================
async def employee_check_in(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access_employee(update, context):
        return

    user = update.effective_user
    date_str = get_today_str()
    assigned_shift = get_assigned_shift(user.id, date_str)

    if not assigned_shift:
        await update.message.reply_text("❌ هنوز شیفت امروز شما تعیین نشده است. با مدیر تماس بگیرید.", reply_markup=kb_employee(user.id))
        return

    # Prevent double check-in
    existing = get_today_attendance(user.id, date_str)
    if existing and existing[2]:
        await update.message.reply_text("✅ ورود شما قبلاً ثبت شده است.", reply_markup=kb_employee(user.id))
        return

    # Calculate delay
    shift = get_shift_by_id(assigned_shift)
    shift_start = parse_hhmm(shift[2])
    now = datetime.now()
    shift_start_dt = datetime.combine(now.date(), shift_start)
    delay_minutes = max(0, int((now - shift_start_dt).total_seconds() // 60))

    conn = db()
    c = conn.cursor()
    c.execute("""
        INSERT INTO attendance (date, user_id, username, full_name, shift_id, check_in_time, delay_minutes)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (date_str, user.id, user.username or "", user.full_name, assigned_shift, now.isoformat(timespec="seconds"), delay_minutes))
    conn.commit()
    conn.close()

    await update.message.reply_text(
        f"✅ ورود ثبت شد!\n\n"
        f"👤 {user.full_name}\n"
        f"🗓️ تاریخ: {date_str}\n"
        f"🕒 شیفت: {shift[1]}\n"
        f"⏱️ تاخیر: {delay_minutes} دقیقه",
        reply_markup=kb_employee(user.id)
    )

    # Notify only real managers (professional)
    await notify_real_managers(
        context,
        f"📌 ثبت ورود\n\n👤 {user.full_name}\n🗓️ {date_str}\n🕒 {shift[1]}\n⏱️ تاخیر: {delay_minutes} دقیقه"
    )

async def employee_check_out(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access_employee(update, context):
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
    await notify_real_managers(context, f"✅ ثبت خروج\n\n👤 {user.full_name}\n🗓️ {date_str}\n🕒 ساعت: {now.strftime('%H:%M')}")


# =============================================================================
# Button router
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

    if text == "📌 ارسال ID برای فعال‌سازی":
        return await send_id(update, context)

    if text == "✅ ثبت ورود":
        return await employee_check_in(update, context)

    if text == "❌ ثبت خروج":
        return await employee_check_out(update, context)

    if text == "👥 تایید کارمندها":
        return await manager_pending_employees(update, context)

    if text == "🧾 لیست کارمندها":
        return await list_employees(update, context)

    if text == "🗓️ تعیین شیفت امروز":
        await update.message.reply_text("برای تعیین شیفت امروز از مدیر پنل استفاده کنید.", reply_markup=kb_manager(user_id))
        return

    if text == "⬅️ بازگشت به منوی اصلی":
        await update.message.reply_text("✅ منوی اصلی", reply_markup=kb_main(user_id))
        return

    await update.message.reply_text("❓ متوجه نشدم. از منو استفاده کن.", reply_markup=kb_main(user_id))


# =============================================================================
# Bot main
# =============================================================================
async def bot_main():
    init_db()
    seed_shifts()

    application = Application.builder().token(BOT_TOKEN).build()

    # Commands
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_cmd))

    # Approvals
    application.add_handler(CallbackQueryHandler(approve_reject_callback, pattern=r"^(approve|reject):"))

    # Shift assignment conversations
    application.add_handler(ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^🗓️ تعیین شیفت امروز$"), assign_shift_start)],
        states={
            ASSIGN_SHIFT_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, assign_shift_user)],
            ASSIGN_SHIFT_SHIFT: [MessageHandler(filters.TEXT & ~filters.COMMAND, assign_shift_shift)],
        },
        fallbacks=[],
    ))

    # Buttons router
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_buttons))

    # Start polling
    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    print("✅ Telegram bot polling started!")

    await asyncio.Event().wait()


def run_bot_thread():
    print("🚀 Starting Telegram bot thread...")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(bot_main())


if __name__ == "__main__":
    threading.Thread(target=run_bot_thread, daemon=True).start()

    print(f"✅ Flask running on PORT={PORT}")
    app.run(host="0.0.0.0", port=PORT)
