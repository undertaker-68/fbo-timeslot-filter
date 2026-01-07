import os
import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://api-seller.ozon.ru"

def _raise_for_status_with_body(r: requests.Response, name: str):
    try:
        r.raise_for_status()
    except requests.HTTPError as e:
        body = (r.text or "").strip()
        # ограничим размер, чтобы лог не раздувался
        body_short = body[:2000]
        raise RuntimeError(
            f"[{name}] HTTP {r.status_code} for {r.request.method} {r.url}. Body: {body_short}"
        ) from e

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
        r = self.session.post(url, json=payload, timeout=60)
        _raise_for_status_with_body(r, self.name)
        return r.json()

    def supply_order_get(self, order_ids: list[int]) -> dict:
        url = f"{BASE_URL}/v3/supply-order/get"
        r = self.session.post(url, json={"order_ids": order_ids}, timeout=60)
        _raise_for_status_with_body(r, self.name)
        return r.json()

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
