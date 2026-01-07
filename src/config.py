import os
from datetime import date

from dotenv import load_dotenv

load_dotenv()

TIMEZONE = os.getenv("TIMEZONE", "Asia/Krasnoyarsk")
MIN_DATE_STR = os.getenv("MIN_DATE", "2025-12-03")  # YYYY-MM-DD
MIN_DATE = date.fromisoformat(MIN_DATE_STR)
