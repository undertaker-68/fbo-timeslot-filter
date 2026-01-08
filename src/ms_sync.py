import os
import re
from datetime import datetime, time
from typing import Any, Optional

from .logger import logger
from .ms_client import MSClient, variants_lat_cyr


def load_ms_required(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"Нет {name} в .env")
    return v


def load_kit_rules() -> dict[str, list[str]]:
    """
    MS_KIT_RULES формат:
      10264-А93:10264+11291;00020-А92:00020+11278;...
    Возвращаем dict: { kit_article: [component_article1, component_article2, ...] }
    """
    raw = (os.getenv("MS_KIT_RULES") or "").strip()
    if not raw:
        return {}

    rules: dict[str, list[str]] = {}
    parts = [p.strip() for p in raw.split(";") if p.strip()]
    for p in parts:
        if ":" not in p:
            continue
        left, right = p.split(":", 1)
        kit = left.strip()
        comps = [x.strip() for x in right.split("+") if x.strip()]
        if kit and comps:
            rules[kit] = comps
    return rules


def resolve_saleschannel_id(account_name: str) -> str:
    # account_name: OZON_1 / OZON_2 (вызов идёт с .upper() из cli)
    key = f"MS_SALESCHANNEL_ID_{account_name}"
    v = os.getenv(key)
    if v:
        return v

    # fallback (если вдруг захочешь общий)
    v2 = os.getenv("MS_SALESCHANNEL_ID")
    if v2:
        return v2

    raise RuntimeError("Не задан канал продаж: MS_SALESCHANNEL_ID_OZON_1/2 (или общий MS_SALESCHANNEL_ID)")


def _extract_city_from_address(addr: str) -> str | None:
    # ищем "г. Ярославль" / "г. Красноярск" и т.п.
    if not addr:
        return None
    m = re.search(r"\bг\.\s*([^,]+)", addr)
    if not m:
        return None
    city = (m.group(1) or "").strip()
    return city or None


def extract_city(ozon_order: dict) -> str | None:
    wh = (ozon_order.get("drop_off_warehouse") or {})
    addr = (wh.get("address") or "").strip()
    city = _extract_city_from_address(addr)
    return city


def _parse_timeslot_from(ozon_order: dict) -> Optional[str]:
    return (((ozon_order.get("timeslot") or {}).get("timeslot") or {}).get("from"))


def _shipment_planned_moment(ozon_order: dict) -> str | None:
    """
    В MS для CustomerOrder используем shipmentPlannedMoment.
    Нам нужна только дата — время фиксируем (по умолчанию 12:00:00).
    Формат MS: "YYYY-MM-DD HH:MM:SS"
    """
    ts = _parse_timeslot_from(ozon_order)
    if not ts:
        return None

    tz_name = os.getenv("TIMEZONE", "Asia/Krasnoyarsk")
    hour = int(os.getenv("MS_PLANNED_HOUR", "12") or "12")

    # Вход: 2026-01-07T11:00:00Z
    # Локальная дата в нужной TZ
    try:
        # python 3.11/3.12: fromisoformat не любит Z, заменим на +00:00
        ts2 = ts.replace("Z", "+00:00")
        dt_utc = datetime.fromisoformat(ts2)

        # безопасно: ZoneInfo есть в stdlib
        from zoneinfo import ZoneInfo  # type: ignore

        dt_local = dt_utc.astimezone(ZoneInfo(tz_name))
        d = dt_local.date()
        planned = datetime.combine(d, time(hour, 0, 0))
        return planned.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        # если что-то пошло не так — хотя бы дату по строке
        try:
            d = ts[:10]
            return f"{d} {hour:02d}:00:00"
        except Exception:
            return None


def build_customerorder_payload(account_name: str, ozon_order: dict, ms_positions: list[dict]) -> dict:
    org_id = load_ms_required("MS_ORGANIZATION_ID")
    agent_id = load_ms_required("MS_AGENT_ID")
    store_id = load_ms_required("MS_STORE_ID")
    state_id = load_ms_required("MS_STATE_ID")
    saleschannel_id = resolve_saleschannel_id(account_name)

    # имя: "2000039442141 - Ярославль" (+ опциональный префикс)
    name_prefix = os.getenv("MS_NAME_PREFIX", "")  # по умолчанию БЕЗ fbo-
    comment_prefix = os.getenv("MS_COMMENT_PREFIX", "Ozon FBO")

    order_id = ozon_order.get("order_id")
    order_number = ozon_order.get("order_number") or str(order_id)
    city = extract_city(ozon_order)
    name = f"{name_prefix}{order_number}{(' - ' + city) if city else ''}"

    shipment_planned = _shipment_planned_moment(ozon_order)

    payload = {
        "name": name,
        "organization": {"meta": {"href": f"https://api.moysklad.ru/api/remap/1.2/entity/organization/{org_id}",
                                  "type": "organization", "mediaType": "application/json"}},
        "agent": {"meta": {"href": f"https://api.moysklad.ru/api/remap/1.2/entity/counterparty/{agent_id}",
                           "type": "counterparty", "mediaType": "application/json"}},
        "store": {"meta": {"href": f"https://api.moysklad.ru/api/remap/1.2/entity/store/{store_id}",
                           "type": "store", "mediaType": "application/json"}},
        "state": {"meta": {"href": f"https://api.moysklad.ru/api/remap/1.2/entity/state/{state_id}",
                           "type": "state", "mediaType": "application/json"}},
        "salesChannel": {"meta": {"href": f"https://api.moysklad.ru/api/remap/1.2/entity/saleschannel/{saleschannel_id}",
                                  "type": "saleschannel", "mediaType": "application/json"}},
        # коротко, без “простыни”
        "description": f"{comment_prefix}; account={account_name}; order_id={order_id}; state={ozon_order.get('state')}",
        "positions": ms_positions,
    }

    if shipment_planned:
        payload["shipmentPlannedMoment"] = shipment_planned

    return payload


def extract_bundle_id(ozon_order: dict) -> str | None:
    for s in (ozon_order.get("supplies") or []):
        if s.get("bundle_id"):
            return s["bundle_id"]
    return None


def ozon_positions_from_bundle(ozon_client, ozon_order: dict) -> list[dict]:
    """
    Достаём товары из /v1/supply-order/bundle по bundle_id.
    Возвращаем: [{article, quantity}]
    """
    bundle_id = extract_bundle_id(ozon_order)
    if not bundle_id:
        logger.warning("Нет bundle_id у order_id=%s", ozon_order.get("order_id"))
        return []

    # ВАЖНО: метод должен существовать в ozon_client.py
    items = ozon_client.supply_order_bundle_items(bundle_id)

    out: list[dict] = []
    for it in items:
        art = (it.get("offer_id") or "").strip()
        qty = float(it.get("quantity") or 0)
        if art and qty > 0:
            out.append({"article": art, "quantity": qty})
    return out


def apply_kit_rules(positions: list[dict], kit_rules: dict[str, list[str]]) -> list[dict]:
    """
    Если position.article совпадает с kit_rule ключом (с учётом лат/кир),
    заменяем на компоненты, qty компоненты = qty комплекта.
    """
    if not kit_rules:
        return positions

    index: dict[str, list[str]] = {}
    for kit, comps in kit_rules.items():
        for v in variants_lat_cyr(kit):
            index[v] = comps

    out: list[dict] = []
    for p in positions:
        art = p["article"]
        qty = p["quantity"]

        comps = None
        for v in variants_lat_cyr(art):
            if v in index:
                comps = index[v]
                break

        if not comps:
            out.append(p)
            continue

        for c_art in comps:
            out.append({"article": c_art, "quantity": qty, "source_kit": art})

    return out


def _pick_sale_price_value(assortment_row: dict) -> int | None:
    """
    Берём цену продажи по умолчанию из salePrices.
    Можно указать MS_PRICE_TYPE_NAME (по умолчанию 'Цена продажи').
    Возвращает value (как в API МС), либо None.
    """
    want_name = (os.getenv("MS_PRICE_TYPE_NAME") or "Цена продажи").strip()
    sale_prices = assortment_row.get("salePrices") or []
    if not isinstance(sale_prices, list) or not sale_prices:
        return None

    # 1) пробуем по названию типа цены
    for sp in sale_prices:
        pt = (sp.get("priceType") or {})
        if (pt.get("name") or "").strip() == want_name:
            v = sp.get("value")
            return int(v) if v is not None else None

    # 2) fallback: первая цена
    v = sale_prices[0].get("value")
    return int(v) if v is not None else None


def sync_orders_to_ms(ozon_client, account_name: str, ozon_orders: list[dict]) -> dict[str, Any]:
    mode = (os.getenv("MS_MODE") or "DRY").upper()
    max_live = int(os.getenv("MS_LIVE_MAX", "0") or "0")

    ms = MSClient()
    kit_rules = load_kit_rules()

    created = 0
    skipped = 0
    duplicates = 0
    errors: list[str] = []

    # кеш: article -> (meta_dict, price_value)
    assortment_cache: dict[str, tuple[dict, int | None]] = {}

    for o in ozon_orders:
        # стоп-кран для LIVE
        if mode == "LIVE" and max_live > 0 and created >= max_live:
            logger.info("[%s] Reached MS_LIVE_MAX=%s, stopping.", account_name, max_live)
            break

        # пропускаем виртуальные
        if (o.get("order_tags") or {}).get("is_virtual"):
            skipped += 1
            continue

        oz_pos = ozon_positions_from_bundle(ozon_client, o)
        if not oz_pos:
            skipped += 1
            continue

        oz_pos = apply_kit_rules(oz_pos, kit_rules)

        ms_positions: list[dict] = []
        local_errs: list[str] = []

        for p in oz_pos:
            art = p["article"]
            qty = p["quantity"]

            cached = assortment_cache.get(art)
            if cached is None:
                # берём строку ассортимента, чтобы достать и meta, и salePrices
                data = ms.get("/entity/assortment", params={"filter": f"article={art}", "limit": 1})
                rows = data.get("rows") or []
                row = rows[0] if rows else None
                if not row:
                    assortment_cache[art] = ({}, None)
                    cached = ({}, None)
                else:
                    meta = row.get("meta") or {}
                    price_val = _pick_sale_price_value(row)
                    assortment_cache[art] = (meta, price_val)
                    cached = (meta, price_val)

            meta, price_val = cached
            if not meta:
                local_errs.append(f'not found in MS by article="{art}"')
                continue

            pos = {
                "assortment": {"meta": meta},
                "quantity": qty,
            }
            # цена обязательна для “цена продажи по умолчанию”
            if price_val is not None:
                pos["price"] = price_val
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
                "[DRY][%s] Would create CustomerOrder: name=%s positions=%s order_id=%s",
                account_name,
                payload.get("name"),
                len(payload.get("positions") or []),
                o.get("order_id"),
            )
            continue

        if mode == "LIVE":
            created_doc = ms.create_customerorder(payload)
            created += 1
            logger.info(
                "[%s] Created CustomerOrder: name=%s id=%s",
                account_name,
                payload.get("name"),
                (created_doc.get("id") if isinstance(created_doc, dict) else None),
            )
            continue

        raise RuntimeError(f"Unknown MS_MODE={mode}")

    return {
        "mode": mode,
        "created_or_would_create": created,
        "skipped": skipped,
        "duplicates": duplicates,
        "errors_count": len(errors),
        "errors": errors[:50],
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
