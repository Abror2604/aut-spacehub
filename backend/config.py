import os
from zoneinfo import ZoneInfo

APP_TIMEZONE = ZoneInfo("Asia/Tashkent")
TIMEZONE_NAME = "Asia/Tashkent"

CAMPUS_OPEN_HOUR = int(os.getenv("CAMPUS_OPEN_HOUR", "8"))    # 08:00
CAMPUS_CLOSE_HOUR = int(os.getenv("CAMPUS_CLOSE_HOUR", "21")) # 21:00
DEFAULT_BOOKING_BUFFER_MINUTES = int(os.getenv("BOOKING_BUFFER_MINUTES", "0"))

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
MINI_APP_URL = os.getenv("MINI_APP_URL", "https://spacehub.aut-edu.uz")
DB_PATH = os.getenv("SPACEHUB_DB_PATH", os.path.join(os.path.dirname(__file__), "..", "spacehub.db"))
DEMO_MODE = os.getenv("DEMO_MODE", "true").lower() == "true"

# Policy defaults (configurable by Admin in DB settings)
DEFAULT_CLUB_AUTO_APPROVE = os.getenv("CLUB_AUTO_APPROVE", "false").lower() == "true"
MIN_RESERVATION_MINUTES = 30
MAX_STUDENT_RESERVATION_MINUTES = 180
MAX_CLUB_RESERVATION_MINUTES = 240
