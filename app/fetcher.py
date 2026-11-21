"""Живі запити до джерел в Інтернеті: HTTP-клієнт з повагою до robots.txt, затримками та обмеженнями.

Адаптери: HtmlAdapter (CSS-селектори) і JsonApiAdapter (шлях у JSON). Обидва керуються профілем,
що зберігається в БД і редагується в адмін-панелі — розбір сторінки не прописаний у коді.
"""
from __future__ import annotations
import ipaddress
import json
import os
import re
import socket
import threading
from collections import deque
import time
from urllib import robotparser
from urllib.parse import quote, urlparse

import requests
from bs4 import BeautifulSoup

from .normalize import norm_mpn

USER_AGENT = "PartsInsightBot/1.0 (academic research; respects robots.txt)"
TIMEOUT = 8
MAX_BYTES = 2_000_000
_robots: dict[str, tuple[float, robotparser.RobotFileParser | None]] = {}
_last_hit: dict[str, float] = {}
_lock = threading.Lock()


class FetchError(Exception):
    pass


def _is_private(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return True
    return False


def _robots_allowed(url: str, allow_local: bool) -> bool:
    p = urlparse(url)
    origin = f"{p.scheme}://{p.netloc}"
    with _lock:
        cached = _robots.get(origin)
    if not cached or time.time() - cached[0] > 3600:
        rp = robotparser.RobotFileParser()
        try:
            r = requests.get(origin + "/robots.txt", timeout=TIMEOUT, headers={"User-Agent": USER_AGENT})
            if r.status_code == 200:
                rp.parse(r.text.splitlines())
            else:
                rp = None  # robots.txt відсутній — обмежень немає
        except requests.RequestException:
            rp = None
        cached = (time.time(), rp)
        with _lock:
            _robots[origin] = cached
    rp = cached[1]
    return True if rp is None else rp.can_fetch(USER_AGENT, url)


def _throttle(host: str, delay_ms: int) -> None:
    if delay_ms <= 0:
        return
    with _lock:
        wait = _last_hit.get(host, 0) + delay_ms / 1000 - time.time()
        _last_hit[host] = time.time() + max(wait, 0)
    if wait > 0:
        time.sleep(wait)


def build_url(template: str, q: str, base_url: str, profile: dict) -> str:
    url = template.replace("{base}", base_url.rstrip("/")).replace("{q}", quote(q, safe=""))
    return url


def http_get(url: str, delay_ms: int, allow_local: bool, headers: dict | None = None, robots: bool = True) -> requests.Response:
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise FetchError("Недопустима адреса джерела")
    if not allow_local and _is_private(p.hostname):
        raise FetchError("Заборонено звернення до внутрішніх адрес")
    if robots and not _robots_allowed(url, allow_local):
        raise FetchError("Заборонено robots.txt")
    _throttle(p.netloc, delay_ms)
    h = {"User-Agent": USER_AGENT, "Accept-Language": "uk,en;q=0.7"}
    h.update(headers or {})
    try:
        r = requests.get(url, headers=h, timeout=TIMEOUT, stream=True, allow_redirects=True)
        body = r.raw.read(MAX_BYTES + 1, decode_content=True)
    except requests.RequestException as e:
        raise FetchError(f"Помилка мережі: {type(e).__name__}") from e
    if len(body) > MAX_BYTES:
        raise FetchError("Відповідь завелика")
    if r.status_code >= 400:
        raise FetchError(f"HTTP {r.status_code}")
    r._content = body
    return r


# ---------- розбір значень ----------
_NUM = re.compile(r"\d[\d\s ]*(?:[.,]\d+)?")
_IN_STOCK = ("в наявності", "in stock", "наявн", "available")
_ON_ORDER = ("під замовлення", "на замовлення", "order", "очікуєть", "pre-order")
_NO_STOCK = ("немає", "нема", "out of stock", "відсутн", "sold out")


def parse_price(text) -> float | None:
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text) if text > 0 else None
    m = _NUM.search(str(text))
    if not m:
        return None
    s = re.sub(r"[\s ]", "", m.group(0)).replace(",", ".")
    try:
        v = float(s)
    except ValueError:
        return None
    return v if v > 0 else None


def parse_avail(v) -> float | None:
    """1 — є, 0.5 — під замовлення, 0 — немає, None — невідомо."""
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return 1.0 if v > 0 else 0.0
    t = str(v).strip().lower()
    for k in _NO_STOCK:
        if k in t:
            return 0.0
    for k in _ON_ORDER:
        if k in t:
            return 0.5
    for k in _IN_STOCK:
        if k in t:
            return 1.0
    m = re.search(r"\d+", t)
    if m:
        return 1.0 if int(m.group()) > 0 else 0.0
    return None


def parse_days(v) -> float | None:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    t = str(v).lower()
    if "сьогодні" in t or "today" in t:
        return 0.0
    if "завтра" in t or "tomorrow" in t:
        return 1.0
    nums = [int(x) for x in re.findall(r"\d+", t)]
    return float(min(nums)) if nums else None


def _select(node, spec: str):
    sel, _, attr = spec.partition("@")
    el = node.select_one(sel) if sel else node
    if el is None:
        return None
    return el.get(attr) if attr else el.get_text(" ", strip=True)


def _dig(obj, path: str):
    for part in path.split("."):
        if isinstance(obj, dict):
            obj = obj.get(part)
        elif isinstance(obj, list) and part.isdigit() and int(part) < len(obj):
            obj = obj[int(part)]
        else:
            return None
    return obj


def parse_html(html: str, profile: dict, page_url: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    f = profile["fields"]
    out = []
    for node in soup.select(profile["item"]):
        raw = {k: _select(node, f[k]) for k in f}
        if raw.get("url"):
            raw["url"] = requests.compat.urljoin(page_url, raw["url"])
        out.append(raw)
    return out


def parse_json(text: str, profile: dict) -> list[dict]:
    try:
        data = json.loads(text)
    except ValueError as e:
        raise FetchError("Невалідний JSON") from e
    items = _dig(data, profile.get("items_path", "")) if profile.get("items_path") else data
    if not isinstance(items, list):
        raise FetchError("items_path не вказує на список")
    f = profile["fields"]
    return [{k: _dig(it, f[k]) for k in f} for it in items if isinstance(it, dict)]


# ---------- автоматичний розбір сторінки магазину (без CSS-селекторів) ----------
_PRICE_RE = re.compile(r"(?<![\d.,])(\d{1,3}(?:[\s\u00a0]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)\s*(грн\.?|₴|uah|usd|\$|eur|€)", re.I)
_SKU_RE = re.compile(r"(?:артикул|арт\.?|код\s*товару|код|sku|part\s*(?:no|number)|oem)\s*[:№#\-]?\s*([A-Za-z0-9][A-Za-z0-9\-./]{3,})", re.I)
_CUR = {"грн": "UAH", "грн.": "UAH", "₴": "UAH", "uah": "UAH", "usd": "USD", "$": "USD", "eur": "EUR", "€": "EUR"}


def _ld_nodes(soup):
    for sc in soup.find_all("script", type=lambda t: t and "ld+json" in t):
        try:
            data = json.loads(sc.string or sc.get_text() or "null")
        except ValueError:
            continue
        queue = deque([data])
        while queue:                      # обхід у порядку появи в документі
            o = queue.popleft()
            if isinstance(o, list):
                queue.extend(o)
            elif isinstance(o, dict):
                yield o
                for k in ("@graph", "itemListElement", "item", "mainEntity"):
                    if k in o:
                        queue.append(o[k])


def _is_type(o, name):
    t = o.get("@type")
    return name in (t if isinstance(t, list) else [t])


def _offer(o):
    off = o.get("offers")
    if isinstance(off, list):
        off = off[0] if off else None
    if not isinstance(off, dict):
        return None, None, None
    price = off.get("price") or off.get("lowPrice")
    av = str(off.get("availability") or "").rsplit("/", 1)[-1]
    av = {"InStock": "в наявності", "OutOfStock": "немає", "PreOrder": "під замовлення", "SoldOut": "немає"}.get(av, av or None)
    return price, off.get("priceCurrency"), av


def _from_jsonld(soup, page_url):
    out = []
    for o in _ld_nodes(soup):
        if not _is_type(o, "Product"):
            continue
        price, cur, av = _offer(o)
        b = o.get("brand")
        out.append({"title": o.get("name"), "url": requests.compat.urljoin(page_url, o["url"]) if o.get("url") else None,
                    "mpn": o.get("mpn") or o.get("sku"), "brand": (b.get("name") if isinstance(b, dict) else b),
                    "price": price, "currency": cur, "availability": av,
                    "image": (lambda im: (im[0] if isinstance(im, list) and im else im))(o.get("image"))})
    return [x for x in out if x["title"] and x["price"]]


def _from_microdata(soup, page_url):
    out = []
    for n in soup.select('[itemtype*="schema.org/Product"]'):
        def ip(name):
            e = n.select_one(f'[itemprop="{name}"]')
            if e is None:
                return None
            return e.get("content") or e.get("href") or e.get_text(" ", strip=True)
        item = {"title": ip("name"), "url": ip("url"), "mpn": ip("mpn") or ip("sku"), "brand": ip("brand"),
                "price": ip("price"), "currency": ip("priceCurrency"), "availability": ip("availability"),
                "image": ip("image")}
        if item["url"]:
            item["url"] = requests.compat.urljoin(page_url, item["url"])
        if item["title"] and item["price"]:
            out.append(item)
    return out


def _prices(el):
    """Ціни в блоці: перевіряємо кожен текстовий фрагмент окремо, щоб артикул не «склеювався» з ціною."""
    out = []
    for seg in el.get_text("|", strip=True).split("|"):
        m = _PRICE_RE.search(seg)
        if m:
            out.append(m)
    return out


def _from_cards(soup, page_url):
    """Евристика: однотипні блоки з посиланням і однією ціною (>=2, або єдиний блок на сторінці)."""
    for bad in soup(["script", "style", "noscript", "header", "footer", "nav"]):
        bad.decompose()
    blocks = ["div", "li", "article", "tr", "section"]

    def is_card(el):
        return 20 < len(el.get_text(" ", strip=True)) < 900 and len(_prices(el)) == 1 and el.find("a", href=True)

    groups: dict[tuple, list] = {}
    for el in soup.find_all(blocks):
        if not is_card(el) or any(is_card(c) for c in el.find_all(blocks)):
            continue                      # беремо найглибший блок
        sig = (el.name, tuple(sorted(el.get("class") or [])), el.parent.name if el.parent else "")
        groups.setdefault(sig, []).append(el)
    if not groups:
        return []
    best = max(groups.values(), key=len)
    if len(best) < 2 and sum(len(g) for g in groups.values()) != 1:
        return []                         # один блок беремо лише коли він єдиний кандидат на сторінці
    out = []
    for el in best:
        txt = el.get_text(" ", strip=True)
        m = _prices(el)[0]
        anchors = [a for a in el.find_all("a", href=True) if a.get_text(strip=True)]
        if not anchors:
            continue
        a = max(anchors, key=lambda x: len(x.get_text(strip=True)))
        sku = _SKU_RE.search(txt)
        im = el.find("img")
        img = (im.get("src") or im.get("data-src") or im.get("data-lazy-src")) if im else None
        out.append({"title": a.get_text(" ", strip=True), "url": requests.compat.urljoin(page_url, a["href"]),
                    "image": requests.compat.urljoin(page_url, img) if img and not img.startswith("data:") else None,
                    "mpn": sku.group(1) if sku else None, "brand": None,
                    "price": m.group(1), "currency": _CUR.get(m.group(2).lower()), "availability": txt})
    return out


def parse_auto(html: str, page_url: str) -> list[dict]:
    """JSON-LD → microdata → евристика карток. Повертає перший непорожній результат."""
    soup = BeautifulSoup(html, "lxml")
    items = _from_jsonld(soup, page_url) or _from_microdata(soup, page_url)
    if not items:
        items = _from_cards(soup, page_url)
    for it in items:
        im = it.get("image")
        if isinstance(im, dict):
            im = im.get("url")
        it["image"] = requests.compat.urljoin(page_url, im) if isinstance(im, str) and im and not im.startswith("data:") else None
    return items


_tokens: dict[tuple, tuple[str, float]] = {}


SECRETS: dict[str, str] = {}   # ключі, введені у веб-інтерфейсі (зберігаються в БД, таблиця settings)


def set_secrets(d: dict[str, str]) -> None:
    SECRETS.clear()
    SECRETS.update({k: v for k, v in d.items() if v})


def _clean(v: str) -> str:
    return (v or "").strip().strip("\"'").strip()


def resolve(v):
    """Секрети: "$ENV:NAME" — спершу значення з веб-налаштувань, потім змінна середовища."""
    if isinstance(v, str) and v.startswith("$ENV:"):
        name = v[5:]
        return _clean(SECRETS.get(name) or os.environ.get(name, ""))
    return v


def _guard(url: str, allow_local: bool) -> None:
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise FetchError("Недопустима адреса")
    if not allow_local and _is_private(p.hostname):
        raise FetchError("Заборонено звернення до внутрішніх адрес")


def oauth_token(auth: dict, base_url: str, allow_local: bool) -> str:
    """OAuth2 client-credentials із кешем токена до завершення терміну дії."""
    cid, sec = resolve(auth.get("client_id")), resolve(auth.get("client_secret"))
    if not cid or not sec:
        raise FetchError("Не задано ключі API (client_id / client_secret)")
    url = auth["token_url"].replace("{base}", base_url.rstrip("/"))
    key = (url, cid, sec)
    cached = _tokens.get(key)
    if cached and cached[1] > time.time() + 30:
        return cached[0]
    _guard(url, allow_local)
    try:
        r = requests.post(url, data={"grant_type": "client_credentials", "scope": auth.get("scope", "")},
                          auth=(cid, sec), headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
        j = r.json()
    except (requests.RequestException, ValueError) as e:
        raise FetchError(f"OAuth: помилка запиту ({type(e).__name__})") from e
    if r.status_code >= 400 or "access_token" not in j:
        why = ""
        if isinstance(j, dict):
            why = " · ".join(str(j[k]) for k in ("error", "error_description") if j.get(k))
        hint = ""
        if cid.upper().startswith("PRD-") and "-PRD-" in sec.upper():
            hint = " Схоже, Client ID і Secret поміняно місцями: App ID має вигляд «назва-…-PRD-…», Cert ID починається з «PRD-»."
        elif "-SBX-" in cid.upper() and "sandbox" not in url:
            hint = " Це Sandbox-ключ, а потрібен набір Production."
        elif "-PRD-" not in cid.upper() and "ebay.com" in url:
            hint = " Client ID eBay Production має містити «-PRD-»."
        raise FetchError(f"OAuth: відмовлено (HTTP {r.status_code}){': ' + why if why else ''} — перевірте ключі.{hint}")
    _tokens[key] = (j["access_token"], time.time() + int(j.get("expires_in", 3600)))
    return j["access_token"]


def mpn_from_title(title: str | None, q: str) -> str | None:
    """Для майданчиків без поля «артикул»: у назві шукаємо фрагмент (до 4 слів), що починається з нормалізованого запиту.

    Знайдений фрагмент добудовується наступними словами з цифрами, тому за неповного запиту
    («0986452») повертається повний артикул («0 986 452 041»). Для запитів без цифр (опис словами)
    артикул не вгадується: запис без артикула відкидається як непридатний.
    """
    nq = norm_mpn(q)
    if not nq or not title or not any(c.isdigit() for c in nq):
        return None
    toks = title.split()
    for i in range(len(toks)):
        acc = ""
        for j in range(i, min(i + 4, len(toks))):
            acc += norm_mpn(toks[j])
            if acc.startswith(nq):
                k = j
                while k + 1 < min(i + 4, len(toks)) and any(c.isdigit() for c in toks[k + 1]):
                    k += 1
                return " ".join(toks[i:k + 1])
            if not nq.startswith(acc):
                break
    return None


def fetch_source_ex(source: dict, q: str, base_url: str) -> tuple[list[dict], dict]:
    """Живий запит до джерела: (сирі записи, діагностика)."""
    profile = json.loads(source["profile"] or "{}")
    url = build_url(source["search_url"], q, base_url, profile)
    key = str(resolve(profile.get("api_key", "")) or "")
    shown_url = url.replace("{api_key}", "***")
    if "{api_key}" in url:
        if not key:
            raise FetchError("Не задано ключ API (api_key)")
        url = url.replace("{api_key}", quote(key, safe=""))
    allow_local = bool(source["allow_local"])
    headers = {k: str(resolve(v)).replace("{api_key}", str(resolve(profile.get("api_key", ""))))
               for k, v in (profile.get("headers") or {}).items()}
    if profile.get("auth"):
        tok = oauth_token(profile["auth"], base_url, allow_local)
        headers = {k: v.replace("{token}", tok) for k, v in headers.items()}
        headers.setdefault("Authorization", f"Bearer {tok}")
    official_api = source["kind"] == "json" and bool(profile.get("auth") or profile.get("api_key"))
    r = http_get(url, source["delay_ms"], allow_local, headers, robots=not official_api)
    meta = {"url": shown_url, "status": r.status_code, "bytes": len(r.content), "title": ""}
    if source["kind"] == "json":
        items = parse_json(r.text, profile)
    else:
        r.encoding = r.encoding if r.encoding and r.encoding.lower() != "iso-8859-1" else (r.apparent_encoding or "utf-8")
        items = parse_auto(r.text, url) if profile.get("auto") else parse_html(r.text, profile, url)
        t = BeautifulSoup(r.text, "lxml").title
        meta["title"] = t.get_text(strip=True) if t else ""
    if profile.get("mpn_from_title") or profile.get("auto"):
        for it in items:
            if not it.get("mpn"):
                it["mpn"] = mpn_from_title(it.get("title"), q)
    for it in items:
        it.setdefault("currency", None)
        if not it.get("currency") and profile.get("currency"):
            it["currency"] = profile["currency"]
    meta["items"] = len(items)
    return items, meta


def fetch_source(source: dict, q: str, base_url: str) -> list[dict]:
    return fetch_source_ex(source, q, base_url)[0]
