from datetime import date, datetime
from typing import Dict, List, Optional, Tuple
from .config import APP_TIMEZONE, TIMEZONE_NAME


def to_mins(hhmm: str) -> int:
    h, m = hhmm.strip().split(":")
    return int(h) * 60 + int(m)


def to_hhmm(mins: int) -> str:
    h = mins // 60
    m = mins % 60
    return f"{h:02d}:{m:02d}"


def format_duration(mins: int) -> str:
    if mins <= 0:
        return "0 minutes"
    h = mins // 60
    m = mins % 60
    parts = []
    if h > 0:
        parts.append(f"{h} hour" + ("s" if h != 1 else ""))
    if m > 0:
        parts.append(f"{m} minute" + ("s" if m != 1 else ""))
    return " ".join(parts)


def weekday_name(date_str: str) -> str:
    d = date.fromisoformat(date_str)
    return d.strftime("%A")


def merge_intervals(intervals: List[Tuple[int, int]]) -> List[Tuple[int, int]]:
    if not intervals:
        return []
    sorted_iv = sorted(intervals, key=lambda x: (x[0], x[1]))
    merged = [sorted_iv[0]]
    for start, end in sorted_iv[1:]:
        prev_s, prev_e = merged[-1]
        if start <= prev_e:
            merged[-1] = (prev_s, max(prev_e, end))
        else:
            merged.append((start, end))
    return merged


def get_room_blocking_events(conn, room_id: str, target_date: str) -> List[Dict]:
    """
    Collects ALL authoritative blocking intervals for a room on target_date:
    1. Official recurring classroom schedules
    2. Confirmed & pending_approval room reservations (student_study + club_event)
    3. Active study sessions (quiet + collaboration + club_event)
    4. Administrative schedule exceptions / maintenance blocks
    """
    dow = weekday_name(target_date)
    blocks: List[Dict] = []

    # 1. Recurring classes
    for r in conn.execute(
        "SELECT id, start_time, end_time, course_title, instructor FROM classroom_schedules WHERE room_id = ? AND day_of_week = ?",
        (room_id, dow),
    ).fetchall():
        blocks.append({
            "source": "class",
            "title": r["course_title"],
            "subtitle": r["instructor"],
            "start": r["start_time"],
            "end": r["end_time"],
            "start_min": to_mins(r["start_time"]),
            "end_min": to_mins(r["end_time"]),
        })

    # 2. Schedule exceptions / admin blocks
    for r in conn.execute(
        "SELECT id, start_time, end_time, reason, exception_type FROM schedule_exceptions WHERE room_id = ? AND exception_date = ?",
        (room_id, target_date),
    ).fetchall():
        blocks.append({
            "source": "exception",
            "title": f"{r['exception_type'].replace('_', ' ').title()}: {r['reason']}",
            "subtitle": "Admin restriction",
            "start": r["start_time"],
            "end": r["end_time"],
            "start_min": to_mins(r["start_time"]),
            "end_min": to_mins(r["end_time"]),
        })

    # 3. Room reservations (confirmed or pending_approval block the slot to prevent double booking)
    for r in conn.execute(
        """SELECT id, booking_code, reservation_type, start_time, end_time, purpose, status
           FROM room_reservations
           WHERE room_id = ? AND reservation_date = ? AND status IN ('confirmed', 'pending_approval')""",
        (room_id, target_date),
    ).fetchall():
        blocks.append({
            "source": r["reservation_type"],
            "title": r["purpose"] or ("Club Reservation" if r["reservation_type"] == "club_event" else "Student Reservation"),
            "subtitle": f"{r['booking_code']} ({r['status']})",
            "start": r["start_time"],
            "end": r["end_time"],
            "start_min": to_mins(r["start_time"]),
            "end_min": to_mins(r["end_time"]),
        })

    # 4. Active study sessions (not linked to an already-counted club reservation, or deduplicated via merge)
    for r in conn.execute(
        """SELECT id, session_code, session_type, title, start_time, end_time
           FROM study_sessions
           WHERE room_id = ? AND session_date = ? AND status = 'active'""",
        (room_id, target_date),
    ).fetchall():
        blocks.append({
            "source": f"session_{r['session_type']}",
            "title": r["title"],
            "subtitle": r["session_code"],
            "start": r["start_time"],
            "end": r["end_time"],
            "start_min": to_mins(r["start_time"]),
            "end_min": to_mins(r["end_time"]),
        })

    blocks.sort(key=lambda b: (b["start_min"], b["end_min"]))
    return blocks


def compute_room_availability(
    conn,
    room_row,
    perm_row,
    target_date: str,
    check_time: str,
) -> Dict:
    """
    Authoritative free-interval calculation in Asia/Tashkent.
    Returns exact status, free_until, remaining_minutes, next_available_window,
    and all continuous free windows for the date.
    """
    open_min = to_mins(perm_row["open_time"])
    close_min = to_mins(perm_row["close_time"])
    buffer_min = int(perm_row["buffer_minutes"] or 0)
    now_min = to_mins(check_time)

    access_mode = perm_row["access_mode"]
    blocks = get_room_blocking_events(conn, room_row["id"], target_date)

    # Check if schedule exists for the campus (surface warning if missing/outdated)
    sched_count = conn.execute("SELECT COUNT(*) AS cnt FROM classroom_schedules").fetchone()["cnt"]
    schedule_warning = None if sched_count > 0 else "University timetable data is missing; availability cannot be confirmed."

    if access_mode == "unavailable" or not room_row["is_active"]:
        return {
            "room_id": room_row["id"],
            "name": room_row["name"],
            "building": room_row["building"],
            "floor": room_row["floor"],
            "room_type": room_row["room_type"],
            "capacity": room_row["capacity"],
            "amenities": [a.strip() for a in room_row["amenities"].split(",") if a.strip()],
            "access_mode": access_mode,
            "timezone": TIMEZONE_NAME,
            "date": target_date,
            "checked_at": check_time,
            "status": "unavailable",
            "status_label": "UNAVAILABLE",
            "free_until": None,
            "remaining_minutes": 0,
            "remaining_text": "0 minutes",
            "next_available_start": None,
            "next_available_end": None,
            "next_available_window": None,
            "current_event": None,
            "free_windows": [],
            "events": blocks,
            "schedule_warning": schedule_warning,
        }

    # Apply buffer around each blocking interval
    padded = [
        (max(open_min, b["start_min"] - buffer_min), min(close_min, b["end_min"] + buffer_min))
        for b in blocks
        if b["end_min"] > open_min and b["start_min"] < close_min
    ]
    merged_busy = merge_intervals(padded)

    # Build all free windows within [open_min, close_min]
    free_windows: List[Tuple[int, int]] = []
    cursor = open_min
    for b_start, b_end in merged_busy:
        if b_start > cursor:
            free_windows.append((cursor, b_start))
        cursor = max(cursor, b_end)
    if cursor < close_min:
        free_windows.append((cursor, close_min))

    free_window_dicts = [
        {"start": to_hhmm(s), "end": to_hhmm(e), "duration_minutes": e - s, "duration_text": format_duration(e - s)}
        for s, e in free_windows
    ]

    # Determine status at check_time
    if now_min < open_min or now_min >= close_min:
        next_win = free_windows[0] if (now_min < open_min and free_windows) else None
        return {
            "room_id": room_row["id"],
            "name": room_row["name"],
            "building": room_row["building"],
            "floor": room_row["floor"],
            "room_type": room_row["room_type"],
            "capacity": room_row["capacity"],
            "amenities": [a.strip() for a in room_row["amenities"].split(",") if a.strip()],
            "access_mode": access_mode,
            "timezone": TIMEZONE_NAME,
            "date": target_date,
            "checked_at": check_time,
            "status": "closed",
            "status_label": f"CLOSED (Hours {perm_row['open_time']}–{perm_row['close_time']})",
            "free_until": None,
            "remaining_minutes": 0,
            "remaining_text": "0 minutes",
            "next_available_start": to_hhmm(next_win[0]) if next_win else None,
            "next_available_end": to_hhmm(next_win[1]) if next_win else None,
            "next_available_window": f"{to_hhmm(next_win[0])}–{to_hhmm(next_win[1])}" if next_win else None,
            "current_event": None,
            "free_windows": free_window_dicts,
            "events": blocks,
            "schedule_warning": schedule_warning,
        }

    # Check if now_min sits inside a free window
    active_free = next(((s, e) for s, e in free_windows if s <= now_min < e), None)
    if active_free:
        rem = active_free[1] - now_min
        free_until = to_hhmm(active_free[1])
        status = "available" if rem >= 60 else "closing_soon"
        label = f"AVAILABLE NOW · FREE UNTIL {free_until}" if status == "available" else f"FREE SOON ENDING · UNTIL {free_until}"
        return {
            "room_id": room_row["id"],
            "name": room_row["name"],
            "building": room_row["building"],
            "floor": room_row["floor"],
            "room_type": room_row["room_type"],
            "capacity": room_row["capacity"],
            "amenities": [a.strip() for a in room_row["amenities"].split(",") if a.strip()],
            "access_mode": access_mode,
            "timezone": TIMEZONE_NAME,
            "date": target_date,
            "checked_at": check_time,
            "status": status,
            "status_label": label,
            "free_until": free_until,
            "remaining_minutes": rem,
            "remaining_text": format_duration(rem),
            "next_available_start": check_time,
            "next_available_end": free_until,
            "next_available_window": f"{check_time}–{free_until}",
            "current_event": None,
            "free_windows": free_window_dicts,
            "events": blocks,
            "schedule_warning": schedule_warning,
        }

    # Currently occupied — find active event and next valid free window after now_min
    current_ev = next((b for b in blocks if b["start_min"] <= now_min < b["end_min"]), None)
    next_win = next(((s, e) for s, e in free_windows if s > now_min), None)
    next_start = to_hhmm(next_win[0]) if next_win else None
    next_end = to_hhmm(next_win[1]) if next_win else None
    next_window_str = f"{next_start}–{next_end}" if next_win else None
    status_label = f"OCCUPIED · NEXT FREE {next_start}" if next_start else "OCCUPIED · NO MORE SLOTS TODAY"

    return {
        "room_id": room_row["id"],
        "name": room_row["name"],
        "building": room_row["building"],
        "floor": room_row["floor"],
        "room_type": room_row["room_type"],
        "capacity": room_row["capacity"],
        "amenities": [a.strip() for a in room_row["amenities"].split(",") if a.strip()],
        "access_mode": access_mode,
        "timezone": TIMEZONE_NAME,
        "date": target_date,
        "checked_at": check_time,
        "status": "occupied",
        "status_label": status_label,
        "free_until": None,
        "remaining_minutes": 0,
        "remaining_text": "0 minutes",
        "next_available_start": next_start,
        "next_available_end": next_end,
        "next_available_window": next_window_str,
        "current_event": current_ev,
        "free_windows": free_window_dicts,
        "events": blocks,
        "schedule_warning": schedule_warning,
    }


def is_interval_free(conn, room_id: str, perm_row, target_date: str, start_time: str, end_time: str) -> Tuple[bool, str]:
    """Authoritative backend check for whether [start_time, end_time) is completely free."""
    s_min = to_mins(start_time)
    e_min = to_mins(end_time)
    if e_min <= s_min:
        return False, "End time must be after start time."
    open_min = to_mins(perm_row["open_time"])
    close_min = to_mins(perm_row["close_time"])
    if s_min < open_min or e_min > close_min:
        return False, f"Requested time is outside room booking hours ({perm_row['open_time']}–{perm_row['close_time']})."

    buffer_min = int(perm_row["buffer_minutes"] or 0)
    blocks = get_room_blocking_events(conn, room_id, target_date)
    for b in blocks:
        b_s = b["start_min"] - buffer_min
        b_e = b["end_min"] + buffer_min
        if s_min < b_e and e_min > b_s:
            return False, f"Time conflict with '{b['title']}' ({b['start']}–{b['end']})."
    return True, "OK"
