"""Власний архів: усе знайдене в Інтернеті накопичується в БД і прив'язується до авто (марка/модель/рік/тип деталі)."""
from __future__ import annotations
import csv
import io
import re

from . import db
from .normalize import brand_key, norm_mpn


def learn_brand(con, brand: str, al: dict, now: str) -> None:
    """Словник росте з відповідей джерел: невідомий бренд додається як непідтверджений (підтверджує адміністратор)."""
    key = brand_key(brand, al)
    if not key or len(key) < 2:
        return
    row = con.execute("SELECT id FROM brands WHERE canonical=?", (key,)).fetchone()
    if row:
        con.execute("UPDATE brands SET seen_count=seen_count+1 WHERE id=?", (row["id"],))
    else:
        con.execute("INSERT INTO brands(canonical,display,verified,source,first_seen,seen_count) VALUES(?,?,0,?,?,1)",
                    (key, brand.strip()[:60], "виявлено в відповідях джерел", now))
        con.execute("INSERT OR IGNORE INTO aliases VALUES(?,?)", (key, key))
        al[key] = key


def _upsert_part(con, mpn, brand, title, kind, image, al, now) -> int:
    nm = norm_mpn(mpn)
    if brand:
        learn_brand(con, brand, al, now)
    bk = brand_key(brand, al) if brand else ""
    row = None
    if bk:
        row = con.execute("SELECT * FROM parts WHERE norm_mpn=? AND brand_key=?", (nm, bk)).fetchone() or \
            con.execute("SELECT * FROM parts WHERE norm_mpn=? AND brand_key=''", (nm,)).fetchone()
    else:
        row = con.execute("SELECT * FROM parts WHERE norm_mpn=? ORDER BY seen_count DESC", (nm,)).fetchone()
    if row:
        con.execute("UPDATE parts SET last_seen=?, seen_count=seen_count+1, brand=CASE WHEN brand='' THEN ? ELSE brand END,"
                    " brand_key=CASE WHEN brand_key='' THEN ? ELSE brand_key END, title=COALESCE(NULLIF(title,''),?),"
                    " image=CASE WHEN image='' THEN ? ELSE image END, kind=CASE WHEN kind='part' THEN ? ELSE kind END WHERE id=?",
                    (now, brand or "", bk, title, image or "", kind, row["id"]))
        return row["id"]
    return con.execute("INSERT INTO parts(norm_mpn,brand_key,brand,mpn,title,kind,image,first_seen,last_seen) VALUES(?,?,?,?,?,?,?,?,?)",
                       (nm, bk, brand or "", str(mpn).strip(), title, kind, image or "", now, now)).lastrowid


def _snapshots(con, part_id, offers, now):
    for o in offers:
        at = o.get("fetched_at") or now                       # F4: зберігаємо час початкового отримання, а не час запису
        sh = o.get("shipping")
        s_cost = sh["cost"] if sh else None
        s_cur = sh["currency"] if sh else None
        s_free = 1 if (sh and sh["cost"] == 0) else 0         # нуль потрапляє сюди лише з підтвердженням джерела (service._ship)
        # однаковий знімок (та сама ціна, доставка, день) не дублюємо; зміна доставки дає новий знімок, старий лишається
        if con.execute("SELECT 1 FROM price_snapshots WHERE part_id=? AND source=? AND url=? AND price=?"
                       " AND (fetched_at=? OR substr(fetched_at,1,10)=?)"
                       " AND COALESCE(shipping_cost,-1)=COALESCE(?,-1) AND COALESCE(shipping_currency,'')=COALESCE(?,'')"
                       " AND COALESCE(shipping_free,-1)=?",
                       (part_id, o["source"], o.get("url", ""), o["price"], at, at[:10], s_cost, s_cur, s_free)).fetchone():
            continue
        con.execute("INSERT INTO price_snapshots(part_id,source,price,currency,unit_price_uah,url,fetched_at,pack,condition,origin,unit_reason,"
                    "shipping_checked,shipping_cost,shipping_currency,shipping_free,total_uah) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (part_id, o["source"], o["price"], o["currency"], o.get("unit_price_uah"), o.get("url", ""), at,
                     o.get("pack"), o.get("condition"), o.get("origin"), o.get("unit_reason") or "",
                     1, s_cost, s_cur, s_free, o.get("total_uah")))


def record_discovery(con, parts: list[dict], vehicle: dict | None, al) -> int:
    """parts: [{mpn, brand, title, kind, image, offers:[{source,price,currency,unit_price_uah,url}]}]"""
    now = db.now_iso()
    for p in parts:
        pid = _upsert_part(con, p["mpn"], p.get("brand", ""), p.get("title", ""), p.get("kind", "part"), p.get("image", ""), al, now)
        _snapshots(con, pid, p.get("offers", []), now)
        if vehicle:
            con.execute("INSERT INTO vehicle_parts(make,model,year,engine,part_type,part_id,first_seen,last_seen) VALUES(?,?,?,?,?,?,?,?)"
                        " ON CONFLICT(make,model,year,engine,part_type,part_id) DO UPDATE SET last_seen=excluded.last_seen, times=times+1",
                        (vehicle["make"], vehicle["model"], vehicle["year"], vehicle.get("engine", ""), vehicle["part_type"], pid, now, now))
    con.commit()
    return len(parts)


def record_groups(con, groups: list[dict], al) -> None:
    """Результати пошуку за артикулом: кожна група → запис у каталозі + знімки цін."""
    parts = []
    for g in groups:
        parts.append({"mpn": g["mpn"], "brand": g["brand"], "title": g["title"], "kind": g.get("kind", "part"),
                      "image": g.get("image", ""),
                      "offers": [{"source": o["source"], "price": o["price"], "currency": o["currency"],
                                  "unit_price_uah": o["unit_price"], "url": o["url"], "fetched_at": o["fetched_at"],
                                  "pack": o["pack"], "condition": o["condition"], "origin": o["origin"],
                                  "unit_reason": o["unit_reason"], "shipping": o.get("shipping"),
                                  "total_uah": o.get("total_uah")} for o in g["offers"]]})
    if parts:
        record_discovery(con, parts, None, al)


_BASE = """SELECT p.*, (SELECT MIN(unit_price_uah) FROM price_snapshots s WHERE s.part_id=p.id
                 AND s.fetched_at=(SELECT MAX(fetched_at) FROM price_snapshots WHERE part_id=p.id)) AS last_min,
              (SELECT MAX(fetched_at) FROM price_snapshots WHERE part_id=p.id) AS last_price_at,
              (SELECT COUNT(*) FROM price_snapshots WHERE part_id=p.id) AS n_snap"""


def compat_note(vrows: list, year, engine: str) -> dict:
    """R3: що саме підтверджує запис довідника для обраного авто. Збіг артикулів сам по собі сумісності не доводить."""
    years = sorted({r["year"] for r in vrows})
    engines = sorted({r["engine"] for r in vrows if r["engine"]})
    eng = (engine or "").strip().lower()
    if year and int(year) not in years:
        return {"level": "none", "text": f"Для вибраного року підтвердження немає (у довіднику роки: {', '.join(map(str, years))}). Перевірте застосовність."}
    if engines:
        if eng and any(eng == e.lower() for e in engines):
            return {"level": "full", "text": f"Підтверджено: марка, модель, рік і модифікація ({engine})."}
        return {"level": "model", "text": "Підтверджено: марка, модель і рік. Модифікацію потрібно уточнити"
                + (f" (у довіднику: {', '.join(engines)})." if engines else ".")}
    return {"level": "model", "text": "Підтверджено лише марку, модель і рік. Модифікацію потрібно уточнити."}


def for_vehicle(con, make, model, part_type, year=None, engine="") -> list[dict]:
    rows = con.execute(_BASE + """, GROUP_CONCAT(DISTINCT v.year) AS years, MAX(v.last_seen) AS vlast
        FROM vehicle_parts v JOIN parts p ON p.id=v.part_id
        WHERE v.make=? AND v.model=? AND v.part_type=? GROUP BY p.id ORDER BY SUM(v.times) DESC, p.seen_count DESC""",
                       (make, model, part_type)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        vrows = con.execute("SELECT year, engine FROM vehicle_parts WHERE part_id=? AND make=? AND model=? AND part_type=?",
                            (d["id"], make, model, part_type)).fetchall()
        d["compat"] = compat_note(vrows, year, engine)
        out.append(d)
    return out


def list_parts(con, q="", make="", model="", kind="", limit=100, offset=0):
    where, args = [], []
    if q:
        where.append("(p.title LIKE ? OR p.mpn LIKE ? OR p.norm_mpn LIKE ? OR p.brand LIKE ?)")
        n = norm_mpn(q)
        args += [f"%{q}%", f"%{q}%", f"%{n or q}%", f"%{q}%"]
    if kind:
        where.append("p.kind=?"); args.append(kind)
    join = ""
    if make or model:
        join = " JOIN vehicle_parts vf ON vf.part_id=p.id"
        if make:
            where.append("vf.make=?"); args.append(make)
        if model:
            where.append("vf.model=?"); args.append(model)
    sql = _BASE + f", (SELECT GROUP_CONCAT(DISTINCT make||' '||model||' '||year) FROM vehicle_parts WHERE part_id=p.id) AS cars FROM parts p{join}"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " GROUP BY p.id ORDER BY p.last_seen DESC LIMIT ? OFFSET ?"
    total = con.execute(f"SELECT COUNT(DISTINCT p.id) FROM parts p{join}" + (" WHERE " + " AND ".join(where) if where else ""), args).fetchone()[0]
    return [dict(r) for r in con.execute(sql, args + [limit, offset])], total


def stats(con) -> dict:
    one = lambda q: con.execute(q).fetchone()[0]
    return {"parts": one("SELECT COUNT(*) FROM parts"), "links": one("SELECT COUNT(*) FROM vehicle_parts"),
            "snapshots": one("SELECT COUNT(*) FROM price_snapshots"),
            "cars": one("SELECT COUNT(*) FROM (SELECT DISTINCT make, model FROM vehicle_parts)"),
            "last": con.execute("SELECT MAX(last_seen) FROM parts").fetchone()[0]}


def export_csv(con) -> str:
    rows, _ = list_parts(con, limit=10**6)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["артикул", "виробник", "назва", "тип", "авто (марка модель рік)", "перша поява", "остання поява",
                "разів знайдено", "остання мін. ціна, грн", "дата ціни", "знімків цін"])
    for r in rows:
        w.writerow([r["mpn"], r["brand"], r["title"], r["kind"], r["cars"] or "", r["first_seen"], r["last_seen"],
                    r["seen_count"], round(r["last_min"]) if r["last_min"] else "", r["last_price_at"] or "", r["n_snap"]])
    return "\ufeff" + out.getvalue()


def part_id(con, norm: str, bkey: str) -> int | None:
    row = con.execute("SELECT id FROM parts WHERE norm_mpn=? AND brand_key=?", (norm, bkey)).fetchone() or \
        con.execute("SELECT id FROM parts WHERE norm_mpn=? ORDER BY seen_count DESC", (norm,)).fetchone()
    return row["id"] if row else None


def history(con, pid: int) -> list[dict]:
    """Мінімальна й медіанна ціна (грн/од.) за днями за накопиченими знімками."""
    out = []
    for r in con.execute("SELECT substr(fetched_at,1,10) d, MIN(unit_price_uah) mn, COUNT(*) n, GROUP_CONCAT(unit_price_uah) ps"
                         " FROM price_snapshots WHERE part_id=? AND unit_price_uah IS NOT NULL GROUP BY d ORDER BY d", (pid,)):
        ps = sorted(float(x) for x in r["ps"].split(","))
        out.append({"date": r["d"], "min": r["mn"], "median": ps[len(ps) // 2], "n": r["n"]})
    return out


def toggle_watch(con, pid: int) -> bool:
    if con.execute("SELECT 1 FROM watchlist WHERE part_id=?", (pid,)).fetchone():
        con.execute("DELETE FROM watchlist WHERE part_id=?", (pid,))
        con.commit()
        return False
    h = history(con, pid)
    con.execute("INSERT INTO watchlist(part_id,added_at,base_price_uah) VALUES(?,?,?)", (pid, db.now_iso(), h[-1]["min"] if h else None))
    con.commit()
    return True


def watched_ids(con) -> set[int]:
    return {r[0] for r in con.execute("SELECT part_id FROM watchlist")}


def watchlist(con) -> list[dict]:
    rows = con.execute(_BASE + """, w.added_at, w.base_price_uah FROM watchlist w JOIN parts p ON p.id=w.part_id
                                   ORDER BY w.added_at DESC""").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["delta"] = ((d["last_min"] - d["base_price_uah"]) / d["base_price_uah"] * 100) if d["last_min"] and d["base_price_uah"] else None
        out.append(d)
    return out


def local_matches(con, q: str, mq: str, al: dict, skip: set | None = None, limit: int = 20) -> list[dict]:
    """Пошук у власному архіві: точний артикул, префікс, аналоги з довідника, слова назви. Працює без мережі."""
    from .normalize import brand_key, tokens
    skip = skip or set()
    n = norm_mpn(mq)
    found: dict[int, dict] = {}

    def add(rows, level, why):
        for r in rows:
            if r["id"] not in found and (r["norm_mpn"], r["brand_key"]) not in skip:
                d = dict(r)
                d["level"], d["why"] = level, why
                found[r["id"]] = d

    base = _BASE + ", (SELECT GROUP_CONCAT(DISTINCT make||' '||model) FROM vehicle_parts WHERE part_id=p.id) AS cars FROM parts p"
    if len(n) >= 3:
        add(con.execute(base + " WHERE p.norm_mpn=?", (n,)), "IDENTICAL", "Артикул збігається")
        ans = {norm_mpn(r["mpn_b"]) for r in con.execute("SELECT mpn_b FROM cross_refs WHERE REPLACE(REPLACE(UPPER(mpn_a),' ',''),'-','')=?", (n,))}
        ans |= {norm_mpn(r["mpn_a"]) for r in con.execute("SELECT mpn_a FROM cross_refs WHERE REPLACE(REPLACE(UPPER(mpn_b),' ',''),'-','')=?", (n,))}
        ans.discard(n)
        for a in list(ans)[:10]:
            add(con.execute(base + " WHERE p.norm_mpn=?", (a,)), "CROSS_REFERENCE", "Аналог із довідника")
        if len(n) >= 4:
            add(con.execute(base + " WHERE p.norm_mpn LIKE ? LIMIT 20", (n + "%",)), "CANDIDATE", "Артикул починається із запиту")
    known = set(al.values())
    words = [t for t in tokens(q) if len(t) >= 2 and brand_key(t, al) not in known and not (len(n) >= 4 and t == n.lower())]
    if words and (len(n) < 4 or not found):
        cond = " AND ".join("LOWER(p.title) LIKE ?" for _ in words)
        add(con.execute(base + f" WHERE {cond} LIMIT 20", [f"%{w.lower()}%" for w in words]), "CANDIDATE", "Збіг у назві")
    order = {"IDENTICAL": 0, "CROSS_REFERENCE": 1, "CANDIDATE": 2}
    return sorted(found.values(), key=lambda d: (order[d["level"]], -(d.get("seen_count") or 0)))[:limit]


def item(con, pid: int) -> dict | None:
    r = con.execute(_BASE + ", (SELECT GROUP_CONCAT(DISTINCT make||' '||model||' '||year) FROM vehicle_parts WHERE part_id=p.id) AS cars"
                    " FROM parts p WHERE p.id=?", (pid,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["offers"] = [dict(x) for x in con.execute(
        "SELECT s.* FROM price_snapshots s WHERE s.part_id=? AND s.fetched_at=(SELECT MAX(fetched_at) FROM price_snapshots"
        " WHERE part_id=? AND source=s.source) ORDER BY unit_price_uah", (pid, pid))]
    d["snapshots"] = [dict(x) for x in con.execute(
        "SELECT * FROM price_snapshots WHERE part_id=? ORDER BY fetched_at DESC, id DESC LIMIT 100", (pid,))]
    return d
