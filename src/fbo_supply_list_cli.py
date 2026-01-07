import json
import os

from .logger import logger
from .ozon_client import iter_accounts
from .timeslot_filter import is_timeslot_valid


def get_states():
    """
    Статусы поставок (supply.state), которые считаем валидными.
    Можно переопределить через ENV: SUPPLY_STATES=...
    """
    raw = os.getenv("SUPPLY_STATES", "").strip()
    if raw:
        return [s.strip() for s in raw.split(",") if s.strip()]

    return [
        "READY_TO_SUPPLY",
        "ACCEPTED_AT_SUPPLY_WAREHOUSE",
        "IN_TRANSIT",
        "ACCEPTANCE_AT_STORAGE_WAREHOUSE",
        "REPORTS_CONFIRMATION_AWAITING",
        "COMPLETED",
        "REJECTED_AT_SUPPLY_WAREHOUSE",
        "REPORT_REJECTED",
    ]


def order_has_wanted_supply_state(order: dict, wanted_states: set[str]) -> bool:
    """
    Проверяем, есть ли у заявки хотя бы одна поставка
    с нужным supply.state
    """
    for s in order.get("supplies", []) or []:
        if s.get("state") in wanted_states:
            return True
    return False


def main():
    states = get_states()

    payload = {
        "limit": 100,
        "sort_by": 1,
        "sort_direction": "DESC",
        "filter": {
            "states": states,
            "date_from": "2025-12-01T00:00:00Z",
            "date_to": "2026-12-31T23:59:59Z",
        },
    }

    logger.info("States: %s", states)

    all_results = {}

    accounts = list(iter_accounts())
    if not accounts:
        raise RuntimeError("В .env не найдено ни одной пары OZON_CLIENT_ID_N / OZON_API_KEY_N")

    for client in accounts:
        try:
            logger.info("[%s] Using Client-Id=%s", client.name, client.client_id)

            # ---------- LIST с пагинацией ----------
            all_order_ids = []
            page_last_id = None

            while True:
                page_payload = payload.copy()
                page_payload["filter"] = payload["filter"].copy()

                if page_last_id:
                    page_payload["last_id"] = page_last_id

                data = client.supply_order_list(page_payload)
                result = data.get("result") or data

                ids = result.get("order_ids") or []
                all_order_ids.extend(ids)

                page_last_id = result.get("last_id")
                if not page_last_id or page_last_id == "":
                    break

            if not all_order_ids:
                logger.info("[%s] Нет заявок по list-фильтру", client.name)
                all_results[client.name] = {"orders": [], "last_id": None}
                continue

            # ---------- GET батчами по 50 ----------
            all_orders = []
            for i in range(0, len(all_order_ids), 50):
                chunk = all_order_ids[i:i + 50]
                details = client.supply_order_get(chunk)
                chunk_orders = (
                    details.get("result", {}).get("orders")
                    or details.get("orders")
                    or []
                )
                if isinstance(chunk_orders, list):
                    all_orders.extend(chunk_orders)

            wanted_states = set(states)

            kept = [
                o for o in all_orders
                if not o.get("order_tags", {}).get("is_virtual", False)
                and order_has_wanted_supply_state(o, wanted_states)
                and is_timeslot_valid(o)
            ]

            logger.info(
                "[%s] Всего заявок: %s; после фильтра: %s",
                client.name,
                len(all_orders),
                len(kept),
            )

            all_results[client.name] = {
                "orders": kept,
                "last_id": page_last_id,
            }

        except Exception as e:
            logger.exception("[%s] Ошибка: %s", client.name, e)
            all_results[client.name] = {"error": str(e)}

    # ---------- ВЫВОД (ОДИН РАЗ) ----------
    out = json.dumps(all_results, ensure_ascii=False)

    out_file = os.getenv("OUT_FILE")
    if out_file:
        with open(out_file, "w", encoding="utf-8") as f:
            f.write(out)
        logger.info("Saved output to %s", out_file)
    else:
        try:
            print(out)
        except BrokenPipeError:
            pass


if __name__ == "__main__":
    main()
