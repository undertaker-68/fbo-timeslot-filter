import json
from .logger import logger
from .ozon_client import iter_accounts
from .timeslot_filter import is_timeslot_valid


def extract_orders(data: dict) -> list:
    result = data.get("result", data)
    orders = (
        result.get("orders")
        or result.get("supply_orders")
        or result.get("items")
        or result.get("supplies")
        or []
    )
    return orders if isinstance(orders, list) else []


def main():
    payload = {}

    all_results = {}

    accounts = list(iter_accounts())
    if not accounts:
        raise RuntimeError("В .env не найдено ни одной пары OZON_CLIENT_ID_N / OZON_API_KEY_N")

    for client in accounts:
        try:
            data = client.supply_order_list(payload)
            orders = extract_orders(data)
            kept = [o for o in orders if is_timeslot_valid(o)]
            logger.info("[%s] Всего заявок: %s; после фильтра: %s", client.name, len(orders), len(kept))
            all_results[client.name] = kept
        except Exception as e:
            logger.exception("[%s] Ошибка запроса/обработки: %s", client.name, e)
            all_results[client.name] = {"error": str(e)}

    print(json.dumps(all_results, ensure_ascii=False))


if __name__ == "__main__":
    main()
