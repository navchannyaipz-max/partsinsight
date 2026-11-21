"""Ранжування пропозицій і цінова аналітика (формули 2.8–2.13)."""
from __future__ import annotations
import math
from statistics import median

WEIGHTS = {"price": 0.45, "avail": 0.20, "delivery": 0.20, "fresh": 0.15}
TAU_D = 2.0      # днів
LAMBDA_F = 7.0   # днів
Z_LIMIT = 3.5
MIN_N_OUTLIER = 5


def unit_price(price: float, currency: str, rates: dict[str, float], pack: int = 1) -> float | None:
    """p* = p·k(c)/q — ціна за одиницю у базовій валюті (2.8)."""
    k = rates.get(currency)
    if k is None or price is None or price <= 0:
        return None
    return price * k / max(pack, 1)


def stats(prices: list[float]) -> dict | None:
    """min / медіана / max (2.12)."""
    if not prices:
        return None
    return {"n": len(prices), "min": min(prices), "median": median(prices), "max": max(prices)}


def modified_z(prices: list[float]) -> list[float | None]:
    """Модифікований Z = 0.6745·(p − pmed)/MAD (2.13). None, якщо MAD = 0 або n < 5."""
    if len(prices) < MIN_N_OUTLIER:
        return [None] * len(prices)
    med = median(prices)
    mad = median(abs(p - med) for p in prices)
    if mad == 0:
        return [None] * len(prices)
    return [0.6745 * (p - med) / mad for p in prices]


def deviation(p: float, pmed: float) -> float:
    return 100.0 * (p - pmed) / pmed if pmed else 0.0


def rank_offers(offers: list[dict]) -> list[dict]:
    """Додає до кожної пропозиції Up/Ua/Ud/Uf, score, coverage, D, Z, outlier (2.9–2.13).

    Очікує поля: unit_price, avail (1 / 0.5 / None), delivery_days, age_days.
    """
    unpriced = [o for o in offers if o.get("unit_price") is None]
    offers = [o for o in offers if o.get("unit_price") is not None]
    for o in unpriced:                                   # без ціни за одиницю в оцінку й медіану не входять
        o.update({"utilities": {}, "coverage": 0.0, "score": None, "dev": None, "z": None, "outlier": False})
    prices = [o["unit_price"] for o in offers]
    if not prices:
        return unpriced
    pmed = median(prices)
    zs = modified_z(prices)
    for o, z in zip(offers, zs):
        u = {"price": pmed / (pmed + o["unit_price"])}
        if o.get("avail") is not None:
            u["avail"] = o["avail"]
        if o.get("delivery_days") is not None:
            u["delivery"] = 1.0 / (1.0 + o["delivery_days"] / TAU_D)
        if o.get("age_days") is not None:
            u["fresh"] = math.exp(-o["age_days"] / LAMBDA_F)
        wsum = sum(WEIGHTS[k] for k in u)
        o["utilities"] = u
        o["coverage"] = wsum / sum(WEIGHTS.values())
        o["score"] = sum(WEIGHTS[k] * v for k, v in u.items()) / wsum
        o["dev"] = deviation(o["unit_price"], pmed)
        o["z"] = z
        o["outlier"] = z is not None and abs(z) > Z_LIMIT
    offers.sort(key=lambda o: (-o["score"], o["unit_price"]))
    return offers + unpriced
