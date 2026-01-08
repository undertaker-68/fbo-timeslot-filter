import os
from typing import Any, Optional
from datetime import datetime, timezone
try:
    from zoneinfo import ZoneInfo
except Exception:  # py<3.9
    ZoneInfo = None  # type: ignore

from .logger import logger
from .ms_client import MSClient, variants_lat_cyr


def _parse_dt(s: str) -> datetime:
    # "2026-01-07T11:00:00Z" -> aware UTC
    if not s:
        raise ValueError("empty datetime")
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


def _tz() -> Any:
    tz_name = os.getenv("TIMEZONE", "Asia/Krasnoyarsk")
    if ZoneInfo is None:
        return timezone.utc
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return timezone.utc


def planned_delivery_moment(order: dict) -> Optional[str]:
    """
    МС: deliveryPlannedMoment (ISO)
    Нам важна ДАТА по таймслоту (локальная), время не важно.
    Делаем YYYY-MM-DDT00:00:00.000Z
    """
    try:
        ts_from = (((order.get("timeslot") or {}).get("timeslot") or {}).get("from")) or ""
        dt_utc = _parse_dt(ts_from)
        dt_local = dt_utc.astimezone(_tz())
        d = dt_local.date().isoformat()
        return f"{d}T00:00:00.000Z"
    except Exception:
        return None


def destination_city(order: dict) -> str:
    """
    Хотим: 'Ярославль' и т.п.
    Берём supplies[0].storage_warehouse.name -> до '_' (например 'ЯРОСЛАВЛЬ_РФЦ')
    """
    try:
        supplies = order.get("supplies") or []
        if not supplies:
            return ""
        wh = (supplies[0].get("storage_warehouse") or {})
        name = (wh.get("name") or "").strip()
        if not name:
            return ""
        head = name.split("_", 1)[0].strip()
        if not head:
            return ""
        # "ЯРОСЛАВЛЬ" -> "Ярославль", "ПЕРМЬ" -> "Пермь"
        return head[:1].upper() + head[1:].lower()
    except Exception:
        return ""


def choose_sale_price_value(assortment_row: dict) -> Optional[int]:
    """
    Пытаемся взять цену продажи по умолчанию из карточки ассортимента.
    В МС цена обычно хранится как int (в копейках/центах).
    """
    # 1) Часто в ответе есть salePrices: [{"value": 12300, "priceType": {...}}, ...]
    sps = assortment_row.get("salePrices")
    if isinstance(sps, list) and sps:
        # пробуем найти "Цена продажи" / "Продажа" по названию типа цены
        best = None
        for x in sps:
            pt = (x.get("priceType") or {})
            nm = (pt.get("name") or "").lower()
            if "продаж" in nm:
                best = x
                break
        if best is None:
            best = sps[0]
        v = best.get("value")
        if isinstance(v, int) and v >= 0:
            return v

    # 2) На всякий: иногда кладут просто "price"
    v2 = assortment_row.get("price")
    if isinstance(v2, int) and v2 >= 0:
        return v2

    return None


def load_kit_rules() -> dict[str, list[str]]:
    """
    Комплекты: ключ=offer_id(артикул), value=[component_articles...]
    Можно хранить в ENV как JSON, но пока проще жёстко/файлом.
    """
    # Если захочешь — вынесем в JSON-файл или ENV.
    return {
        "10264-А93": ["10264", "11291"],
        "00020-А92": ["00020", "11278"],
        "00026-В92": ["00026", "11284"],
        "00493-Е90": ["00493", "11287"],
    }


def apply_kit_rules(positions: list[dict], kit_rules: dict[str, list[str]]) -> list[dict]:
    """
    Если article совпадает с комплектом (с учётом лат/кир),
    заменяем на компоненты, qty компоненты = qty комплекта.
    """
    if not kit_rules:
        return positions

    # индекс правил по всем вариантам лат/кир
    index: dict[str, list[str]] = {}
    for kit, comps in kit_rules.items():
        for v in variants_lat_cyr(kit):
            index[v] = comps

    out: list[dict] = []
    for p in positions:
        art = p["article"]
        qty = p["quantity"]
        comps = index.get(art)
        if not comps:
            out.append(p)
            continue
        for c in comps:
            out.append({"article": c, "quantity": qty})
    return out


def ozon_positions_from_bundle(ozon_client, order: dict) -> list[dict]:
    """
    Берём bundle_id из supply[0].bundle_id и вытаскиваем позиции по /v1/supply-order/bundle
    """
    supplies = order.get("supplies") or []
    if not supplies:
        return []
    bundle_id = supplies[0].get("bundle_id")
    if not bundle_id:
        return []

    # ВАЖНО: в ozon_client должен быть метод supply_order_bundle_items(bundle_id, limit=100)
    items = ozon_client.supply_order_bundle_items(bundle_id, limit=100)

    out: list[dict] = []
    for it in items or []:
        art = (it.get("offer_id") or "").strip()
        qty = float(it.get("quantity") or 0)
        if art and qty > 0:
            out.append({"article": art, "quantity": qty})
    return out


def build_customerorder_payload(account_name: str, order: dict, ms_positions: list[dict]) -> dict[str, Any]:
    order_number = (order.get("order_number") or "").strip()
    order_id = order.get("order_id")

    # Короткий комментарий
    city = destination_city(order)
    if city:
        descr = f"{order_number} - {city}"
    else:
        descr = f"{order_number}"

    payload: dict[str, Any] = {
        "name": f"fbo-{order_number}",
        "description": descr,
        # удобно для дедупликации (по желанию можно проверять и не создавать дубль)
        "externalCode": f"{account_name}:{order_id}",
        "positions": ms_positions,
    }

    dm = planned_delivery_moment(order)
    if dm:
        payload["deliveryPlannedMoment"] = dm  # поле МС для плановой даты отгрузки :contentReference[oaicite:2]{index=2}

    # Остальные поля (organization/agent/store/state/salesChannel и т.д.)
    # у тебя уже задаются в коде ниже или в ENV — оставляем как было, если оно уже есть.
    return payload


def sync_orders_to_ms(ozon_client, account_name: str, ozon_orders: list[dict]) -> dict[str, Any]:
    """
    DRY: только логируем что бы создали
    LIVE: реально создаём (ограничиваем MS_LIVE_MAX)
    """
    mode = (os.getenv("MS_MODE") or "DRY").upper()
    max_live = int(os.getenv("MS_LIVE_MAX", "0") or "0")

    ms = MSClient()
    kit_rules = load_kit_rules()

    created = 0
    skipped = 0
    duplicates = 0
    errors: list[str] = []

    # cache: article -> (assortment_meta, price_value_int)
    assortment_cache: dict[str, tuple[dict, Optional[int]]] = {}

    for o in ozon_orders:
        # пропускаем виртуальные
        if (o.get("order_tags") or {}).get("is_virtual"):
            skipped += 1
            continue

        if mode == "LIVE" and max_live > 0 and created >= max_live:
            logger.info("[%s] Reached MS_LIVE_MAX=%s, stopping.", account_name, max_live)
            break

        # 1) получить позиции из Ozon bundle
        oz_pos = ozon_positions_from_bundle(ozon_client, o)
        if not oz_pos:
            skipped += 1
            continue

        # 2) развернуть комплекты
        oz_pos = apply_kit_rules(oz_pos, kit_rules)

        # 3) собрать ms positions (assortment meta + quantity + price)
        ms_positions: list[dict] = []
        local_errs: list[str] = []

        for p in oz_pos:
            art = p["article"]
            qty = p["quantity"]

            meta: Optional[dict] = None
            price_val: Optional[int] = None

            if art in assortment_cache:
                meta, price_val = assortment_cache[art]
            else:
                # ВАЖНО: ms.find_assortment_by_article должен возвращать ПОЛНУЮ строку (а не только meta),
                # чтобы мы могли вытащить salePrices.
                row = ms.find_assortment_by_article(art)
                if row:
                    # row может быть meta (старое поведение) или полная сущность.
                    if "meta" in row and isinstance(row.get("meta"), dict) and len(row.keys()) > 1:
                        meta = {"meta": row["meta"]} if "href" in row["meta"] else row["meta"]
                        price_val = choose_sale_price_value(row)
                    else:
                        # если вернули только meta
                        meta = row
                        price_val = None

                if meta:
                    assortment_cache[art] = (meta, price_val)

            if not meta:
                local_errs.append(f'not found in MS by article="{art}"')
                continue

            pos = {"assortment": meta, "quantity": qty}
            if isinstance(price_val, int):
                pos["price"] = price_val  # цена в "копейках/центах" :contentReference[oaicite:3]{index=3}
            ms_positions.append(pos)

        if local_errs:
            errors.append(f'order_id={o.get("order_id")}: ' + "; ".join(local_errs))

        if not ms_positions:
            skipped += 1
            continue

        payload = build_customerorder_payload(account_name, o, ms_positions)

        if mode == "DRY":
            created += 1
            logger.info(
                "[DRY][%s] Would create CustomerOrder: name=%s positions=%s order_id=%s deliveryPlannedMoment=%s",
                account_name,
                payload.get("name"),
                len(payload.get("positions") or []),
                o.get("order_id"),
                payload.get("deliveryPlannedMoment"),
            )
            continue

        # LIVE
        try:
            created_doc = ms.create_customerorder(payload)
            created += 1
            logger.info(
                "[LIVE][%s] Created CustomerOrder: name=%s id=%s order_id=%s",
                account_name,
                created_doc.get("name"),
                (created_doc.get("id") or (created_doc.get("meta") or {}).get("href")),
                o.get("order_id"),
            )
        except Exception as e:
            errors.append(f'order_id={o.get("order_id")}: {e}')
            logger.exception("[%s] create_customerorder error", account_name)

    return {
        "mode": mode,
        "created": created,
        "skipped": skipped,
        "duplicates": duplicates,
        "errors": errors,
    }


def sync_orders_to_ms_dry(ozon_client, account_name: str, ozon_orders: list[dict]) -> dict[str, Any]:
    """
    Совместимость со старым импортом.
    Всегда форсит DRY.
    """
    old = os.getenv("MS_MODE")
    os.environ["MS_MODE"] = "DRY"
    try:
        return sync_orders_to_ms(ozon_client, account_name, ozon_orders)
    finally:
        if old is None:
            os.environ.pop("MS_MODE", None)
        else:
            os.environ["MS_MODE"] = old
