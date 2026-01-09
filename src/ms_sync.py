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

                o["_ms_customerorder_id"] = ms_id
                updated += 1
                logger.info("[LIVE][%s] Updated CustomerOrder: name=%s id=%s order_id=%s", account_name, name, ms_id, o.get("order_id"))
            else:
                created_doc = ms.create_customerorder(payload)
                created += 1
                doc_id = created_doc.get("id") or ((created_doc.get("meta") or {}).get("href") or "")
                o["_ms_customerorder_id"] = created_doc.get("id")
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

MOVE_STATE_ID = "b0d2c89d-5c7c-11ef-0a80-0cd4001f5885"
MOVE_SOURCE_STORE_ID = "7cdb9b20-9910-11ec-0a80-08670002d998"
DEMAND_STATE_ID = "b543e330-44e4-11f0-0a80-0da5002260ab"

def sync_moves_from_orders(ozon_client, account_name: str, ozon_orders: list[dict]) -> dict[str, Any]:
    """
    1 заказ = 1 перемещение (move)
    Upsert:
      - если move существует -> обновляем ТОЛЬКО позиции (qty/price)
      - если нет -> создаём (applicable=True, если ошибка -> applicable=False)
    """
    attempted = 0
    mode = (os.getenv("MS_MODE") or "DRY").upper()
    max_live = int(os.getenv("MS_LIVE_MAX", "0") or "0")

    ms = MSClient()
    kit_rules = load_kit_rules()

    created = 0
    created_unapplicable = 0
    updated = 0
    skipped = 0
    errors: list[str] = []
    warnings: list[str] = []

    assortment_cache: dict[str, dict] = {}

    price_type_href = (os.getenv("MS_PRICE_TYPE_HREF") or "").strip()
    price_type_name = (os.getenv("MS_PRICE_TYPE_NAME") or "Цена продажи").strip()

    # targetStore (склад куда) = тот же, что в Заказе -> у тебя это MS_STORE_ID
    target_store_id = (os.getenv("MS_STORE_ID") or "").strip()
    if not target_store_id:
        raise RuntimeError("MS_STORE_ID не задан в .env")

    for o in ozon_orders:
        # пропускаем виртуальные (как в заказах)
        if (o.get("order_tags") or {}).get("is_virtual"):
            skipped += 1
            continue

        if mode == "LIVE" and max_live > 0 and attempted >= max_live:
            logger.info("[%s] Reached MS_LIVE_MAX=%s (attempted), stopping.", account_name, max_live)
            break

        order_id = o.get("order_id")
        order_number = (o.get("order_number") or "").strip()

        # Название документа = как в Заказе (CustomerOrder name)
        name_prefix = (os.getenv("MS_NAME_PREFIX") or "fbo-").strip() or "fbo-"
        doc_name = f"{name_prefix}{order_number}"

        # Комментарий = как в Заказе (CustomerOrder description)
        city = destination_city(o)
        descr = f"{order_number} - {city}" if city else f"{order_number}"

        external_code = f"{account_name}:{order_id}"

        # ----------- собираем позиции так же, как для CustomerOrder -----------
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

            price_val = _extract_sale_price_value(
                row,
                price_type_href=price_type_href,
                price_type_name=price_type_name,
            )
            if price_val is None:
                warnings.append(f'order_id={order_id}: no sale price "{price_type_name}" for article="{art}" -> price=0')
                price_val = 0

            ms_positions.append({
                "assortment": ass,
                "quantity": qty,
                "price": int(price_val),
            })

        if local_errs:
            errors.append(f'order_id={order_id}: ' + "; ".join(local_errs))

        if not ms_positions:
            skipped += 1
            continue

        # ----------- UPSERT move -----------
        try:
            existing = ms.find_move_by_external_code(external_code)
        except Exception as e:
            errors.append(f"order_id={order_id}: {e}")
            continue

        if mode == "DRY":
            logger.info(
                "[DRY][%s] Would %s Move: externalCode=%s name=%s positions=%s order_id=%s",
                account_name,
                "UPDATE" if existing else "CREATE",
                external_code,
                doc_name,
                len(ms_positions),
                order_id,
            )
            continue

        attempted += 1

        try:
            if existing:
                move_id = existing.get("id") or ((existing.get("meta") or {}).get("href") or "").rsplit("/", 1)[-1]
                ms.replace_move_positions(move_id, ms_positions)
                updated += 1
                logger.info("[LIVE][%s] Updated Move positions: id=%s order_id=%s", account_name, move_id, order_id)
            else:
                payload = {
                    "name": doc_name,
                    "description": descr,
                    "externalCode": external_code,
                    "applicable": True,

                    "state": {
                        "meta": {
                            "href": f"https://api.moysklad.ru/api/remap/1.2/entity/state/{MOVE_STATE_ID}",
                            "type": "state",
                            "mediaType": "application/json",
                        }
                    },
                    # организация = как в заказе (берём из env, так же как в build_customerorder_payload)
                    "organization": {
                        "meta": {
                            "href": f"https://api.moysklad.ru/api/remap/1.2/entity/organization/{(os.getenv('MS_ORGANIZATION_ID') or '').strip()}",
                            "type": "organization",
                            "mediaType": "application/json",
                        }
                    },
                    "sourceStore": {
                        "meta": {
                            "href": f"https://api.moysklad.ru/api/remap/1.2/entity/store/{MOVE_SOURCE_STORE_ID}",
                            "type": "store",
                            "mediaType": "application/json",
                        }
                    },
                    "targetStore": {
                        "meta": {
                            "href": f"https://api.moysklad.ru/api/remap/1.2/entity/store/{target_store_id}",
                            "type": "store",
                            "mediaType": "application/json",
                        }
                    },
                    "positions": ms_positions,
                }

                customerorder_id = o.get("_ms_customerorder_id")
                if customerorder_id:
                    payload["customerOrder"] = {
                        "meta": {
                            "href": f"https://api.moysklad.ru/api/remap/1.2/entity/customerorder/{customerorder_id}",
                            "type": "customerorder",
                            "mediaType": "application/json",
                        }
                    }

                # 1) пробуем создать проведённым
                try:
                    ms.create_move(payload)
                    created += 1
                    logger.info("[LIVE][%s] Created Move (applicable): order_id=%s", account_name, order_id)
                except Exception:
                    # 2) если не вышло — создаём непроведённым
                    payload["applicable"] = False
                    ms.create_move(payload)
                    created_unapplicable += 1
                    logger.info("[LIVE][%s] Created Move (NOT applicable): order_id=%s", account_name, order_id)

        except Exception as e:
            errors.append(f"order_id={order_id}: {e}")
            logger.exception("[%s] move upsert error", account_name)

    return {
        "mode": mode,
        "created": created,
        "created_unapplicable": created_unapplicable,
        "updated": updated,
        "skipped": skipped,
        "errors": errors,
        "warnings": warnings,
        "attempted": attempted,
    }

DEMAND_ALLOWED_SUPPLY_STATES = {
    "ACCEPTED_AT_SUPPLY_WAREHOUSE",
    "IN_TRANSIT",
    "ACCEPTANCE_AT_STORAGE_WAREHOUSE",
    "REPORTS_CONFIRMATION_AWAITING",
    "COMPLETED",
    "REJECTED_AT_SUPPLY_WAREHOUSE",
    "REPORT_REJECTED",
}

def _order_allows_demand(order: dict) -> bool:
    """
    Demand создаём, если у заявки есть хотя бы одна поставка
    со статусом из DEMAND_ALLOWED_SUPPLY_STATES.
    Пока только READY_TO_SUPPLY — не создаём.
    """
    for s in order.get("supplies", []) or []:
        if s.get("state") in DEMAND_ALLOWED_SUPPLY_STATES:
            return True
    return False


def sync_demands_from_orders(ozon_client, account_name: str, ozon_orders: list[dict]) -> dict[str, Any]:
    """
    Demand (Отгрузка):
      - Создаём ТОЛЬКО если supply.state != READY_TO_SUPPLY (по whitelist статусов).
      - Если Demand уже есть -> НИЧЕГО не делаем (полный skip).
      - Если создать applicable=True не удалось -> создаём applicable=False (как у move).
      - Связь в МС: customerOrder.
    """
    attempted = 0
    mode = (os.getenv("MS_MODE") or "DRY").upper()
    max_live = int(os.getenv("MS_LIVE_MAX", "0") or "0")

    ms = MSClient()
    kit_rules = load_kit_rules()

    created = 0
    created_unapplicable = 0
    skipped_not_ready = 0
    skipped_exists = 0
    skipped = 0
    errors: list[str] = []
    warnings: list[str] = []

    assortment_cache: dict[str, dict] = {}

    price_type_href = (os.getenv("MS_PRICE_TYPE_HREF") or "").strip()
    price_type_name = (os.getenv("MS_PRICE_TYPE_NAME") or "Цена продажи").strip()

    org_id = (os.getenv("MS_ORGANIZATION_ID") or "").strip()
    agent_id = (os.getenv("MS_AGENT_ID") or "").strip()
    store_id = (os.getenv("MS_STORE_ID") or "").strip()  # склад отгрузки
    if not org_id:
        raise RuntimeError("MS_ORGANIZATION_ID не задан в .env")
    if not agent_id:
        raise RuntimeError("MS_AGENT_ID не задан в .env")
    if not store_id:
        raise RuntimeError("MS_STORE_ID не задан в .env")

    name_prefix = (os.getenv("MS_NAME_PREFIX") or "fbo-").strip() or "fbo-"

    for o in ozon_orders:
        if (o.get("order_tags") or {}).get("is_virtual"):
            skipped += 1
            continue

        if not _order_allows_demand(o):
            skipped_not_ready += 1
            continue

        if mode == "LIVE" and max_live > 0 and attempted >= max_live:
            logger.info("[%s] Reached MS_LIVE_MAX=%s (attempted), stopping.", account_name, max_live)
            break

        order_id = o.get("order_id")
        order_number = (o.get("order_number") or "").strip()

        # Важно: это тот же CustomerOrder name, который уже используется
        co_name = f"{name_prefix}{order_number}"

        # Ссылка на CustomerOrder (чтобы demand отображался в "связке")
        customerorder_id = o.get("_ms_customerorder_id")
        if not customerorder_id:
            # На всякий случай (DRY или если заказ не писали в этом прогоне):
            try:
                co = ms.find_customerorder_by_external_code(co_name)
                if co and co.get("id"):
                    customerorder_id = co["id"]
            except Exception:
                customerorder_id = None

        if not customerorder_id:
            errors.append(f"order_id={order_id}: cannot determine MS customerorder id for demand link")
            continue

        customerorder_href = f"https://api.moysklad.ru/api/remap/1.2/entity/customerorder/{customerorder_id}"

        # Если Demand уже есть -> полный skip (по твоему требованию)
        try:
            existing = ms.find_demand_by_customerorder_href(customerorder_href)
        except Exception as e:
            errors.append(f"order_id={order_id}: {e}")
            continue

        if existing:
            skipped_exists += 1
            if mode == "DRY":
                logger.info("[DRY][%s] Demand exists -> SKIP: order_id=%s customerOrder=%s", account_name, order_id, customerorder_id)
            continue

        # ---------- позиции как в заказе/перемещении ----------
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

            row: Optional[dict] = assortment_cache.get(art)
            if row is None:
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

            price_val = _extract_sale_price_value(
                row,
                price_type_href=price_type_href,
                price_type_name=price_type_name,
            )
            if price_val is None:
                warnings.append(f'order_id={order_id}: no sale price "{price_type_name}" for article="{art}" -> price=0')
                price_val = 0

            ms_positions.append({"assortment": ass, "quantity": qty, "price": int(price_val)})

        if local_errs:
            errors.append(f'order_id={order_id}: ' + "; ".join(local_errs))

        if not ms_positions:
            skipped += 1
            continue

        city = destination_city(o)
        descr = f"{order_number} - {city}" if city else f"{order_number}"

        demand_payload = {
            "name": co_name,
            "externalCode": f"{account_name}:{order_id}",
            "description": "Auto-generated Demand",
            "customerOrder": {
                "meta": {
                    "href": customerorder_href,
                    "type": "customerorder",
                    "mediaType": "application/json",
                }
            },
            "positions": ms_positions,
            "state": {
                "meta": {
                    "href": f"https://api.moysklad.ru/api/remap/1.2/entity/state/{DEMAND_STATE_ID}",
                    "type": "state",
                    "mediaType": "application/json",
                }
            },
        }

        if mode == "DRY":
            logger.info(
                "[DRY][%s] Would CREATE Demand: order_id=%s customerOrder=%s positions=%s",
                account_name, order_id, customerorder_id, len(ms_positions),
            )
            continue

        attempted += 1

        try:
            # 1) пробуем создать Demand
            ms.create_demand(demand_payload)
            created += 1
            logger.info("[LIVE][%s] Created Demand (applicable): order_id=%s", account_name, order_id)
        except Exception:
            # 2) если не вышло — создаём непроведённым
            demand_payload["applicable"] = False
            ms.create_demand(demand_payload)
            created_unapplicable += 1
            logger.info("[LIVE][%s] Created Demand (NOT applicable): order_id=%s", account_name, order_id)

    return {
        "mode": mode,
        "created": created,
        "created_unapplicable": created_unapplicable,
        "skipped_not_ready": skipped_not_ready,
        "skipped_exists": skipped_exists,
        "skipped": skipped,
        "errors": errors,
        "warnings": warnings,
        "attempted": attempted,
    }
