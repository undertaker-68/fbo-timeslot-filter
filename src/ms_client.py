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

        # кэш: article -> meta (или None если не найдено)
        self._article_cache: dict[str, dict | None] = {}

    def _request_with_retry(self, method: str, url: str, *, params=None, json=None, max_attempts: int = 8) -> requests.Response:
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

                # чуть-чуть троттлим даже на успехе (чтобы меньше ловить 429)
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

    def post(self, path: str, payload: dict) -> dict:
        url = f"{MS_BASE_URL}{path}"
        r = self._request_with_retry("POST", url, json=payload)
        return r.json()

    def find_customerorder_by_name(self, name: str) -> dict | None:
        # ищем ровно по имени
        data = self.get("/entity/customerorder", params={"filter": f"name={name}", "limit": 1})
        rows = data.get("rows") or []
        return rows[0] if rows else None

    def create_customerorder(self, payload: dict) -> dict:
        return self.post("/entity/customerorder", payload)

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
