import hashlib
import hmac
import json
import os
from typing import Dict, Optional
from urllib.parse import parse_qsl
from fastapi import Header, HTTPException
from .config import BOT_TOKEN, DEMO_MODE
from .database import get_connection, now_tashkent_iso


def verify_telegram_init_data(init_data: str, bot_token: str) -> Dict:
    """
    Official Telegram Mini App initData HMAC-SHA256 verification.
    https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
    """
    if not init_data or not bot_token:
        raise HTTPException(status_code=401, detail="Missing Telegram initData or bot token.")

    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        raise HTTPException(status_code=401, detail="Invalid Telegram initData: missing hash.")

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        raise HTTPException(status_code=401, detail="Invalid Telegram initData signature.")

    user_raw = pairs.get("user")
    if not user_raw:
        raise HTTPException(status_code=401, detail="Telegram initData missing user payload.")
    return json.loads(user_raw)


def resolve_user_from_db(conn, telegram_id: str, full_name: str = "AUT Student", username: str = "") -> Dict:
    tg_id = str(telegram_id)
    row = conn.execute("SELECT * FROM users WHERE telegram_id = ?", (tg_id,)).fetchone()
    ts = now_tashkent_iso()

    if not row:
        cur = conn.execute(
            "INSERT INTO users (telegram_id, username, full_name, created_at) VALUES (?, ?, ?, ?)",
            (tg_id, username, full_name, ts),
        )
        user_id = cur.lastrowid
        conn.execute(
            "INSERT OR IGNORE INTO user_roles (user_id, role, granted_at) VALUES (?, 'student', ?)",
            (user_id, ts),
        )
        # Auto-assign Admin + Approved Club Leader to configured ADMIN_TELEGRAM_IDS,
        # OR to the very first real Telegram user who opens the platform when no admin exists yet.
        admin_ids = {x.strip() for x in os.getenv("ADMIN_TELEGRAM_IDS", "").split(",") if x.strip()}
        existing_admins = conn.execute("SELECT COUNT(*) AS cnt FROM user_roles WHERE role = 'admin'").fetchone()["cnt"]
        if (admin_ids and tg_id in admin_ids) or (not admin_ids and existing_admins == 0):
            conn.execute("INSERT OR IGNORE INTO user_roles (user_id, role, granted_at) VALUES (?, 'admin', ?)", (user_id, ts))
            conn.execute("INSERT OR IGNORE INTO user_roles (user_id, role, granted_by, granted_at) VALUES (?, 'club_leader', ?, ?)", (user_id, user_id, ts))
            first_club = conn.execute("SELECT id FROM clubs ORDER BY id LIMIT 1").fetchone()
            if first_club:
                conn.execute(
                    "INSERT OR IGNORE INTO club_leaders (user_id, club_id, status, approved_by, approved_at) VALUES (?, ?, 'approved', ?, ?)",
                    (user_id, first_club["id"], user_id, ts),
                )
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    else:
        # Keep display name / username synced with live Telegram profile
        if full_name and full_name != "AUT Student" and (row["full_name"] != full_name or (username and row["username"] != username)):
            conn.execute("UPDATE users SET full_name = ?, username = ? WHERE id = ?", (full_name, username or row["username"], row["id"]))
            row = conn.execute("SELECT * FROM users WHERE id = ?", (row["id"],)).fetchone()

    roles = [
        r["role"]
        for r in conn.execute("SELECT role FROM user_roles WHERE user_id = ?", (row["id"],)).fetchall()
    ]
    approved_clubs = [
        dict(c)
        for c in conn.execute(
            """SELECT c.id, c.name, c.category FROM club_leaders cl
               JOIN clubs c ON c.id = cl.club_id
               WHERE cl.user_id = ? AND cl.status = 'approved' AND c.is_active = 1""",
            (row["id"],),
        ).fetchall()
    ]
    return {
        "id": row["id"],
        "telegram_id": row["telegram_id"],
        "username": row["username"],
        "full_name": row["full_name"],
        "email": row["email"],
        "roles": roles,
        "is_admin": "admin" in roles,
        "is_approved_club_leader": ("club_leader" in roles and len(approved_clubs) > 0),
        "approved_clubs": approved_clubs,
    }


def get_current_user(
    x_telegram_init_data: Optional[str] = Header(default=None),
    x_demo_telegram_id: Optional[str] = Header(default=None),
) -> Dict:
    """
    Authenticates via cryptographically signed Telegram WebApp initData (HMAC-SHA256).
    If BOT_TOKEN is not yet passed to uvicorn, safely parses Telegram initData user payload
    only when DEMO_MODE=true; in production set TELEGRAM_BOT_TOKEN on uvicorn and DEMO_MODE=false.
    """
    conn = get_connection()
    try:
        if x_telegram_init_data:
            if BOT_TOKEN:
                tg_user = verify_telegram_init_data(x_telegram_init_data, BOT_TOKEN)
            elif DEMO_MODE:
                pairs = dict(parse_qsl(x_telegram_init_data, keep_blank_values=True))
                user_raw = pairs.get("user")
                if not user_raw:
                    raise HTTPException(status_code=401, detail="Telegram initData missing user payload.")
                tg_user = json.loads(user_raw)
            else:
                raise HTTPException(status_code=401, detail="Server TELEGRAM_BOT_TOKEN not configured.")

            full_name = f"{tg_user.get('first_name', '')} {tg_user.get('last_name', '')}".strip() or "AUT Student"
            return resolve_user_from_db(conn, str(tg_user["id"]), full_name, tg_user.get("username", ""))

        if DEMO_MODE and x_demo_telegram_id:
            return resolve_user_from_db(conn, str(x_demo_telegram_id), f"AUT User {x_demo_telegram_id}", "")

        raise HTTPException(status_code=401, detail="Open SpaceHub from Telegram to authenticate.")
    finally:
        conn.close()


def require_club_leader(user: Dict) -> Dict:
    if not user.get("is_approved_club_leader"):
        raise HTTPException(status_code=403, detail="Access denied: Approved Club Leader permissions required.")
    return user


def require_admin(user: Dict) -> Dict:
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Access denied: Administrator permissions required.")
    return user
