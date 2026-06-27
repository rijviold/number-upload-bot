"""Number Upload Bot.

A separate Telegram bot where the admin uploads phone-number files for a chosen
panel. Numbers are parsed and stored (tagged by panel). Panel APIs and OTP
fetching are added later, one panel at a time.

Runtime config (env vars):
- TELEGRAM_BOT_TOKEN : bot token from BotFather (required)
- ADMIN_IDS          : comma-separated Telegram user IDs (default: 7430635878)
- DATA_DIR           : where the SQLite db lives (default: this folder)
- PORT               : health server port (Railway sets this; default 5000)
"""

import os
import re
import json
import sqlite3
import logging
import threading
from pathlib import Path
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
)
logger = logging.getLogger("upload_bot")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
ADMIN_IDS = {
    int(x)
    for x in os.environ.get("ADMIN_IDS", "7430635878").replace(" ", "").split(",")
    if x
}
DB_PATH = Path(os.environ.get("DATA_DIR", str(Path(__file__).parent))) / "upload_bot.db"
PROVIDERS_PATH = Path(__file__).parent / "providers.json"

MAX_FILE_BYTES = 5 * 1024 * 1024  # 5 MB


# --------------------------------------------------------------------------- #
# Providers (panels)
# --------------------------------------------------------------------------- #
def load_providers():
    try:
        with open(PROVIDERS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("panels", [])
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def get_provider(panel_id):
    for p in load_providers():
        if p.get("id") == panel_id:
            return p
    return None


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #
def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS numbers (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                panel_id  TEXT NOT NULL,
                number    TEXT NOT NULL,
                status    TEXT NOT NULL DEFAULT 'available',
                added_at  TEXT NOT NULL,
                UNIQUE(panel_id, number)
            )
            """
        )
        conn.commit()


def add_numbers(panel_id, numbers):
    added, dup = 0, 0
    now = datetime.now(timezone.utc).isoformat()
    with _db() as conn:
        for n in numbers:
            try:
                conn.execute(
                    "INSERT INTO numbers(panel_id, number, status, added_at) "
                    "VALUES (?, ?, 'available', ?)",
                    (panel_id, n, now),
                )
                added += 1
            except sqlite3.IntegrityError:
                dup += 1
        conn.commit()
    return added, dup


def stock_counts():
    with _db() as conn:
        return conn.execute(
            "SELECT panel_id, COUNT(*) AS total, "
            "SUM(CASE WHEN status='available' THEN 1 ELSE 0 END) AS avail "
            "FROM numbers GROUP BY panel_id ORDER BY panel_id"
        ).fetchall()


def delete_panel_numbers(panel_id):
    with _db() as conn:
        cur = conn.execute("DELETE FROM numbers WHERE panel_id = ?", (panel_id,))
        conn.commit()
        return cur.rowcount


# --------------------------------------------------------------------------- #
# Number parsing
# --------------------------------------------------------------------------- #
def parse_numbers(raw_bytes):
    """Extract phone numbers from a .txt/.csv file.

    Returns (unique_numbers, invalid_count). A token is "invalid" only when it
    contains digits but is not a plausible phone-number length (7-15 digits).
    Pure-text tokens (e.g. CSV headers) are ignored, not counted as invalid.
    """
    text = raw_bytes.decode("utf-8", errors="ignore")
    found, seen = [], set()
    invalid = 0
    for token in re.split(r"[\s,;|]+", text):
        token = token.strip()
        if not token:
            continue
        digits = re.sub(r"\D", "", token)
        if not digits:
            continue
        if 7 <= len(digits) <= 15:
            if digits not in seen:
                seen.add(digits)
                found.append(digits)
        else:
            invalid += 1
    return found, invalid


# --------------------------------------------------------------------------- #
# Keyboards
# --------------------------------------------------------------------------- #
def is_admin(uid):
    return uid in ADMIN_IDS


def main_menu(uid):
    rows = [["📞 নম্বর নিন", "📊 আমার তথ্য"], ["ℹ️ সাহায্য"]]
    if is_admin(uid):
        rows.append(["🛠 অ্যাডমিন প্যানেল"])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def admin_panel_kb():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📤 নম্বর আপলোড করুন", callback_data="up_start")],
            [InlineKeyboardButton("📦 স্টক দেখুন", callback_data="up_stock")],
            [InlineKeyboardButton("🗑️ নম্বর মুছুন", callback_data="up_delmenu")],
        ]
    )


def panel_select_kb(action):
    rows, row = [], []
    for p in load_providers():
        row.append(InlineKeyboardButton(p["name"], callback_data=f"{action}:{p['id']}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("🔙 ব্যাক", callback_data="up_back")])
    return InlineKeyboardMarkup(rows)


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_text(
        "👋 স্বাগতম!\n\nএটি নম্বর আপলোড বট।\n"
        + ("🛠 আপনি অ্যাডমিন — নিচের মেনু থেকে নম্বর আপলোড করতে পারবেন।"
           if is_admin(uid) else "নিচের মেনু ব্যবহার করুন।"),
        reply_markup=main_menu(uid),
    )


async def cmd_myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🆔 আপনার Telegram ID: <code>{update.effective_user.id}</code>",
        parse_mode="HTML",
    )


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ এই অংশ শুধু অ্যাডমিনের জন্য।")
        return
    await update.message.reply_text(
        "🛠 <b>অ্যাডমিন প্যানেল</b>", parse_mode="HTML", reply_markup=admin_panel_kb()
    )


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    data = q.data or ""

    if not is_admin(uid):
        await q.edit_message_text("⛔ শুধু অ্যাডমিন।")
        return

    if data == "up_back":
        await q.edit_message_text(
            "🛠 <b>অ্যাডমিন প্যানেল</b>", parse_mode="HTML", reply_markup=admin_panel_kb()
        )

    elif data == "up_start":
        if not load_providers():
            await q.edit_message_text(
                "⚠️ এখনো কোনো প্যানেল যোগ করা হয়নি।\n"
                "API যোগ করা হলে এখানে প্যানেলগুলো দেখা যাবে।",
                reply_markup=admin_panel_kb(),
            )
            return
        await q.edit_message_text(
            "📤 ফাইলটা কোন প্যানেলের? নিচ থেকে বেছে নিন 👇",
            reply_markup=panel_select_kb("upload"),
        )

    elif data == "up_delmenu":
        if not load_providers():
            await q.edit_message_text("⚠️ কোনো প্যানেল নেই।", reply_markup=admin_panel_kb())
            return
        await q.edit_message_text(
            "🗑️ কোন প্যানেলের নম্বর মুছবেন? 👇",
            reply_markup=panel_select_kb("del"),
        )

    elif data == "up_stock":
        rows = stock_counts()
        names = {p["id"]: p["name"] for p in load_providers()}
        if not rows:
            await q.edit_message_text("📦 স্টক খালি।", reply_markup=admin_panel_kb())
            return
        lines = ["📦 <b>স্টক</b>\n"]
        for r in rows:
            nm = names.get(r["panel_id"], r["panel_id"])
            lines.append(f"• {nm}: মোট {r['total']}টি · উপলব্ধ {r['avail']}টি")
        await q.edit_message_text(
            "\n".join(lines), parse_mode="HTML", reply_markup=admin_panel_kb()
        )

    elif data.startswith("upload:"):
        pid = data.split(":", 1)[1]
        p = get_provider(pid)
        if not p:
            await q.edit_message_text("⚠️ প্যানেল পাওয়া যায়নি।", reply_markup=admin_panel_kb())
            return
        context.user_data["awaiting_upload_panel"] = pid
        await q.edit_message_text(
            f"✅ প্যানেল: <b>{p['name']}</b>\n\n"
            "এবার নম্বরের ফাইলটা পাঠান 📎\n"
            "(📎 আইকন → <b>File</b> → .txt বা .csv ফাইল বেছে নিন)",
            parse_mode="HTML",
        )

    elif data.startswith("del:"):
        pid = data.split(":", 1)[1]
        p = get_provider(pid)
        removed = delete_panel_numbers(pid)
        nm = p["name"] if p else pid
        await q.edit_message_text(
            f"🗑️ <b>{nm}</b> — {removed}টি নম্বর মুছে ফেলা হয়েছে।",
            parse_mode="HTML",
            reply_markup=admin_panel_kb(),
        )


async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    pid = context.user_data.get("awaiting_upload_panel")

    if not pid:
        if is_admin(uid):
            await update.message.reply_text(
                "ℹ️ ফাইল আপলোড করতে আগে 🛠 অ্যাডমিন প্যানেল → 📤 নম্বর আপলোড করুন → "
                "প্যানেল সিলেক্ট করুন, তারপর ফাইল পাঠান।"
            )
        return

    if not is_admin(uid):
        return

    p = get_provider(pid)
    if not p:
        context.user_data.pop("awaiting_upload_panel", None)
        await update.message.reply_text("⚠️ প্যানেল পাওয়া যায়নি। আবার চেষ্টা করুন।")
        return

    doc = update.message.document
    if doc.file_size and doc.file_size > MAX_FILE_BYTES:
        await update.message.reply_text("⚠️ ফাইলটা খুব বড় (৫MB-এর বেশি)। ছোট ফাইল পাঠান।")
        return

    tg_file = await doc.get_file()
    raw = await tg_file.download_as_bytearray()
    numbers, invalid = parse_numbers(bytes(raw))

    if not numbers:
        await update.message.reply_text(
            "⚠️ ফাইলে কোনো বৈধ নম্বর পাওয়া যায়নি। ফরম্যাট দেখে আবার পাঠান।"
        )
        return

    added, dup = add_numbers(pid, numbers)
    context.user_data.pop("awaiting_upload_panel", None)
    await update.message.reply_text(
        f"✅ <b>{p['name']}</b> — আপলোড সম্পন্ন\n\n"
        f"📄 ফাইল: {doc.file_name}\n"
        f"🔢 ফাইলে বৈধ নম্বর: {len(numbers)}টি\n"
        f"➕ নতুন যোগ হয়েছে: {added}টি\n"
        f"♻️ আগে থেকেই ছিল (বাদ): {dup}টি\n"
        f"❌ ভুল ফরম্যাট (বাদ): {invalid}টি",
        parse_mode="HTML",
        reply_markup=admin_panel_kb(),
    )


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").strip()
    uid = update.effective_user.id
    if txt == "🛠 অ্যাডমিন প্যানেল":
        if is_admin(uid):
            await update.message.reply_text(
                "🛠 <b>অ্যাডমিন প্যানেল</b>",
                parse_mode="HTML",
                reply_markup=admin_panel_kb(),
            )
        else:
            await update.message.reply_text("⛔ শুধু অ্যাডমিন।")
    elif txt == "📞 নম্বর নিন":
        await update.message.reply_text("⏳ এই ফিচারটি শীঘ্রই আসছে (API যোগ হলে চালু হবে)।")
    elif txt == "📊 আমার তথ্য":
        await update.message.reply_text(f"🆔 আপনার ID: {uid}")
    elif txt == "ℹ️ সাহায্য":
        await update.message.reply_text(
            "ℹ️ /start দিয়ে শুরু করুন।\n"
            "অ্যাডমিন হলে 🛠 অ্যাডমিন প্যানেল থেকে নম্বর আপলোড করতে পারবেন।\n"
            "/myid দিয়ে নিজের Telegram ID দেখতে পারবেন।"
        )


# --------------------------------------------------------------------------- #
# Health server (Railway worker — harmless, binds PORT if provided)
# --------------------------------------------------------------------------- #
class _Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, *args):
        pass


def start_health_server():
    port = int(os.environ.get("PORT", "5000"))
    try:
        srv = ThreadingHTTPServer(("0.0.0.0", port), _Health)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        logger.info("Health server listening on %s", port)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Health server failed to start: %s", exc)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    if not BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set")

    init_db()
    start_health_server()

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("myid", cmd_myid))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    logger.info("Upload bot starting (admins=%s)...", ADMIN_IDS)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
