import os
from typing import Any

from .logger import logger
from .ms_client import MSClient, variants_lat_cyr


def _ms_meta(entity: str, entity_id: str) -> dict:
    return {
        "meta": {
            "href": f"https://api.moysklad.ru/api/remap/1.2/entity/{entity}/{entity_id}",
            "type": entity,
            "mediaType": "application/json",
        }
    }


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
    Учитываем лат/кир: ключи нормализуем через варианты.
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
    # account_name: ozon_1 / ozon_2
    key = f"MS_SALESCHANNEL_ID_{account_name.upper()}"  # OZON_1, OZON_2
    v = os.getenv(key)
    if v:
        return v
    # fallback (если вдруг захочешь общий)
    v2 = os.getenv("MS_SALESCHANNEL_ID")
    if v2:
        return v2
    raise RuntimeError("Не задан канал продаж: MS_SALESCHANNEL_ID_OZON_1/2 (или общий MS_SALESCHANNEL_ID)")


def build_customerorder_payload(account_name: str, ozon_order: dict, ms_positions: list[dict]) -> dict:
    org_id = load_ms_required("MS_ORGANIZATION_ID")
    agent_id = load_ms_required("MS_AGENT_ID")
    store_id = load_ms_required("MS_STORE_ID")
    state_id = load_ms_required("MS_STATE_ID")
    saleschannel_id = resolve_saleschannel_id(account_name)

    name_prefix = os.getenv("MS_NAME_PREFIX", "OZON FBO")
    comment_prefix = os.getenv("MS_COMMENT_PREFIX", "Ozon FBO supply-order")

    order_id = ozon_order.get("order_id")
    order_number = ozon_order.get("order_number")
    timeslot_from = ((ozon_order.get("timeslot") or {}).get("timeslot") or {}).get("from")

    payload = {
        "name": f"{name_prefix}{order_number or order_id}",
        "organization": _ms_meta("organization", org_id),
        "agent": _ms_meta("counterparty", agent_id),
        "store": _ms_meta("store", store_id),
        "state": _ms_meta("state", state_id),
        "salesChannel": _ms_meta("saleschannel", saleschannel_id),
        "description": (
            f"{comment_prefix}. "
            f"account={account_name}; order_id={order_id}; order_number={order_number}; "
            f"state={ozon_order.get('state')}; timeslot_from={timeslot_from}"
        ),
        "positions": ms_positions,
    }
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

    items = ozon_client.supply_order_bundle_items(bundle_id)
    out = []
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

    # построим индекс правил с учётом вариантов лат/кир для ключей
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

        # разворачиваем по правилам
        for c_art in comps:
            out.append({"article": c_art, "quantity": qty, "source_kit": art})

    return out


def build_ms_positions(ms: MSClient, ozon_positions: list[dict]) -> tuple[list[dict], list[str]]:
    """
    Сопоставляем по article -> meta.
    Возвращаем (ms_positions, errors)
    """
    ms_positions: list[dict] = []
    errors: list[str] = []

    for p in ozon_positions:
        art = p["article"]
        qty = p["quantity"]

        meta = ms.find_assortment_by_article(art)
        if not meta:
            errors.append(f'not found in MS by article="{art}"')
            continue

        ms_positions.append({
            "assortment": meta,
            "quantity": qty,
        })

    return ms_positions, errors

def sync_orders_to_ms(ozon_client, account_name: str, ozon_orders: list[dict]) -> dict[str, Any]:
    mode = (os.getenv("MS_MODE") or "DRY").upper()
    max_live = int(os.getenv("MS_LIVE_MAX", "0") or "0")

    ms = MSClient()
    kit_rules = load_kit_rules()

    created = 0
    skipped = 0
    duplicates = 0
    errors: list[str] = []

    for o in ozon_orders:
        # стоп-кран для LIVE
        if mode == "LIVE" and max_live > 0 and created >= max_live:
            logger.info("[%s] Reached MS_LIVE_MAX=%s, stopping.", account_name, max_live)
            break

        # пропускаем виртуальные
        if (o.get("order_tags") or {}).get("is_virtual"):
            skipped += 1
            continue

        # ... дальше твоя логика: bundle -> kit_rules -> build_ms_positions -> duplicate check ...

        if mode == "DRY":
            created += 1
            logger.info("[DRY][%s] Would create CustomerOrder: ...", account_name)
            continue

        if mode == "LIVE":
            created_doc = ms.create_customerorder(payload)
            created += 1
            logger.info("[%s] Created CustomerOrder: name=%s id=%s", account_name, payload.get("name"), created_doc.get("id"))
            continue

    return {
        "mode": mode,
        "created_or_would_create": created,
        "skipped": skipped,
        "duplicates": duplicates,
        "errors_count": len(errors),
        "errors": errors[:50],
    }

# совместимость со старым именем
def sync_orders_to_ms_dry(ozon_client, account_name: str, ozon_orders: list[dict]) -> dict[str, Any]:
    os.environ["MS_MODE"] = (os.getenv("MS_MODE") or "DRY")
    return sync_orders_to_ms(ozon_client, account_name, ozon_orders)


def sync_orders_to_ms_dry(ozon_client, account_name: str, ozon_orders: list[dict]) -> dict[str, Any]:
    """
    DRY режим: ничего не создаём. Только готовим payload и логируем итог.
    """
    mode = (os.getenv("MS_MODE") or "DRY").upper()
    if mode != "DRY":
        raise RuntimeError("Пока включён только DRY. Для LIVE сделаем отдельным шагом после проверки.")

    ms = MSClient()
    kit_rules = load_kit_rules()

    would_create = 0
    skipped = 0
    errors: list[str] = []

    for o in ozon_orders:
        if (o.get("order_tags") or {}).get("is_virtual"):
            skipped += 1
            continue

        oz_pos = ozon_positions_from_bundle(ozon_client, o)
        if not oz_pos:
            skipped += 1
            continue

        oz_pos = apply_kit_rules(oz_pos, kit_rules)

        ms_pos, errs = build_ms_positions(ms, oz_pos)
        if errs:
            errors.append(f'order_id={o.get("order_id")}: ' + "; ".join(errs))
        if not ms_pos:
            skipped += 1
            continue

        payload = build_customerorder_payload(account_name, o, ms_pos)
        would_create += 1

        logger.info(
            "[DRY][%s] Would create CustomerOrder: name=%s positions=%s order_id=%s",
            account_name,
            payload.get("name"),
            len(payload.get("positions") or []),
            o.get("order_id"),
        )

    return {"mode": mode, "would_create": would_create, "skipped": skipped, "errors_count": len(errors), "errors": errors[:50]}
