import os
import time
import random
import requests
from dotenv import load_dotenv

load_dotenv()

MS_BASE_URL = "https://api.moysklad.ru/api/remap/1.2"

# гомоглифы латиница<->кириллица (чтобы article находился при смешанных буквах)
LAT_TO_CYR = {
    "A": "А", "B": "В", "C": "С", "E": "Е", "H": "Н", "K": "К",
    "M": "М", "O": "О", "P": "Р", "T": "Т", "X": "Х", "Y": "У",
    "a": "а", "c": "с", "e": "е", "o": "о", "p": "р", "x": "х", "y": "у",
}
CYR_TO_LAT = {v: k for k, v in LAT_TO_CYR.items()}


def variants_lat_cyr(s: str) -> list[str]:
    s = (s or "").strip()
    if not s:
        return []
    to_cyr = "".join(LAT_TO_CYR.get(ch, ch) for ch in s)
    to_lat = "".join(CYR_TO_LAT.get(ch, ch) for ch in s)

    seen = set()
    out = []
    for v in (s, to_cyr, to_lat):
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


class MSClient:
    def __init__(self):
        token = os.getenv("MS_TOKEN")
        if not token:
            raise RuntimeError("Нет MS_TOKEN в .env")

        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json;charset=utf-8",
            "Accept": "application/json;charset=utf-8",
        })

        # кэш: article -> row (или None если не найдено)
        self._article_cache: dict[str, dict | None] = {}

    def _request_with_retry(
        self,
        method: str,
        url: str,
        *,
        params=None,
        json=None,
        max_attempts: int = 8
    ) -> requests.Response:
        last_exc = None
        for attempt in range(1, max_attempts + 1):
            try:
                r = self.session.request(method, url, params=params, json=json, timeout=60)

                # retry on 429 / 5xx
                if r.status_code == 429 or (500 <= r.status_code <= 599):
                    ra = r.headers.get("Retry-After")
                    if ra:
                        try:
                            delay = float(ra)
                        except Exception:
                            delay = 1.0
                    else:
                        delay = min(10.0, 0.6 * (2 ** (attempt - 1))) + random.uniform(0.0, 0.25)

                    if attempt == max_attempts:
                        raise RuntimeError(f"MS {method} {url} -> {r.status_code}: {(r.text or '')[:2000]}")

                    time.sleep(delay)
                    continue

                if r.status_code >= 400:
                    raise RuntimeError(f"MS {method} {url} -> {r.status_code}: {(r.text or '')[:2000]}")

                # лёгкий троттлинг даже на успехе
                time.sleep(0.03)
                return r

            except (requests.Timeout, requests.ConnectionError) as e:
                last_exc = e
                delay = min(10.0, 0.6 * (2 ** (attempt - 1))) + random.uniform(0.0, 0.25)
                time.sleep(delay)

        raise RuntimeError(f"MS {method} failed after retries: {url}. Last error: {last_exc}")

    def get(self, path: str, params: dict | None = None) -> dict:
        url = f"{MS_BASE_URL}{path}"
        r = self._request_with_retry("GET", url, params=params)
        return r.json()

    def post(self, path: str, payload) -> dict:
        url = f"{MS_BASE_URL}{path}"
        r = self._request_with_retry("POST", url, json=payload)
        return r.json()

    def put(self, path: str, payload: dict) -> dict:
        url = f"{MS_BASE_URL}{path}"
        r = self._request_with_retry("PUT", url, json=payload)
        return r.json()

    def delete(self, path: str) -> dict:
        url = f"{MS_BASE_URL}{path}"
        r = self._request_with_retry("DELETE", url)
        if r.text:
            try:
                return r.json()
            except Exception:
                return {"ok": True}
        return {"ok": True}

    # -------- CustomerOrder helpers --------

    def find_customerorder_by_name(self, name: str) -> dict | None:
        data = self.get("/entity/customerorder", params={"filter": f"name={name}", "limit": 1})
        rows = data.get("rows") or []
        return rows[0] if rows else None

    def find_customerorder_by_external_code(self, external_code: str) -> dict | None:
        """
        Ищем заказ по externalCode, возвращаем его ID.
        """
        data = self.get("/entity/customerorder", params={"filter": f"externalCode={external_code}", "limit": 1})
        rows = data.get("rows") or []
        if rows:
            return rows[0]
        return None

    def create_customerorder(self, payload: dict) -> dict:
        return self.post("/entity/customerorder", payload)

    def update_customerorder(self, order_id: str, payload: dict) -> dict:
        return self.put(f"/entity/customerorder/{order_id}", payload)

    def get_customerorder_positions(self, order_id: str, limit: int = 1000) -> list[dict]:
        data = self.get(f"/entity/customerorder/{order_id}/positions", params={"limit": limit})
        return data.get("rows") or []

    def delete_customerorder_position(self, order_id: str, position_id: str) -> None:
        self.delete(f"/entity/customerorder/{order_id}/positions/{position_id}")

    def replace_customerorder_positions(self, order_id: str, positions: list[dict]) -> None:
        """
        Перезапись состава:
          1) удалить все текущие позиции
          2) добавить новые позиции
        """
        current = self.get_customerorder_positions(order_id, limit=1000)
        for p in current:
            pid = p.get("id")
            if pid:
                self.delete_customerorder_position(order_id, pid)

        if not positions:
            return

        # В МС иногда принимается либо массив позиций, либо {"positions": [...]}
        try:
            self.post(f"/entity/customerorder/{order_id}/positions", positions)
            return
        except Exception:
            self.post(f"/entity/customerorder/{order_id}/positions", {"positions": positions})

    # -------- Move helpers --------

    def find_move_by_external_code(self, external_code: str) -> dict | None:
        data = self.get(
            "/entity/move",
            params={"filter": f"externalCode={external_code}", "limit": 2},
        )
        rows = data.get("rows") or []
        if not rows:
            return None
        if len(rows) > 1:
            raise RuntimeError(f"Multiple Move found for externalCode={external_code}")
        return rows[0]


    def create_move(self, payload: dict) -> dict:
        return self.post("/entity/move", payload)


    def update_move(self, move_id: str, payload: dict) -> dict:
        return self.put(f"/entity/move/{move_id}", payload)


    def replace_move_positions(self, move_id: str, positions: list[dict]) -> None:
        current = self.get(f"/entity/move/{move_id}/positions", params={"limit": 1000})
        for p in current.get("rows") or []:
            pid = p.get("id")
            if pid:
                self.delete(f"/entity/move/{move_id}/positions/{pid}")

        if not positions:
            return

        try:
            self.post(f"/entity/move/{move_id}/positions", positions)
        except Exception:
            self.post(f"/entity/move/{move_id}/positions", {"positions": positions})
    # -------- Demand helpers --------

    def find_demand_by_customerorder_href(self, customerorder_href: str) -> dict | None:
        """
        Ищем demand по привязке к CustomerOrder.
        В МС нельзя фильтровать /entity/demand по customerOrder, поэтому читаем сам заказ и смотрим поле demands.
        Возвращаем meta demand (или None).
        """
        customerorder_href = (customerorder_href or "").strip()
        if not customerorder_href:
            return None

        # достаём id заказа из href
        customerorder_id = customerorder_href.rstrip("/").split("/")[-1]

        co = self.get(f"/entity/customerorder/{customerorder_id}")
        demands = co.get("demands") or []

        if not demands:
            return None
        if len(demands) > 1:
            raise RuntimeError(f"Multiple Demand found for customerOrder={customerorder_href}")

        # demands[0] — это meta-объект отгрузки
        return demands[0]

    def create_demand(self, payload: dict) -> dict:
        return self.post("/entity/demand", payload)

    # -------- Assortment by article --------

    def find_assortment_by_article(self, article: str) -> dict | None:
        """
        Ищем в /entity/assortment по article (с учётом лат/кир гомоглифов).
        Возвращаем ПОЛНУЮ строку rows[0] (meta + salePrices и т.д.) или None.
        С кэшем, чтобы не бить МС сотни раз одним и тем же артикулом.
        """
        article = (article or "").strip()
        if not article:
            return None

        if article in self._article_cache:
            return self._article_cache[article]

        for a in variants_lat_cyr(article):
            if a in self._article_cache:
                row = self._article_cache[a]
                self._article_cache[article] = row
                return row

            data = self.get("/entity/assortment", params={"filter": f"article={a}", "limit": 1})
            rows = data.get("rows") or []
            row = rows[0] if rows else None

            self._article_cache[a] = row
            if row:
                self._article_cache[article] = row
                return row

        self._article_cache[article] = None
        return None
