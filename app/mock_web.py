"""Локальна імітація «Інтернету» для демонстрації й тестів.

Це НЕ імпорт файлів: система надсилає справжні HTTP-запити на ці адреси під час кожного пошуку
і розбирає отримані HTML/JSON тим самим кодом, що й для зовнішніх сайтів. Демо-дані вигадані.
"""
from __future__ import annotations
import zlib
from flask import Blueprint, jsonify, request, Response
from markupsafe import escape

from .normalize import norm_mpn, norm_text

bp = Blueprint("mock", __name__, url_prefix="/mock")

# (назва, виробник, артикул як на сайті, базова ціна UAH)
CATALOG = [
    ("Масляний фільтр", "Bosch", "0 986 452 041", 285),
    ("Масляний фільтр", "MANN-FILTER", "W 719/30", 262),
    ("Масляний фільтр", "Knecht", "OC 47", 241),
    ("Повітряний фільтр", "Bosch", "F 026 400 238", 420),
    ("Повітряний фільтр", "MANN-FILTER", "C 25 114/1", 455),
    ("Салонний фільтр", "Bosch", "1 987 432 097", 310),
    ("Гальмівні колодки передні", "TRW", "GDB1550", 1180),
    ("Гальмівні колодки передні", "Brembo", "P 85 114", 1490),
    ("Гальмівні колодки передні", "Bosch", "0 986 AB1 234", 1260),
    ("Гальмівний диск", "Brembo", "09.A426.11", 1950),
    ("Свічка запалювання", "NGK", "BKR6E", 148),
    ("Свічка запалювання", "Denso", "K20PR-U", 139),
    ("Амортизатор передній", "Sachs", "315 873", 2380),
    ("Паливний фільтр", "Bosch", "F 026 402 065", 515),
    ("Ремінь ГРМ", "Gates", "5560XS", 690),
    ("Підшипник маточини", "SKF", "VKBA 3640", 1740),
]


# «Схожі, але інші» деталі: потрапляють лише до нечіткого магазину (shop-c) і слугують пасткою для хибних об'єднань.
DECOYS = [
    ("Масляний фільтр", "Bosch", "0 986 452 042", 290),
    ("Масляний фільтр", "MANN-FILTER", "W 719/45", 270),
    ("Масляний фільтр", "Knecht", "OC 471", 255),
    ("Повітряний фільтр", "Bosch", "F 026 400 239", 430),
    ("Гальмівні колодки передні", "TRW", "GDB1551", 1210),
    ("Свічка запалювання", "NGK", "BKR6EK", 155),
    ("Амортизатор передній", "Sachs", "315 874", 2410),
    ("Паливний фільтр", "Bosch", "F 026 402 066", 520),
]


def _h(*parts) -> int:
    return zlib.crc32("|".join(map(str, parts)).encode())


def _matches(q: str, title: str, brand: str, mpn: str) -> bool:
    nq = norm_mpn(q)
    if nq and any(c.isdigit() for c in nq) and nq in norm_mpn(mpn):
        return True
    words = norm_text(q).split()
    hay = norm_text(f"{title} {brand}")
    return bool(words) and not any(c.isdigit() for c in q) and all(w in hay for w in words)


def _price(shop: str, idx: int, base: int, spread: float) -> int:
    f = 1 + ((_h(shop, idx) % 2000) / 1000 - 1) * spread
    return round(base * f)


def _cyr(mpn: str, shop: str, idx: int) -> str:
    """Шум у даних: частина артикулів має кириличні літери-гомогліфи."""
    if _h(shop, idx, "c") % 3 == 0:
        return mpn.replace("B", "В").replace("K", "К").replace("C", "С").replace("O", "О")
    return mpn


@bp.get("/robots.txt")
def robots():
    return Response("User-agent: *\nAllow: /\n", mimetype="text/plain")


@bp.get("/shop-a/search")
def shop_a():
    q = request.args.get("q", "")
    cards = []
    for i, (t, b, m, base) in enumerate(CATALOG):
        if not _matches(q, t, b, m):
            continue
        stock = ["В наявності", "В наявності", "Під замовлення", "Немає в наявності"][_h("a", i, "s") % 4]
        days = ["Доставка: сьогодні", "Доставка: 1-2 дні", "Доставка: 3 дні"][_h("a", i, "d") % 3]
        cards.append(
            f'<article class="item"><a class="item-title" href="/mock/shop-a/p/{i}">{escape(t)} {escape(b)} {escape(m)}</a>'
            f'<span class="brand">{escape(b)}</span><span class="sku">{escape(_cyr(m, "a", i))}</span>'
            f'<span class="price">{_price("a", i, base, 0.08):,} грн</span>'.replace(",", " ")
            + f'<span class="stock">{stock}</span><span class="delivery">{days}</span></article>')
    return f'<!doctype html><meta charset="utf-8"><title>Магазин А</title><main>{"".join(cards)}</main>'


@bp.get("/shop-b/catalog")
def shop_b():
    q = request.args.get("query", "")
    rows = []
    for i, (t, b, m, base) in enumerate(CATALOG):
        if not _matches(q, t, b, m):
            continue
        qty = [12, 3, 0, 40][_h("b", i, "s") % 4]
        eta = ["завтра", "2 дн.", "5 дн."][_h("b", i, "d") % 3]
        mm = m.replace(" ", "-")
        rows.append(
            f'<tr><td class="name"><a href="/mock/shop-b/item/{i}">{escape(t)} {escape(b)}</a></td>'
            f'<td class="maker">{escape(b.upper())}</td><td class="code">{escape(_cyr(mm, "b", i))}</td>'
            f'<td class="cost">{_price("b", i, base, 0.12)}.00 UAH</td><td class="qty">{qty}</td>'
            f'<td class="eta">{eta}</td></tr>')
    return ('<!doctype html><meta charset="utf-8"><title>Магазин Б</title>'
            f'<table id="catalog"><tbody>{"".join(rows)}</tbody></table>')


@bp.get("/api/v1/parts")
def api_parts():
    q = request.args.get("search", "")
    items = []
    for i, (t, b, m, base) in enumerate(CATALOG):
        if not _matches(q, t, b, m):
            continue
        for s in range(3):
            price = _price("api", i * 10 + s, base, 0.10)
            cur = "UAH"
            if s == 1:
                cur, price = "EUR", round(base / 45 * 1.05, 2)
            cond = "used" if (i == 0 and s == 2) else "new"
            if i == 0 and s == 2:
                price = round(base * 0.45)
            if i == 2 and s == 0:
                price = base * 3          # «аномальна» ціна для демонстрації викидів
            items.append({
                "name": f"{t} {b} {m}", "article": m.replace(" ", ""), "brand": b,
                "price": {"amount": price, "currency": cur},
                "stock": [True, True, False, True][_h("api", i, s) % 4],
                "delivery_days": [1, 2, 4, 7][_h("api", i, s, "d") % 4],
                "condition": cond, "pack": 1,
                "link": f"/mock/api/v1/parts/{i}/{s}"})
    return jsonify({"data": {"items": items}})


@bp.post("/oauth/token")
def oauth_token():
    a = request.authorization
    if not a or a.username != "demo" or a.password != "demo-secret" \
            or request.form.get("grant_type") != "client_credentials":
        return jsonify(error="invalid_client"), 401
    return jsonify(access_token="tok-demo-123", token_type="Bearer", expires_in=3600)


@bp.get("/api/v2/parts")
def api_v2():
    if request.headers.get("Authorization") != "Bearer tok-demo-123":
        return jsonify(error="unauthorized"), 401
    q = request.args.get("q", "")
    items = []
    for i, (t, b, m, base) in enumerate(CATALOG):
        if _matches(q, t, b, m):
            items.append({"title": f"{b} {t} {m} NEU", "itemWebUrl": f"/mock/api/v2/item/{i}",
                          "price": {"value": f"{base / 45 * 0.97:.2f}", "currency": "EUR"},
                          "condition": "New" if _h("v2", i) % 5 else "Used"})
    return jsonify(itemSummaries=items)


@bp.get("/shop-c/fuzzy")
def shop_c():
    """Нечіткий пошук (як у багатьох магазинів): повертає і схожі номери, і товари з будь-яким зі слів запиту."""
    from .normalize import trigram_sim
    q = request.args.get("q", "")
    nq, words = norm_mpn(q), norm_text(q).split()
    cards = []
    for i, (t, b, m, base) in enumerate(CATALOG + DECOYS):
        hay = norm_text(f"{t} {b}")
        by_num = any(c.isdigit() for c in nq) and trigram_sim(nq, norm_mpn(m)) >= 0.3
        by_words = bool(words) and not any(c.isdigit() for c in q) and any(w in hay for w in words)
        if not (by_num or by_words):
            continue
        cards.append(
            f'<article class="item"><a class="item-title" href="/mock/shop-c/p/{i}">{escape(t)} {escape(b)} {escape(m)}</a>'
            f'<span class="brand">{escape(b)}</span><span class="sku">{escape(m)}</span>'
            f'<span class="price">{_price("c", i, base, 0.09)} грн</span>'
            f'<span class="stock">В наявності</span><span class="delivery">Доставка: 1-2 дні</span></article>')
    return f'<!doctype html><meta charset="utf-8"><title>Магазин В</title><main>{"".join(cards)}</main>'
