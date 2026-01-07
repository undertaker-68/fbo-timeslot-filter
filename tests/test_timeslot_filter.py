import sys
from datetime import date

# чтобы тесты видели src/ без установки пакета
sys.path.append("src")

import pytz
from dateutil import parser

import config
from timeslot_filter import is_timeslot_valid


def test_missing_timeslot_returns_false():
    order = {"order_id": 1}
    assert is_timeslot_valid(order) is False


def test_before_min_date_filtered_out():
    # 2025-12-02 10:00 UTC -> 2025-12-02 17:00 Asia/Krasnoyarsk => date 2025-12-02 < 2025-12-03
    order = {
        "order_id": 2,
        "timeslot": {"timeslot": {"from": "2025-12-02T10:00:00Z"}}
    }
    assert is_timeslot_valid(order) is False


def test_min_date_included():
    # 2025-12-03 00:00 UTC -> 2025-12-03 07:00 Asia/Krasnoyarsk => date 2025-12-03 (включаем)
    order = {
        "order_id": 3,
        "timeslot": {"timeslot": {"from": "2025-12-03T00:00:00Z"}}
    }
    assert is_timeslot_valid(order) is True


def test_after_min_date_included():
    order = {
        "order_id": 4,
        "timeslot": {"timeslot": {"from": "2026-01-08T11:00:00Z"}}
    }
    assert is_timeslot_valid(order) is True
