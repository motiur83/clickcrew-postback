from flask import Flask, request, jsonify
import os
import sqlite3
import hmac

app = Flask (__name__)

DB = "clickcrew_postback.db"
SECRET = os.environ.get("POSTBACK_SECRET", "")

def init_db():
    with sqlite3.connect(DB) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS conversions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                member_id TEXT,
                offer_id TEXT,
                click_id TEXT,
                payout REAL,
                country TEXT,
                os TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

@app.route("/", methods=["GET"])
def home():
    return "ClickCrew Postback Receiver is running!"

@app.route("/postback", methods=["GET"])
def postback():
    if not SECRET:
        return jsonify({"error": "Receiver not configured"}), 503

    if not hmac.compare_digest(
        request.args.get("key", ""), SECRET
    ):
        return jsonify({"error": "Unauthorized"}), 401

    member = request.args.get("token_1", "")
    offer = request.args.get("token_2", "")
    click_id = request.args.get("click_id", "")
    payout = request.args.get("payout", "0")

    try:
        payout = float(payout)
    except ValueError:
        return jsonify({"error": "Invalid payout"}), 400

    if not member or not offer or not click_id:
        return jsonify({"error": "Missing tracking data"}), 400

    init_db()

    with sqlite3.connect(DB) as con:
        con.execute("""
            INSERT INTO conversions
            (member_id, offer_id, click_id, payout, country, os)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            member,
            offer,
            click_id,
            payout,
            request.args.get("country", ""),
            request.args.get("os", "")
        ))

    return jsonify({"status": "success"})

init_db()

if name == "main":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 10000))
    )
