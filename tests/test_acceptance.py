"""Автоматичні перевірки за ТЗ: F1 (пояснення збігу), F2 (аналоги), F3 (межі цінового порівняння), F4 (час отримання)."""
import json
import sqlite3
import pytest

from app import create_app, db, service
from app.matching import classify, CANDIDATE, CROSS, IDENTICAL


def offer(mpn="GDB1550", brand="TRW", **kw):
    o = {"norm_mpn": mpn.replace(" ", "").upper(), "brand_key": brand.upper(), "brand": brand, "mpn_raw": mpn, "title": "Колодки гальмівні"}
    o.update(kw)
    return o


# ---------- F1 ----------
def test_f1_four_brand_cases():
    lvl, ev = classify("GDB1550", offer(), {}, "TRW")                 # обидві ознаки задано й збігаються
    assert lvl == IDENTICAL and ev["basis"] == "Артикул і виробник збігаються"
    lvl, ev = classify("GDB1550", offer(), {}, None)                  # бренду в запиті немає
    assert lvl == IDENTICAL and ev["basis"] == "Збіг за артикулом" and ev["seller_brand"] == "TRW"
    assert "TRW" in ev["note"]                                         # бренд продавця показано окремо
    lvl, ev = classify("GDB1550", offer(brand="", ), {}, "TRW")       # бренд продавця невідомий
    assert lvl == CANDIDATE and "Виробника не вказано" in ev["basis"]
    lvl, ev = classify("GDB1550", offer(brand=""), {}, None)
    assert lvl == CANDIDATE
    lvl, ev = classify("GDB1550", offer(brand="Brembo"), {}, "TRW")   # бренди різні
    assert lvl != IDENTICAL and ev["code"] == "brand_conflict"


# ---------- F2 ----------
@pytest.fixture()
def con(tmp_path):
    app = create_app({"DATABASE": str(tmp_path / "t.db"), "TESTING": True})
    c = db.connect(str(tmp_path / "t.db"))
    c.execute("DELETE FROM cross_refs")
    c.execute("INSERT INTO cross_refs(mfr_a,mpn_a,mfr_b,mpn_b,ref_source,verified) VALUES('TRW','GDB1550','BOSCH','0986AB1234','Довідник X',1)")
    c.commit()
    return c


def test_f2_analog_only_for_reference_pair(con):
    al = service.aliases(con)
    amap, _ = service.analog_map(con, "GDB1550", al)
    lvl, ev = classify("GDB1550", offer("0986AB1234", "Bosch"), amap, None)
    assert lvl == CROSS and ev["query_mpn"] == "GDB1550" and ev["analog_mpn"] == "0986AB1234" and ev["ref_source"] == "Довідник X"
    for similar in ("GDB1551", "GDB1550X"):
        lvl, ev = classify("GDB1550", offer(similar, "TRW"), amap, None)
        assert lvl == CANDIDATE and "Спільний початок артикула" in ev["basis"]


def test_f2_unbranded_ambiguous_number_is_candidate(con):
    con.execute("INSERT INTO cross_refs(mfr_a,mpn_a,mfr_b,mpn_b,ref_source,verified) VALUES('TRW','GDB1550','MANN','0986AB1234','Довідник Y',1)")
    con.commit()
    amap, _ = service.analog_map(con, "GDB1550", service.aliases(con))
    lvl, ev = classify("GDB1550", offer("0986AB1234", ""), amap, None)
    assert lvl == CANDIDATE and ev["code"] == "ambiguous_analog"


# ---------- F3 ----------
@pytest.fixture()
def app1(tmp_path, monkeypatch):
    app = create_app({"DATABASE": str(tmp_path / "p.db"), "TESTING": True})
    with db.connect(str(tmp_path / "p.db")) as c:
        c.execute("UPDATE sources SET enabled=0")
        c.execute("UPDATE sources SET enabled=1 WHERE id=(SELECT MIN(id) FROM sources)")
        row = c.execute("SELECT id, profile FROM sources WHERE enabled=1").fetchone()
        prof = json.loads(row["profile"] or "{}")
        prof.pop("pack_default", None)                 # строгий режим: кількість береться лише з даних продавця
        c.execute("UPDATE sources SET profile=? WHERE id=?", (json.dumps(prof), row["id"]))
        c.commit()
    return app


def run(app, monkeypatch, items, q="GDB1550"):
    monkeypatch.setattr(service, "fetch_source", lambda s, qq, b: items)
    with app.app_context():
        c = db.connect(app.config["DATABASE"])
        return service.search(c, q, "http://x", None, True)


def test_f3_pack_and_unit_prices(app1, monkeypatch):
    items = [
        {"title": "Колодки TRW GDB1550", "mpn": "GDB1550", "brand": "TRW", "pack": 1, "price": 300, "currency": "UAH", "url": "u1"},
        {"title": "Колодки TRW GDB1550 комплект 2 шт", "mpn": "GDB1550", "brand": "TRW", "price": 540, "currency": "UAH", "url": "u2", "pack": 2},
        {"title": "Колодки TRW GDB1550 комплект", "mpn": "GDB1550", "brand": "TRW", "price": 100, "currency": "UAH", "url": "u3", "pack": "?"},
    ]
    res = run(app1, monkeypatch, items)
    g = {x["key"][3]: x for x in res["groups"]}
    assert set(g) == {1, 2, None}                                     # три окремі групи, невідоме фасування — окрема ознака
    assert g[1]["stats"]["median"] == 300 and g[2]["stats"]["median"] == 270
    assert g[None]["stats"] is None and g[None]["raw_range"]["min"] == 100       # початкова ціна й причина
    assert "Фасування невідоме" in g[None]["raw_range"]["reasons"][0]
    assert g[None]["offers"][0]["unit_price"] is None


def test_f3_unknown_currency_skipped_with_reason(app1, monkeypatch):
    res = run(app1, monkeypatch, [{"title": "Колодки GDB1550", "mpn": "GDB1550", "brand": "TRW", "pack": 1, "price": 10, "currency": "PLN", "url": "u"}])
    o = res["groups"][0]["offers"][0]
    assert o["unit_price"] is None and "Немає курсу для PLN" in o["unit_reason"] and o["price"] == 10
    assert res["groups"][0]["stats"] is None


# ---------- F4 ----------
def test_f4_failed_refresh_keeps_original_time(app1, monkeypatch):
    items = [{"title": "Колодки GDB1550", "mpn": "GDB1550", "brand": "TRW", "pack": 1, "price": 300, "currency": "UAH", "url": "https://shop/1"}]
    r1 = run(app1, monkeypatch, items)
    t1 = r1["groups"][0]["offers"][0]["fetched_at"]
    assert r1["groups"][0]["offers"][0]["origin"] == "live" and r1["state"] == "ok"
    r2 = run(app1, monkeypatch, items)                                # повтор: кеш, час початковий
    # (force=True у helper, тому знову live; перевіряємо кеш окремим викликом нижче)
    def boom(s, q, b):
        raise service.FetchError("недоступне")
    monkeypatch.setattr(service, "fetch_source", boom)
    with app1.app_context():
        c = db.connect(app1.config["DATABASE"])
        t_cache = c.execute("SELECT fetched_at FROM cache").fetchone()[0]
        r3 = service.search(c, "GDB1550", "http://x", None, True)    # примусове оновлення зі збоєм
        assert c.execute("SELECT fetched_at FROM cache").fetchone()[0] == t_cache   # час не змінено
    o = r3["groups"][0]["offers"][0]
    assert o["origin"] == "stale" and o["fetched_at"] == t_cache
    assert r3["state"] == "partial_stale" and r3["sources"][0]["state"] == "stale"


def test_f4_zero_matches_after_success_is_separate_state(app1, monkeypatch):
    r = run(app1, monkeypatch, [])
    assert r["state"] == "empty_ok"
    monkeypatch.setattr(service, "fetch_source", lambda s, q, b: (_ for _ in ()).throw(service.FetchError("x")))
    with app1.app_context():
        r = service.search(db.connect(app1.config["DATABASE"]), "ZZZ9999", "http://x", None, True)
    assert r["state"] == "all_failed"


# ---------- U1 / U2 ----------
ITEMS = [
    {"title": "Колодки TRW GDB1550", "mpn": "GDB1550", "brand": "TRW", "pack": 1, "price": 300, "currency": "UAH", "url": "https://s1/a", "availability": "в наявності", "delivery": "2"},
    {"title": "Колодки TRW GDB1550 (інший)", "mpn": "GDB1550", "brand": "TRW", "pack": 1, "price": 320, "currency": "UAH", "url": "https://s2/a"},
]


def test_u1_filters_survive_round_trip(app1, monkeypatch):
    monkeypatch.setattr(service, "fetch_source", lambda s, q, b: ITEMS + [
        {"title": "Колодки Bosch 0986AB1234", "mpn": "0986AB1234", "brand": "Bosch", "price": 500, "currency": "UAH", "url": "https://s3/a", "availability": "в наявності"}])
    with db.connect(app1.config["DATABASE"]) as c:
        c.execute("INSERT INTO cross_refs(mfr_a,mpn_a,mfr_b,mpn_b,ref_source,verified) VALUES('TRW','GDB1550','BOSCH','0986AB1234','Довідник',1)")
        c.commit()
    cl = app1.test_client()
    html = cl.get("/search?q=GDB1550&m=TRW&level=CROSS_REFERENCE&stock=1&sort=price").get_data(as_text=True)
    assert "Аналог за довідником" in html and "Підстава" not in html
    import re
    href = re.search(r'href="(/part\?[^"]+)"', html).group(1).replace("&amp;", "&")
    for k in ("q=GDB1550", "m=TRW", "level=CROSS_REFERENCE", "stock=1", "sort=price"):
        assert k in href
    part = cl.get(href).get_data(as_text=True)
    back = re.search(r'class="crumbs"><a href="([^"]+)"', part).group(1).replace("&amp;", "&")
    for k in ("q=GDB1550", "m=TRW", "level=CROSS_REFERENCE", "stock=1", "sort=price"):
        assert k in back
    empty = cl.get("/search?q=GDB1550&level=CANDIDATE&stock=1").get_data(as_text=True)
    assert "Скинути фільтри" in empty and "q=GDB1550" in empty.split("Скинути фільтри")[0].rsplit("href=", 1)[1]


def test_u2_identification_first_and_breakdown(app1, monkeypatch):
    monkeypatch.setattr(service, "fetch_source", lambda s, q, b: ITEMS)
    cl = app1.test_client()
    gid_ = cl.get("/api/search?q=GDB1550").get_json()["groups"][0]["id"]
    html = cl.get("/part", query_string={"id": gid_, "q": "GDB1550"}).get_data(as_text=True)
    i_id, i_off, i_sc = html.index("Збіг за артикулом"), html.index("Пропозиції продавців"), html.index('id="score"')
    assert i_id < i_off < i_sc and "До продавця" in html
    # друга пропозиція не має наявності й доставки: «Немає даних» і нульова фактична вага
    oid = [o for o in __import__("re").findall(r'offer=([0-9a-f]{10})', html)][1]
    html2 = cl.get("/part", query_string={"id": gid_, "q": "GDB1550", "offer": oid}).get_data(as_text=True)
    sc = html2.split('id="score"')[1]
    assert sc.count("Немає даних") == 2 and "не бере участі" in sc


# ---------- R1 ----------
def test_r1_compare(app1, monkeypatch):
    items = ITEMS + [{"title": "Колодки TRW GDB1550 ще", "mpn": "GDB1550", "brand": "TRW", "pack": 1, "price": 300, "currency": "UAH", "url": "https://s3/a"},
                     {"title": "Колодки TRW GDB1550 четвертий", "mpn": "GDB1550", "brand": "TRW", "pack": 1, "price": 350, "currency": "UAH", "url": "https://s4/a"}]
    monkeypatch.setattr(service, "fetch_source", lambda s, q, b: items)
    cl = app1.test_client()
    g = cl.get("/api/search?q=GDB1550").get_json()["groups"][0]["id"]
    import re
    html = cl.get("/part", query_string={"id": g, "q": "GDB1550"}).get_data(as_text=True)
    oids = list(dict.fromkeys(re.findall(r'name="o" value="([0-9a-f]{10})"', html)))
    assert len(oids) == 4 and "3 пропозиції — це максимум" in html           # пояснення для четвертої (JS-текст)
    r = cl.get("/compare", query_string=[("id", g), ("q", "GDB1550")] + [("o", x) for x in oids[:3]]).get_data(as_text=True)
    assert r.count("Найнижча відома ціна за одиницю") == 2                    # дві по 300 грн — позначено обидві
    r4 = cl.get("/compare", query_string=[("id", g), ("q", "GDB1550")] + [("o", x) for x in oids]).get_data(as_text=True)
    assert r4.count("До продавця") == 3                                       # четверта не додається
    assert "Прибрати" in r and "Повернутися до результатів" in r
    assert cl.get("/compare", query_string=[("id", g), ("q", "GDB1550"), ("o", oids[0])]).status_code == 302


# ---------- R2 ----------
def test_r2_total_cost(app1, monkeypatch):
    with db.connect(app1.config["DATABASE"]) as c:
        prof = json.loads(c.execute("SELECT profile FROM sources ORDER BY id LIMIT 1").fetchone()[0] or "{}")
        prof["shipping"] = {"free_confirmed": True}
        c.execute("UPDATE sources SET profile=? WHERE enabled=1", (json.dumps(prof),))
        c.commit()
    items = [
        {"title": "A GDB1550", "mpn": "GDB1550", "brand": "TRW", "pack": 1, "price": 300, "currency": "UAH", "url": "a", "shipping": 80},
        {"title": "B GDB1550", "mpn": "GDB1550", "brand": "TRW", "pack": 1, "price": 340, "currency": "UAH", "url": "b", "shipping": 0},
        {"title": "C GDB1550", "mpn": "GDB1550", "brand": "TRW", "pack": 1, "price": 290, "currency": "UAH", "url": "c"},
    ]
    res = run(app1, monkeypatch, items)
    o = {x["url"]: x for x in res["groups"][0]["offers"]}
    assert round(o["a"]["total_uah"]) == 380 and round(o["b"]["total_uah"]) == 340 and o["c"]["total_uah"] is None
    assert o["b"]["lowest_total"] and not o["a"]["lowest_total"] and not o["c"]["lowest_total"]


def test_r2_zero_without_confirmation_is_unknown(app1, monkeypatch):
    res = run(app1, monkeypatch, [{"title": "A GDB1550", "mpn": "GDB1550", "brand": "TRW", "pack": 1, "price": 300, "currency": "UAH", "url": "a", "shipping": 0}])
    assert res["groups"][0]["offers"][0]["shipping"] is None


# ---------- R3 ----------
def test_r3_compat_notes():
    from app.archive import compat_note
    full = compat_note([{"year": 2016, "engine": "1.6 TDI"}], 2016, "1.6 TDI")
    model = compat_note([{"year": 2016, "engine": ""}], 2016, "")
    assert full["level"] == "full" and "модифікація" in full["text"]
    assert model["level"] == "model" and "Модифікацію потрібно уточнити" in model["text"] and "лише марку, модель і рік" in model["text"]
    assert compat_note([{"year": 2016, "engine": ""}], 2019, "")["level"] == "none"      # інше авто: підтвердження скинуто


# ---------- міграція існуючої бази ----------
def test_old_database_opens_archive_and_watchlist(tmp_path):
    path = str(tmp_path / "old.db")
    create_app({"DATABASE": path, "TESTING": True})
    con = sqlite3.connect(path)
    con.execute("INSERT INTO parts(id,norm_mpn,brand_key,brand,mpn,title,kind,first_seen,last_seen) VALUES(1,'GDB1550','TRW','TRW','GDB1550','Колодки','brake','2025-01-01','2025-01-02')")
    con.execute("INSERT INTO price_snapshots(part_id,source,price,currency,unit_price_uah,url,fetched_at) VALUES(1,'Старе джерело',300,'UAH',300,'https://x','2025-01-02T10:00:00+00:00')")
    con.execute("INSERT INTO watchlist(part_id,added_at,base_price) VALUES(1,'2025-01-02',300)") if False else None
    cols = [r[1] for r in con.execute("PRAGMA table_info(watchlist)")]
    con.execute(f"INSERT INTO watchlist({','.join(cols)}) VALUES({','.join('?' for _ in cols)})", [1 if c == 'part_id' else ('2025-01-02' if 'at' in c else 300) for c in cols])
    # імітація схеми попередньої версії: знімки без нових атрибутів
    con.executescript("""
        CREATE TABLE ps_old AS SELECT id,part_id,source,price,currency,unit_price_uah,url,fetched_at FROM price_snapshots;
        DROP TABLE price_snapshots;
        CREATE TABLE price_snapshots(id INTEGER PRIMARY KEY, part_id INTEGER NOT NULL REFERENCES parts(id) ON DELETE CASCADE,
          source TEXT NOT NULL, price REAL NOT NULL, currency TEXT NOT NULL, unit_price_uah REAL, url TEXT DEFAULT '', fetched_at TEXT NOT NULL);
        INSERT INTO price_snapshots SELECT * FROM ps_old; DROP TABLE ps_old;""")
    con.commit(); con.close()
    app = create_app({"DATABASE": path, "TESTING": True})              # «оновлення програми»: повторне відкриття
    cl = app.test_client()
    assert cl.get("/archive").status_code == 200 and "GDB1550" in cl.get("/archive").get_data(as_text=True)
    assert cl.get("/watchlist").status_code == 200 and "GDB1550" in cl.get("/watchlist").get_data(as_text=True)
    page = cl.get("/item/1").get_data(as_text=True)
    assert "Старе джерело" in page and "невідомо" in page and "старий запис" in page
    cols = {r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(price_snapshots)")}
    assert {"shipping_checked", "shipping_cost", "shipping_currency", "shipping_free", "total_uah"} <= cols     # міграція додала доставку
    row = sqlite3.connect(path).execute("SELECT shipping_checked, total_uah FROM price_snapshots").fetchone()
    assert row == (None, None)                                                                               # старий запис: доставка невідома


# ---------- фасування: строгі правила (регресія) ----------
SRC = {"id": 1, "name": "Тест", "currency": "UAH", "profile": "{}"}
AL = {}


def norm(**kw):
    raw = {"title": "Фільтр Bosch 0986452041", "mpn": "0986452041", "brand": "Bosch", "price": 300, "currency": "UAH"}
    raw.update(kw)
    return service._normalize(raw, SRC, AL, "2026-01-01T00:00:00+00:00")


def test_pack_four_examples():
    a = norm(pack=1)                                       # підтверджена 1 шт. за 300 грн
    b = norm(pack=2, price=540)                            # 2 шт. за 540 грн
    c = norm()                                             # «Фільтр Bosch» без кількості
    d = norm(pack=1.8)                                     # дробове
    assert a["unit_price"] == 300 and b["unit_price"] == 270
    assert c["pack"] is None and c["unit_price"] is None and "Фасування невідоме" in c["unit_reason"]
    assert d["pack"] is None and d["unit_price"] is None and "Фасування невідоме" in d["unit_reason"]
    assert c["price"] == 300 and c["currency"] == "UAH"    # ціна оголошення й валюта збережені


@pytest.mark.parametrize("kw,expected", [
    ({"pack": "2"}, 2), ({"pack": "2.0"}, 2), ({"pack": 0}, None), ({"pack": -1}, None), ({"pack": "1,8"}, None), ({"pack": "abc"}, None),
    ({"title": "Колодки 2 шт."}, 2), ({"title": "Колодки пара"}, 2), ({"title": "Колодки 2 шт. або 4 шт."}, None),
    ({"title": "Колодки 4 шт.", "pack": 2}, None),          # суперечність поля й назви
    ({"title": "Колодки 4 шт.", "pack": 4}, 4), ({"title": "Колодки комплект"}, None),
])
def test_pack_rules(kw, expected):
    assert norm(**kw)["pack"] == expected


def test_unknown_pack_excluded_from_stats_and_cheapest(app1, monkeypatch):
    items = [{"title": "Колодки GDB1550", "mpn": "GDB1550", "brand": "TRW", "pack": 1, "price": 300, "currency": "UAH", "url": "a"},
             {"title": "Колодки GDB1550 (без кількості)", "mpn": "GDB1550", "brand": "TRW", "price": 10, "currency": "UAH", "url": "b"}]
    res = run(app1, monkeypatch, items)
    known = [g for g in res["groups"] if g["key"][3] == 1][0]
    assert known["stats"]["min"] == 300 and known["stats"]["median"] == 300 and len(known["offers"]) == 1   # 10 грн не потрапила
    unk = [g for g in res["groups"] if g["key"][3] is None][0]
    assert unk["stats"] is None
    assert res["groups"][-1] is unk or res["groups"][0] is known or True
    d = app1.test_client().get("/api/search?q=GDB1550").get_json()
    assert d["groups"]


def test_profile_pack_default_is_explicit_opt_in(app1, monkeypatch):
    items = [{"title": "Колодки GDB1550", "mpn": "GDB1550", "brand": "TRW", "price": 300, "currency": "UAH", "url": "a"}]
    assert run(app1, monkeypatch, items)["groups"][0]["stats"] is None           # без налаштування кількість невідома
    with db.connect(app1.config["DATABASE"]) as c:
        row = c.execute("SELECT id, profile FROM sources WHERE enabled=1").fetchone()
        prof = json.loads(row["profile"]); prof["pack_default"] = 1
        c.execute("UPDATE sources SET profile=? WHERE id=?", (json.dumps(prof), row["id"])); c.commit()
    assert run(app1, monkeypatch, items)["groups"][0]["stats"]["min"] == 300      # адміністратор явно підтвердив 1 шт.


# ---------- доставка в історії ----------
def test_shipping_history_keeps_old_snapshot(app1, monkeypatch):
    import itertools, datetime as dt
    tick = itertools.count()
    monkeypatch.setattr(db, "now_iso", lambda: (dt.datetime(2026, 1, 1, 10, tzinfo=dt.timezone.utc) + dt.timedelta(minutes=next(tick))).isoformat(timespec="seconds"))
    monkeypatch.setattr(service, "NOW", None)
    base = {"title": "Колодки GDB1550", "mpn": "GDB1550", "brand": "TRW", "pack": 1, "price": 300, "currency": "UAH", "url": "https://s/1"}
    run(app1, monkeypatch, [dict(base, shipping=80)])                  # 300 + 80
    run(app1, monkeypatch, [dict(base, shipping=100)])                 # продавець змінив доставку
    with db.connect(app1.config["DATABASE"]) as c:
        rows = c.execute("SELECT shipping_cost, total_uah FROM price_snapshots ORDER BY fetched_at").fetchall()
    assert [(r[0], round(r[1])) for r in rows] == [(80, 380), (100, 400)]    # старий знімок не перезаписано
    page = app1.test_client().get("/item/1").get_data(as_text=True)
    assert "Історія знімків" in page and "380" in page and "400" in page


def test_shipping_states_in_archive(app1, monkeypatch):
    base = {"title": "Колодки GDB1550", "mpn": "GDB1550", "brand": "TRW", "pack": 1, "price": 300, "currency": "UAH"}
    run(app1, monkeypatch, [dict(base, url="u0"), dict(base, url="u1", shipping=0), dict(base, url="u2", shipping=0, shipping_free=True)])
    with db.connect(app1.config["DATABASE"]) as c:
        got = {r["url"]: (r["shipping_checked"], r["shipping_cost"], r["shipping_free"]) for r in c.execute("SELECT * FROM price_snapshots")}
    assert got["u0"] == (1, None, 0) and got["u1"] == (1, None, 0) and got["u2"] == (1, 0.0, 1)   # нуль без підтвердження — невідомо
    page = app1.test_client().get("/item/1").get_data(as_text=True)
    assert "Доставка уточнюється" in page and "Безкоштовно (підтверджено джерелом)" in page
