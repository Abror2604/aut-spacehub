import os
import threading
import uuid
from datetime import datetime
from typing import Dict, List, Optional
import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from .auth import get_current_user, require_admin, require_club_leader
from .availability import compute_room_availability, is_interval_free, to_mins
from .config import (
    APP_TIMEZONE,
    MAX_CLUB_RESERVATION_MINUTES,
    MAX_STUDENT_RESERVATION_MINUTES,
    MIN_RESERVATION_MINUTES,
    TIMEZONE_NAME,
)
from .database import get_connection, init_db, now_tashkent_iso, transaction
from .schemas import (
    AdminClubLeaderApprove,
    AdminEventReview,
    AdminRoomBlockCreate,
    AdminRoomCreate,
    AdminRoomPermissionUpdate,
    AdminScheduleCreate,
    AdminSessionCreate,
    ClubEventCreate,
    ClubEventUpdate,
    SessionJoinRequest,
    StudentReservationCreate,
)

app = FastAPI(title="AUT SpaceHub API", version="2.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _live_bot_token() -> str:
    return os.getenv("TELEGRAM_BOT_TOKEN", "").strip()


def _live_app_url() -> str:
    url = (
        os.getenv("MINI_APP_URL")
        or os.getenv("RENDER_EXTERNAL_URL")
        or "https://aut-spacehub.onrender.com"
    ).strip().rstrip("/")
    return url


def _tg_api_call(method: str, payload: dict):
    token = _live_bot_token()
    if not token:
        return None
    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{token}/{method}",
            json=payload,
            timeout=6.0,
        )
        return resp.json() if resp.status_code == 200 else None
    except Exception:
        return None


def _send_telegram_message_async(chat_id: int, text: str, include_open_btn: bool = True):
    token = _live_bot_token()
    if not token or not chat_id:
        return
    payload = {
        "chat_id": int(chat_id),
        "text": text,
        "parse_mode": "HTML",
    }
    if include_open_btn:
        app_url = _live_app_url()
        if app_url.startswith("https://"):
            payload["reply_markup"] = {
                "inline_keyboard": [
                    [{"text": "🏫 Open AUT SpaceHub", "web_app": {"url": app_url}}]
                ]
            }
    threading.Thread(target=_tg_api_call, args=("sendMessage", payload), daemon=True).start()


def _register_telegram_webhook():
    token = _live_bot_token()
    app_url = _live_app_url()
    if not token or not app_url.startswith("https://"):
        return
    _tg_api_call("setWebhook", {"url": f"{app_url}/telegram/webhook", "drop_pending_updates": False})
    _tg_api_call(
        "setMyCommands",
        {
            "commands": [
                {"command": "start", "description": "Open AUT SpaceHub"},
                {"command": "rooms", "description": "Check free rooms right now"},
                {"command": "bookings", "description": "View your active bookings"},
                {"command": "help", "description": "How to use AUT SpaceHub"},
            ]
        },
    )


@app.on_event("startup")
def on_startup():
    conn = init_db(seed=True)
    try:
        conn.execute("ALTER TABLE room_reservations ADD COLUMN checked_in_at TEXT")
        conn.commit()
    except Exception:
        pass
    conn.close()
    if _live_bot_token() and _live_app_url().startswith("https://"):
        threading.Thread(target=_register_telegram_webhook, daemon=True).start()


def _queue_notification(conn, user_id: int, event_key: str, notif_type: str, title: str, message: str):
    cur = conn.execute(
        """INSERT OR IGNORE INTO notifications (user_id, event_key, notif_type, title, message, is_sent, created_at)
           VALUES (?, ?, ?, ?, ?, 0, ?)""",
        (user_id, event_key, notif_type, title, message, now_tashkent_iso()),
    )
    if cur.rowcount and _live_bot_token():
        u = conn.execute("SELECT telegram_id FROM users WHERE id = ?", (user_id,)).fetchone()
        if u and u["telegram_id"]:
            _send_telegram_message_async(
                u["telegram_id"],
                f"<b>{title}</b>\n{message}",
                include_open_btn=True,
            )


def _log_admin(conn, admin_user_id: int, action_type: str, target_entity: str, target_id: str, details: str = ""):
    conn.execute(
        """INSERT INTO admin_actions (admin_user_id, action_type, target_entity, target_id, details, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (admin_user_id, action_type, target_entity, str(target_id), details, now_tashkent_iso()),
    )


def _get_setting(conn, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM platform_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def _default_tashkent_date_time():
    now = datetime.now(APP_TIMEZONE)
    return now.date().isoformat(), now.strftime("%H:%M")


# ---------------------------------------------------------------------------
# STATIC MINI APP & ADMIN PANEL ROUTES
# ---------------------------------------------------------------------------
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


@app.get("/", response_class=HTMLResponse)
def serve_mini_app():
    path = os.path.join(BASE_DIR, "frontend", "index.html")
    if os.path.exists(path):
        return FileResponse(path)
    return HTMLResponse("<h1>AUT SpaceHub API Running</h1>")


@app.get("/admin", response_class=HTMLResponse)
def serve_admin_panel():
    path = os.path.join(BASE_DIR, "admin", "index.html")
    if os.path.exists(path):
        return FileResponse(path)
    return HTMLResponse("<h1>AUT SpaceHub Admin Panel</h1>")


# ---------------------------------------------------------------------------
# HEALTH & CURRENT USER PROFILE
# ---------------------------------------------------------------------------
@app.get("/api/health")
def health():
    return {"status": "ok", "timezone": TIMEZONE_NAME, "service": "AUT SpaceHub"}


@app.get("/api/me")
def get_me(user: Dict = Depends(get_current_user)):
    conn = get_connection()
    try:
        notifs = [
            dict(n)
            for n in conn.execute(
                "SELECT id, notif_type, title, message, created_at FROM notifications WHERE user_id = ? ORDER BY id DESC LIMIT 10",
                (user["id"],),
            ).fetchall()
        ]
        demo_mode = _get_setting(conn, "demo_mode", "true") == "true"
        schedule_term = _get_setting(conn, "schedule_term", "AUT Fall 2026 (Demonstration Data)")
        return {
            "user": user,
            "demo_mode": demo_mode,
            "schedule_term": schedule_term,
            "timezone": TIMEZONE_NAME,
            "notifications": notifs,
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# STUDENT: ROOMS & AUTHORITATIVE AVAILABILITY
# ---------------------------------------------------------------------------
@app.get("/api/rooms")
def list_rooms(
    date: Optional[str] = Query(default=None, description="YYYY-MM-DD"),
    time: Optional[str] = Query(default=None, description="HH:MM in Asia/Tashkent"),
    available_now: bool = Query(default=False),
    min_duration_minutes: int = Query(default=0, ge=0),
    min_capacity: int = Query(default=0, ge=0),
    floor: Optional[int] = Query(default=None),
    building: Optional[str] = Query(default=None),
    include_club_only: bool = Query(default=False),
    user: Dict = Depends(get_current_user),
):
    d_str, t_str = _default_tashkent_date_time()
    target_date = date or d_str
    check_time = time or t_str

    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT r.*, p.access_mode, p.open_time, p.close_time, p.buffer_minutes, p.notes AS perm_notes
               FROM rooms r JOIN room_permissions p ON r.id = p.room_id
               WHERE r.is_active = 1 ORDER BY r.floor, r.id"""
        ).fetchall()

        results = []
        for r in rows:
            # Ordinary students do not book club_only rooms; show only student_only & shared unless requested
            if not include_club_only and r["access_mode"] == "club_only":
                continue
            if min_capacity and r["capacity"] < min_capacity:
                continue
            if floor is not None and r["floor"] != floor:
                continue
            if building and building.lower() not in r["building"].lower():
                continue

            avail = compute_room_availability(conn, r, r, target_date, check_time)
            avail["student_bookable"] = r["access_mode"] in ("student_only", "shared")
            avail["club_bookable"] = r["access_mode"] in ("club_only", "shared")

            if available_now and avail["status"] not in ("available", "closing_soon"):
                continue
            if min_duration_minutes > 0 and avail["remaining_minutes"] < min_duration_minutes:
                continue
            results.append(avail)

        return {
            "date": target_date,
            "checked_at": check_time,
            "timezone": TIMEZONE_NAME,
            "count": len(results),
            "rooms": results,
        }
    finally:
        conn.close()


@app.get("/api/rooms/{room_id}/availability")
def get_room_detail(
    room_id: str,
    date: Optional[str] = Query(default=None),
    time: Optional[str] = Query(default=None),
    user: Dict = Depends(get_current_user),
):
    d_str, t_str = _default_tashkent_date_time()
    target_date = date or d_str
    check_time = time or t_str

    conn = get_connection()
    try:
        r = conn.execute(
            """SELECT r.*, p.access_mode, p.open_time, p.close_time, p.buffer_minutes, p.notes AS perm_notes
               FROM rooms r JOIN room_permissions p ON r.id = p.room_id
               WHERE r.id = ?""",
            (room_id,),
        ).fetchone()
        if not r:
            raise HTTPException(status_code=404, detail="Room not found.")
        avail = compute_room_availability(conn, r, r, target_date, check_time)
        avail["student_bookable"] = r["access_mode"] in ("student_only", "shared")
        avail["club_bookable"] = r["access_mode"] in ("club_only", "shared")
        return avail
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# STUDENT: MAKE & CANCEL ROOM RESERVATION (WITH CONCURRENCY LOCK)
# ---------------------------------------------------------------------------
@app.post("/api/reservations", status_code=201)
def create_student_reservation(payload: StudentReservationCreate, user: Dict = Depends(get_current_user)):
    dur = to_mins(payload.end_time) - to_mins(payload.start_time)
    if dur < MIN_RESERVATION_MINUTES:
        raise HTTPException(status_code=400, detail=f"Minimum reservation duration is {MIN_RESERVATION_MINUTES} minutes.")
    if dur > MAX_STUDENT_RESERVATION_MINUTES:
        raise HTTPException(status_code=400, detail=f"Maximum student reservation is {MAX_STUDENT_RESERVATION_MINUTES} minutes.")

    conn = get_connection()
    try:
        with transaction(conn):
            r = conn.execute(
                """SELECT r.*, p.access_mode, p.open_time, p.close_time, p.buffer_minutes
                   FROM rooms r JOIN room_permissions p ON r.id = p.room_id
                   WHERE r.id = ? AND r.is_active = 1""",
                (payload.room_id,),
            ).fetchone()
            if not r:
                raise HTTPException(status_code=404, detail="Room not found.")
            if r["access_mode"] not in ("student_only", "shared"):
                raise HTTPException(
                    status_code=403,
                    detail=f"Room {r['name']} is designated as '{r['access_mode']}' and cannot be reserved for ordinary student study.",
                )
            if payload.expected_attendees > r["capacity"]:
                raise HTTPException(status_code=400, detail=f"Attendees exceed room capacity ({r['capacity']}).")

            ok, reason = is_interval_free(conn, r["id"], r, payload.reservation_date, payload.start_time, payload.end_time)
            if not ok:
                raise HTTPException(status_code=409, detail=reason)

            ts = now_tashkent_iso()
            code = "STU-" + uuid.uuid4().hex[:6].upper()
            cur = conn.execute(
                """INSERT INTO room_reservations
                   (booking_code, room_id, user_id, reservation_type, reservation_date, start_time, end_time,
                    purpose, expected_attendees, status, created_at, updated_at)
                   VALUES (?, ?, ?, 'student_study', ?, ?, ?, ?, ?, 'confirmed', ?, ?)""",
                (
                    code,
                    r["id"],
                    user["id"],
                    payload.reservation_date,
                    payload.start_time,
                    payload.end_time,
                    payload.purpose,
                    payload.expected_attendees,
                    ts,
                    ts,
                ),
            )
            res_id = cur.lastrowid
            _queue_notification(
                conn,
                user["id"],
                f"res_confirm_{res_id}",
                "booking_confirmed",
                "Room Reservation Confirmed",
                f"{r['name']} booked on {payload.reservation_date} ({payload.start_time}–{payload.end_time}). Code: {code}",
            )
            return {
                "id": res_id,
                "booking_code": code,
                "room_id": r["id"],
                "room_name": r["name"],
                "reservation_date": payload.reservation_date,
                "start_time": payload.start_time,
                "end_time": payload.end_time,
                "status": "confirmed",
            }
    finally:
        conn.close()


@app.post("/api/reservations/{reservation_id}/cancel")
def cancel_reservation(reservation_id: int, user: Dict = Depends(get_current_user)):
    conn = get_connection()
    try:
        with transaction(conn):
            row = conn.execute("SELECT * FROM room_reservations WHERE id = ?", (reservation_id,)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Reservation not found.")
            if row["user_id"] != user["id"] and not user["is_admin"]:
                raise HTTPException(status_code=403, detail="You cannot cancel another user's booking.")
            if row["status"] == "cancelled":
                raise HTTPException(status_code=400, detail="Reservation is already cancelled.")

            ts = now_tashkent_iso()
            conn.execute("UPDATE room_reservations SET status = 'cancelled', updated_at = ? WHERE id = ?", (ts, reservation_id))
            _queue_notification(
                conn,
                row["user_id"],
                f"res_cancel_{reservation_id}",
                "booking_cancelled",
                "Reservation Cancelled",
                f"Booking {row['booking_code']} (Room {row['room_id']}, {row['reservation_date']} {row['start_time']}–{row['end_time']}) was cancelled.",
            )
            return {"id": reservation_id, "status": "cancelled"}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# SESSIONS (QUIET STUDY, COLLABORATION & PUBLISHED CLUB EVENTS)
# ---------------------------------------------------------------------------
def _compute_check_in_status(res_date: str, start_time: str, end_time: str, checked_in_at: Optional[str]) -> str:
    if checked_in_at:
        return "checked_in"
    today_str, now_time = _default_tashkent_date_time()
    if res_date > today_str:
        return "upcoming"
    if res_date < today_str:
        return "missed"
    now_m = to_mins(now_time)
    start_m = to_mins(start_time)
    end_m = to_mins(end_time)
    if now_m < start_m - 15:
        return "upcoming"
    if now_m <= start_m + 15:
        return "open"
    if now_m < end_m:
        return "overdue"
    return "missed"


@app.post("/api/reservations/{reservation_id}/check-in")
def check_in_reservation(reservation_id: int, user: Dict = Depends(get_current_user)):
    conn = get_connection()
    try:
        with transaction(conn):
            row = conn.execute(
                """SELECT rr.*, r.name AS room_name
                   FROM room_reservations rr JOIN rooms r ON r.id = rr.room_id
                   WHERE rr.id = ?""",
                (reservation_id,),
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Reservation not found.")
            if row["user_id"] != user["id"] and not user["is_admin"]:
                raise HTTPException(status_code=403, detail="You can only check in to your own reservation.")
            if row["status"] != "confirmed":
                raise HTTPException(status_code=400, detail="Only confirmed reservations can be checked in.")
            if row["checked_in_at"]:
                return {
                    "id": reservation_id,
                    "checked_in_at": row["checked_in_at"],
                    "check_in_status": "checked_in",
                    "message": "Already checked in.",
                }
            today_str, now_time = _default_tashkent_date_time()
            now_m = to_mins(now_time)
            start_m = to_mins(row["start_time"])
            end_m = to_mins(row["end_time"])
            if row["reservation_date"] > today_str or (row["reservation_date"] == today_str and now_m < start_m - 15):
                open_m = max(0, start_m - 15)
                open_str = f"{open_m // 60:02d}:{open_m % 60:02d}"
                raise HTTPException(
                    status_code=400,
                    detail=f"Check-in opens 15 minutes before start ({row['reservation_date']} at {open_str} Tashkent time).",
                )
            if row["reservation_date"] < today_str or (row["reservation_date"] == today_str and now_m >= end_m):
                raise HTTPException(status_code=400, detail="This reservation window has already ended.")
            ts = now_tashkent_iso()
            conn.execute("UPDATE room_reservations SET checked_in_at = ?, updated_at = ? WHERE id = ?", (ts, ts, reservation_id))
            _queue_notification(
                conn,
                user["id"],
                f"res_checkin_{reservation_id}",
                "booking_checked_in",
                "Room Check-In Confirmed",
                f"Checked in to {row['room_name']} ({row['reservation_date']} {row['start_time']}–{row['end_time']}). Code: {row['booking_code']}",
            )
            return {
                "id": reservation_id,
                "checked_in_at": ts,
                "check_in_status": "checked_in",
                "room_name": row["room_name"],
            }
    finally:
        conn.close()


@app.get("/api/sessions")
def list_sessions(
    session_type: Optional[str] = Query(default=None),
    date: Optional[str] = Query(default=None),
    user: Dict = Depends(get_current_user),
):
    conn = get_connection()
    try:
        sql = """SELECT s.*, r.name AS room_name, r.floor
                 FROM study_sessions s JOIN rooms r ON s.room_id = r.id
                 WHERE s.status = 'active' AND s.is_public = 1"""
        params: List = []
        if session_type and session_type in ("quiet", "collaboration", "club_event"):
            sql += " AND s.session_type = ?"
            params.append(session_type)
        if date:
            sql += " AND s.session_date = ?"
            params.append(date)
        sql += " ORDER BY s.session_date, s.start_time"

        out = []
        for s in conn.execute(sql, params).fetchall():
            agg = conn.execute(
                """SELECT COALESCE(SUM(group_size), 0) AS seats, COUNT(*) AS groups
                   FROM session_registrations WHERE session_id = ? AND status = 'confirmed'""",
                (s["id"],),
            ).fetchone()
            my_reg = conn.execute(
                "SELECT id, group_size FROM session_registrations WHERE session_id = ? AND user_id = ? AND status = 'confirmed'",
                (s["id"], user["id"]),
            ).fetchone()
            seats_taken = agg["seats"]
            groups_taken = agg["groups"]
            seats_left = max(0, s["max_capacity"] - seats_taken)
            groups_left = max(0, s["max_groups"] - groups_taken) if s["max_groups"] else None
            is_full = seats_left < s["min_group_size"] or (groups_left is not None and groups_left == 0)

            out.append({
                "id": s["id"],
                "session_code": s["session_code"],
                "session_type": s["session_type"],
                "title": s["title"],
                "description": s["description"],
                "room_id": s["room_id"],
                "room_name": s["room_name"],
                "floor": s["floor"],
                "session_date": s["session_date"],
                "start_time": s["start_time"],
                "end_time": s["end_time"],
                "max_capacity": s["max_capacity"],
                "min_group_size": s["min_group_size"],
                "max_groups": s["max_groups"],
                "seats_taken": seats_taken,
                "seats_available": seats_left,
                "groups_available": groups_left,
                "is_full": is_full,
                "user_registered": my_reg is not None,
                "user_registration_id": my_reg["id"] if my_reg else None,
            })
        return {"sessions": out}
    finally:
        conn.close()


@app.post("/api/sessions/{session_id}/join", status_code=201)
def join_session(session_id: int, payload: SessionJoinRequest, user: Dict = Depends(get_current_user)):
    conn = get_connection()
    try:
        with transaction(conn):
            s = conn.execute("SELECT * FROM study_sessions WHERE id = ? AND status = 'active'", (session_id,)).fetchone()
            if not s:
                raise HTTPException(status_code=404, detail="Study session not found.")
            if not s["is_public"]:
                raise HTTPException(status_code=403, detail="This event is private to club members.")

            dup = conn.execute(
                "SELECT id FROM session_registrations WHERE session_id = ? AND user_id = ? AND status = 'confirmed'",
                (session_id, user["id"]),
            ).fetchone()
            if dup:
                raise HTTPException(status_code=409, detail="You are already registered for this session.")

            if payload.group_size < s["min_group_size"]:
                raise HTTPException(
                    status_code=400,
                    detail=f"This session requires at least {s['min_group_size']} students per group.",
                )

            agg = conn.execute(
                """SELECT COALESCE(SUM(group_size), 0) AS seats, COUNT(*) AS groups
                   FROM session_registrations WHERE session_id = ? AND status = 'confirmed'""",
                (session_id,),
            ).fetchone()
            if agg["seats"] + payload.group_size > s["max_capacity"]:
                raise HTTPException(status_code=409, detail="Session is full or does not have enough seats remaining.")
            if s["max_groups"] is not None and agg["groups"] + 1 > s["max_groups"]:
                raise HTTPException(status_code=409, detail="No group slots remaining in this collaboration session.")

            ts = now_tashkent_iso()
            cur = conn.execute(
                """INSERT INTO session_registrations (session_id, user_id, group_size, notes, status, created_at)
                   VALUES (?, ?, ?, ?, 'confirmed', ?)""",
                (session_id, user["id"], payload.group_size, payload.notes, ts),
            )
            reg_id = cur.lastrowid
            _queue_notification(
                conn,
                user["id"],
                f"sess_join_{reg_id}",
                "session_joined",
                "Session Registration Confirmed",
                f"Joined '{s['title']}' in Room {s['room_id']} on {s['session_date']} ({s['start_time']}–{s['end_time']}).",
            )
            return {"registration_id": reg_id, "session_id": session_id, "group_size": payload.group_size, "status": "confirmed"}
    finally:
        conn.close()


@app.post("/api/sessions/{session_id}/leave")
def leave_session(session_id: int, user: Dict = Depends(get_current_user)):
    conn = get_connection()
    try:
        with transaction(conn):
            reg = conn.execute(
                "SELECT * FROM session_registrations WHERE session_id = ? AND user_id = ? AND status = 'confirmed'",
                (session_id, user["id"]),
            ).fetchone()
            if not reg:
                raise HTTPException(status_code=404, detail="Active registration not found.")
            conn.execute("UPDATE session_registrations SET status = 'cancelled' WHERE id = ?", (reg["id"],))
            return {"registration_id": reg["id"], "status": "cancelled"}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# MY BOOKINGS (UPCOMING, PAST, CANCELLED)
# ---------------------------------------------------------------------------
@app.get("/api/my-bookings")
def my_bookings(user: Dict = Depends(get_current_user)):
    today_str, _ = _default_tashkent_date_time()
    conn = get_connection()
    try:
        room_res = [
            dict(r)
            for r in conn.execute(
                """SELECT rr.*, r.name AS room_name FROM room_reservations rr
                   JOIN rooms r ON rr.room_id = r.id
                   WHERE rr.user_id = ? ORDER BY rr.reservation_date DESC, rr.start_time DESC""",
                (user["id"],),
            ).fetchall()
        ]
        sess_regs = [
            dict(r)
            for r in conn.execute(
                """SELECT sr.id AS registration_id, sr.group_size, sr.status AS registration_status,
                          s.id AS session_id, s.session_code, s.session_type, s.title, s.room_id,
                          r.name AS room_name, s.session_date, s.start_time, s.end_time
                   FROM session_registrations sr
                   JOIN study_sessions s ON sr.session_id = s.id
                   JOIN rooms r ON s.room_id = r.id
                   WHERE sr.user_id = ? ORDER BY s.session_date DESC, s.start_time DESC""",
                (user["id"],),
            ).fetchall()
        ]

        upcoming, past, cancelled = [], [], []
        for rr in room_res:
            item = {
                "kind": "room_reservation",
                "id": rr["id"],
                "code": rr["booking_code"],
                "title": rr["purpose"] or f"{rr['room_name']} Study Booking",
                "room_id": rr["room_id"],
                "room_name": rr["room_name"],
                "date": rr["reservation_date"],
                "start_time": rr["start_time"],
                "end_time": rr["end_time"],
                "status": rr["status"],
                "reservation_type": rr["reservation_type"],
                "checked_in_at": rr.get("checked_in_at"),
                "check_in_status": _compute_check_in_status(
                    rr["reservation_date"], rr["start_time"], rr["end_time"], rr.get("checked_in_at")
                ) if rr["status"] == "confirmed" else rr["status"],
            }
            if rr["status"] in ("cancelled", "rejected"):
                cancelled.append(item)
            elif rr["reservation_date"] < today_str:
                past.append(item)
            else:
                upcoming.append(item)

        for sr in sess_regs:
            item = {
                "kind": "session_registration",
                "id": sr["session_id"],
                "registration_id": sr["registration_id"],
                "code": sr["session_code"],
                "title": sr["title"],
                "session_type": sr["session_type"],
                "group_size": sr["group_size"],
                "room_id": sr["room_id"],
                "room_name": sr["room_name"],
                "date": sr["session_date"],
                "start_time": sr["start_time"],
                "end_time": sr["end_time"],
                "status": sr["registration_status"],
            }
            if sr["registration_status"] == "cancelled":
                cancelled.append(item)
            elif sr["session_date"] < today_str:
                past.append(item)
            else:
                upcoming.append(item)

        return {"upcoming": upcoming, "past": past, "cancelled": cancelled}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# DEDICATED CLUB PORTAL (APPROVED CLUB LEADERS ONLY)
# ---------------------------------------------------------------------------
@app.get("/api/club/dashboard")
def club_dashboard(user: Dict = Depends(get_current_user)):
    require_club_leader(user)
    club_ids = [c["id"] for c in user["approved_clubs"]]
    conn = get_connection()
    try:
        placeholders = ",".join("?" for _ in club_ids)
        events = [
            dict(e)
            for e in conn.execute(
                f"""SELECT ce.*, c.name AS club_name, r.name AS room_name,
                           (SELECT COALESCE(SUM(sr.group_size), 0)
                            FROM study_sessions ss
                            JOIN session_registrations sr ON sr.session_id = ss.id AND sr.status = 'confirmed'
                            WHERE ss.club_event_id = ce.id) AS registered_count
                    FROM club_events ce
                    JOIN clubs c ON ce.club_id = c.id
                    JOIN rooms r ON ce.room_id = r.id
                    WHERE ce.club_id IN ({placeholders})
                    ORDER BY ce.event_date, ce.start_time""",
                club_ids,
            ).fetchall()
        ]
        return {
            "clubs": user["approved_clubs"],
            "events": events,
            "pending_count": sum(1 for e in events if e["status"] == "pending_approval"),
            "confirmed_count": sum(1 for e in events if e["status"] == "confirmed"),
        }
    finally:
        conn.close()


@app.get("/api/club/rooms/search")
def search_club_rooms(
    event_date: str = Query(...),
    start_time: str = Query(...),
    end_time: str = Query(...),
    expected_participants: int = Query(default=10, ge=1),
    floor: Optional[int] = Query(default=None),
    user: Dict = Depends(get_current_user),
):
    require_club_leader(user)
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT r.*, p.access_mode, p.open_time, p.close_time, p.buffer_minutes
               FROM rooms r JOIN room_permissions p ON r.id = p.room_id
               WHERE r.is_active = 1 AND p.access_mode IN ('club_only', 'shared')
               ORDER BY CASE p.access_mode WHEN 'club_only' THEN 0 ELSE 1 END, r.capacity"""
        ).fetchall()

        eligible = []
        for r in rows:
            if r["capacity"] < expected_participants:
                continue
            if floor is not None and r["floor"] != floor:
                continue
            ok, _ = is_interval_free(conn, r["id"], r, event_date, start_time, end_time)
            if ok:
                eligible.append({
                    "room_id": r["id"],
                    "name": r["name"],
                    "building": r["building"],
                    "floor": r["floor"],
                    "room_type": r["room_type"],
                    "capacity": r["capacity"],
                    "access_mode": r["access_mode"],
                    "amenities": [a.strip() for a in r["amenities"].split(",") if a.strip()],
                })
        return {
            "event_date": event_date,
            "start_time": start_time,
            "end_time": end_time,
            "eligible_rooms": eligible,
        }
    finally:
        conn.close()


@app.post("/api/club/events", status_code=201)
def create_club_event(payload: ClubEventCreate, user: Dict = Depends(get_current_user)):
    require_club_leader(user)
    if payload.club_id not in [c["id"] for c in user["approved_clubs"]]:
        raise HTTPException(status_code=403, detail="You are not an approved leader for this club.")

    dur = to_mins(payload.end_time) - to_mins(payload.start_time)
    if dur < MIN_RESERVATION_MINUTES or dur > MAX_CLUB_RESERVATION_MINUTES:
        raise HTTPException(
            status_code=400,
            detail=f"Club events must be between {MIN_RESERVATION_MINUTES} and {MAX_CLUB_RESERVATION_MINUTES} minutes.",
        )

    conn = get_connection()
    try:
        with transaction(conn):
            r = conn.execute(
                """SELECT r.*, p.access_mode, p.open_time, p.close_time, p.buffer_minutes
                   FROM rooms r JOIN room_permissions p ON r.id = p.room_id
                   WHERE r.id = ? AND r.is_active = 1""",
                (payload.room_id,),
            ).fetchone()
            if not r:
                raise HTTPException(status_code=404, detail="Room not found.")
            if r["access_mode"] not in ("club_only", "shared"):
                raise HTTPException(
                    status_code=403,
                    detail=f"Room {r['name']} is '{r['access_mode']}' and not eligible for club bookings.",
                )
            if payload.expected_participants > r["capacity"]:
                raise HTTPException(status_code=400, detail=f"Expected participants exceed room capacity ({r['capacity']}).")

            ok, reason = is_interval_free(conn, r["id"], r, payload.event_date, payload.start_time, payload.end_time)
            if not ok:
                raise HTTPException(status_code=409, detail=reason)

            auto_approve = _get_setting(conn, "club_auto_approve", "false") == "true"
            status = "confirmed" if auto_approve else "pending_approval"
            ts = now_tashkent_iso()
            code = "CLB-" + uuid.uuid4().hex[:6].upper()

            cur_res = conn.execute(
                """INSERT INTO room_reservations
                   (booking_code, room_id, user_id, reservation_type, reservation_date, start_time, end_time,
                    purpose, expected_attendees, status, created_at, updated_at)
                   VALUES (?, ?, ?, 'club_event', ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    code,
                    r["id"],
                    user["id"],
                    payload.event_date,
                    payload.start_time,
                    payload.end_time,
                    payload.title,
                    payload.expected_participants,
                    status,
                    ts,
                    ts,
                ),
            )
            res_id = cur_res.lastrowid
            cur_ev = conn.execute(
                """INSERT INTO club_events
                   (club_id, leader_user_id, reservation_id, title, description, category, event_date,
                    start_time, end_time, room_id, expected_participants, is_open_to_all, is_published,
                    status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)""",
                (
                    payload.club_id,
                    user["id"],
                    res_id,
                    payload.title,
                    payload.description,
                    payload.category,
                    payload.event_date,
                    payload.start_time,
                    payload.end_time,
                    r["id"],
                    payload.expected_participants,
                    1 if payload.is_open_to_all else 0,
                    status,
                    ts,
                    ts,
                ),
            )
            ev_id = cur_ev.lastrowid
            _queue_notification(
                conn,
                user["id"],
                f"club_ev_create_{ev_id}",
                "club_event_created",
                "Club Event Request Submitted" if status == "pending_approval" else "Club Event Confirmed",
                f"'{payload.title}' in {r['name']} ({payload.event_date} {payload.start_time}–{payload.end_time}) is {status.replace('_', ' ')}.",
            )
            return {"event_id": ev_id, "reservation_id": res_id, "booking_code": code, "status": status}
    finally:
        conn.close()


@app.patch("/api/club/events/{event_id}")
def update_club_event(event_id: int, payload: ClubEventUpdate, user: Dict = Depends(get_current_user)):
    require_club_leader(user)
    conn = get_connection()
    try:
        with transaction(conn):
            ev = conn.execute("SELECT * FROM club_events WHERE id = ?", (event_id,)).fetchone()
            if not ev:
                raise HTTPException(status_code=404, detail="Club event not found.")
            if ev["club_id"] not in [c["id"] for c in user["approved_clubs"]]:
                raise HTTPException(status_code=403, detail="Not authorized for this club event.")
            if ev["status"] in ("cancelled", "rejected"):
                raise HTTPException(status_code=400, detail="Cannot modify a cancelled or rejected event.")

            title = payload.title or ev["title"]
            desc = payload.description or ev["description"]
            cat = payload.category or ev["category"]
            exp = payload.expected_participants or ev["expected_participants"]
            is_open = ev["is_open_to_all"] if payload.is_open_to_all is None else (1 if payload.is_open_to_all else 0)
            ts = now_tashkent_iso()

            conn.execute(
                """UPDATE club_events SET title=?, description=?, category=?, expected_participants=?, is_open_to_all=?, updated_at=?
                   WHERE id=?""",
                (title, desc, cat, exp, is_open, ts, event_id),
            )
            conn.execute(
                "UPDATE study_sessions SET title=?, description=?, max_capacity=?, is_public=? WHERE club_event_id=?",
                (title, desc, exp, is_open, event_id),
            )
            return {"event_id": event_id, "title": title, "status": ev["status"]}
    finally:
        conn.close()


@app.post("/api/club/events/{event_id}/publish")
def publish_club_event(event_id: int, user: Dict = Depends(get_current_user)):
    require_club_leader(user)
    conn = get_connection()
    try:
        with transaction(conn):
            ev = conn.execute("SELECT * FROM club_events WHERE id = ?", (event_id,)).fetchone()
            if not ev:
                raise HTTPException(status_code=404, detail="Club event not found.")
            if ev["club_id"] not in [c["id"] for c in user["approved_clubs"]]:
                raise HTTPException(status_code=403, detail="Not authorized for this club event.")
            if ev["status"] != "confirmed":
                raise HTTPException(status_code=400, detail="Only confirmed club events can be published to Sessions.")
            if not ev["is_open_to_all"]:
                raise HTTPException(status_code=400, detail="Private club events cannot be published publicly.")

            ts = now_tashkent_iso()
            conn.execute("UPDATE club_events SET is_published = 1, updated_at = ? WHERE id = ?", (ts, event_id))
            existing = conn.execute("SELECT id FROM study_sessions WHERE club_event_id = ?", (event_id,)).fetchone()
            if existing:
                sess_id = existing["id"]
                conn.execute("UPDATE study_sessions SET status = 'active', is_public = 1 WHERE id = ?", (sess_id,))
            else:
                code = "CE-" + uuid.uuid4().hex[:5].upper()
                cur = conn.execute(
                    """INSERT INTO study_sessions
                       (session_code, session_type, title, description, room_id, session_date, start_time, end_time,
                        max_capacity, min_group_size, max_groups, club_event_id, is_public, status, created_by, created_at)
                       VALUES (?, 'club_event', ?, ?, ?, ?, ?, ?, ?, 1, NULL, ?, 1, 'active', ?, ?)""",
                    (
                        code,
                        ev["title"],
                        ev["description"],
                        ev["room_id"],
                        ev["event_date"],
                        ev["start_time"],
                        ev["end_time"],
                        ev["expected_participants"],
                        event_id,
                        user["id"],
                        ts,
                    ),
                )
                sess_id = cur.lastrowid
            return {"event_id": event_id, "session_id": sess_id, "is_published": True}
    finally:
        conn.close()


@app.post("/api/club/events/{event_id}/cancel")
def cancel_club_event(event_id: int, user: Dict = Depends(get_current_user)):
    require_club_leader(user)
    conn = get_connection()
    try:
        with transaction(conn):
            ev = conn.execute("SELECT * FROM club_events WHERE id = ?", (event_id,)).fetchone()
            if not ev:
                raise HTTPException(status_code=404, detail="Club event not found.")
            if ev["club_id"] not in [c["id"] for c in user["approved_clubs"]]:
                raise HTTPException(status_code=403, detail="Not authorized for this club event.")

            ts = now_tashkent_iso()
            conn.execute("UPDATE club_events SET status = 'cancelled', is_published = 0, updated_at = ? WHERE id = ?", (ts, event_id))
            if ev["reservation_id"]:
                conn.execute("UPDATE room_reservations SET status = 'cancelled', updated_at = ? WHERE id = ?", (ts, ev["reservation_id"]))
            conn.execute("UPDATE study_sessions SET status = 'cancelled' WHERE club_event_id = ?", (event_id,))
            return {"event_id": event_id, "status": "cancelled"}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# ADMINISTRATOR ENDPOINTS (STRICT BACKEND RBAC)
# ---------------------------------------------------------------------------
@app.get("/api/admin/overview")
def admin_overview(user: Dict = Depends(get_current_user)):
    require_admin(user)
    conn = get_connection()
    try:
        rooms = [dict(r) for r in conn.execute("SELECT r.*, p.access_mode, p.open_time, p.close_time, p.buffer_minutes FROM rooms r JOIN room_permissions p ON r.id=p.room_id ORDER BY r.floor, r.id").fetchall()]
        clubs = [dict(c) for c in conn.execute("SELECT * FROM clubs ORDER BY id").fetchall()]
        users = [dict(u) for u in conn.execute("SELECT * FROM users ORDER BY id").fetchall()]
        pending_events = [dict(e) for e in conn.execute("SELECT ce.*, c.name AS club_name FROM club_events ce JOIN clubs c ON ce.club_id=c.id ORDER BY ce.id DESC").fetchall()]
        reservations = [dict(r) for r in conn.execute("SELECT rr.*, u.full_name FROM room_reservations rr JOIN users u ON rr.user_id=u.id ORDER BY rr.id DESC LIMIT 50").fetchall()]
        audit = [dict(a) for a in conn.execute("SELECT * FROM admin_actions ORDER BY id DESC LIMIT 30").fetchall()]
        return {
            "rooms": rooms,
            "clubs": clubs,
            "users": users,
            "club_events": pending_events,
            "reservations": reservations,
            "audit_log": audit,
            "club_auto_approve": _get_setting(conn, "club_auto_approve", "false") == "true",
        }
    finally:
        conn.close()


@app.post("/api/admin/rooms", status_code=201)
def admin_add_room(payload: AdminRoomCreate, user: Dict = Depends(get_current_user)):
    require_admin(user)
    conn = get_connection()
    try:
        with transaction(conn):
            conn.execute(
                "INSERT INTO rooms (id, name, building, floor, room_type, capacity, amenities) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (payload.id, payload.name, payload.building, payload.floor, payload.room_type, payload.capacity, payload.amenities),
            )
            conn.execute(
                "INSERT INTO room_permissions (room_id, access_mode, open_time, close_time, buffer_minutes) VALUES (?, ?, ?, ?, ?)",
                (payload.id, payload.access_mode, payload.open_time, payload.close_time, payload.buffer_minutes),
            )
            _log_admin(conn, user["id"], "add_room", "room", payload.id, f"access_mode={payload.access_mode}")
            return {"room_id": payload.id, "status": "created"}
    finally:
        conn.close()


@app.patch("/api/admin/rooms/{room_id}/permissions")
def admin_update_room_permissions(room_id: str, payload: AdminRoomPermissionUpdate, user: Dict = Depends(get_current_user)):
    require_admin(user)
    if payload.access_mode not in ("student_only", "club_only", "shared", "unavailable"):
        raise HTTPException(status_code=400, detail="Invalid access_mode.")
    conn = get_connection()
    try:
        with transaction(conn):
            cur = conn.execute("SELECT * FROM room_permissions WHERE room_id = ?", (room_id,)).fetchone()
            if not cur:
                raise HTTPException(status_code=404, detail="Room not found.")
            conn.execute(
                """UPDATE room_permissions SET access_mode=?, open_time=?, close_time=?, buffer_minutes=?, notes=?
                   WHERE room_id=?""",
                (
                    payload.access_mode,
                    payload.open_time or cur["open_time"],
                    payload.close_time or cur["close_time"],
                    cur["buffer_minutes"] if payload.buffer_minutes is None else payload.buffer_minutes,
                    cur["notes"] if payload.notes is None else payload.notes,
                    room_id,
                ),
            )
            _log_admin(conn, user["id"], "update_room_permissions", "room", room_id, f"access_mode={payload.access_mode}")
            return {"room_id": room_id, "access_mode": payload.access_mode}
    finally:
        conn.close()


@app.post("/api/admin/schedules", status_code=201)
def admin_add_schedule(payload: AdminScheduleCreate, user: Dict = Depends(get_current_user)):
    require_admin(user)
    conn = get_connection()
    try:
        with transaction(conn):
            cur = conn.execute(
                """INSERT INTO classroom_schedules (room_id, day_of_week, start_time, end_time, course_title, instructor, is_demo)
                   VALUES (?, ?, ?, ?, ?, ?, 1)""",
                (payload.room_id, payload.day_of_week, payload.start_time, payload.end_time, payload.course_title, payload.instructor),
            )
            _log_admin(conn, user["id"], "add_schedule", "classroom_schedule", str(cur.lastrowid), f"{payload.room_id} {payload.day_of_week} {payload.start_time}-{payload.end_time}")
            return {"schedule_id": cur.lastrowid, "status": "created"}
    finally:
        conn.close()


@app.post("/api/admin/rooms/block", status_code=201)
def admin_block_room(payload: AdminRoomBlockCreate, user: Dict = Depends(get_current_user)):
    require_admin(user)
    conn = get_connection()
    try:
        with transaction(conn):
            ts = now_tashkent_iso()
            cur = conn.execute(
                """INSERT INTO schedule_exceptions (room_id, exception_date, start_time, end_time, reason, exception_type, created_by, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (payload.room_id, payload.exception_date, payload.start_time, payload.end_time, payload.reason, payload.exception_type, user["id"], ts),
            )
            _log_admin(conn, user["id"], "block_room", "room", payload.room_id, f"{payload.exception_date} {payload.start_time}-{payload.end_time}: {payload.reason}")
            return {"exception_id": cur.lastrowid, "status": "blocked"}
    finally:
        conn.close()


@app.post("/api/admin/club-leaders/approve")
def admin_approve_club_leader(payload: AdminClubLeaderApprove, user: Dict = Depends(get_current_user)):
    require_admin(user)
    conn = get_connection()
    try:
        with transaction(conn):
            ts = now_tashkent_iso()
            status = "approved" if payload.action == "approve" else "revoked"
            conn.execute(
                """INSERT INTO club_leaders (user_id, club_id, status, approved_by, approved_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(user_id, club_id) DO UPDATE SET status=excluded.status, approved_by=excluded.approved_by, approved_at=excluded.approved_at""",
                (payload.user_id, payload.club_id, status, user["id"], ts),
            )
            if status == "approved":
                conn.execute(
                    "INSERT OR IGNORE INTO user_roles (user_id, role, granted_by, granted_at) VALUES (?, 'club_leader', ?, ?)",
                    (payload.user_id, user["id"], ts),
                )
            else:
                remaining = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM club_leaders WHERE user_id = ? AND status = 'approved'",
                    (payload.user_id,),
                ).fetchone()["cnt"]
                if remaining == 0:
                    conn.execute("DELETE FROM user_roles WHERE user_id = ? AND role = 'club_leader'", (payload.user_id,))

            _queue_notification(
                conn,
                payload.user_id,
                f"leader_{payload.user_id}_{payload.club_id}_{status}_{ts}",
                "club_leader_status",
                "Club Leader Status Updated",
                f"Your club leader access for club #{payload.club_id} has been {status}.",
            )
            _log_admin(conn, user["id"], f"{status}_club_leader", "user", str(payload.user_id), f"club_id={payload.club_id}")
            return {"user_id": payload.user_id, "club_id": payload.club_id, "status": status}
    finally:
        conn.close()


@app.post("/api/admin/events/{event_id}/review")
def admin_review_club_event(event_id: int, payload: AdminEventReview, user: Dict = Depends(get_current_user)):
    require_admin(user)
    if payload.decision not in ("approve", "reject"):
        raise HTTPException(status_code=400, detail="Decision must be 'approve' or 'reject'.")
    conn = get_connection()
    try:
        with transaction(conn):
            ev = conn.execute("SELECT * FROM club_events WHERE id = ?", (event_id,)).fetchone()
            if not ev:
                raise HTTPException(status_code=404, detail="Club event not found.")
            new_status = "confirmed" if payload.decision == "approve" else "rejected"
            ts = now_tashkent_iso()
            conn.execute("UPDATE club_events SET status = ?, updated_at = ? WHERE id = ?", (new_status, ts, event_id))
            if ev["reservation_id"]:
                conn.execute("UPDATE room_reservations SET status = ?, updated_at = ? WHERE id = ?", (new_status, ts, ev["reservation_id"]))
            _queue_notification(
                conn,
                ev["leader_user_id"],
                f"ev_review_{event_id}_{new_status}",
                "club_event_reviewed",
                f"Club Event {new_status.title()}",
                f"Your club event '{ev['title']}' in Room {ev['room_id']} was {new_status}. {payload.note}".strip(),
            )
            _log_admin(conn, user["id"], f"{payload.decision}_club_event", "club_event", str(event_id), payload.note)
            return {"event_id": event_id, "status": new_status}
    finally:
        conn.close()


@app.post("/api/admin/sessions", status_code=201)
def admin_create_session(payload: AdminSessionCreate, user: Dict = Depends(get_current_user)):
    require_admin(user)
    conn = get_connection()
    try:
        with transaction(conn):
            ts = now_tashkent_iso()
            prefix = "QS-" if payload.session_type == "quiet" else "CS-"
            code = prefix + uuid.uuid4().hex[:5].upper()
            cur = conn.execute(
                """INSERT INTO study_sessions
                   (session_code, session_type, title, description, room_id, session_date, start_time, end_time,
                    max_capacity, min_group_size, max_groups, club_event_id, is_public, status, created_by, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 1, 'active', ?, ?)""",
                (
                    code,
                    payload.session_type,
                    payload.title,
                    payload.description,
                    payload.room_id,
                    payload.session_date,
                    payload.start_time,
                    payload.end_time,
                    payload.max_capacity,
                    payload.min_group_size,
                    payload.max_groups,
                    user["id"],
                    ts,
                ),
            )
            _log_admin(conn, user["id"], "create_session", "study_session", str(cur.lastrowid), code)
            return {"session_id": cur.lastrowid, "session_code": code}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# TELEGRAM BOT WEBHOOK (LIVE /start, /rooms, /bookings, /help)
# ---------------------------------------------------------------------------
@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    try:
        update = await request.json()
    except Exception:
        return {"ok": True}

    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return {"ok": True}

    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    from_user = msg.get("from") or {}
    tg_id = from_user.get("id")
    first_name = (from_user.get("first_name") or "Student").strip()
    text = (msg.get("text") or "").strip()

    if not chat_id:
        return {"ok": True}

    cmd = text.split()[0].lower().split("@")[0] if text.startswith("/") else ""

    if cmd == "/start":
        reply = (
            f"👋 <b>Welcome to AUT SpaceHub, {first_name}!</b>\n\n"
            "Find live free classrooms at <b>AUT Beshyogoch Campus</b>, book quiet study slots, "
            "join Collaboration sessions, or submit club events — all right inside Telegram.\n\n"
            "Tap the button below to launch SpaceHub, or use /rooms and /bookings anytime."
        )
        _send_telegram_message_async(chat_id, reply, include_open_btn=True)
        return {"ok": True}

    if cmd == "/rooms":
        date_str, time_str = _default_tashkent_date_time()
        conn = get_connection()
        try:
            rooms = conn.execute(
                """SELECT r.*, p.access_mode, p.open_time, p.close_time, p.buffer_minutes, p.notes AS perm_notes
                   FROM rooms r JOIN room_permissions p ON r.id = p.room_id
                   WHERE r.is_active = 1 AND p.access_mode IN ('student_only', 'shared')
                   ORDER BY r.floor, r.id"""
            ).fetchall()
            free_lines = []
            for r in rooms:
                avail = compute_room_availability(conn, r, r, date_str, time_str)
                if avail["status"] == "free_now":
                    free_lines.append(f"• <b>{r['name']}</b> — {avail['status_label']}")
        finally:
            conn.close()
        if free_lines:
            shown = "\n".join(free_lines[:8])
            more = f"\n<i>+{len(free_lines) - 8} more in SpaceHub</i>" if len(free_lines) > 8 else ""
            reply = f"🟢 <b>Free AUT Rooms Now ({time_str} Tashkent)</b>\n{shown}{more}"
        else:
            reply = (
                f"🕒 <b>AUT Campus Status ({time_str} Tashkent)</b>\n"
                "No rooms are free right now (campus study hours are 08:00–21:00). "
                "Open SpaceHub to view and book tomorrow's open slots."
            )
        _send_telegram_message_async(chat_id, reply, include_open_btn=True)
        return {"ok": True}

    if cmd == "/bookings":
        conn = get_connection()
        try:
            user = conn.execute("SELECT id FROM users WHERE telegram_id = ?", (tg_id,)).fetchone() if tg_id else None
            lines = []
            if user:
                res_rows = conn.execute(
                    """SELECT rr.room_id, rr.reservation_date, rr.start_time, rr.end_time, rr.booking_code
                       FROM room_reservations rr
                       WHERE rr.user_id = ? AND rr.status = 'confirmed'
                       ORDER BY rr.reservation_date, rr.start_time LIMIT 6""",
                    (user["id"],),
                ).fetchall()
                for row in res_rows:
                    lines.append(
                        f"• <b>Room {row['room_id']}</b> — {row['reservation_date']} "
                        f"({row['start_time']}–{row['end_time']}) · <code>{row['booking_code']}</code>"
                    )
                sess_rows = conn.execute(
                    """SELECT s.title, s.room_id, s.session_date, s.start_time, s.end_time
                       FROM session_registrations sr
                       JOIN study_sessions s ON s.id = sr.session_id
                       WHERE sr.user_id = ? AND sr.status = 'confirmed'
                       ORDER BY s.session_date, s.start_time LIMIT 6""",
                    (user["id"],),
                ).fetchall()
                for row in sess_rows:
                    lines.append(
                        f"• <b>{row['title']}</b> (Room {row['room_id']}) — "
                        f"{row['session_date']} ({row['start_time']}–{row['end_time']})"
                    )
        finally:
            conn.close()
        if lines:
            reply = "📅 <b>Your Active AUT SpaceHub Bookings</b>\n" + "\n".join(lines)
        else:
            reply = "📅 You don't have any active bookings yet. Tap below to reserve a study room or join a session!"
        _send_telegram_message_async(chat_id, reply, include_open_btn=True)
        return {"ok": True}

    if cmd == "/help":
        reply = (
            "ℹ️ <b>AUT SpaceHub Commands</b>\n"
            "/start — Welcome screen & launch button\n"
            "/rooms — Check free AUT rooms right now\n"
            "/bookings — See your active bookings\n"
            "/help — Show this help menu\n\n"
            "You'll also receive automatic Telegram confirmations here whenever you book a room or join a session."
        )
        _send_telegram_message_async(chat_id, reply, include_open_btn=True)
        return {"ok": True}

    reply = "Tap <b>Open AUT SpaceHub</b> below to browse rooms, join sessions, or manage bookings."
    _send_telegram_message_async(chat_id, reply, include_open_btn=True)
    return {"ok": True}
