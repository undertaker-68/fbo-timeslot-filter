from timeslot_filter import is_timeslot_valid


def process_orders(orders: list[dict]):
    for order in orders:
        if not is_timeslot_valid(order):
            continue

        print(f"Обрабатываем поставку {order.get('order_id')}")
