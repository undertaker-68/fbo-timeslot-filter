import pytz
from dateutil import parser

from .config import TIMEZONE, MIN_DATE
from .logger import logger


def is_timeslot_valid(order: dict) -> bool:
    try:
        timeslot_from = order["timeslot"]["timeslot"]["from"]
    except KeyError:
        logger.warning("Нет таймслота | order_id=%s", order.get("order_id"))
        return False

    utc_dt = parser.isoparse(timeslot_from)

    local_tz = pytz.timezone(TIMEZONE)
    local_dt = utc_dt.astimezone(local_tz)

    return local_dt.date() >= MIN_DATE
