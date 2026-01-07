import os
import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://api-seller.ozon.ru"

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
        url = f"{BASE_URL}/v1/supply-order/list"
        r = self.session.post(url, json=payload, timeout=60)
        r.raise_for_status()
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
