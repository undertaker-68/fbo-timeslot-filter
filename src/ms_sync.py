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
    МС: deliveryPlannedMoment
    Формат МС: 'YYYY-MM-DD HH:MM:SS.mmm' (без 'T' и без 'Z')
    Нам важна ДАТА по таймслоту (локальная), время не важно.
    Ставим 00:00:00.000
    """
    try:
        ts_from = (((order.get("timeslot") or {}).get("timeslot") or {}).get("from")) or ""
        dt_utc = _parse_dt(ts_from)
        dt_local = dt_utc.astimezone(_tz())
        d = dt_local.date().isoformat()
        return f"{d} 00:00:00.000"
    except Exception:
        return None


def _ensure_assortment_meta(x: dict | None) -> dict | None:
    """
    На вход может прийти:
      - meta: {"href": "...", "type": "...", "mediaType": "..."}
      - объект: {"meta": {...}, ...}
      - уже обёрнутый: {"meta": {...}}
    На выходе ВСЕГДА: {"meta": {...}} или None
    """
    if not x or not isinstance(x, dict):
        return None

    if "meta" in x and isinstance(x.get("meta"), dict):
        meta = x["meta"]
    else:
        meta = x

    if not isinstance(meta, dict):
        return None

    if not meta.get("href") or not meta.get("type"):
        return None

    return {"meta": meta}


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
        return head[:1].upper() + head[1:].lower()
    except Exception:
        return ""


def load_kit_rules() -> dict[str, list[str]]:
    """
    Комплекты берём из ENV MS_KIT_RULES.

    Формат:
      MS_KIT_RULES=10264-А93:10264+11291;00020-А92:00020+11278

    То есть:
      <kit_article>:<component1>+<component2>[+...];<kit2>:...
    """
    raw = (os.getenv("MS_KIT_RULES") or "").strip()
    if not raw:
        return {}

    out: dict[str, list[str]] = {}
    for block in raw.split(";"):
        block = block.strip()
        if not block:
            continue
        if ":" not in block:
            continue
        kit, comps_raw = block.split(":", 1)
        kit = kit.strip()
        comps = [c.strip() for c in comps_raw.split("+") if c.strip()]
        if kit and comps:
            out[kit] = comps
    return out


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

    # в ozon_client должен быть метод supply_order_bundle_items(bundle_id, limit=100)
    items = ozon_client.supply_order_bundle_items(bundle_id, limit=100)

    out: list[dict] = []
    for it in items or []:
        art = (it.get("offer_id") or "").strip()
        qty = float(it.get("quantity") or 0)
        if art and qty > 0:
            out.append({"article": art, "quantity": qty})
    return out


def build_customerorder_payload(account_name: str, order: dict, ms_positions: list[dict]) -> dict[str, Any]:
    # обязательные справочники из ENV
    org_id = os.getenv("MS_ORGANIZATION_ID", "").strip()
    agent_id = os.getenv("MS_AGENT_ID", "").strip()
    store_id = os.getenv("MS_STORE_ID", "").strip()
    state_id = os.getenv("MS_STATE_ID", "").strip()
    saleschannel_id = (os.getenv(f"MS_SALESCHANNEL_ID_{account_name}", "") or os.getenv("MS_SALESCHANNEL_ID", "")).strip()

    if not org_id:
        raise RuntimeError("MS_ORGANIZATION_ID не задан в .env")
    if not agent_id:
        raise RuntimeError("MS_AGENT_ID не задан в .env")
    if not store_id:
        raise RuntimeError("MS_STORE_ID не задан в .env")
    if not state_id:
        raise RuntimeError("MS_STATE_ID не задан в .env")
    if not saleschannel_id:
        raise RuntimeError("MS_SALESCHANNEL_ID_<ACCOUNT> не задан (например MS_SALESCHANNEL_ID_OZON_1)")

    order_number = (order.get("order_number") or "").strip()
    order_id = order.get("order_id")

    # имя заказа
    name_prefix = os.getenv("MS_NAME_PREFIX", "fbo-")
    name = f"{name_prefix}{order_number}"

    # короткий комментарий: "номер - город"
    city = destination_city(order)
    descr = f"{order_number} - {city}" if city else f"{order_number}"

    payload: dict[str, Any] = {
        "name": name,
        "description": descr,
        "externalCode": f"{account_name}:{order_id}",

        "organization": {
            "meta": {
                "href": f"https://api.moysklad.ru/api/remap/1.2/entity/organization/{org_id}",
                "type": "organization",
                "mediaType": "application/json",
            }
        },
        "agent": {
            "meta": {
                "href": f"https://api.moysklad.ru/api/remap/1.2/entity/counterparty/{agent_id}",
                "type": "counterparty",
                "mediaType": "application/json",
            }
        },
        "store": {
            "meta": {
                "href": f"https://api.moysklad.ru/api/remap/1.2/entity/store/{store_id}",
                "type": "store",
                "mediaType": "application/json",
            }
        },
        "state": {
            "meta": {
                "href": f"https://api.moysklad.ru/api/remap/1.2/entity/state/{state_id}",
                "type": "state",
                "mediaType": "application/json",
            }
        },
        "salesChannel": {
            "meta": {
                "href": f"https://api.moysklad.ru/api/remap/1.2/entity/saleschannel/{saleschannel_id}",
                "type": "saleschannel",
                "mediaType": "application/json",
            }
        },

        "positions": ms_positions,
    }

    # deliveryPlannedMoment из таймслота
    dm = planned_delivery_moment(order)
    if dm:
        payload["deliveryPlannedMoment"] = dm

    # !!! КЛЮЧЕВОЕ: priceType "Цена продажи"
    # Чтобы МС сам подставил цены в позициях по типу цен "Цена продажи"
    price_type_href = (os.getenv("MS_PRICE_TYPE_HREF") or "").strip()
    if price_type_href:
        payload["priceType"] = {
            "meta": {
                "href": price_type_href,
                "type": "pricetype",
                "mediaType": "application/json",
            }
        }

    return payload


def sync_orders_to_ms(ozon_client, account_name: str, ozon_orders: list[dict]) -> dict[str, Any]:
    """
    DRY: только логируем что бы создали
    LIVE: реально создаём (ограничиваем MS_LIVE_MAX)
    """
    attempted = 0
    mode = (os.getenv("MS_MODE") or "DRY").upper()
    max_live = int(os.getenv("MS_LIVE_MAX", "0") or "0")

    ms = MSClient()
    kit_rules = load_kit_rules()

    created = 0
    skipped = 0
    duplicates = 0
    errors: list[str] = []

    # cache: article -> meta
    assortment_cache: dict[str, dict] = {}

    for o in ozon_orders:
        # пропускаем виртуальные
        if (o.get("order_tags") or {}).get("is_virtual"):
            skipped += 1
            continue

        if mode == "LIVE" and max_live > 0 and attempted >= max_live:
            logger.info("[%s] Reached MS_LIVE_MAX=%s (attempted), stopping.", account_name, max_live)
            break

        # 1) позиции из Ozon bundle
        oz_pos = ozon_positions_from_bundle(ozon_client, o)
        if not oz_pos:
            skipped += 1
            continue

        # 2) развернуть комплекты
        oz_pos = apply_kit_rules(oz_pos, kit_rules)

        # 3) собрать позиции МС: assortment meta + quantity
        # !!! ВАЖНО: НЕ передаём price вообще. Цена будет взята из priceType ("Цена продажи") в документе.
        ms_positions: list[dict] = []
        local_errs: list[str] = []

        for p in oz_pos:
            art = p["article"]
            qty = p["quantity"]

            meta: Optional[dict] = None
            if art in assortment_cache:
                meta = assortment_cache[art]
            else:
                row = ms.find_assortment_by_article(art)
                if row and isinstance(row.get("meta"), dict):
                    meta = row["meta"]  # чистая meta
                    assortment_cache[art] = meta

            ass = _ensure_assortment_meta(meta)
            if not ass:
                local_errs.append(f'bad assortment meta for article="{art}"')
                continue

            ms_positions.append({"assortment": ass, "quantity": qty})

        if local_errs:
            errors.append(f'order_id={o.get("order_id")}: ' + "; ".join(local_errs))

        if not ms_positions:
            skipped += 1
            continue

        payload = build_customerorder_payload(account_name, o, ms_positions)

        if mode == "DRY":
            created += 1
            logger.info(
                "[DRY][%s] Would create CustomerOrder: name=%s positions=%s order_id=%s deliveryPlannedMoment=%s priceType=%s",
                account_name,
                payload.get("name"),
                len(payload.get("positions") or []),
                o.get("order_id"),
                payload.get("deliveryPlannedMoment"),
                (payload.get("priceType") or {}).get("meta", {}).get("href"),
            )
            continue

        # LIVE
        try:
            attempted += 1
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
        "attempted": attempted,
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
