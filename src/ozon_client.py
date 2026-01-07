import os
import time
import random
import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://api-seller.ozon.ru"


def _raise_for_status_with_body(r: requests.Response, name: str):
    try:
        r.raise_for_status()
    except requests.HTTPError as e:
        body = (r.text or "").strip()
        body_short = body[:2000]
        raise RuntimeError(
            f"[{name}] HTTP {r.status_code} for {r.request.method} {r.url}. Body: {body_short}"
        ) from e


def _post_with_retry(session: requests.Session, url: str, name: str, payload: dict, max_attempts: int = 7) -> requests.Response:
    """
    Ретраи для 429 и 5xx (и сетевых ошибок).
    Используем экспоненциальный backoff + небольшой jitter.
    """
    last_exc = None
    for attempt in range(1, max_attempts + 1):
        try:
            r = session.post(url, json=payload, timeout=60)

            # retry on 429 / 5xx
            if r.status_code == 429 or (500 <= r.status_code <= 599):
                retry_after = r.headers.get("Retry-After")
                if retry_after:
                    try:
                        delay = float(retry_after)
                    except Exception:
                        delay = 1.0
                else:
                    delay = min(10.0, 0.5 * (2 ** (attempt - 1))) + random.uniform(0.0, 0.25)

                if attempt == max_attempts:
                    _raise_for_status_with_body(r, name)

                time.sleep(delay)
                continue

            # hard fail on other 4xx
            if r.status_code >= 400:
                _raise_for_status_with_body(r, name)

            return r

        except (requests.Timeout, requests.ConnectionError) as e:
            last_exc = e
            delay = min(10.0, 0.5 * (2 ** (attempt - 1))) + random.uniform(0.0, 0.25)
            time.sleep(delay)

    raise RuntimeError(f"[{name}] POST failed after retries: {url}. Last error: {last_exc}")


class OzonClient:
    def __init__(self, client_id: str, api_key: str, name: str = "ozon"):
        if not client_id or not api_key:
            raise RuntimeError(f"[{name}] Нет Client-Id или Api-Key в .env")

        self.name = name
        self.client_id = str(client_id)
        self.api_key = api_key

        self.session = requests.Session()
        self.session.headers.update({
            "Client-Id": self.client_id,
            "Api-Key": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    def supply_order_list(self, payload: dict) -> dict:
        url = f"{BASE_URL}/v3/supply-order/list"
        r = _post_with_retry(self.session, url, self.name, payload)
        return r.json()

    def supply_order_get(self, order_ids: list[int]) -> dict:
        url = f"{BASE_URL}/v3/supply-order/get"
        r = _post_with_retry(self.session, url, self.name, {"order_ids": order_ids})
        # легкая пауза, чтобы не ловить per-second limit на длинных сериях батчей
        time.sleep(0.15)
        return r.json()

    def supply_order_bundle_items(self, bundle_id: str, limit: int = 100) -> list[dict]:
        """
        /v1/supply-order/bundle:
        limit должен быть строго (0, 100]
        """
        # clamp 1..100
        limit = min(max(int(limit), 1), 100)

        url = f"{BASE_URL}/v1/supply-order/bundle"
        items: list[dict] = []
        last_id = ""

        while True:
            payload = {"bundle_ids": [bundle_id], "limit": limit}
            if last_id:
                payload["last_id"] = last_id

            r = _post_with_retry(self.session, url, self.name, payload)
            data = r.json() or {}

            items.extend(data.get("items", []) or [])
            if not data.get("has_next"):
                break

            last_id = data.get("last_id") or ""
            if not last_id:
                break

            # мягко притормозим между страницами bundle
            time.sleep(0.05)

        return [{"offer_id": x.get("offer_id"), "quantity": x.get("quantity")} for x in items]


def iter_accounts(prefix: str = "OZON", max_accounts: int = 20):
    """
    Ожидаем переменные вида:
      OZON_CLIENT_ID_1, OZON_API_KEY_1
      OZON_CLIENT_ID_2, OZON_API_KEY_2
    ...
    """
    for i in range(1, max_accounts + 1):
        cid = os.getenv(f"{prefix}_CLIENT_ID_{i}")
        key = os.getenv(f"{prefix}_API_KEY_{i}")
        if not cid and not key:
            continue
        name = f"{prefix.lower()}_{i}"
        yield OzonClient(cid, key, name=name)
