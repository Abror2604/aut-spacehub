# AUT SpaceHub — Telegram Bot, Mini App & Smart University Room Booking Platform

**American University of Technology (AUT) · Beshyogoch Campus, Tashkent, Uzbekistan**

AUT SpaceHub is a connected three-part platform consisting of a **Telegram Bot**, a mobile-first **Telegram Mini App**, and a **FastAPI + SQLite Backend & Administrator Panel**. It calculates exact classroom free intervals in `Asia/Tashkent` using Fall 2026 demonstration timetables, enforces role-based access control (`Student`, `Approved Club Leader`, `Administrator`), and prevents concurrent booking conflicts with SQLite `BEGIN IMMEDIATE` write locks.

---

## 1. Architecture Overview

- **`backend/`**: FastAPI REST API (`app.py`), SQLite schema with 14 tables (`database.py`), official Telegram WebApp `initData` HMAC-SHA256 authentication & server-side RBAC (`auth.py`), and the authoritative `Asia/Tashkent` availability calculator (`availability.py`).
- **`bot/bot.py`**: Telegram Bot handling `/start` (with the `Open SpaceHub` WebApp button), `/help`, `/mybookings`, `/club` (approved club leaders only), and deduplicated notification delivery.
- **`frontend/index.html`**: Mobile-first Telegram Mini App with 4 student navigation tabs (`Home`, `Rooms`, `Sessions`, `My Bookings`) plus a role-gated 5th tab (`Club Portal`) for approved club leaders.
- **`admin/index.html`**: Separate Administrator Panel (`/admin`) for room permissions (`student_only`, `club_only`, `shared`, `unavailable`), recurring class schedules, maintenance blocks, club leader approvals, event approvals, and audit logs.
- **`tests/test_workflows.py`**: Automated test suite verifying all 26 acceptance criteria and concurrent double-booking prevention.

---

## 2. Local Setup & Running

```bash
cp .env.example .env
# Edit .env and add your TELEGRAM_BOT_TOKEN and HTTPS MINI_APP_URL

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 1. Start the FastAPI backend + Mini App + Admin Panel
uvicorn backend.app:app --host 0.0.0.0 --port 8000

# 2. In a second terminal, start the Telegram Bot
python3 bot/bot.py

# 3. Run the automated 26-workflow + concurrency test suite
python3 tests/test_workflows.py
```

Or run with Docker:
```bash
docker compose up --build
```

---

## 3. Connecting the Mini App with @BotFather

1. Open Telegram and message **@BotFather**.
2. Send `/newbot`, choose a display name (e.g., `AUT SpaceHub`), and pick a username ending in `bot` (e.g., `aut_spacehub_bot`).
3. Copy the HTTP API token into `TELEGRAM_BOT_TOKEN` in `.env`.
4. Deploy the FastAPI backend to an HTTPS host (Render, Railway, Fly.io, or a university VPS with Nginx/Caddy SSL — Telegram Mini Apps strictly require HTTPS).
5. Set `MINI_APP_URL=https://your-domain.uz` in `.env`.
6. In **@BotFather**, send `/setmenubutton` → select your bot → set button text to `Open SpaceHub` and URL to `https://your-domain.uz`.
7. (Optional) Send `/newapp` in **@BotFather** to register a direct short link (`t.me/aut_spacehub_bot/spacehub`).
8. Set `DEMO_MODE=false` in production so only cryptographically verified Telegram `initData` requests are accepted.

---

## 4. Demonstration Mode vs. Future AUT Production Integration

The current platform runs in **Demonstration Mode** using sample schedules modelled on AUT Fall 2026 timetables (`Freshmen_Schedule_Fall2026`, `SoDT_Schedule_Fall2026`, `SoB_Schedule_Fall2026`). It does **not** connect to live AUT Registrar systems.

**Features requiring future AUT authorization / API integration:**
1. **AUT Registrar / SIS Timetable API**: Real-time sync of official class changes, room swaps, and exam schedules.
2. **AUT Single Sign-On (`@aut-edu.uz`)**: Automatic student & faculty identity verification linked to Telegram accounts.
3. **Student Affairs Club Registry**: Automated verification of official club presidents and annual club renewals.
4. **Campus Facility / Access Control**: Digital door displays or QR check-in readers to detect walk-in occupancy and auto-release no-shows.
