"""Безкоштовний каталог аналогів: Auto Parts Cross-Reference API (RapidAPI, 300 запитів на день).

Відповідь: {"r": [{"p": {b, n, ...}, "a": [{"p": {b, n, ...}, "cf": 0-100, "vu": [...], "vd": [...]}]}]}.
Знайдені аналоги з cf >= MIN_CF записуються у cross_refs (verified=1) і далі працюють без нових запитів:
повторний пошук цього артикула в API не виконується (таблиця xref_lookups, повтор не раніше ніж за 30 днів).
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone

import requests

from . import db
from .normalize import norm_mpn

HOST = "auto-parts-cross-reference-api.p.rapidapi.com"
MIN_CF = 70
KEY_NAME = "RAPIDAPI_KEY"
RECHECK_DAYS = 30
MAX_ANALOGS = 12


class CrossApiError(Exception):
    pass


def fetch(number: str, key: str, timeout: int = 8) -> list[dict]:
    """Список аналогів [{mfr, mpn, cf, vu, vd, main_mfr, main_mpn}] або виняток CrossApiError."""
    try:
        r = requests.get(f"https://{HOST}/v1/crosses", params={"number": number, "min_confidence": MIN_CF},
                         headers={"X-RapidAPI-Key": key, "X-RapidAPI-Host": HOST}, timeout=timeout)
    except requests.RequestException as e:
        raise CrossApiError(f"Помилка мережі ({type(e).__name__})") from e
    if r.status_code in (401, 403):
        raise CrossApiError("Ключ RapidAPI відхилено: перевірте ключ і підписку на API (план Basic).")
    if r.status_code == 429:
        raise CrossApiError("Вичерпано добовий ліміт запитів (300).")
    if r.status_code >= 400:
        raise CrossApiError(f"HTTP {r.status_code}")
    try:
        data = r.json()
    except ValueError as e:
        raise CrossApiError("Відповідь не є JSON") from e
    out = []
    rows = data.get("r") if isinstance(data, dict) else None
    for res in rows if isinstance(rows, list) else []:
        if not isinstance(res, dict):
            continue
        p = res.get("p") if isinstance(res.get("p"), dict) else {}
        alts = res.get("a") if isinstance(res.get("a"), list) else []
        for a in alts:
            if not isinstance(a, dict):
                continue
            ap = a.get("p") if isinstance(a.get("p"), dict) else {}
            cf = a.get("cf")
            if isinstance(cf, str):
                try:
                    cf = float(cf)
                except ValueError:
                    cf = None
            if ap.get("n") and isinstance(cf, (int, float)) and cf >= MIN_CF:
                vu, vd = a.get("vu"), a.get("vd")
                out.append({"mfr": str(ap.get("b") or ""), "mpn": str(ap["n"]), "cf": cf,
                            "vu": len(vu) if isinstance(vu, list) else 0, "vd": len(vd) if isinstance(vd, list) else 0,
                            "main_mfr": str(p.get("b") or ""), "main_mpn": str(p.get("n") or number)})
    out.sort(key=lambda x: -x["cf"])
    return out[:MAX_ANALOGS]


def enrich(con, mpn: str, key: str, al: dict) -> int:
    """Додає аналоги артикула з API в cross_refs. Повертає кількість нових записів (0 — нічого/уже перевірялось)."""
    nm = norm_mpn(mpn)
    if not key or not nm or not any(c.isdigit() for c in nm):
        return 0
    row = con.execute("SELECT at FROM xref_lookups WHERE norm_mpn=?", (nm,)).fetchone()
    if row and datetime.fromisoformat(row["at"]) > datetime.now(timezone.utc) - timedelta(days=RECHECK_DAYS):
        return 0
    try:
        found = fetch(mpn, key)
    except CrossApiError:
        return 0                                   # збій не блокує пошук і не фіксується як «перевірено»
    from .archive import learn_brand
    added, now = 0, db.now_iso()
    for x in found:
        src = f"Cross-Reference API (довіра {x['cf']:.0f}%, підтверджень {x['vu']})"
        if con.execute("SELECT 1 FROM cross_refs WHERE mpn_a=? AND mpn_b=? AND mfr_b=?", (x["main_mpn"], x["mpn"], x["mfr"])).fetchone():
            continue
        con.execute("INSERT INTO cross_refs(mfr_a,mpn_a,mfr_b,mpn_b,ref_source,verified) VALUES(?,?,?,?,?,1)",
                    (x["main_mfr"], x["main_mpn"], x["mfr"], x["mpn"], src))
        for b in (x["main_mfr"], x["mfr"]):
            if b:
                learn_brand(con, b, al, now)
        added += 1
    con.execute("INSERT OR REPLACE INTO xref_lookups(norm_mpn, at, found) VALUES(?,?,?)", (nm, now, len(found)))
    con.commit()
    return added
