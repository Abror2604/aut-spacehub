"""
Automated Verification Suite for AUT SpaceHub (All 26 Acceptance Criteria + Concurrency Race Test)
"""
import concurrent.futures
import os
import sys
import tempfile
import unittest

TEST_DB = os.path.join(tempfile.gettempdir(), "spacehub_live_test.db")
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)
os.environ["SPACEHUB_DB_PATH"] = TEST_DB
os.environ["DEMO_MODE"] = "true"
os.environ["ADMIN_TELEGRAM_IDS"] = "900003"

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient
from backend.app import app
from backend.database import init_db
from bot.bot import handle_command

ADMIN = {"X-Demo-Telegram-Id": "900003"}
CLUB_LEADER = {"X-Demo-Telegram-Id": "900002"}
STUDENT_1 = {"X-Demo-Telegram-Id": "900001"}
STUDENT_2 = {"X-Demo-Telegram-Id": "900004"}


class TestSpaceHubProduction(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db(TEST_DB, seed=True).close()
        cls.client = TestClient(app)
        # Register test accounts dynamically (just like real Telegram users)
        cls.client.get("/api/me", headers=ADMIN)
        cls.leader_id = cls.client.get("/api/me", headers=CLUB_LEADER).json()["user"]["id"]
        cls.student1_id = cls.client.get("/api/me", headers=STUDENT_1).json()["user"]["id"]
        cls.student2_id = cls.client.get("/api/me", headers=STUDENT_2).json()["user"]["id"]
        # Admin approves CLUB_LEADER for Club 1
        cls.client.post("/api/admin/club-leaders/approve", headers=ADMIN, json={"user_id": cls.leader_id, "club_id": 1, "action": "approve"})

    def test_01_to_11_student_workflows(self):
        bot_start = handle_command("900001", "Student One", "stu1", "/start")
        self.assertIn("Welcome to SpaceHub!", bot_start["text"])
        self.assertEqual(bot_start["reply_markup"]["inline_keyboard"][0][0]["text"], "Open SpaceHub")

        bot_club_denied = handle_command("900001", "Student One", "stu1", "/club")
        self.assertIn("restricted", bot_club_denied["text"].lower())

        # Available rooms & exact free duration (Wed 2026-11-25 11:00 -> Room 201 free until 15:00 = 240 mins)
        res = self.client.get("/api/rooms?date=2026-11-25&time=11:00&available_now=true", headers=STUDENT_1)
        self.assertEqual(res.status_code, 200)
        rooms = res.json()["rooms"]
        r201 = next(r for r in rooms if r["room_id"] == "201")
        self.assertEqual(r201["free_until"], "15:00")
        self.assertEqual(r201["remaining_minutes"], 240)
        self.assertFalse(any(r["room_id"] == "404" for r in rooms))

        # Occupied room shows next free window
        r402 = self.client.get("/api/rooms/402/availability?date=2026-11-25&time=11:15", headers=STUDENT_1).json()
        self.assertEqual(r402["status"], "occupied")
        self.assertEqual(r402["next_available_start"], "11:50")

        # Filter >= 2 hours continuous
        res2h = self.client.get("/api/rooms?date=2026-11-25&time=11:00&min_duration_minutes=120", headers=STUDENT_1).json()
        self.assertTrue(all(r["remaining_minutes"] >= 120 for r in res2h["rooms"]))

        # Student cannot reserve club-only Room 404
        self.assertEqual(self.client.post("/api/reservations", headers=STUDENT_1, json={
            "room_id": "404", "reservation_date": "2026-11-25", "start_time": "18:00", "end_time": "19:00"
        }).status_code, 403)

        # Valid reservation + overlapping rejection
        book = self.client.post("/api/reservations", headers=STUDENT_1, json={
            "room_id": "201", "reservation_date": "2026-11-25", "start_time": "11:00", "end_time": "12:30", "purpose": "Midterm Study"
        })
        self.assertEqual(book.status_code, 201)
        res_id = book.json()["id"]
        self.assertEqual(self.client.post("/api/reservations", headers=STUDENT_2, json={
            "room_id": "201", "reservation_date": "2026-11-25", "start_time": "12:00", "end_time": "13:00"
        }).status_code, 409)

        # Sessions start 100% open (no fake bookings!)
        sessions = self.client.get("/api/sessions", headers=STUDENT_1).json()["sessions"]
        self.assertTrue(all(s["seats_taken"] == 0 for s in sessions))
        self.assertEqual(self.client.post(f"/api/sessions/{sessions[0]['id']}/join", headers=STUDENT_1, json={"group_size": 1}).status_code, 201)

        # Admin creates a 3-seat collaboration session, fills it, and verifies full-session rejection
        full_s = self.client.post("/api/admin/sessions", headers=ADMIN, json={
            "session_type": "collaboration", "title": "Small Group Test", "description": "Test cap",
            "room_id": "302", "session_date": "2026-11-27", "start_time": "16:00", "end_time": "17:00",
            "max_capacity": 3, "min_group_size": 3, "max_groups": 1
        }).json()["session_id"]
        self.assertEqual(self.client.post(f"/api/sessions/{full_s}/join", headers=STUDENT_1, json={"group_size": 3}).status_code, 201)
        self.assertEqual(self.client.post(f"/api/sessions/{full_s}/join", headers=STUDENT_2, json={"group_size": 3}).status_code, 409)

        # Cancel protection & own cancellation
        self.assertEqual(self.client.post(f"/api/reservations/{res_id}/cancel", headers=STUDENT_2).status_code, 403)
        self.assertEqual(self.client.post(f"/api/reservations/{res_id}/cancel", headers=STUDENT_1).status_code, 200)

    def test_12_to_26_club_admin_and_concurrency(self):
        self.assertEqual(self.client.get("/api/club/dashboard", headers=STUDENT_1).status_code, 403)
        self.assertEqual(self.client.get("/api/club/dashboard", headers=CLUB_LEADER).status_code, 200)

        ev = self.client.post("/api/club/events", headers=CLUB_LEADER, json={
            "club_id": 1, "title": "AUT Founder Fireside Chat", "description": "Q&A with Tashkent founders.",
            "category": "Panel", "event_date": "2026-11-26", "start_time": "15:00", "end_time": "17:00",
            "room_id": "501", "expected_participants": 35, "is_open_to_all": True
        })
        self.assertEqual(ev.status_code, 201)
        ev_id = ev.json()["event_id"]

        # Overlap rejected
        self.assertEqual(self.client.post("/api/club/events", headers=CLUB_LEADER, json={
            "club_id": 1, "title": "Conflict", "description": "Overlap test", "category": "Meeting",
            "event_date": "2026-11-26", "start_time": "16:00", "end_time": "18:00",
            "room_id": "501", "expected_participants": 20, "is_open_to_all": True
        }).status_code, 409)

        # Admin approves -> leader publishes -> student joins
        self.assertEqual(self.client.post(f"/api/admin/events/{ev_id}/review", headers=ADMIN, json={"decision": "approve"}).status_code, 200)
        sess_id = self.client.post(f"/api/club/events/{ev_id}/publish", headers=CLUB_LEADER).json()["session_id"]
        self.assertEqual(self.client.post(f"/api/sessions/{sess_id}/join", headers=STUDENT_1, json={"group_size": 1}).status_code, 201)

        # Admin blocks Room 301 & updates availability
        self.assertEqual(self.client.post("/api/admin/rooms/block", headers=ADMIN, json={
            "room_id": "301", "exception_date": "2026-11-27", "start_time": "10:00", "end_time": "12:00", "reason": "Maintenance"
        }).status_code, 201)
        avail = self.client.get("/api/rooms/301/availability?date=2026-11-27&time=10:30", headers=STUDENT_1).json()
        self.assertEqual(avail["status"], "occupied")

        # 8-thread concurrency race test
        def attempt(i):
            hdr = STUDENT_1 if i % 2 == 0 else STUDENT_2
            return self.client.post("/api/reservations", headers=hdr, json={
                "room_id": "201", "reservation_date": "2026-11-27", "start_time": "14:00", "end_time": "15:00", "purpose": f"Race {i}"
            }).status_code

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            statuses = list(ex.map(attempt, range(8)))
        self.assertEqual(statuses.count(201), 1)
        self.assertEqual(statuses.count(409), 7)


if __name__ == "__main__":
    unittest.main(verbosity=2)
