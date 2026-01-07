import json
from .logger import logger
from .ozon_client import iter_accounts
from .timeslot_filter import is_timeslot_valid


def main():
    # Рабочий payload, который ты проверил в PowerShell
    payload = {
        "limit": 100,
        "sort_by": 1,
        "sort_direction": "DESC",
        "filter": {
            "states": ["IN_TRANSIT"],  # если нужно больше статусов — расширим
            "date_from": "2025-12-01T00:00:00Z",
            "date_to": "2026-12-31T23:59:59Z",
        },
    }

    all_results = {}

    accounts = list(iter_accounts())
    if not accounts:
        raise RuntimeError("В .env не найдено ни одной пары OZON_CLIENT_ID_N / OZON_API_KEY_N")

    for client in accounts:
        try:
            logger.info("[%s] Using Client-Id=%s", client.name, client.client_id)
            data = client.supply_order_list(payload)
            result = data.get("result", {})
            order_ids = result.get("order_ids", []) or []
            last_id = result.get("last_id")

            if not order_ids:
                logger.info("[%s] order_ids пустой (нет заявок по фильтру). last_id=%s", client.name, last_id)
                all_results[client.name] = {"orders": [], "last_id": last_id}
                continue

            # Получаем полные данные по заявкам
            details = client.supply_order_get(order_ids)
            orders = (details.get("result", {}).get("orders") or details.get("orders") or [])

            if not isinstance(orders, list):
                logger.warning("[%s] Неожиданная структура supply-order/get", client.name)
                all_results[client.name] = {"error": "unexpected get response structure"}
                continue

            kept = [o for o in orders if is_timeslot_valid(o)]
            logger.info("[%s] Всего заявок: %s; после фильтра: %s", client.name, len(orders), len(kept))

            all_results[client.name] = {"orders": kept, "last_id": last_id}

        except Exception as e:
            logger.exception("[%s] Ошибка запроса/обработки: %s", client.name, e)
            all_results[client.name] = {"error": str(e)}

    print(json.dumps(all_results, ensure_ascii=False))


if __name__ == "__main__":
    main()
