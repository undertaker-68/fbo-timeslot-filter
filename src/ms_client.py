import os
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
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    def get(self, path: str, params: dict | None = None) -> dict:
        url = f"{MS_BASE_URL}{path}"
        r = self.session.get(url, params=params, timeout=60)
        if r.status_code >= 400:
            raise RuntimeError(f"MS GET {url} -> {r.status_code}: {(r.text or '')[:2000]}")
        return r.json()

    def post(self, path: str, payload: dict) -> dict:
        url = f"{MS_BASE_URL}{path}"
        r = self.session.post(url, json=payload, timeout=60)
        if r.status_code >= 400:
            raise RuntimeError(f"MS POST {url} -> {r.status_code}: {(r.text or '')[:2000]}")
        return r.json()

    def find_assortment_by_article(self, article: str) -> dict | None:
        for a in variants_lat_cyr(article):
            data = self.get("/entity/assortment", params={"filter": f"article={a}", "limit": 1})
            rows = data.get("rows") or []
            if rows:
                return rows[0].get("meta")
        return None
