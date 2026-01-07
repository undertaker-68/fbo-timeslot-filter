import json
import os

from .logger import logger
from .ozon_client import iter_accounts
from .timeslot_filter import is_timeslot_valid

def get_states():
    # из .env можно задать: SUPPLY_STATES=CREATED,CONFIRMED,IN_PROCESS,IN_TRANSIT,DELIVERED
    raw = os.getenv("SUPPLY_STATES", "").strip()
    if raw:
        return [s.strip() for s in raw.split(",") if s.strip()]

    # дефолтные “нормальные” статусы
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

def main():
    # Рабочий payload, который ты проверил в PowerShell
    payload = {
        "limit": 100,
        "sort_by": 1,
        "sort_direction": "DESC",
        "filter": {
            "states": get_states(),  # если нужно больше статусов — расширим
            "date_from": "2025-12-01T00:00:00Z",
            "date_to": "2026-12-31T23:59:59Z",
        },
    }

    logger.info("States: %s", payload["filter"]["states"])
    
    all_results: dict = {}

    accounts = list(iter_accounts())
    if not accounts:
        raise RuntimeError("В .env не найдено ни одной пары OZON_CLIENT_ID_N / OZON_API_KEY_N")

    for client in accounts:
        try:
            logger.info("[%s] Using Client-Id=%s", client.name, client.client_id)

            data = client.supply_order_list(payload)

            # Ozon иногда возвращает { "result": {...} }, а иногда сразу { "order_ids": [...], "last_id": "..." }
            result = data.get("result") or data

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
                if not page_last_id:
                    break
                if page_last_id == "":
                    break

            order_ids = all_order_ids
            last_id = page_last_id

            # last_id иногда приходит пустой строкой
            if last_id == "":
                last_id = None

            if not order_ids:
                logger.info("[%s] order_ids пустой. last_id=%s", client.name, last_id)
                all_results[client.name] = {"orders": [], "last_id": last_id}
                continue

            # Получаем полные данные по заявкам
            # /v3/supply-order/get принимает 1..50 order_ids за раз
            all_orders = []
            for i in range(0, len(order_ids), 50):
                chunk = order_ids[i:i+50]
                details = client.supply_order_get(chunk)
                chunk_orders = (details.get("result", {}).get("orders") or details.get("orders") or [])
                if isinstance(chunk_orders, list):
                    all_orders.extend(chunk_orders)

            orders = all_orders

            if not isinstance(orders, list):
                logger.warning("[%s] Неожиданная структура supply-order/get", client.name)
                all_results[client.name] = {"error": "unexpected get response structure"}
                continue

            wanted_states = set(payload["filter"]["states"])

            def order_has_wanted_supply_state(order: dict) -> bool:
                for s in order.get("supplies", []) or []:
                    if s.get("state") in wanted_states:
                        return True
                return False

            kept = [o for o in orders if order_has_wanted_supply_state(o) and is_timeslot_valid(o)]
            logger.info("[%s] Всего заявок: %s; после фильтра: %s", client.name, len(orders), len(kept))

            all_results[client.name] = {"orders": kept, "last_id": last_id}

        except Exception as e:
            logger.exception("[%s] Ошибка: %s", client.name, e)
            all_results[client.name] = {"error": str(e)}

    # Вывод результата — один раз после обработки всех аккаунтов
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
