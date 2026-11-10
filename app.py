
import os
import sqlite3
import threading
import uuid
import urllib.parse
import hmac
from contextlib import contextmanager

import telebot
from telebot import types
from flask import Flask, request, jsonify, redirect

# ================= CONFIG =================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "6150331795"))
POSTBACK_SECRET = os.environ.get("POSTBACK_SECRET", "")
DB = os.environ.get("DATABASE_PATH", "clickcrew.db")

BASE_URL = os.environ.get(
    "PUBLIC_BASE_URL",
    "https://clickcrew-postback.onrender.com"
).rstrip("/")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is missing")

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

# ================= DATABASE =================

@contextmanager
def db():
    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row

    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def init_db():
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS members (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            balance REAL DEFAULT 0,
            joined_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS offers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            url TEXT NOT NULL,
            payout REAL DEFAULT 0,
            active INTEGER DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS conversions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id INTEGER,
            offer_id INTEGER,
            click_id TEXT UNIQUE,
            payout REAL,
            country TEXT,
            os TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS withdrawals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id INTEGER,
            amount REAL,
            status TEXT DEFAULT 'Pending',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS tracking_links (
            token TEXT PRIMARY KEY,
            member_id INTEGER NOT NULL,
            offer_id INTEGER NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS clicks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            click_id TEXT UNIQUE NOT NULL,
            member_id INTEGER NOT NULL,
            offer_id INTEGER NOT NULL,
            clicked_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_clicks_member
        ON clicks(member_id);

        CREATE INDEX IF NOT EXISTS idx_clicks_offer
        ON clicks(offer_id);

        CREATE INDEX IF NOT EXISTS idx_conversions_member
        ON conversions(member_id);
        """)


init_db()

# ================= KEYBOARDS =================

def main_menu(user_id):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("📋 Offers", "👤 My Account")
    kb.row("💰 My Earnings", "🏦 Withdraw")

    if user_id == ADMIN_ID:
        kb.row("⚙️ Admin Panel")

    return kb


def admin_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("➕ Add Offer", "📋 Manage Offers")
    kb.row("👥 Members", "📊 Statistics")
    kb.row("📈 Click Reports")
    kb.row("💸 Withdrawals")
    kb.row("🏠 Main Menu")
    return kb


# ================= MEMBER REGISTRATION =================

@bot.message_handler(commands=["start"])
def start(message):
    uid = message.from_user.id
    username = message.from_user.username or ""

    with db() as con:
        con.execute("""
            INSERT OR IGNORE INTO members (user_id, username)
            VALUES (?, ?)
        """, (uid, username))

        con.execute("""
            UPDATE members SET username=? WHERE user_id=?
        """, (username, uid))

    bot.send_message(
        uid,
        "🎉 Welcome to ClickCrew!\n\n"
        "Earn by promoting available offers.\n"
        "Choose an option below.",
        reply_markup=main_menu(uid)
    )


# ================= MY ACCOUNT =================

@bot.message_handler(
    func=lambda m: m.text and "My Account" in m.text
)
def account(message):
    uid = message.from_user.id

    try:
        with db() as con:
            member = con.execute(
                "SELECT * FROM members WHERE user_id=?",
                (uid,)
            ).fetchone()

            if not member:
                bot.reply_to(message, "Please send /start first.")
                return

            clicks = con.execute(
                "SELECT COUNT(*) FROM clicks WHERE member_id=?",
                (uid,)
            ).fetchone()[0]

            conversions = con.execute(
                "SELECT COUNT(*) FROM conversions WHERE member_id=?",
                (uid,)
            ).fetchone()[0]

            total = con.execute(
                "SELECT COALESCE(SUM(payout),0) "
                "FROM conversions WHERE member_id=?",
                (uid,)
            ).fetchone()[0]

        bot.send_message(
            message.chat.id,
            "👤 MY ACCOUNT\n\n"
            f"🆔 Member ID: {uid}\n"
            f"👤 Username: @{member['username'] or 'Not set'}\n"
            f"📅 Joined: {member['joined_at']}\n\n"
            f"🔗 Total Clicks: {clicks}\n"
            f"✅ Conversions: {conversions}\n"
            f"💰 Total Earnings: ${total:.2f}\n"
            f"💵 Available Balance: ${member['balance']:.2f}"
        )

    except Exception as e:
        print("ACCOUNT ERROR:", repr(e))
        bot.reply_to(message, "Account loading failed.")


# ================= MY EARNINGS =================

@bot.message_handler(func=lambda m: m.text == "💰 My Earnings")
def earnings(message):
    uid = message.from_user.id

    with db() as con:
        total = con.execute(
            "SELECT COALESCE(SUM(payout),0) "
            "FROM conversions WHERE member_id=?",
            (uid,)
        ).fetchone()[0]

        count = con.execute(
            "SELECT COUNT(*) FROM conversions WHERE member_id=?",
            (uid,)
        ).fetchone()[0]

        member = con.execute(
            "SELECT balance FROM members WHERE user_id=?",
            (uid,)
        ).fetchone()

    balance = member["balance"] if member else 0

    bot.reply_to(
        message,
        "💰 MY EARNINGS\n\n"
        f"Total Conversions: {count}\n"
        f"Total Earned: ${total:.2f}\n"
        f"Available Balance: ${balance:.2f}"
    )


# ================= OFFERS =================

@bot.message_handler(func=lambda m: m.text == "📋 Offers")
def show_offers(message):
    uid = message.from_user.id

    with db() as con:
        offers = con.execute(
            "SELECT * FROM offers WHERE active=1 ORDER BY id DESC"
        ).fetchall()

    if not offers:
        bot.reply_to(message, "No active offers available right now.")
        return

    for offer in offers:
        kb = types.InlineKeyboardMarkup()

        kb.add(types.InlineKeyboardButton(
            "🔗 Get Offer Link",
            callback_data=f"offer_{offer['id']}"
        ))

        bot.send_message(
            uid,
            f"📌 {offer['name']}\n"
            f"💵 Payout: ${offer['payout']:.2f}",
            reply_markup=kb
        )


# ================= GENERATE TRACKING LINK =================

@bot.callback_query_handler(func=lambda c: c.data.startswith("offer_"))
def get_offer(call):
    uid = call.from_user.id

    try:
        offer_id = int(call.data.split("_", 1)[1])

        with db() as con:
            offer = con.execute(
                "SELECT * FROM offers WHERE id=? AND active=1",
                (offer_id,)
            ).fetchone()

            member = con.execute(
                "SELECT user_id FROM members WHERE user_id=?",
                (uid,)
            ).fetchone()

            if not offer or not member:
                bot.answer_callback_query(
                    call.id,
                    "Offer unavailable or send /start first."
                )
                return

            # Stable tracking token for this generated link.
            token = uuid.uuid4().hex

            con.execute("""
                INSERT INTO tracking_links
                (token, member_id, offer_id)
                VALUES (?, ?, ?)
            """, (token, uid, offer_id))

        tracked_url = f"{BASE_URL}/go/{token}"

        bot.answer_callback_query(call.id, "Your link is ready!")

        bot.send_message(
            uid,
            f"📌 {offer['name']}\n\n"
            f"Your personal offer link:\n\n"
            f"{tracked_url}\n\n"
            "Copy and promote this link."
        )

    except Exception as e:
        print("OFFER LINK ERROR:", repr(e))
        bot.answer_callback_query(call.id, "Could not generate link.")


# ================= CLICK TRACKING REDIRECT =================

@app.route("/go/<token>", methods=["GET"])
def track_click(token):

    try:
        with db() as con:
            link = con.execute(
                "SELECT * FROM tracking_links WHERE token=?",
                (token,)
            ).fetchone()

            if not link:
                return "Invalid tracking link.", 404

            offer = con.execute(
                "SELECT * FROM offers WHERE id=? AND active=1",
                (link["offer_id"],)
            ).fetchone()

            if not offer:
                return "This offer is no longer available.", 410

            member = con.execute(
                "SELECT user_id FROM members WHERE user_id=?",
                (link["member_id"],)
            ).fetchone()

            if not member:
                return "Member not found.", 404

            # Every actual redirect gets a fresh Click ID.
            click_id = uuid.uuid4().hex

            con.execute("""
                INSERT INTO clicks
                (click_id, member_id, offer_id)
                VALUES (?, ?, ?)
            """, (
                click_id,
                link["member_id"],
                link["offer_id"]
            ))

            # Preserve original URL and replace tracking parameters.
            parts = urllib.parse.urlsplit(offer["url"])

            params = [
                (k, v)
                for k, v in urllib.parse.parse_qsl(
                    parts.query,
                    keep_blank_values=True
                )
                if k not in ("click_id", "token_1", "token_2")
            ]

            params.extend([
                ("click_id", click_id),
                ("token_1", str(link["member_id"])),
                ("token_2", str(link["offer_id"]))
            ])

            destination = urllib.parse.urlunsplit((
                parts.scheme,
                parts.netloc,
                parts.path,
                urllib.parse.urlencode(params),
                parts.fragment
            ))

        return redirect(destination, code=302)

    except Exception as e:
        print("CLICK TRACKING ERROR:", repr(e))
        return "Tracking error. Please try again.", 500


# ================= WITHDRAWAL =================

@bot.message_handler(func=lambda m: m.text == "🏦 Withdraw")
def withdraw_info(message):
    bot.reply_to(
        message,
        "To request a withdrawal, send:\n\n"
        "/withdraw 10\n\n"
        "Replace 10 with your requested amount in USD."
    )


@bot.message_handler(commands=["withdraw"])
def withdraw(message):
    uid = message.from_user.id
    parts = message.text.split()

    if len(parts) != 2:
        bot.reply_to(message, "Example: /withdraw 10")
        return

    try:
        amount = float(parts[1])
    except ValueError:
        bot.reply_to(message, "Invalid amount.")
        return

    if amount <= 0:
        bot.reply_to(message, "Amount must be greater than zero.")
        return

    with db() as con:
        con.execute("BEGIN IMMEDIATE")

        member = con.execute(
            "SELECT balance FROM members WHERE user_id=?",
            (uid,)
        ).fetchone()

        if not member or member["balance"] < amount:
            bot.reply_to(message, "Insufficient available balance.")
            return

        con.execute(
            "INSERT INTO withdrawals (member_id, amount) VALUES (?, ?)",
            (uid, amount)
        )

        withdrawal_id = con.execute(
            "SELECT last_insert_rowid()"
        ).fetchone()[0]

    bot.reply_to(
        message,
        f"✅ Withdrawal request #{withdrawal_id} submitted.\n"
        "Status: Pending"
    )

    bot.send_message(
        ADMIN_ID,
        f"💸 New withdrawal request\n\n"
        f"Request ID: {withdrawal_id}\n"
        f"Member: {uid}\n"
        f"Amount: ${amount:.2f}\n\n"
        f"Approve: /approve {withdrawal_id}\n"
        f"Reject: /reject {withdrawal_id}"
    )


# ================= ADMIN PANEL =================

@bot.message_handler(func=lambda m: m.text == "⚙️ Admin Panel")
def admin_panel(message):
    if message.from_user.id != ADMIN_ID:
        return

    bot.send_message(
        message.chat.id,
        "⚙️ ClickCrew Admin Panel",
        reply_markup=admin_menu()
    )


@bot.message_handler(func=lambda m: m.text == "🏠 Main Menu")
def back_menu(message):
    bot.send_message(
        message.chat.id,
        "Main Menu",
        reply_markup=main_menu(message.from_user.id)
    )


# ================= ADD OFFER =================

@bot.message_handler(func=lambda m: m.text == "➕ Add Offer")
def add_offer_info(message):
    if message.from_user.id != ADMIN_ID:
        return

    bot.reply_to(
        message,
        "Send offer details in this format:\n\n"
        "/addoffer Offer Name | Offer URL | Payout\n\n"
        "Example:\n"
        "/addoffer Test Offer | https://example.com/offer | 1.50"
    )


@bot.message_handler(commands=["addoffer"])
def add_offer(message):
    if message.from_user.id != ADMIN_ID:
        return

    try:
        data = message.text.replace("/addoffer", "", 1).strip()
        name, url, payout = [x.strip() for x in data.split("|")]
        payout = float(payout)

        if (
            not name
            or not url.startswith(("http://", "https://"))
            or payout < 0
        ):
            raise ValueError()

        with db() as con:
            con.execute(
                "INSERT INTO offers (name, url, payout) VALUES (?, ?, ?)",
                (name, url, payout)
            )

        bot.reply_to(message, f"✅ Offer added: {name}")

    except Exception:
        bot.reply_to(
            message,
            "Invalid format.\n\n"
            "/addoffer Name | URL | Payout"
        )


# ================= MANAGE OFFERS =================

@bot.message_handler(func=lambda m: m.text == "📋 Manage Offers")
def manage_offers(message):
    if message.from_user.id != ADMIN_ID:
        return

    with db() as con:
        offers = con.execute(
            "SELECT * FROM offers ORDER BY id"
        ).fetchall()

    if not offers:
        bot.reply_to(message, "No offers found.")
        return

    for offer in offers:
        bot.send_message(
            message.chat.id,
            f"ID: {offer['id']}\n"
            f"Name: {offer['name']}\n"
            f"Payout: ${offer['payout']:.2f}\n"
            f"Status: {'Active' if offer['active'] else 'Inactive'}\n\n"
            f"Deactivate: /off {offer['id']}\n"
            f"Activate: /on {offer['id']}\n"
            f"Delete: /deleteoffer {offer['id']}"
        )


@bot.message_handler(commands=["off", "on", "deleteoffer"])
def change_offer(message):
    if message.from_user.id != ADMIN_ID:
        return

    try:
        command, offer_id = message.text.split()
        offer_id = int(offer_id)

        with db() as con:
            if command == "/off":
                con.execute(
                    "UPDATE offers SET active=0 WHERE id=?",
                    (offer_id,)
                )
            elif command == "/on":
                con.execute(
                    "UPDATE offers SET active=1 WHERE id=?",
                    (offer_id,)
                )
            else:
                con.execute(
                    "DELETE FROM offers WHERE id=?",
                    (offer_id,)
                )

        bot.reply_to(message, "✅ Offer updated.")

    except Exception:
        bot.reply_to(message, "Use the command followed by an Offer ID.")


# ================= MEMBERS =================

@bot.message_handler(func=lambda m: m.text == "👥 Members")
def members_list(message):
    if message.from_user.id != ADMIN_ID:
        return

    with db() as con:
        count = con.execute(
            "SELECT COUNT(*) FROM members"
        ).fetchone()[0]

        rows = con.execute("""
            SELECT
                m.user_id,
                m.username,
                m.balance,
                COUNT(c.id) AS total_clicks
            FROM members m
            LEFT JOIN clicks c ON c.member_id=m.user_id
            GROUP BY m.user_id
            ORDER BY m.joined_at DESC
            LIMIT 30
        """).fetchall()

    result = f"👥 Total Members: {count}\n\n"

    for row in rows:
        result += (
            f"ID: {row['user_id']}\n"
            f"@{row['username'] or '-'}\n"
            f"Clicks: {row['total_clicks']}\n"
            f"Balance: ${row['balance']:.2f}\n\n"
        )

    bot.send_message(message.chat.id, result)


# ================= STATISTICS =================

@bot.message_handler(func=lambda m: m.text == "📊 Statistics")
def statistics(message):
    if message.from_user.id != ADMIN_ID:
        return

    with db() as con:
        members = con.execute(
            "SELECT COUNT(*) FROM members"
        ).fetchone()[0]

        offers = con.execute(
            "SELECT COUNT(*) FROM offers WHERE active=1"
        ).fetchone()[0]

        clicks = con.execute(
            "SELECT COUNT(*) FROM clicks"
        ).fetchone()[0]

        unique_clicks = con.execute(
            "SELECT COUNT(DISTINCT click_id) FROM clicks"
        ).fetchone()[0]

        conversions = con.execute(
            "SELECT COUNT(*) FROM conversions"
        ).fetchone()[0]

        payout = con.execute(
            "SELECT COALESCE(SUM(payout),0) FROM conversions"
        ).fetchone()[0]

    bot.send_message(
        message.chat.id,
        "📊 CLICKCREW STATISTICS\n\n"
        f"Members: {members}\n"
        f"Active Offers: {offers}\n"
        f"Total Clicks: {clicks}\n"
        f"Unique Clicks: {unique_clicks}\n"
        f"Conversions: {conversions}\n"
        f"Total Payout: ${payout:.2f}"
    )


# ================= CLICK REPORTS =================

@bot.message_handler(func=lambda m: m.text == "📈 Click Reports")
def click_reports(message):
    if message.from_user.id != ADMIN_ID:
        return

    with db() as con:
        rows = con.execute("""
            SELECT
                m.user_id,
                m.username,
                COUNT(c.id) AS clicks,
                COUNT(DISTINCT c.click_id) AS unique_clicks
            FROM members m
            LEFT JOIN clicks c ON c.member_id=m.user_id
            GROUP BY m.user_id
            ORDER BY clicks DESC
            LIMIT 30
        """).fetchall()

    if not rows:
        bot.reply_to(message, "No member data found.")
        return

    result = "📈 MEMBER CLICK REPORT\n\n"

    for row in rows:
        result += (
            f"Member: {row['user_id']}\n"
            f"Username: @{row['username'] or '-'}\n"
            f"Clicks: {row['clicks']}\n"
            f"Unique Clicks: {row['unique_clicks']}\n\n"
        )

    bot.send_message(message.chat.id, result)


# ================= WITHDRAWAL ADMIN =================

@bot.message_handler(func=lambda m: m.text == "💸 Withdrawals")
def withdrawal_list(message):
    if message.from_user.id != ADMIN_ID:
        return

    with db() as con:
        rows = con.execute(
            "SELECT * FROM withdrawals "
            "WHERE status='Pending' ORDER BY id"
        ).fetchall()

    if not rows:
        bot.reply_to(message, "No pending withdrawals.")
        return

    for row in rows:
        bot.send_message(
            message.chat.id,
            f"Request: #{row['id']}\n"
            f"Member: {row['member_id']}\n"
            f"Amount: ${row['amount']:.2f}\n"
            f"Approve: /approve {row['id']}\n"
            f"Reject: /reject {row['id']}"
        )


# ================= APPROVE WITHDRAWAL =================

@bot.message_handler(commands=["approve"])
def approve_withdrawal(message):
    if message.from_user.id != ADMIN_ID:
        return

    try:
        wid = int(message.text.split()[1])

        with db() as con:
            con.execute("BEGIN IMMEDIATE")

            row = con.execute(
                "SELECT * FROM withdrawals "
                "WHERE id=? AND status='Pending'",
                (wid,)
            ).fetchone()

            if not row:
                bot.reply_to(
                    message,
                    "Request not found or already processed."
                )
                return

            member = con.execute(
                "SELECT balance FROM members WHERE user_id=?",
                (row["member_id"],)
            ).fetchone()

            if not member or member["balance"] < row["amount"]:
                bot.reply_to(message, "Member balance is insufficient.")
                return

            con.execute(
                "UPDATE members SET balance=balance-? WHERE user_id=?",
                (row["amount"], row["member_id"])
            )

            con.execute(
                "UPDATE withdrawals SET status='Approved' WHERE id=?",
                (wid,)
            )

        bot.reply_to(message, "✅ Withdrawal approved.")

        bot.send_message(
            row["member_id"],
            f"✅ Your withdrawal #{wid} has been approved."
        )

    except Exception as e:
        print("APPROVE ERROR:", repr(e))
        bot.reply_to(message, "Could not approve this request.")


# ================= REJECT WITHDRAWAL =================

@bot.message_handler(commands=["reject"])
def reject_withdrawal(message):
    if message.from_user.id != ADMIN_ID:
        return

    try:
        wid = int(message.text.split()[1])

        with db() as con:
            row = con.execute(
                "SELECT * FROM withdrawals "
                "WHERE id=? AND status='Pending'",
                (wid,)
            ).fetchone()

            if not row:
                bot.reply_to(
                    message,
                    "Request not found or already processed."
                )
                return

            con.execute(
                "UPDATE withdrawals SET status='Rejected' WHERE id=?",
                (wid,)
            )

        bot.reply_to(message, "Withdrawal rejected.")

        bot.send_message(
            row["member_id"],
            f"❌ Your withdrawal #{wid} was rejected."
        )

    except Exception as e:
        print("REJECT ERROR:", repr(e))
        bot.reply_to(message, "Could not reject this request.")


# ================= POSTBACK =================

@app.route("/", methods=["GET"])
def home():
    return "ClickCrew Bot Server is running!"


@app.route("/postback", methods=["GET"])
def postback():

    if not POSTBACK_SECRET:
        return jsonify({
            "error": "Postback secret not configured"
        }), 503

    if not hmac.compare_digest(
        request.args.get("key", ""),
        POSTBACK_SECRET
    ):
        return jsonify({"error": "Unauthorized"}), 401

    member = request.args.get("token_1", "")
    offer = request.args.get("token_2", "")
    click_id = request.args.get("click_id", "")

    if not click_id:
        return jsonify({"error": "Missing click_id"}), 400

    try:
        payout = float(request.args.get("payout", "0"))

        if payout <= 0:
            raise ValueError()

        member = int(member)
        offer = int(offer)

    except (ValueError, TypeError):
        return jsonify({
            "error": "Invalid tracking data or payout"
        }), 400

    try:
        with db() as con:
            con.execute("BEGIN IMMEDIATE")

            result = con.execute(
                "INSERT OR IGNORE INTO conversions "
                "(member_id, offer_id, click_id, payout, country, os) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    member,
                    offer,
                    click_id,
                    payout,
                    request.args.get("country", ""),
                    request.args.get("os", "")
                )
            )

            if result.rowcount == 0:
                return jsonify({"status": "duplicate"})

            con.execute(
                "UPDATE members SET balance=balance+? "
                "WHERE user_id=?",
                (payout, member)
            )

        try:
            bot.send_message(
                member,
                "🎉 New conversion!\n"
                f"Offer ID: {offer}\n"
                f"Earned: ${payout:.2f}"
            )
        except Exception as e:
            print("NOTIFICATION ERROR:", repr(e))

        return jsonify({"status": "success"})

    except sqlite3.Error as e:
        print("POSTBACK DATABASE ERROR:", repr(e))
        return jsonify({"error": "Database error"}), 500


# ================= START BOT =================

def start_bot():
    print("ClickCrew Telegram Bot is starting...")

    try:
        bot.remove_webhook()

        bot_info = bot.get_me()

        print(
            "Telegram connected successfully: "
            f"@{bot_info.username}"
        )

        print("Starting Telegram polling...")

        bot.infinity_polling(
            skip_pending=True,
            timeout=30
        )

    except Exception as e:
        print("BOT START ERROR:", repr(e))


threading.Thread(
    target=start_bot,
    daemon=True
).start()
