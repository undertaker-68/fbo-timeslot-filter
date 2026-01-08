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
    MS: deliveryPlannedMoment
    Формат МС: 'YYYY-MM-DD HH:MM:SS.mmm'
    Дата = локальная дата timeslot.from, время = 00:00:00.000
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
    MS_KIT_RULES=10264-А93:10264+11291;00020-А92:00020+11278
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

    items = ozon_client.supply_order_bundle_items(bundle_id, limit=100)

    out: list[dict] = []
    for it in items or []:
        art = (it.get("offer_id") or "").strip()
        qty = float(it.get("quantity") or 0)
        if art and qty > 0:
            out.append({"article": art, "quantity": qty})
    return out


def _extract_sale_price_value(row: dict, *, price_type_href: str, price_type_name: str) -> Optional[int]:
    """
    Берём цену из row['salePrices'] по priceType (href или name).
    Возвращаем int value (в минимальных единицах МС).
    """
    sale_prices = row.get("salePrices") or []
    for sp in sale_prices:
        pt = sp.get("priceType") or {}
        pt_meta = pt.get("meta") or {}
        href = (pt_meta.get("href") or "").strip()
        name = (pt.get("name") or "").strip()

        if price_type_href and href == price_type_href:
            try:
                return int(float(sp.get("value") or 0))
            except Exception:
                return None

        if (not price_type_href) and price_type_name and name == price_type_name:
            try:
                return int(float(sp.get("value") or 0))
            except Exception:
                return None

    return None


def build_customerorder_payload(account_name: str, order: dict, ms_positions: list[dict]) -> dict[str, Any]:
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

    name_prefix = (os.getenv("MS_NAME_PREFIX") or "fbo-").strip()
    if not name_prefix:
        name_prefix = "fbo-"
    name = f"{name_prefix}{order_number}"

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

    dm = planned_delivery_moment(order)
    if dm:
        payload["deliveryPlannedMoment"] = dm

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
    DRY: логируем create/update
    LIVE: create или update (upsert), MS_LIVE_MAX ограничивает attempted
    """
    attempted = 0
    mode = (os.getenv("MS_MODE") or "DRY").upper()
    max_live = int(os.getenv("MS_LIVE_MAX", "0") or "0")

    ms = MSClient()
    kit_rules = load_kit_rules()

    created = 0
    updated = 0
    skipped = 0
    duplicates = 0  # оставим для совместимости, но теперь не используем как ошибку
    errors: list[str] = []
    warnings: list[str] = []

    assortment_cache: dict[str, dict] = {}

    price_type_href = (os.getenv("MS_PRICE_TYPE_HREF") or "").strip()
    price_type_name = (os.getenv("MS_PRICE_TYPE_NAME") or "Цена продажи").strip()

    for o in ozon_orders:
        # пропускаем виртуальные
        if (o.get("order_tags") or {}).get("is_virtual"):
            skipped += 1
            continue

        if mode == "LIVE" and max_live > 0 and attempted >= max_live:
            logger.info("[%s] Reached MS_LIVE_MAX=%s (attempted), stopping.", account_name, max_live)
            break

        oz_pos = ozon_positions_from_bundle(ozon_client, o)
        if not oz_pos:
            skipped += 1
            continue

        oz_pos = apply_kit_rules(oz_pos, kit_rules)

        ms_positions: list[dict] = []
        local_errs: list[str] = []

        for p in oz_pos:
            art = (p.get("article") or "").strip()
            qty = float(p.get("quantity") or 0)

            if not art or qty <= 0:
                continue

            row: Optional[dict] = None
            if art in assortment_cache:
                row = assortment_cache[art]
            else:
                found = ms.find_assortment_by_article(art)
                if found:
                    row = found
                    assortment_cache[art] = found

            if not row:
                local_errs.append(f'not found in MS by article="{art}"')
                continue

            ass = _ensure_assortment_meta(row.get("meta"))
            if not ass:
                local_errs.append(f'bad assortment meta for article="{art}"')
                continue

            # Цена: если не нашли — ставим 0 и это НЕ ошибка, только warning
            price_val = _extract_sale_price_value(
                row,
                price_type_href=price_type_href,
                price_type_name=price_type_name,
            )
            if price_val is None:
                warnings.append(f'order_id={o.get("order_id")}: no sale price "{price_type_name}" for article="{art}" -> price=0')
                price_val = 0

            ms_positions.append({
                "assortment": ass,
                "quantity": qty,
                "price": int(price_val),
            })

        if local_errs:
            errors.append(f'order_id={o.get("order_id")}: ' + "; ".join(local_errs))

        if not ms_positions:
            skipped += 1
            continue

        payload = build_customerorder_payload(account_name, o, ms_positions)
        name = payload.get("name")

        # ---------- UPSERT ----------
        existing = None
        try:
            if name:
                existing = ms.find_customerorder_by_name(name)
        except Exception:
            existing = None

        if mode == "DRY":
            if existing:
                logger.info(
                    "[DRY][%s] Would UPDATE CustomerOrder: name=%s positions=%s order_id=%s deliveryPlannedMoment=%s",
                    account_name,
                    name,
                    len(payload.get("positions") or []),
                    o.get("order_id"),
                    payload.get("deliveryPlannedMoment"),
                )
            else:
                logger.info(
                    "[DRY][%s] Would CREATE CustomerOrder: name=%s positions=%s order_id=%s deliveryPlannedMoment=%s",
                    account_name,
                    name,
                    len(payload.get("positions") or []),
                    o.get("order_id"),
                    payload.get("deliveryPlannedMoment"),
                )
            continue

        attempted += 1

        try:
            if existing:
                ms_id = existing.get("id")
                if not ms_id:
                    # fallback: вытащим id из meta.href
                    href = ((existing.get("meta") or {}).get("href") or "")
                    ms_id = href.rsplit("/", 1)[-1] if href else None

                if not ms_id:
                    raise RuntimeError("Cannot determine MS customerorder id for update")

                # 1) обновляем шапку (без positions)
                hdr = dict(payload)
                hdr.pop("positions", None)

                ms.update_customerorder(ms_id, hdr)

                # 2) перезаписываем позиции
                ms.replace_customerorder_positions(ms_id, payload.get("positions") or [])

                updated += 1
                logger.info("[LIVE][%s] Updated CustomerOrder: name=%s id=%s order_id=%s", account_name, name, ms_id, o.get("order_id"))
            else:
                created_doc = ms.create_customerorder(payload)
                created += 1
                doc_id = created_doc.get("id") or ((created_doc.get("meta") or {}).get("href") or "")
                logger.info("[LIVE][%s] Created CustomerOrder: name=%s id=%s order_id=%s", account_name, created_doc.get("name"), doc_id, o.get("order_id"))

        except Exception as e:
            errors.append(f'order_id={o.get("order_id")}: {e}')
            logger.exception("[%s] upsert customerorder error", account_name)

    return {
        "mode": mode,
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "duplicates": duplicates,
        "errors": errors,
        "warnings": warnings,
        "attempted": attempted,
    }
