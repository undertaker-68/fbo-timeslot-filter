import os
from datetime import date

import pytz
from dotenv import load_dotenv

load_dotenv()

TIMEZONE = os.getenv("TIMEZONE", "Asia/Krasnoyarsk")

# Новое правило: последние N дней (по умолчанию 20)
LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", "20"))

MIN_DATE_STR = os.getenv("MIN_DATE", "2025-12-03")  # YYYY-MM-DD
MIN_DATE = date.fromisoformat(MIN_DATE_STR)
