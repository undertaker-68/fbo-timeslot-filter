import os
from datetime import datetime, timedelta

import pytz
from dotenv import load_dotenv

load_dotenv()

TIMEZONE = os.getenv("TIMEZONE", "Asia/Krasnoyarsk")

# Всегда последние 20 дней
LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", "20"))

tz = pytz.timezone(TIMEZONE)
MIN_DATE = (datetime.now(tz).date() - timedelta(days=LOOKBACK_DAYS))
