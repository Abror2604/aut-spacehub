"""
AUT SpaceHub Telegram Bot
Entry point for students, approved club leaders, and notifications.
Requires TELEGRAM_BOT_TOKEN and MINI_APP_URL environment variables.
"""
import asyncio
import logging
import os
import sys
import httpx

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backend.config import BOT_TOKEN, MINI_APP_URL
from backend.database import get_connection, init_db
from backend.auth import resolve_user_from_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("spacehub.bot")

WELCOME_TEXT = (
    "Welcome to SpaceHub!\n"
    "Find available AUT classrooms, reserve study spaces and discover university sessions and club activities."
)

HELP_TEXT = (
    "AUT SpaceHub Commands:\n"
    "/start — Open SpaceHub Mini App\n"
    "/mybookings — View your upcoming reservations & sessions\n"
    "/club — Open Club Portal (approved club leaders only)\n"
    "/help — Show this help message\n\n"
    "Note: Demonstration mode uses sample AUT Fall 2026 schedules."
)


def build_open_miniapp_keyboard(url: str, label: str = "Open SpaceHub"):
    return {
        "inline_keyboard": [
            [{"text": label, "web_app": {"url": url}}]
        ]
    }


def handle_command(telegram_id: str, full_name: str, username: str, text: str) -> dict:
    """
    Pure command dispatcher usable both by live Telegram polling/webhooks AND automated tests.
    Returns {text, reply_markup}.
    """
    cmd = (text or "").strip().split()[0].lower()
    conn = get_connection()
    try:
        user = resolve_user_from_db(conn, str(telegram_id), full_name, username)

        if cmd == "/start":
            return {
                "text": WELCOME_TEXT,
                "reply_markup": build_open_miniapp_keyboard(MINI_APP_URL, "Open SpaceHub"),
            }

        if cmd == "/help":
            return {"text": HELP_TEXT, "reply_markup": build_open_miniapp_keyboard(MINI_APP_URL, "Open SpaceHub")}

        if cmd == "/mybookings":
            res_rows = conn.execute(
                """SELECT rr.booking_code, r.name AS room_name, rr.reservation_date, rr.start_time, rr.end_time, rr.status
                   FROM room_reservations rr JOIN rooms r ON rr.room_id = r.id
                   WHERE rr.user_id = ? AND rr.status IN ('confirmed', 'pending_approval')
                   ORDER BY rr.reservation_date, rr.start_time LIMIT 5""",
                (user["id"],),
            ).fetchall()
            sess_rows = conn.execute(
                """SELECT s.title, r.name AS room_name, s.session_date, s.start_time, s.end_time
                   FROM session_registrations sr
                   JOIN study_sessions s ON sr.session_id = s.id
                   JOIN rooms r ON s.room_id = r.id
                   WHERE sr.user_id = ? AND sr.status = 'confirmed'
                   ORDER BY s.session_date, s.start_time LIMIT 5""",
                (user["id"],),
            ).fetchall()

            lines = ["Your Upcoming SpaceHub Bookings:"]
            for r in res_rows:
                lines.append(f"• {r['room_name']} | {r['reservation_date']} {r['start_time']}–{r['end_time']} ({r['status'].replace('_', ' ')}) [{r['booking_code']}]")
            for s in sess_rows:
                lines.append(f"• Session: {s['title']} | {s['room_name']} {s['session_date']} {s['start_time']}–{s['end_time']}")
            if len(lines) == 1:
                lines.append("You have no upcoming bookings yet.")
            return {
                "text": "\n".join(lines),
                "reply_markup": build_open_miniapp_keyboard(f"{MINI_APP_URL}#bookings", "Manage in SpaceHub"),
            }

        if cmd == "/club":
            if not user["is_approved_club_leader"]:
                return {
                    "text": "Club Portal access is restricted to administrator-approved AUT club leaders. Contact AUT Student Affairs to verify your club account.",
                    "reply_markup": build_open_miniapp_keyboard(MINI_APP_URL, "Open SpaceHub"),
                }
            club_names = ", ".join(c["name"] for c in user["approved_clubs"])
            return {
                "text": f"Approved Club Leader Portal\nClubs: {club_names}\nOpen the Mini App to create club events and reserve dedicated club rooms.",
                "reply_markup": build_open_miniapp_keyboard(f"{MINI_APP_URL}#club", "Open Club Portal"),
            }

        return {
            "text": "Use /start to open SpaceHub or /help to see available commands.",
            "reply_markup": build_open_miniapp_keyboard(MINI_APP_URL, "Open SpaceHub"),
        }
    finally:
        conn.close()


async def flush_pending_notifications(client: httpx.AsyncClient):
    """Sends unsent notifications from SQLite to Telegram users once (deduplicated via is_sent=1)."""
    if not BOT_TOKEN:
        return
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT n.id, u.telegram_id, n.title, n.message
               FROM notifications n JOIN users u ON n.user_id = u.id
               WHERE n.is_sent = 0 ORDER BY n.id LIMIT 20"""
        ).fetchall()
        for row in rows:
            try:
                await client.post(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                    json={
                        "chat_id": row["telegram_id"],
                        "text": f"*{row['title']}*\n{row['message']}",
                        "parse_mode": "Markdown",
                    },
                    timeout=10.0,
                )
                conn.execute("UPDATE notifications SET is_sent = 1 WHERE id = ?", (row["id"],))
            except Exception as exc:
                logger.warning("Failed sending notification %s: %s", row["id"], exc)
    finally:
        conn.close()


async def run_polling():
    if not BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN is not set. Copy .env.example to .env and configure your token.")
        return
    init_db(seed=True).close()
    offset = 0
    logger.info("AUT SpaceHub Telegram Bot started (polling mode)...")
    async with httpx.AsyncClient() as client:
        while True:
            try:
                resp = await client.get(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
                    params={"offset": offset, "timeout": 20},
                    timeout=25.0,
                )
                data = resp.json()
                for upd in data.get("result", []):
                    offset = upd["update_id"] + 1
                    msg = upd.get("message")
                    if not msg or "text" not in msg:
                        continue
                    sender = msg["from"]
                    full_name = f"{sender.get('first_name', '')} {sender.get('last_name', '')}".strip() or "AUT Student"
                    reply = handle_command(str(sender["id"]), full_name, sender.get("username", ""), msg["text"])
                    await client.post(
                        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                        json={
                            "chat_id": msg["chat"]["id"],
                            "text": reply["text"],
                            "reply_markup": reply["reply_markup"],
                        },
                        timeout=10.0,
                    )
                await flush_pending_notifications(client)
            except Exception as exc:
                logger.error("Polling error: %s", exc)
                await asyncio.sleep(3)


if __name__ == "__main__":
    asyncio.run(run_polling())
