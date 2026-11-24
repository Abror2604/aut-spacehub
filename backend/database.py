import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from .config import DB_PATH, APP_TIMEZONE


def now_tashkent_iso() -> str:
    return datetime.now(APP_TIMEZONE).isoformat(timespec="seconds")


def get_connection(db_path: str = None) -> sqlite3.Connection:
    path = db_path or DB_PATH
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, timeout=30.0, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=10000;")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    """Serializable write lock using BEGIN IMMEDIATE to prevent concurrent booking races."""
    conn.execute("BEGIN IMMEDIATE;")
    try:
        yield conn
        conn.execute("COMMIT;")
    except Exception:
        conn.execute("ROLLBACK;")
        raise


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id TEXT UNIQUE NOT NULL,
    username TEXT,
    full_name TEXT NOT NULL,
    email TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_roles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('student', 'club_leader', 'admin')),
    granted_by INTEGER REFERENCES users(id),
    granted_at TEXT NOT NULL,
    UNIQUE(user_id, role)
);

CREATE TABLE IF NOT EXISTS clubs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    category TEXT NOT NULL,
    description TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS club_leaders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    club_id INTEGER NOT NULL REFERENCES clubs(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'revoked')),
    approved_by INTEGER REFERENCES users(id),
    approved_at TEXT,
    UNIQUE(user_id, club_id)
);

CREATE TABLE IF NOT EXISTS rooms (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    building TEXT NOT NULL DEFAULT 'Beshyogoch Main Campus',
    floor INTEGER NOT NULL,
    room_type TEXT NOT NULL,
    capacity INTEGER NOT NULL,
    amenities TEXT NOT NULL DEFAULT '',
    is_active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS room_permissions (
    room_id TEXT PRIMARY KEY REFERENCES rooms(id) ON DELETE CASCADE,
    access_mode TEXT NOT NULL CHECK (access_mode IN ('student_only', 'club_only', 'shared', 'unavailable')),
    open_time TEXT NOT NULL DEFAULT '08:00',
    close_time TEXT NOT NULL DEFAULT '21:00',
    buffer_minutes INTEGER NOT NULL DEFAULT 0,
    notes TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS classroom_schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    day_of_week TEXT NOT NULL CHECK (day_of_week IN ('Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday')),
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    course_title TEXT NOT NULL,
    instructor TEXT DEFAULT '',
    is_demo INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS schedule_exceptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    exception_date TEXT NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    reason TEXT NOT NULL,
    exception_type TEXT NOT NULL CHECK (exception_type IN ('maintenance', 'admin_block', 'special_event', 'closure')),
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS room_reservations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    booking_code TEXT UNIQUE NOT NULL,
    room_id TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    reservation_type TEXT NOT NULL CHECK (reservation_type IN ('student_study', 'club_event')),
    reservation_date TEXT NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    purpose TEXT DEFAULT '',
    expected_attendees INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL CHECK (status IN ('pending_approval', 'confirmed', 'rejected', 'cancelled')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS study_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_code TEXT UNIQUE NOT NULL,
    session_type TEXT NOT NULL CHECK (session_type IN ('quiet', 'collaboration', 'club_event')),
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    room_id TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    session_date TEXT NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    max_capacity INTEGER NOT NULL,
    min_group_size INTEGER NOT NULL DEFAULT 1,
    max_groups INTEGER,
    club_event_id INTEGER,
    is_public INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'cancelled')),
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS club_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    club_id INTEGER NOT NULL REFERENCES clubs(id) ON DELETE CASCADE,
    leader_user_id INTEGER NOT NULL REFERENCES users(id),
    reservation_id INTEGER REFERENCES room_reservations(id) ON DELETE SET NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    category TEXT NOT NULL,
    event_date TEXT NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    room_id TEXT NOT NULL REFERENCES rooms(id),
    expected_participants INTEGER NOT NULL,
    is_open_to_all INTEGER NOT NULL DEFAULT 1,
    is_published INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL CHECK (status IN ('pending_approval', 'confirmed', 'rejected', 'cancelled')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS session_registrations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES study_sessions(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    group_size INTEGER NOT NULL DEFAULT 1,
    notes TEXT DEFAULT '',
    status TEXT NOT NULL CHECK (status IN ('confirmed', 'cancelled')),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    event_key TEXT UNIQUE NOT NULL,
    notif_type TEXT NOT NULL,
    title TEXT NOT NULL,
    message TEXT NOT NULL,
    is_sent INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS admin_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_user_id INTEGER NOT NULL REFERENCES users(id),
    action_type TEXT NOT NULL,
    target_entity TEXT NOT NULL,
    target_id TEXT NOT NULL,
    details TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS platform_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def init_db(db_path: str = None, seed: bool = True) -> sqlite3.Connection:
    conn = get_connection(db_path)
    conn.executescript(SCHEMA_SQL)
    if seed:
        seed_campus_data(conn)
    return conn


def seed_campus_data(conn: sqlite3.Connection):
    """
    Seeds ONLY real AUT campus structure (13 Beshyogoch rooms, Fall 2026 class timetables,
    official clubs, and open upcoming study sessions with ZERO fake user bookings).
    """
    cur = conn.execute("SELECT COUNT(*) AS cnt FROM rooms")
    if cur.fetchone()["cnt"] > 0:
        return

    now_dt = datetime.now(APP_TIMEZONE)
    today = now_dt.date()
    tomorrow = today + timedelta(days=1)
    day_after = today + timedelta(days=2)
    ts = now_dt.isoformat(timespec="seconds")

    with transaction(conn):
        settings = [
            ("club_auto_approve", "false"),
            ("demo_mode", "false"),
            ("schedule_last_updated", today.isoformat()),
            ("schedule_term", "AUT Fall 2026 Timetable"),
            ("cancellation_cutoff_minutes", "15"),
        ]
        conn.executemany("INSERT OR IGNORE INTO platform_settings (key, value) VALUES (?, ?)", settings)

        # Official AUT Clubs (no fake users pre-seeded; real Telegram users register automatically on /start or Mini App open)
        clubs = [
            ("AUT Innovation & Entrepreneurship Club", "Business & Innovation", "Workshops, startup pitches, and design sprints for AUT students.", 1, ts),
            ("AUT Tech & Coding Society", "Technology", "Algorithm jams, hackathons, and software engineering study circles.", 1, ts),
            ("AUT Debate & Diplomacy Club", "Public Speaking", "Weekly parliamentary debates and MUN preparation.", 1, ts),
        ]
        conn.executemany(
            "INSERT INTO clubs (name, category, description, is_active, created_at) VALUES (?, ?, ?, ?, ?)",
            clubs,
        )

        # 13 AUT Beshyogoch Campus Rooms
        rooms = [
            ("201", "Room 201", "Beshyogoch Main Campus", 2, "Seminar Room", 24, "Whiteboard, Projector, Power Outlets"),
            ("202", "Room 202", "Beshyogoch Main Campus", 2, "Lecture Room", 30, "Whiteboard, Projector, Wi-Fi"),
            ("301", "Room 301", "Beshyogoch Main Campus", 3, "Quiet Study & Lecture Room", 28, "Quiet Zone, Whiteboard, Power Outlets"),
            ("302", "Room 302", "Beshyogoch Main Campus", 3, "Business Collaboration Studio", 24, "Movable Tables, Whiteboard, Screen"),
            ("303", "Room 303", "Beshyogoch Main Campus", 3, "Tech Lab", 24, "Workstations, Projector, Power Strips"),
            ("304", "Room 304", "Beshyogoch Main Campus", 3, "TEM 230 / Trade Room", 28, "Whiteboard, Projector, Natural Light"),
            ("401", "Room 401", "Beshyogoch Main Campus", 4, "Seminar Room", 30, "Whiteboard, Projector, AC"),
            ("402", "Room 402", "Beshyogoch Main Campus", 4, "Physics & Study Room", 28, "Large Desks, Projector, Power Outlets"),
            ("403", "Room 403", "Beshyogoch Main Campus", 4, "Digital & Seminar Room", 26, "Hybrid AV, Whiteboard, Wi-Fi"),
            ("404", "Room 404", "Beshyogoch Main Campus", 4, "Club & Collaboration Room", 30, "Group Tables, Dual Whiteboards, Projector"),
            ("501", "Room 501", "Beshyogoch Main Campus", 5, "Club Event Hall", 40, "Tiered Seating, Stage Mic, AV System"),
            ("503", "Room 503", "Beshyogoch Main Campus", 5, "Quiet Computer Lab", 26, "Quiet Zone, Power Outlets, Projector"),
            ("601", "Room 601", "Beshyogoch Main Campus", 6, "Main Auditorium & Club Hall", 45, "Large Hall, Projector, Sound System"),
        ]
        conn.executemany(
            "INSERT INTO rooms (id, name, building, floor, room_type, capacity, amenities) VALUES (?, ?, ?, ?, ?, ?, ?)",
            rooms,
        )

        perms = [
            ("201", "student_only", "08:00", "21:00", 0, "Individual & small-group student study"),
            ("202", "shared", "08:00", "21:00", 0, "Open for students and approved club activities"),
            ("301", "student_only", "08:00", "21:00", 0, "Quiet study priority"),
            ("302", "shared", "08:00", "21:00", 0, "Collaboration & club workshops"),
            ("303", "student_only", "08:00", "21:00", 0, "Student coding & study"),
            ("304", "shared", "08:00", "21:00", 0, "Study & club sessions"),
            ("401", "student_only", "08:00", "21:00", 0, "Quiet study"),
            ("402", "student_only", "08:00", "21:00", 0, "Student study"),
            ("403", "unavailable", "08:00", "21:00", 0, "Reserved for faculty & graduate labs"),
            ("404", "club_only", "08:00", "21:00", 0, "Dedicated club collaboration & workshop room"),
            ("501", "club_only", "08:00", "21:00", 0, "Dedicated club event hall"),
            ("503", "student_only", "08:00", "21:00", 0, "Quiet computer lab"),
            ("601", "shared", "08:00", "21:00", 0, "Large hall for major club events & open study"),
        ]
        conn.executemany(
            "INSERT INTO room_permissions (room_id, access_mode, open_time, close_time, buffer_minutes, notes) VALUES (?, ?, ?, ?, ?, ?)",
            perms,
        )

        # AUT Fall 2026 Class Schedules (Freshmen, SoDT, SoB)
        classes = [
            # Monday
            ("202", "Monday", "08:00", "10:50", "COM 225 (Sec 1)", "Zarrina"),
            ("202", "Monday", "12:00", "14:50", "COM 225 (Sec 2)", "Zarrina"),
            ("301", "Monday", "08:00", "10:50", "AEC 101 (Data Sci)", "Max Olson"),
            ("301", "Monday", "12:00", "14:50", "MAT 114 (Sec 11)", "Dr. Alisher"),
            ("302", "Monday", "08:00", "10:50", "AEC 101 (Ind. Eng)", "Max Olson"),
            ("303", "Monday", "12:00", "14:50", "SER 315 (SE 2024)", "Dr. Shirin N."),
            ("303", "Monday", "18:30", "21:00", "CSE 575 (M.S. SE)", "Dr. Rajan T."),
            ("304", "Monday", "08:00", "10:50", "TEM 230 (Int. Trade)", "Muhammad Yusuf M."),
            ("304", "Monday", "12:00", "14:50", "TGM 312 (Int. Trade)", "Muhammad Yusuf M."),
            ("401", "Monday", "08:00", "10:50", "ECN 212 (BA Sec B)", "Dr. James"),
            ("401", "Monday", "12:00", "14:50", "SOS 110 (Bus & Tech)", "Dr. James"),
            ("402", "Monday", "08:00", "10:50", "PHY 121 (SE 2025)", "Dr. Daniyarbek R."),
            ("402", "Monday", "11:00", "11:50", "PHY 122 (SE 2025)", "Dr. Daniyarbek R."),
            ("403", "Monday", "15:00", "17:50", "FSE 100 (Sec 5)", "Dr. Uveze"),
            ("404", "Monday", "15:00", "16:00", "AIL 101 (Sec 11)", "Dr. Mohammed Talib"),
            ("404", "Monday", "17:00", "18:00", "AIL 101 (Sec 3)", "Khurshidbek A."),
            ("501", "Monday", "08:00", "10:50", "AEC 101 (BA Sec A)", "Mr. Khasan"),
            ("501", "Monday", "15:00", "17:50", "PRM 203 (Tourism)", "Khabibullo D."),
            ("601", "Monday", "08:00", "10:50", "FIS 201 (CS 2025)", "Dr. Artem"),
            ("601", "Monday", "15:00", "16:00", "AIL 101 (Sec 4)", "Dr. Khamidulla"),
            # Tuesday
            ("201", "Tuesday", "15:00", "17:50", "COM 225 (Sec 11)", "Dr. James"),
            ("202", "Tuesday", "10:00", "11:00", "AIL 101 (CS 2025)", "Dinora Gafurova"),
            ("301", "Tuesday", "08:00", "10:50", "COM 225 (Sec 10)", "Dr. James"),
            ("302", "Tuesday", "12:00", "14:50", "ECN 212 (Bus & Tech)", "Dr. Khamidulla"),
            ("303", "Tuesday", "12:00", "14:50", "CIS 105 (Sec 7)", "Nilufar Nosirjonova"),
            ("304", "Tuesday", "11:00", "12:00", "AIL 101 (SE 2025)", "Dinora Gafurova"),
            ("401", "Tuesday", "08:00", "10:50", "FIS 201 (Data Sci)", "Akbarjon K."),
            ("402", "Tuesday", "12:00", "14:50", "MGT 302 (Int. Trade)", "Muzaffar Aripkhodjaev"),
            ("404", "Tuesday", "12:00", "14:50", "FIS 201 (BA Sec A)", "Akbarjon K."),
            ("501", "Tuesday", "08:00", "10:50", "FIS 201 (Int. Trade)", "Muzaffar Aripkhodjaev"),
            ("503", "Tuesday", "08:00", "10:50", "CIS 105 (Sec 1)", "Nilufar Nosirjonova"),
            ("601", "Tuesday", "08:00", "10:50", "MAT 275 (Ind. Eng)", "Mukhabbat K."),
            # Wednesday
            ("201", "Wednesday", "08:00", "10:50", "AEC 101 (Int. Trade)", "Max Olson"),
            ("201", "Wednesday", "15:00", "17:50", "COM 225 (Sec 11)", "Dr. James"),
            ("202", "Wednesday", "08:00", "10:50", "AEC 101 (BA Sec B)", "Mr. Khasan"),
            ("202", "Wednesday", "15:00", "17:50", "MAT 265 (Sec 5)", "Azizbek Abdullayev"),
            ("301", "Wednesday", "08:00", "10:50", "MAT 267 (CS 2025)", "Mukhabbat K."),
            ("303", "Wednesday", "15:00", "17:50", "FSE 100 (Sec 3)", "Dr. Uveze"),
            ("401", "Wednesday", "08:00", "10:50", "ECN 212 (BA Sec A)", "Dr. James"),
            ("402", "Wednesday", "08:00", "10:50", "PHY 121 (Ind. Eng)", "Dr. Daniyar R."),
            ("402", "Wednesday", "11:00", "11:50", "PHY 122 (Ind. Eng)", "Dr. Daniyar R."),
            ("403", "Wednesday", "11:00", "12:00", "AIL 101 (Bus & Tech)", "Khabibullo D."),
            ("404", "Wednesday", "15:00", "17:50", "COM 225 (Sec 7)", "Dr. Kuba"),
            ("501", "Wednesday", "12:00", "14:50", "COM 225 (Bus & Tech)", "Dr. James"),
            ("501", "Wednesday", "15:00", "17:50", "MAT 343 (SE 2024)", "Dr. Daniyarbek R."),
            ("503", "Wednesday", "08:00", "10:50", "SER 222 (SE 2025)", "Dr. Shirin N."),
            ("601", "Wednesday", "08:00", "10:50", "MAT 210 (Sec 2)", "Dr. Alisher"),
            ("601", "Wednesday", "15:00", "17:50", "MAT 210 (Sec 1)", "Dr. Alisher"),
            # Thursday
            ("201", "Thursday", "12:00", "14:50", "MAT 267 (SE 2025)", "Mukhabbat K."),
            ("202", "Thursday", "12:00", "14:50", "COM 225 (Sec 3)", "Zarrina"),
            ("301", "Thursday", "12:00", "14:50", "AEC 101 (CS 2025)", "Mr. Khasan"),
            ("302", "Thursday", "12:00", "14:50", "ECN 306 (Int. Trade)", "Dr. Khamidulla"),
            ("303", "Thursday", "08:00", "10:50", "DAT 250 (Data Sci)", "Dr. Shirin"),
            ("304", "Thursday", "12:00", "14:50", "MAT 114 (Sec 11)", "Dr. Alisher"),
            ("401", "Thursday", "08:00", "10:50", "SOS 110 (BA Sec A)", "Khabibullo D."),
            ("402", "Thursday", "12:00", "14:50", "AEC 101 (Bus & Tech)", "Mr. Danyor"),
            ("404", "Thursday", "08:00", "10:50", "MAT 267 (Ind. Eng)", "Mukhabbat K."),
            ("501", "Thursday", "08:00", "10:50", "COM 225 (Sec 8)", "Dr. Kuba"),
            ("503", "Thursday", "15:00", "17:50", "SER 334 (SE 2024)", "Dr. Mohammed Uveze"),
            ("601", "Thursday", "08:00", "10:50", "ECN 211 (Int. Trade)", "Dr. Khamidulla"),
            # Friday
            ("201", "Friday", "08:00", "10:50", "COM 263 (Int. Trade)", "Kubanych Takirbashev"),
            ("202", "Friday", "08:00", "10:50", "TGM 310 (Int. Trade)", "Dr. Aisena"),
            ("302", "Friday", "10:00", "10:50", "AIL 101 (Ind. Eng)", "Dilafruz Ikramova"),
            ("304", "Friday", "08:00", "10:50", "AEC 101 (SE 2025)", "Max Olson"),
            ("402", "Friday", "08:00", "10:50", "PHY 121 (CS 2025)", "Dr. Daniyarbek R."),
            ("404", "Friday", "08:00", "09:00", "AIL 101 (Sec 8)", "Khurshidbek A."),
            ("501", "Friday", "08:00", "10:50", "MAT 265 (Sec 3)", "Dr. Jamol"),
            ("601", "Friday", "08:00", "09:00", "AIL 101 (BA Sec A)", "Khabibullo D."),
        ]
        conn.executemany(
            "INSERT INTO classroom_schedules (room_id, day_of_week, start_time, end_time, course_title, instructor) VALUES (?, ?, ?, ?, ?, ?)",
            classes,
        )

        # Open Study Sessions (100% open seats, ZERO fake bookings)
        sessions = [
            ("QS-101", "quiet", "Quiet Study — Midterms & Reading Focus", "Silent individual workspace for exam prep, essays, and focused coursework.", "301", tomorrow.isoformat(), "12:00", "14:00", 15, 1, None, None, 1, "active", None, ts),
            ("QS-102", "quiet", "Quiet Study — Afternoon Focus Block", "Distraction-free silent study session in the computer lab.", "503", day_after.isoformat(), "13:00", "15:00", 12, 1, None, None, 1, "active", None, ts),
            ("CS-201", "collaboration", "Collaboration — Group Project & Case Prep", "Active, interactive room where discussion and whiteboarding are encouraged. Min 3 students per group.", "302", tomorrow.isoformat(), "14:00", "16:00", 16, 3, 4, None, 1, "active", None, ts),
        ]
        conn.executemany(
            """INSERT INTO study_sessions
            (session_code, session_type, title, description, room_id, session_date, start_time, end_time,
             max_capacity, min_group_size, max_groups, club_event_id, is_public, status, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            sessions,
        )
