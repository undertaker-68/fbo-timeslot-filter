import json
import sys

from .timeslot_filter import is_timeslot_valid


def main():
    data = json.load(sys.stdin)
    orders = data.get("orders", data)  # поддержим {"orders":[...]} и просто список
    kept = [o for o in orders if is_timeslot_valid(o)]
    print(json.dumps({"orders": kept}, ensure_ascii=False))


if __name__ == "__main__":
    main()
