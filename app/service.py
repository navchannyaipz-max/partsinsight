"""Оркестрація пошуку: живі запити до джерел → нормалізація → рівні відповідності → аналітика."""
from __future__ import annotations
import hashlib
import json
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from . import analytics, archive, crossapi, db
from .fetcher import FetchError, fetch_source, parse_avail, parse_days, parse_price, resolve
from .matching import CANDIDATE, CROSS, IDENTICAL, LEVEL_ORDER, classify, search_score
from .normalize import brand_key, norm_mpn
from .partmeta import category_of, guess_mpn

RATES = {"UAH": 1.0, "USD": 41.5, "EUR": 45.0}
CUR_ALIAS = {"грн": "UAH", "₴": "UAH", "UAH": "UAH", "$": "USD", "USD": "USD", "€": "EUR", "EUR": "EUR"}
CACHE_TTL = 900  # секунд


def _cur(raw, default):
    if not raw:
        return default
    return CUR_ALIAS.get(str(raw).strip().upper(), CUR_ALIAS.get(str(raw).strip(), None))


def _img(raw) -> str:
    v = raw.get("image")
    if isinstance(v, list):
        v = v[0] if v else ""
    if isinstance(v, dict):
        v = v.get("url") or v.get("contentUrl") or ""
    v = str(v or "").strip()
    return v if v.startswith(("http://", "https://")) and len(v) < 1000 else ""


def _cond(raw) -> str:
    t = str(raw or "").lower()
    return "used" if any(k in t for k in ("used", "б/в", "бу", "refurb")) else "new"


NOW = None          # лише для відтворення експерименту: «поточний» час, зафіксований у знімку збору


def _now() -> datetime:
    return NOW or datetime.now(timezone.utc)


def _age_days(iso: str) -> float:
    dt = datetime.fromisoformat(iso)
    return max((_now() - dt).total_seconds() / 86400, 0.0)


def aliases(con) -> dict[str, str]:
    return {r["alias"]: r["canonical"] for r in con.execute("SELECT alias, canonical FROM aliases")}


def analog_map(con, q: str, al: dict[str, str]):
    """Аналоги з перевіреного довідника: {(виробник, артикул): ref} та рядки для розширення запиту."""
    nq = norm_mpn(q)
    amap, extra = {}, []
    if not nq:
        return amap, extra
    for r in con.execute("SELECT * FROM cross_refs WHERE verified=1"):
        a, b = (r["mfr_a"], r["mpn_a"]), (r["mfr_b"], r["mpn_b"])
        for src, dst in ((a, b), (b, a)):
            if norm_mpn(src[1]) == nq and norm_mpn(dst[1]) != nq:
                amap[(brand_key(dst[0], al), norm_mpn(dst[1]))] = {"ref_source": r["ref_source"], "via": src[1]}
                extra.append(dst[1])
    return amap, list(dict.fromkeys(extra))[:3]


def current_app_log(where: str) -> None:
    import logging
    logging.getLogger("partsinsight").exception("Збій у %s (проігноровано)", where)


def _s(v) -> str:
    """Будь-яке значення з джерела -> безпечний рядок."""
    if v is None or isinstance(v, bool):
        return ""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, (list, tuple)):
        return _s(v[0]) if v else ""
    if isinstance(v, dict):
        for k in ("name", "title", "value", "text", "url", "src"):
            if k in v:
                return _s(v[k])
    return ""


_PACK_NUM = re.compile(r"(?:(?:комплект|к-т|набір|набор|set|kit|упаковка|уп\.?)\s*(?:з|из|of|x|×)?\s*(\d{1,3})\b)|(?:\b(\d{1,3})\s*(?:шт\.?|штук|pcs?|pieces)(?![A-Za-zА-Яа-я]))|(?:\bx\s?(\d{1,3})\b)", re.I)
_PAIR = re.compile(r"\b(пара|pair)\b", re.I)


def _int_pack(v):
    """Підтверджене додатне ціле з поля джерела: 2, «2», «2.0»; дробове, нульове, від’ємне чи нечислове → None."""
    if v is None or v == "" or isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v if v >= 1 else None
    try:
        f = float(str(v).strip().replace(",", "."))
    except ValueError:
        return None
    if f != f or f in (float("inf"), float("-inf")) or f != int(f) or f < 1:
        return None
    return int(f)


def _title_packs(title: str) -> set[int]:
    found = set()
    if _PAIR.search(title or ""):
        found.add(2)
    for m in _PACK_NUM.finditer(title or ""):
        g = next((x for x in m.groups() if x), None)
        if g and int(g) >= 1:
            found.add(int(g))
    return found


def parse_pack(raw: dict, title: str, default=None) -> tuple[int | None, str]:
    """(кількість одиниць | None = «Фасування невідоме», примітка).

    Приймається лише підтверджене додатне ціле з поля джерела або однозначної назви («2 шт.», «пара»).
    Відсутнє, дробове, нульове чи суперечливе значення → None. Припущення «1 шт.» не робиться; єдиний виняток —
    явний параметр pack_default у профілі джерела, який задає адміністратор.
    """
    from_title = _title_packs(title)
    v = raw.get("pack")
    if v not in (None, ""):
        n = _int_pack(v)
        if n is None:
            return None, f"значення «{v}» не є додатним цілим"
        if from_title and n not in from_title:
            return None, f"суперечність: поле {n}, назва {sorted(from_title)}"
        return n, "джерело"
    if len(from_title) == 1:
        return next(iter(from_title)), "з назви"
    if len(from_title) > 1:
        return None, f"у назві різні кількості {sorted(from_title)}"
    if default:
        n = _int_pack(default)
        if n:
            return n, "задано в профілі джерела"
    return None, "кількість не вказана"


def _profile(src) -> dict:
    try:
        p = src["profile"]
        return json.loads(p or "{}") if isinstance(p, str) else (p or {})
    except (TypeError, ValueError, KeyError):
        return {}


def _normalize(raw: dict, src, al, fetched_at: str) -> dict | None:
    if not isinstance(raw, dict):
        return None
    raw = dict(raw)
    for k in ("mpn", "brand", "title", "url", "currency", "condition"):
        if k in raw:
            raw[k] = _s(raw[k])
    mpn_raw = _s(raw.get("mpn"))
    nm = norm_mpn(mpn_raw)
    price = parse_price(raw.get("price"))
    cur_raw = raw.get("currency")
    cur = _cur(cur_raw, src["currency"])
    if cur is None and cur_raw:
        cur = str(cur_raw).strip().upper()[:8]          # невідомий код валюти показуємо як є, без розрахунків
    if not nm or price is None:
        return None
    title = _s(raw.get("title"))
    pack, pack_note = parse_pack(raw, title, _profile(src).get("pack_default"))
    up, reason = None, ""
    if not cur:
        reason = "Валюта не вказана: ціну за одиницю не розраховано"
    elif cur not in RATES:
        reason = f"Немає курсу для {cur}: ціну за одиницю не розраховано"
    elif pack is None:
        reason = f"Фасування невідоме ({pack_note}): ціну за одиницю не розраховано"
    else:
        up = analytics.unit_price(price, cur, RATES, pack)
        if up is None:
            reason = "Ціну за одиницю не вдалося розрахувати"
    return {
        "source_id": src["id"], "source": src["name"], "title": title,
        "brand": _s(raw.get("brand")), "brand_key": brand_key(_s(raw.get("brand")), al),
        "mpn_raw": mpn_raw, "norm_mpn": nm, "price": price, "currency": cur or "", "pack": pack, "pack_note": pack_note,
        "unit_price": up, "unit_reason": reason, "avail": parse_avail(raw.get("availability")),
        "delivery_days": parse_days(raw.get("delivery")), "url": _s(raw.get("url")),
        "condition": _cond(raw.get("condition")), "fetched_at": fetched_at, "image": _img(raw),
        "age_days": _age_days(fetched_at), "demo": str(src["name"]).startswith("Демо"),
        "shipping": _ship(raw, src, cur),
        "oid": hashlib.sha1(f"{src['id']}|{_s(raw.get('url'))}|{title}|{price}".encode()).hexdigest()[:10],
    }


def _num0(v):
    """Число з нулем включно (parse_price нуль відкидає)."""
    if v is None or v == "" or isinstance(v, bool):
        return None
    try:
        return float(str(v).replace(",", ".").replace(" ", "").replace("\xa0", ""))
    except ValueError:
        m = re.search(r"\d+(?:[.,]\d+)?", str(v))
        return float(m.group(0).replace(",", ".")) if m else None


def _ship(raw: dict, src, cur: str) -> dict | None:
    """R2: вартість доставки — з поля пропозиції або з профілю джерела. None = невідомо («Доставка уточнюється»).

    Нуль означає безкоштовну доставку лише за явного підтвердження джерела (shipping.free_confirmed у профілі
    або поле shipping_free у відповіді); інакше нуль вважається невідомим значенням.
    """
    try:
        prof = json.loads(src["profile"] or "{}") if isinstance(src["profile"], str) else (src["profile"] or {})
    except (TypeError, ValueError, KeyError):
        prof = {}
    cfg = prof.get("shipping") if isinstance(prof.get("shipping"), dict) else {}
    cost = _num0(raw.get("shipping"))
    scur = _cur(raw.get("shipping_currency"), None) if raw.get("shipping_currency") else None
    if cost is None:
        cost = _num0(cfg.get("cost"))
        scur = _cur(cfg.get("currency"), None) if cfg.get("currency") else scur
    if cost is None or cost < 0:
        return None
    if cost == 0 and not (cfg.get("free_confirmed") or raw.get("shipping_free") in (True, 1, "1", "true", "так")):
        return None
    return {"cost": cost, "currency": scur or cur or "UAH"}


def split_query(q: str, al: dict) -> tuple[str, str | None]:
    """«Ford DG9Z-2200-N» -> («DG9Z-2200-N», FORD): артикул окремо від слів-пояснень і бренду."""
    from .vehicles import VEHICLES
    toks = [t for t in (x.strip(",;:()[]«»\"'") for x in q.split()) if t]
    if len(toks) < 2:
        return q, None
    known = set(al.values())
    makes = {m.upper() for m in VEHICLES}
    brand = None
    for t in toks:
        k = brand_key(t, al)
        if k in known or t.upper() in makes:
            brand = k if k in known else t.upper()
            break
    mp = [t for t in toks if any(c.isdigit() for c in t) and len(norm_mpn(t)) >= 4]
    if len(mp) == 1:
        short = [t for t in toks if t != mp[0] and t.isalpha() and len(t) <= 3 and t.upper() != brand]
        return (q if short else mp[0]), brand
    if len(mp) == 0 and brand:                  # «колодки bosch»: бренд окремо, решта — слова
        rest = [t for t in toks if brand_key(t, al) != brand and t.upper() != brand]
        return (" ".join(rest) or q), brand
    return q, None


def _totals(offs: list[dict]) -> None:
    """R2: підсумок = ціна упаковки + відома доставка (у гривнях); найнижчий підсумок лише серед відомих."""
    for o in offs:
        sh, k = o.get("shipping"), RATES.get(o["currency"])
        o["total_uah"] = None
        if sh and k is not None and RATES.get(sh["currency"]) is not None:
            o["total_uah"] = o["price"] * k + sh["cost"] * RATES[sh["currency"]]
        o["lowest_total"] = False
    known = [o["total_uah"] for o in offs if o["total_uah"] is not None]
    if known:
        for o in offs:
            o["lowest_total"] = o["total_uah"] is not None and abs(o["total_uah"] - min(known)) < 0.005


def _raw_range(offs: list[dict]) -> dict:
    """Початкові ціни (у валюті продавця) для груп без розрахунку за одиницю."""
    cur = {o["currency"] for o in offs}
    ps = [o["price"] for o in offs]
    return {"min": min(ps), "max": max(ps), "currency": cur.pop() if len(cur) == 1 else "різні валюти",
            "reasons": sorted({o["unit_reason"] for o in offs if o["unit_reason"]})}


def gid(g_key: tuple) -> str:
    return "~".join(str(x) for x in g_key)


def plan_queries(con, q: str) -> dict:
    """Спільна для застосунку й експерименту E1 логіка: що саме запитувати в джерел (початковий запит + аналоги)."""
    q = q.strip()
    al = aliases(con)
    mq, qbrand = split_query(q, al)
    amap, extra = analog_map(con, mq, al)
    return {"q": q, "mq": mq, "qbrand": qbrand, "amap": amap, "extra": extra, "queries": [q] + extra, "aliases": al}


def search(con, q: str, base_url: str, manufacturer: str | None = None, force: bool = False, replay=None) -> dict:
    t0 = time.time()
    q = q.strip()
    al = aliases(con)
    mkey = brand_key(manufacturer, al) if manufacturer else None
    mq, qbrand = split_query(q, al)             # mq — артикул для зіставлення; q — повний текст для живого пошуку
    key = resolve("$ENV:" + crossapi.KEY_NAME)
    if key:                                   # безкоштовний каталог аналогів: одноразово на артикул, далі з бази
        try:
            crossapi.enrich(con, mq, key, al)
        except Exception:
            current_app_log("crossapi.enrich")
    plan = plan_queries(con, q)
    amap, extra, queries = plan["amap"], plan["extra"], plan["queries"]
    sources = [dict(r) for r in con.execute("SELECT * FROM sources WHERE enabled=1 ORDER BY id")]

    jobs, cached = [], {}
    for s in sources:
        for qq in queries:
            row = None if force else con.execute(
                "SELECT fetched_at, items FROM cache WHERE source_id=? AND qkey=?", (s["id"], qq.lower())).fetchone()
            if row and _age_seconds(row["fetched_at"]) < CACHE_TTL:
                cached[(s["id"], qq)] = (json.loads(row["items"]), row["fetched_at"])
            else:
                jobs.append((s, qq))

    def run(job):
        s, qq = job
        t = time.time()
        if replay is not None:                             # відтворення збереженого знімка: без мережі, помилки не ковтаємо
            items, at = replay(s["name"], qq)
            return job, items, None, 0, at
        try:
            return job, fetch_source(s, qq, base_url), None, int((time.time() - t) * 1000), None
        except (FetchError, KeyError, ValueError) as e:
            return job, None, str(e) or type(e).__name__, int((time.time() - t) * 1000), None
        except Exception as e:  # парсинг чужої верстки не має валити весь пошук
            return job, None, f"Помилка розбору: {type(e).__name__}", int((time.time() - t) * 1000), None

    results = {}
    if jobs:
        with ThreadPoolExecutor(max_workers=min(8, len(jobs))) as ex:
            for job, items, err, ms, at in ex.map(run, jobs):
                s, qq = job
                now = at or db.now_iso()
                if items is not None:                      # F4: лише вдале оновлення змінює час отримання
                    con.execute("INSERT OR REPLACE INTO cache VALUES(?,?,?,?)",
                                (s["id"], qq.lower(), now, json.dumps(items, ensure_ascii=False)))
                results[(s["id"], qq)] = (items, now, err, ms)
    stale = {}                                             # невдале оновлення: показуємо збережене з початковим часом
    for (sid, qq), (items, _now, err, _ms) in results.items():
        if err:
            row = con.execute("SELECT fetched_at, items FROM cache WHERE source_id=? AND qkey=?", (sid, qq.lower())).fetchone()
            if row:
                stale[(sid, qq)] = (json.loads(row["items"]), row["fetched_at"])
    status = {}
    for s in sources:
        n_ok = n_err = n_items = 0
        err_msg, ms_tot, used_cache, fetched, used_stale, stale_at = "", 0, False, None, False, None
        for qq in queries:
            k = (s["id"], qq)
            if k in cached:
                items, fa = cached[k]
                used_cache, err, ms = True, None, 0
            else:
                items, fa, err, ms = results[k]
            ms_tot += ms
            if err:
                n_err += 1
                err_msg = err
                if k in stale:
                    used_stale, stale_at = True, stale[k][1]
                    n_items += len(stale[k][0])
            else:
                n_ok += 1
                n_items += len(items)
                fetched = fa
        fresh = any(k[0] == s["id"] for k in results)
        if n_ok > 0:
            state = "empty" if n_items == 0 else ("live" if fresh else "cache")
        else:
            state = "stale" if used_stale else "error"
        status[s["id"]] = {"id": s["id"], "name": s["name"], "kind": s["kind"], "ok": n_ok > 0,
                           "state": state, "demo": str(s["name"]).startswith("Демо"),
                           "error": err_msg if n_ok == 0 else "", "items": n_items, "ms": ms_tot,
                           "cached": used_cache and not fresh, "fetched_at": fetched or stale_at}
        if jobs and fresh:
            con.execute("UPDATE sources SET last_status=?, last_error=?, last_ms=?, last_items=?, last_at=? WHERE id=?",
                        ("ok" if n_ok else "error", err_msg, ms_tot, n_items, db.now_iso(), s["id"]))
    con.commit()

    # --- нормалізація та зіставлення ---
    seen, offers, invalid, rejected = set(), [], 0, 0
    srcmap = {s["id"]: s for s in sources}
    sets = {k: (v[0], v[1], "cache") for k, v in cached.items()}
    for k, (items, now, err, _ms) in results.items():
        if not err and items is not None:
            sets[k] = (items, now, "live")
        elif k in stale:
            sets[k] = (stale[k][0], stale[k][1], "stale")
    for (sid, qq), (items, fa, origin) in sets.items():
        for raw in items:
            o = _normalize(raw, srcmap[sid], al, fa)
            if not o:
                invalid += 1
                continue
            o["origin"] = origin
            dk = (sid, o["url"] or o["title"], o["norm_mpn"], o["price"])
            if dk in seen:
                continue
            seen.add(dk)
            c = classify(mq, o, amap, mkey or qbrand)
            if not c:
                rejected += 1
                continue
            o["level"], o["evidence"] = c
            o["match_score"] = search_score(mq, o, mkey or qbrand)
            offers.append(o)

    groups: dict[tuple, dict] = {}
    for o in offers:
        k = (o["brand_key"], o["norm_mpn"], o["condition"], o["pack"])
        g = groups.setdefault(k, {"key": k, "id": gid(k), "offers": []})
        g["offers"].append(o)
    out = []
    for g in groups.values():
        offs = analytics.rank_offers(g["offers"])
        _totals(offs)
        best_level = min((o["level"] for o in offs), key=LEVEL_ORDER.get)
        ev = next(o["evidence"] for o in offs if o["level"] == best_level)
        prices = [o["unit_price"] for o in offs if o["unit_price"] is not None]
        g.update({
            "level": best_level, "evidence": ev, "stats": analytics.stats(prices),
            "raw_range": _raw_range(offs),
            "title": Counter(o["title"] for o in offs).most_common(1)[0][0],
            "brand": Counter(o["brand"] for o in offs).most_common(1)[0][0],
            "mpn": Counter(o["mpn_raw"] for o in offs).most_common(1)[0][0],
            "condition": g["key"][2], "pack": g["key"][3],
            "match_score": max(o["match_score"] for o in offs),
            "n_sources": len({o["source_id"] for o in offs}),
            "best": offs[0],
            "image": next((o["image"] for o in offs if o["image"]), ""),
            "kind": category_of(Counter(o["title"] for o in offs).most_common(1)[0][0]),
        })
        out.append(g)
    out.sort(key=lambda g: (LEVEL_ORDER[g["level"]], -g["match_score"], g["stats"]["min"] if g["stats"] else float("inf")))
    try:
        archive.record_groups(con, out, al)
    except Exception:
        current_app_log("archive.record_groups")
    ms = int((time.time() - t0) * 1000)
    con.execute("INSERT INTO searches(q,at,n_offers,n_sources,ms) VALUES(?,?,?,?,?)",
                (q, db.now_iso(), len(offers), len(sources), ms))
    con.commit()
    try:
        local = archive.local_matches(con, q, mq, al, {(g["key"][1], g["key"][0]) for g in out})
    except Exception:
        current_app_log("archive.local_matches")
        local = []
    states = [v["state"] for v in status.values()]
    if not sources:
        state = "no_sources"
    elif all(x in ("error",) for x in states):
        state = "all_failed"
    elif any(x == "stale" for x in states):
        state = "partial_stale" if out else "all_failed"
    elif any(x == "error" for x in states):
        state = "partial" if out else "partial_empty"
    elif not out:
        state = "empty_ok"
    else:
        state = "ok"
    return {"q": q, "mq": mq, "local": local, "state": state, "groups": out, "sources": list(status.values()), "ms": ms, "analog_queries": extra,
            "n_offers": len(offers), "invalid": invalid, "rejected": rejected, "rates": RATES}


def _age_seconds(iso: str) -> float:
    return (_now() - datetime.fromisoformat(iso)).total_seconds()


def discover(con, q: str, base_url: str, exclude: set[str] | None = None, force: bool = False,
             vehicle: dict | None = None) -> dict:
    """Пошук деталей за описом (марка, модель, тип): живі запити, артикул беремо з поля або з назви.

    Повертає кандидатів, згрупованих за артикулом, БЕЗ цінової аналітики: ціни та аналоги користувач
    отримує, коли відкриває конкретну деталь (звичайний пошук за артикулом).
    """
    t0 = time.time()
    al = aliases(con)
    ex = set(exclude or ()) | set(al) | {v for v in al.values()}
    sources = [dict(r) for r in con.execute("SELECT * FROM sources WHERE enabled=1 ORDER BY id")]
    qk = q.lower()
    status, raw_items = [], []

    def run(s):
        try:
            return s, fetch_source(s, q, base_url), db.now_iso(), None
        except (FetchError, KeyError, ValueError) as e:
            return s, None, None, str(e) or type(e).__name__
        except Exception as e:
            return s, None, None, f"Помилка розбору: {type(e).__name__}"

    todo = []
    for s in sources:      # кеш читаємо в основному потоці (SQLite-з'єднання не потокобезпечне)
        row = None if force else con.execute(
            "SELECT fetched_at, items FROM cache WHERE source_id=? AND qkey=?", (s["id"], qk)).fetchone()
        if row and _age_seconds(row["fetched_at"]) < CACHE_TTL:
            status.append({"name": s["name"], "ok": True, "error": "", "items": len(json.loads(row["items"])), "cached": True})
            raw_items.extend((s, it) for it in json.loads(row["items"]))
        else:
            todo.append(s)
    if todo:
        with ThreadPoolExecutor(max_workers=min(6, len(todo))) as pool:
            results = list(pool.map(run, todo))
        for s, items, fa, err in results:
            if items is not None:
                con.execute("INSERT OR REPLACE INTO cache VALUES(?,?,?,?)", (s["id"], qk, fa, json.dumps(items, ensure_ascii=False)))
            status.append({"name": s["name"], "ok": err is None, "error": err or "", "items": len(items or []), "cached": False})
            raw_items.extend((s, it) for it in items or [])
        con.commit()

    groups: dict[str, dict] = {}
    loose = []
    for s, it in raw_items:
        title = (it.get("title") or "").strip()
        price = parse_price(it.get("price"))
        if not title or price is None:
            continue
        mpn = it.get("mpn") or guess_mpn(title, ex)
        rec = {"title": title, "url": it.get("url") or "", "source": s["name"], "image": _img(it),
               "brand": (it.get("brand") or "").strip()}
        nm = norm_mpn(str(mpn)) if mpn else ""
        if not nm:
            loose.append(rec)
            continue
        g = groups.setdefault(nm, {"mpn": str(mpn).strip(), "norm": nm, "titles": [], "image": "", "sources": set(), "n": 0,
                                   "offers": [], "brands": []})
        cur = _cur(it.get("currency"), s["currency"])
        pk, _pn = parse_pack(it, title, _profile(s).get("pack_default"))
        up = analytics.unit_price(price, cur, RATES, pk) if (cur and pk) else None
        if cur:
            g["offers"].append({"source": s["name"], "price": price, "currency": cur, "unit_price_uah": up, "url": rec["url"]})
        if rec["brand"]:
            g["brands"].append(rec["brand"])
        g["titles"].append(title)
        g["n"] += 1
        g["sources"].add(s["name"])
        g["image"] = g["image"] or rec["image"]
    out = []
    for g in groups.values():
        title = Counter(g["titles"]).most_common(1)[0][0]
        brand = next((w for w in title.replace(",", " ").split() if brand_key(w, al) in set(al.values())), "")
        brand = brand or next((b for b in g["brands"] if brand_key(b, al) in set(al.values())), "")
        out.append({"mpn": g["mpn"], "norm": g["norm"], "title": title, "brand": brand, "image": g["image"],
                    "kind": category_of(title), "n": g["n"], "n_sources": len(g["sources"]),
                    "analogs": analogs_for(con, g["mpn"]), "offers": g["offers"]})
    if out:
        try:
            archive.record_discovery(con, out, vehicle, al)
        except Exception:
            current_app_log("archive.record_discovery")
    out.sort(key=lambda x: (-x["n"], x["mpn"]))
    uniq, seen_l = [], set()
    for x in loose:
        k = (x["title"].lower(), x["url"])
        if k not in seen_l:
            seen_l.add(k)
            x["kind"] = category_of(x["title"])
            uniq.append(x)
    return {"q": q, "parts": out, "loose": uniq[:8], "sources": status, "ms": int((time.time() - t0) * 1000)}


def analogs_for(con, mpn: str) -> list[dict]:
    from .vehicles import analogs_of
    return analogs_of(con, "", mpn)
