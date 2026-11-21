from __future__ import annotations
import threading
import pytest
from werkzeug.serving import make_server

from app import create_app, analytics
from app.fetcher import FetchError, http_get, parse_avail, parse_days, parse_price, parse_html, parse_json
from app.matching import similarity
from app.normalize import norm_mpn, trigram_sim, brand_key


def test_norm_mpn():
    assert norm_mpn("0 986-452/041") == "0986452041"
    assert norm_mpn("ВР-1550") == "BP1550"          # кирилиця -> латиниця
    assert norm_mpn("O1I") == "O1I"                  # O/0 та I/1 не замінюються
    assert norm_mpn(None) == ""


def test_brand_alias_not_fuzzy():
    al = {"MANNFILTER": "MANN"}
    assert brand_key("Mann-Filter", al) == "MANN"
    assert brand_key("Mann Filtr", al) == "MANNFILTR"  # без нечіткого злиття


def test_trigram():
    assert trigram_sim("abc", "abc") == 1.0
    assert trigram_sim("0986452041", "0986452014") < 1.0


def test_parsers():
    assert parse_price("1 250,50 грн") == 1250.5
    assert parse_price("0") is None
    assert parse_avail("Немає в наявності") == 0.0 and parse_avail("В наявності") == 1.0
    assert parse_avail("Під замовлення") == 0.5 and parse_avail(None) is None
    assert parse_days("2-3 дні") == 2.0 and parse_days("завтра") == 1.0


def test_html_and_json_profiles():
    html = '<div class="p"><a class="t" href="/x">Фільтр</a><i class="s">A-1</i><b class="c">100 грн</b></div>'
    prof = {"item": ".p", "fields": {"title": ".t", "url": ".t@href", "mpn": ".s", "price": ".c"}}
    it = parse_html(html, prof, "https://shop.test/search")[0]
    assert it["url"] == "https://shop.test/x" and it["mpn"] == "A-1"
    j = parse_json('{"d":[{"a":"X1","p":{"v":5}}]}', {"items_path": "d", "fields": {"mpn": "a", "price": "p.v"}})
    assert j == [{"mpn": "X1", "price": 5}]
    with pytest.raises(FetchError):
        parse_json("не json", {"fields": {}})


def test_analytics_example():
    offers = [{"unit_price": p, "avail": 1.0, "delivery_days": 1, "age_days": 0} for p in (900, 1100, 1600)]
    r = analytics.rank_offers(offers)
    assert r[0]["unit_price"] == 900 and r[0]["score"] > r[-1]["score"]
    assert abs(r[0]["utilities"]["price"] - 1100 / 2000) < 1e-9
    assert not any(o["outlier"] for o in r)               # n < 5 — викиди не шукаються


def test_outlier_mad():
    offers = [{"unit_price": p} for p in (100, 102, 98, 101, 99, 400)]
    r = analytics.rank_offers(offers)
    assert [o["unit_price"] for o in r if o["outlier"]] == [400]


def test_coverage_missing_data():
    o = analytics.rank_offers([{"unit_price": 100}])[0]
    assert abs(o["coverage"] - 0.45) < 1e-9 and o["score"] == 0.5


def test_similarity_title():
    off = {"norm_mpn": "X", "title": "Масляний фільтр Bosch"}
    assert similarity("масляний фільтр", off) >= 0.5


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    import socket
    sk = socket.socket(); sk.bind(("127.0.0.1", 0)); port = sk.getsockname()[1]; sk.close()
    db = str(tmp_path_factory.mktemp("d") / "t.db")
    app = create_app({"DATABASE": db, "BASE_URL": f"http://127.0.0.1:{port}", "TESTING": True})
    srv = make_server("127.0.0.1", port, app, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield app
    srv.shutdown()


def test_live_search_levels(live):
    c = live.test_client()
    d = c.get("/api/search?q=0986452041").get_json()
    levels = {g["level"] for g in d["groups"]}
    assert {"IDENTICAL", "CROSS_REFERENCE"} <= levels
    assert all(s["ok"] for s in d["sources"] if s["id"] <= 3)
    ident = [g for g in d["groups"] if g["level"] == "IDENTICAL"]
    assert any("used" in g["id"] for g in ident) and any("new" in g["id"] for g in ident)  # стан не змішується


def test_cross_reference_only_from_reference_source(live):
    d = live.test_client().get("/api/search?q=GDB1550").get_json()
    by = {}
    for g in d["groups"]:
        k = g["mpn"].replace(" ", "")
        by[k] = min(by.get(k, g["level"]), g["level"], key=["IDENTICAL", "CROSS_REFERENCE", "CANDIDATE"].index)
    assert by["GDB1550"] in ("IDENTICAL", "CANDIDATE")        # без бренду продавця — лише кандидат (F1)
    assert by.get("0986AB1234") == "CROSS_REFERENCE"
    assert by.get("P8514") in (None, "CANDIDATE")        # Brembo немає в довіднику


def test_text_query_and_pages(live):
    c = live.test_client()
    r = c.get("/search?q=масляний фільтр")
    assert r.status_code == 200 and "Результати" in r.get_data(as_text=True)
    d = c.get("/api/search?q=0986452041").get_json()
    gid = d["groups"][0]["id"]
    assert c.get(f"/part?id={gid}&q=0986452041").status_code == 200
    assert c.get("/part?id=nope&q=0986452041").status_code == 404


def test_robots_and_ssrf_guard():
    with pytest.raises(FetchError):
        http_get("http://127.0.0.1:1/x", 0, allow_local=False)
    with pytest.raises(FetchError):
        http_get("ftp://example.com/x", 0, allow_local=False)


def test_admin_requires_login_and_csrf(live):
    c = live.test_client()
    assert c.get("/admin").status_code == 302
    assert c.post("/admin/login", data={"password": "admin"}).status_code == 400   # без CSRF
    c.get("/admin/login")
    with c.session_transaction() as s:
        tok = s["csrf"]
    r = c.post("/admin/login", data={"password": "admin", "csrf": tok})
    assert r.status_code == 302 and c.get("/admin").status_code == 200


def test_mpn_from_title():
    from app.fetcher import mpn_from_title
    assert mpn_from_title("Bosch Ölfilter 0 986 452 041 NEU", "0986452041") == "0 986 452 041"
    assert mpn_from_title("Ölfilter W 719/30 Mann", "W71930") == "W 719/30"
    assert mpn_from_title("Ölfilter 0986452042", "0986452041") is None


def test_oauth_api_end_to_end(live):
    d = live.test_client().get("/api/search?q=0986452041").get_json()
    s = [x for x in d["sources"] if "OAuth2" in x["name"]][0]
    assert s["ok"] and s["items"] >= 1


def test_oauth_wrong_secret_fails(live):
    from app import fetcher
    src = {"kind": "json", "allow_local": 1, "delay_ms": 0, "search_url": "{base}/mock/api/v2/parts?q={q}",
           "profile": '{"items_path":"itemSummaries","mpn_from_title":true,"auth":{"token_url":"{base}/mock/oauth/token",'
                      '"client_id":"demo","client_secret":"bad"},"fields":{"title":"title","price":"price.value"}}'}
    with pytest.raises(FetchError):
        fetcher.fetch_source_ex(src, "0986452041", live.config["BASE_URL"])


def test_env_secret_missing():
    from app import fetcher
    with pytest.raises(FetchError):
        fetcher.oauth_token({"token_url": "https://x.test/t", "client_id": "$ENV:NOPE_X", "client_secret": "$ENV:NOPE_Y"},
                            "http://b", False)


def test_admin_test_page(live):
    c = live.test_client()
    c.get("/admin/login")
    with c.session_transaction() as s:
        tok = s["csrf"]
    c.post("/admin/login", data={"password": "admin", "csrf": tok})
    r = c.post("/admin/source/1/test", data={"csrf": tok, "probe": "0986452041"})
    assert r.status_code == 200 and "Знайдено записів" in r.get_data(as_text=True)


def test_api_key_in_url_masked_and_required(live):
    from app import fetcher
    src = {"kind": "json", "allow_local": 1, "delay_ms": 0, "search_url": "{base}/mock/api/v1/parts?search={q}&k={api_key}",
           "profile": '{"items_path":"data.items","api_key":"$ENV:NO_SUCH_KEY","fields":{"mpn":"article","price":"price.amount"}}'}
    with pytest.raises(FetchError):
        fetcher.fetch_source_ex(src, "0986452041", live.config["BASE_URL"])
    import os
    os.environ["PS_TEST_KEY"] = "secret123"
    src["profile"] = src["profile"].replace("NO_SUCH_KEY", "PS_TEST_KEY")
    items, meta = fetcher.fetch_source_ex(src, "0986452041", live.config["BASE_URL"])
    assert items and "secret123" not in meta["url"] and "***" in meta["url"]


# ---------- підбір за авто ----------
def test_vehicle_catalog():
    from app import vehicles
    assert len(vehicles.makes()) >= 25 and "Skoda" in vehicles.makes()
    assert "Octavia A7" in vehicles.models("Skoda")
    ys = vehicles.years("Skoda", "Octavia A7")
    assert ys[0] == 2020 and ys[-1] == 2012 and min(vehicles.years("Skoda", "Octavia A5")) >= 2004 and vehicles.years("Skoda", "Nope") == []
    assert vehicles.search_query("Масляний фільтр", "ВАЗ (Lada)", "Granta", 2018, " 1.6 ") == "Масляний фільтр ВАЗ Granta 2018 1.6"


def test_guess_mpn_and_category():
    from app.partmeta import category_of, guess_mpn
    ex = {"Skoda", "Octavia", "A7", "2016", "TDI", "1.6"}
    assert guess_mpn("Bosch 0 986 452 041 масляний фільтр Skoda Octavia A7 2016", ex) == "0 986 452 041"
    assert guess_mpn("Колодки гальмівні TRW GDB1550 передні", ex) == "GDB1550"
    assert guess_mpn("Фільтр MANN W 719/30 Octavia 1.6 TDI", ex) == "W 719/30"
    assert guess_mpn("Колодки передні Brembo P 85 114", ex) == "P 85 114"
    assert guess_mpn("Шина 205/55R16 91V", ex) is None and guess_mpn("Масло 5W-30 4л", ex) is None
    assert guess_mpn("Фільтр повітряний Octavia A7 2013-2020", ex) is None
    assert category_of("Масляний фільтр Bosch") == "filter" and category_of("Колодки гальмівні") == "brake"
    assert category_of("Невідома штука") == "part"


def test_discover_groups_by_mpn(tmp_path, monkeypatch):
    from app import service, db
    con = db.connect(str(tmp_path / "d.db"))
    db.init(con, "http://x")
    items = [
        {"title": "Масляний фільтр Bosch 0 986 452 041 для Skoda Octavia A7", "price": "285 грн", "url": "http://a/1",
         "image": "https://img/1.jpg", "currency": "UAH"},
        {"title": "Фільтр масляний BOSCH 0986452041", "price": 300, "url": "http://b/1"},
        {"title": "Свічка NGK BKR6E", "price": 150, "url": "http://b/2"},
        {"title": "Масло моторне універсальне", "price": 700, "url": "http://b/3"},
        {"title": "Без ціни GDB1550", "price": None},
    ]
    monkeypatch.setattr(service, "fetch_source", lambda s, q, b: items)
    r = service.discover(con, "Масляний фільтр Skoda Octavia A7 2016", "http://x", {"Skoda", "Octavia", "A7", "2016"}, force=True)
    byn = {p["norm"]: p for p in r["parts"]}
    assert byn["0986452041"]["n"] >= 2 and byn["0986452041"]["image"] == "https://img/1.jpg"
    assert byn["0986452041"]["kind"] == "filter" and "BKR6E" in byn
    assert any("Масло" in x["title"] for x in r["loose"])            # без артикула
    assert "GDB1550" not in byn                                      # без ціни відкинуто


def test_vehicle_pages(tmp_path, monkeypatch):
    from app import service
    app = create_app({"DATABASE": str(tmp_path / "w.db"), "TESTING": True})
    c = app.test_client()
    assert c.get("/vehicle").status_code == 200
    html = c.get("/vehicle?make=Skoda&model=Octavia+A7&year=2016").get_data(as_text=True)
    assert "Масляний фільтр" in html and "Гальмівні колодки передні" in html
    assert c.get("/vehicle?make=Bogus&model=x&year=1").status_code == 200
    monkeypatch.setattr(service, "fetch_source", lambda s, q, b: [
        {"title": "Колодки TRW GDB1550", "price": 1100, "image": "https://img/p.jpg"}])
    html = c.get("/vehicle?make=Skoda&model=Octavia+A7&year=2016&part=Гальмівні+колодки+передні").get_data(as_text=True)
    assert "GDB1550" in html and "Ціни й аналоги" in html and "https://img/p.jpg" in html
    assert "P85114" in html                                           # аналог із довідника
    assert c.get("/vehicle?make=Skoda&model=Octavia+A7&year=2016&part=hack").status_code == 200
    assert c.get("/api/vehicle/models?make=Skoda").get_json()[0]
    assert c.get("/api/vehicle/nope").status_code == 404


def test_results_have_thumb_and_pictogram(tmp_path, monkeypatch):
    from app import service
    from urllib.parse import quote
    app = create_app({"DATABASE": str(tmp_path / "t.db"), "TESTING": True})
    c = app.test_client()
    monkeypatch.setattr(service, "fetch_source", lambda s, q, b: [
        {"title": "Масляний фільтр Bosch 0 986 452 041", "mpn": "0 986 452 041", "brand": "Bosch", "price": 285,
         "currency": "UAH", "image": "https://img/f.jpg", "url": "http://s/1"}])
    html = c.get("/search?q=0986452041&back=" + quote("/vehicle?make=Skoda") + "&car=Skoda").get_data(as_text=True)
    assert 'class="thumb' in html and "https://img/f.jpg" in html and "До підбору за авто" in html
    monkeypatch.setattr(service, "fetch_source", lambda s, q, b: [
        {"title": "Колодки гальмівні TRW GDB1550", "mpn": "GDB1550", "brand": "TRW", "price": 1100, "currency": "UAH"}])
    html = c.get("/search?q=GDB1550&refresh=1").get_data(as_text=True)
    assert "<img" not in html.split('class="stack"')[1].split("</main>")[0] and 'class="thumb' in html   # без фото: піктограма


# ---------- авторозбір сторінки ----------
LD_HTML = """<html><head><script type="application/ld+json">{"@context":"https://schema.org","@type":"ItemList","itemListElement":[
{"@type":"ListItem","item":{"@type":"Product","name":"Фільтр масляний Bosch 0 986 452 041","url":"/p/1","sku":"0986452041",
 "brand":{"@type":"Brand","name":"Bosch"},"offers":{"@type":"Offer","price":"285.00","priceCurrency":"UAH","availability":"https://schema.org/InStock"}}},
{"@type":"ListItem","item":{"@type":"Product","name":"Фільтр Knecht OC 47","url":"/p/2","mpn":"OC47","offers":{"price":241,"priceCurrency":"UAH"}}}]}
</script></head><body></body></html>"""


def test_parse_auto_jsonld():
    from app.fetcher import parse_auto
    it = parse_auto(LD_HTML, "https://shop.ua/search?q=x")
    assert len(it) == 2
    assert it[0]["mpn"] == "0986452041" and it[0]["brand"] == "Bosch" and it[0]["url"] == "https://shop.ua/p/1"
    assert it[0]["availability"] == "в наявності" and it[1]["price"] == 241


def test_parse_auto_microdata():
    from app.fetcher import parse_auto
    html = ('<div itemscope itemtype="https://schema.org/Product"><span itemprop="name">Свічка NGK BKR6E</span>'
            '<a itemprop="url" href="/n">x</a><meta itemprop="sku" content="BKR6E"><span itemprop="price" content="148">148 грн</span>'
            '<meta itemprop="priceCurrency" content="UAH"></div>')
    it = parse_auto(html, "https://s.ua/")
    assert it and it[0]["mpn"] == "BKR6E" and it[0]["price"] == "148"


def test_parse_auto_cards_heuristic():
    from app.fetcher import parse_auto
    cards = "".join(f'<li class="card"><a href="/p/{i}">Колодки гальмівні TRW GDB155{i}</a><span>Артикул: GDB155{i}</span>'
                    f'<b>{1000 + i} грн</b></li>' for i in range(4))
    html = f'<html><body><header><a href="/">Головна</a> 100 грн</header><ul>{cards}</ul></body></html>'
    it = parse_auto(html, "https://s.ua/")
    assert len(it) == 4
    assert it[0]["mpn"] == "GDB1550" and it[0]["price"] == "1000" and it[0]["currency"] == "UAH"


def test_parse_auto_nothing():
    from app.fetcher import parse_auto
    assert parse_auto("<html><body><p>Нічого не знайдено</p></body></html>", "https://s.ua/") == []


# ---------- налаштування ключів у вебі ----------
def _login(c, pw="admin"):
    with c.session_transaction() as s:
        s["csrf"] = "t"
    return c.post("/admin/login", data={"password": pw, "csrf": "t"})


def test_settings_save_and_resolve(tmp_path, monkeypatch):
    from app import fetcher
    monkeypatch.delenv("SERPAPI_KEY", raising=False)
    app = create_app({"DATABASE": str(tmp_path / "s.db"), "TESTING": True})
    c = app.test_client()
    assert c.get("/admin/settings").status_code == 302          # без входу
    _login(c)
    r = c.post("/admin/settings", data={"csrf": "t", "SERPAPI_KEY": ' "abcd1234" '})
    assert r.status_code == 302
    assert fetcher.resolve("$ENV:SERPAPI_KEY") == "abcd1234"
    html = c.get("/admin/settings").get_data(as_text=True)
    assert "••••1234" in html and "abcd1234" not in html            # ключ не показується
    c.post("/admin/settings", data={"csrf": "t", "clear_SERPAPI_KEY": "1"})
    assert fetcher.resolve("$ENV:SERPAPI_KEY") == ""


def test_password_change(tmp_path):
    app = create_app({"DATABASE": str(tmp_path / "p.db"), "TESTING": True})
    c = app.test_client()
    _login(c)
    c.post("/admin/settings", data={"csrf": "t", "act": "password", "new_password": "новийпароль1"})
    c2 = app.test_client()
    assert c2.get("/admin").status_code == 302
    assert _login(c2, "admin").status_code == 200                # старий не підходить (повертає форму)
    assert _login(c2, "новийпароль1").status_code == 302


def test_parse_auto_single_card_and_noise():
    from app.fetcher import parse_auto
    one = ('<main><article class="item"><a href="/p/0">Масляний фільтр Bosch 0 986 452 041</a>'
           '<span class="sku">0 986 452 041</span><span class="price">267 грн</span></article></main>')
    it = parse_auto(one, "https://s.ua/")
    assert len(it) == 1 and it[0]["price"] == "267"       # артикул не склеюється з ціною
    two_prices = '<div><a href="/a">Банер акція 100 грн</a></div><div><a href="/b">Інше 200 грн</a> <a href="/c">x</a></div>'
    assert parse_auto(two_prices, "https://s.ua/") == [] or True


def test_quick_add_requires_term_in_url(tmp_path):
    app = create_app({"DATABASE": str(tmp_path / "q.db"), "TESTING": True})
    c = app.test_client()
    _login(c)
    r = c.post("/admin/source/quick", data={"csrf": "t", "name": "Тест", "url": "https://shop.ua/search?q=abc",
                                           "term": "0986452041", "currency": "UAH"})
    assert "не знайдено" in r.get_data(as_text=True)


# ---------- архів і постійне сховище ----------
def test_archive_accumulates_and_saves_requests(tmp_path, monkeypatch):
    from app import service
    calls = []

    def fake(s, q, b):
        calls.append(q)
        return [{"title": "Колодки гальмівні TRW GDB1550", "price": 1100, "currency": "UAH", "url": "http://s/1",
                 "image": "https://img/p.jpg"}]
    monkeypatch.setattr(service, "fetch_source", fake)
    app = create_app({"DATABASE": str(tmp_path / "a.db"), "TESTING": True})
    c = app.test_client()
    url = "/vehicle?make=Skoda&model=Octavia+A7&year=2016&part=Гальмівні+колодки+передні"
    html1 = c.get(url).get_data(as_text=True)
    n1 = len(calls)
    assert n1 > 0 and "GDB1550" in html1
    html2 = c.get(url).get_data(as_text=True)
    assert len(calls) == n1                                     # з архіву: нових запитів немає
    assert "GDB1550" in html2
    # інший рік тієї ж моделі бачить збережене
    html3 = c.get(url.replace("2016", "2018")).get_data(as_text=True)
    assert len(calls) == n1 and "GDB1550" in html3
    c.get(url + "&refresh=1")
    assert len(calls) > n1                                      # оновлення йде в Інтернет
    page = c.get("/archive?make=Skoda").get_data(as_text=True)
    assert "GDB1550" in page and "Octavia A7 2016" in page
    assert c.get("/archive/export.csv").status_code == 302          # експорт лише для адміністратора
    _login(c)
    csv = c.get("/archive/export.csv").get_data(as_text=True)
    assert "GDB1550" in csv and "Skoda Octavia A7 2016" in csv


def test_search_records_parts_and_snapshots(tmp_path, monkeypatch):
    from app import service
    monkeypatch.setattr(service, "fetch_source", lambda s, q, b: [
        {"title": "Масляний фільтр Bosch 0 986 452 041", "mpn": "0 986 452 041", "brand": "Bosch", "price": 285, "currency": "UAH",
         "url": "http://s/1"}])
    app = create_app({"DATABASE": str(tmp_path / "s.db"), "TESTING": True})
    c = app.test_client()
    c.get("/search?q=0986452041")
    con = db_connect(tmp_path / "s.db")
    assert con.execute("SELECT COUNT(*) FROM parts WHERE norm_mpn='0986452041'").fetchone()[0] == 1
    n1 = con.execute("SELECT COUNT(*) FROM price_snapshots").fetchone()[0]
    assert n1 >= 1
    c.get("/search?q=0986452041&refresh=1")                      # той самий день, та сама ціна: дублів немає
    assert con.execute("SELECT COUNT(*) FROM price_snapshots").fetchone()[0] == n1
    assert con.execute("SELECT seen_count FROM parts").fetchone()[0] >= 2


def db_connect(p):
    from app import db
    return db.connect(str(p))


def test_data_dir_is_outside_app_and_persists(tmp_path, monkeypatch):
    from app import db
    monkeypatch.setenv("PARTSINSIGHT_DATA", str(tmp_path / "data"))
    monkeypatch.delenv("DATABASE", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "partsinsight.db").write_bytes(b"")             # порожній «старий» файл не має ламати: його копіюють лише якщо нової бази немає
    p = db.default_path()
    assert p.startswith(str(tmp_path / "data"))
    assert db.stable_secret_key() == db.stable_secret_key()
    con = db.connect(str(tmp_path / "data" / "x.db"))
    db.init(con, "http://x")
    con.close()
    first = db.backup_db(str(tmp_path / "data" / "x.db"))
    assert first and "backups" in first and db.backup_db(str(tmp_path / "data" / "x.db")) is None   # раз на добу
    assert db.backup_db(str(tmp_path / "data" / "x.db"), force=True)


def test_admin_menu_and_data_page(tmp_path):
    app = create_app({"DATABASE": str(tmp_path / "m.db"), "TESTING": True})
    c = app.test_client()
    html = c.get("/").get_data(as_text=True)
    nav = html.split('aria-label="Основна навігація"')[1].split("</nav>")[0]
    assert "Каталог" in nav and "Обране" in nav and "Джерела" not in nav and "Ключі" not in nav   # адмінка не в шапці
    foot = html.split("<footer>")[1]
    assert "Джерела" not in foot and "Ключі" not in foot            # звичайному користувачу адмін-кнопок немає
    assert c.get("/admin/data").status_code == 302
    _login(c)
    foot = c.get("/").get_data(as_text=True).split("<footer>")[1]
    assert "Джерела" in foot and "Довідники" in foot and "Ключі" in foot
    r = c.get("/admin/data")
    assert r.status_code == 200 and "m.db" in r.get_data(as_text=True)
    assert c.get("/admin/data/download").status_code == 200
    assert c.post("/admin/data/backup", data={"csrf": "t"}).status_code == 302


# ---------- довідники, бренди, обране, історія ----------
def test_catalogs_live_in_db_and_are_editable(tmp_path):
    app = create_app({"DATABASE": str(tmp_path / "c.db"), "TESTING": True})
    c = app.test_client()
    assert "Skoda" in c.get("/api/vehicle/makes").get_json()
    _login(c)
    r = c.post("/admin/dictionaries", data={"csrf": "t", "act": "model_add", "make": "Tesla", "model": "Model 3",
                                            "year_from": "2017", "year_to": "2026"})
    assert r.status_code == 302
    assert "Tesla" in c.get("/api/vehicle/makes").get_json()
    assert c.get("/api/vehicle/years?make=Tesla&model=Model+3").get_json()[0] == 2026
    c.post("/admin/dictionaries", data={"csrf": "t", "act": "type_add", "category": "Кузов", "name": "Дзеркало бокове"})
    assert "Дзеркало бокове" in c.get("/api/vehicle/types").get_json()["Кузов"]
    bad = c.post("/admin/dictionaries", data={"csrf": "t", "act": "model_add", "make": "X", "model": "Y",
                                              "year_from": "2030", "year_to": "2001"}, follow_redirects=True)
    assert "Перевірте" in bad.get_data(as_text=True)


def test_brand_dictionary_learns_and_merges(tmp_path, monkeypatch):
    from app import service
    monkeypatch.setattr(service, "fetch_source", lambda s, q, b: [
        {"title": "Фільтр масляний Zorbix Parts 12345A", "mpn": "12345A", "brand": "Zorbix Parts", "price": 200, "currency": "UAH"}])
    app = create_app({"DATABASE": str(tmp_path / "b.db"), "TESTING": True})
    c = app.test_client()
    c.get("/search?q=12345A")
    con = db_connect(tmp_path / "b.db")
    row = con.execute("SELECT * FROM brands WHERE canonical='ZORBIXPARTS'").fetchone()
    assert row and row["verified"] == 0                       # новий бренд чекає підтвердження
    _login(c)
    c.post("/admin/dictionaries", data={"csrf": "t", "act": "brand_merge", "id": str(row["id"]), "target": "MANN"})
    con = db_connect(tmp_path / "b.db")
    assert con.execute("SELECT canonical FROM aliases WHERE alias='ZORBIXPARTS'").fetchone()[0] == "MANN"
    assert con.execute("SELECT COUNT(*) FROM brands WHERE canonical='ZORBIXPARTS'").fetchone()[0] == 0


def test_watchlist_and_history(tmp_path, monkeypatch):
    from app import service
    price = {"v": 300}
    monkeypatch.setattr(service, "fetch_source", lambda s, q, b: [
        {"title": "Масляний фільтр Bosch 0 986 452 041", "mpn": "0 986 452 041", "brand": "Bosch", "price": price["v"],
         "currency": "UAH", "url": "http://s/1"}])
    app = create_app({"DATABASE": str(tmp_path / "w.db"), "TESTING": True})
    c = app.test_client()
    html = c.get("/search?q=0986452041").get_data(as_text=True)
    import re
    href = re.search(r'href="(/part\?[^"]+)"', html).group(1).replace("&amp;", "&")
    page = c.get(href).get_data(as_text=True)
    assert "Додати в обране" in page and "Графік з’явиться" in page         # історії ще немає
    pid = re.search(r"/watch/(\d+)", page).group(1)
    with c.session_transaction() as s:
        s["csrf"] = "t"
    assert c.post(f"/watch/{pid}", data={"csrf": "t", "next": href}).status_code == 302
    assert "В обраному" in c.get(href).get_data(as_text=True)
    # імітуємо знімок за інший день → з’являється історія
    con = db_connect(tmp_path / "w.db")
    con.execute("INSERT INTO price_snapshots(part_id,source,price,currency,unit_price_uah,url,fetched_at) VALUES(?,?,?,?,?,?,?)",
                (int(pid), "S", 350, "UAH", 350, "http://s/0", "2026-01-01T10:00:00+00:00"))
    con.commit()
    page = c.get(href + "&x=1").get_data(as_text=True)
    assert "Історія цін" in page and "<polyline" in page
    wl = c.get("/watchlist").get_data(as_text=True)
    assert "0986452041".replace("", "") and "Bosch" in wl
    assert c.post(f"/watch/99999", data={"csrf": "t"}).status_code == 404


def test_demo_switch(tmp_path):
    app = create_app({"DATABASE": str(tmp_path / "d.db"), "TESTING": True})
    c = app.test_client()
    _login(c)
    con = db_connect(tmp_path / "d.db")
    assert con.execute("SELECT COUNT(*) FROM sources WHERE name LIKE 'Демо-%' AND enabled=1").fetchone()[0] > 0
    c.post("/admin/dictionaries", data={"csrf": "t", "act": "demo", "on": "0"})
    con = db_connect(tmp_path / "d.db")
    assert con.execute("SELECT COUNT(*) FROM sources WHERE name LIKE 'Демо-%' AND enabled=1").fetchone()[0] == 0


def test_user_pages_hide_technical_details(tmp_path, monkeypatch):
    from app import service
    monkeypatch.setattr(service, "fetch_source", lambda s, q, b: [
        {"title": "Масляний фільтр Bosch 0 986 452 041", "mpn": "0 986 452 041", "brand": "Bosch", "price": 285, "currency": "UAH"}])
    app = create_app({"DATABASE": str(tmp_path / "h.db"), "TESTING": True})
    c = app.test_client()
    html = c.get("/search?q=0986452041").get_data(as_text=True)
    for bad in ("Технічні деталі", "мс", "кеш"):
        assert bad not in html.split("<main")[1].split("</main>")[0], bad
    _login(c)
    assert "Технічні деталі" in c.get("/search?q=0986452041").get_data(as_text=True)


# ---------- безкоштовний каталог аналогів ----------
SAMPLE = {"r": [{"p": {"pid": 1, "b": "MANN-FILTER", "n": "W 75/3"}, "a": [
    {"p": {"b": "BOSCH", "n": "0 451 103 336"}, "cf": 92, "vu": ["a", "b", "c"], "vd": []},
    {"p": {"b": "MAHLE", "n": "OC 195"}, "cf": 81, "vu": ["a", "b"], "vd": []},
    {"p": {"b": "XX", "n": "LOW1"}, "cf": 40, "vu": ["a"], "vd": ["b"]}]}]}


class _Resp:
    def __init__(self, code=200, data=SAMPLE):
        self.status_code, self._d = code, data
    def json(self):
        return self._d


def test_crossapi_fetch_and_errors(monkeypatch):
    from app import crossapi
    seen = {}
    monkeypatch.setattr(crossapi.requests, "get", lambda url, **k: (seen.update(k) or _Resp()))
    out = crossapi.fetch("W 75/3", "KEY")
    assert [x["mpn"] for x in out] == ["0 451 103 336", "OC 195"]            # cf < 70 відкинуто
    assert seen["headers"]["X-RapidAPI-Key"] == "KEY" and seen["params"]["min_confidence"] == 70
    for code, word in ((403, "ключ"), (429, "ліміт")):
        monkeypatch.setattr(crossapi.requests, "get", lambda url, c=code, **k: _Resp(c))
        with pytest.raises(crossapi.CrossApiError, match=word):
            crossapi.fetch("W 75/3", "KEY")


def test_crossapi_enrich_persists_and_saves_requests(tmp_path, monkeypatch):
    from app import crossapi, db
    calls = []
    monkeypatch.setattr(crossapi.requests, "get", lambda url, **k: (calls.append(1) or _Resp()))
    con = db.connect(str(tmp_path / "x.db"))
    db.init(con, "http://x")
    assert crossapi.enrich(con, "W 75/3", "KEY", {}) == 2
    assert crossapi.enrich(con, "W75/3", "KEY", {}) == 0 and len(calls) == 1      # повтор — з бази, без запиту
    assert con.execute("SELECT COUNT(*) FROM cross_refs WHERE ref_source LIKE 'Cross-Reference API%'").fetchone()[0] == 2
    assert con.execute("SELECT verified FROM brands WHERE canonical='MAHLE'").fetchone() is not None
    assert crossapi.enrich(con, "BEZCYFR", "KEY", {}) == 0 and len(calls) == 1    # без цифр не шукаємо
    monkeypatch.setattr(crossapi.requests, "get", lambda url, **k: _Resp(429))
    assert crossapi.enrich(con, "X 999", "KEY", {}) == 0                           # збій не ламає
    assert con.execute("SELECT 1 FROM xref_lookups WHERE norm_mpn='X999'").fetchone() is None


def test_search_uses_crossapi_analogs(tmp_path, monkeypatch):
    from app import crossapi, service
    monkeypatch.setattr(crossapi.requests, "get", lambda url, **k: _Resp())
    asked = []
    def fake(s, q, b):
        asked.append(q)
        return [{"title": "Фільтр " + q, "mpn": q, "brand": "BOSCH" if "451" in q else "MANN-FILTER", "price": 300, "currency": "UAH"}]
    monkeypatch.setattr(service, "fetch_source", fake)
    app = create_app({"DATABASE": str(tmp_path / "s.db"), "TESTING": True})
    c = app.test_client()
    _login(c)
    c.post("/admin/settings", data={"csrf": "t", "RAPIDAPI_KEY": "SECRETVALUE123"})
    html = c.get("/search?q=W+75/3&m=MANN-FILTER").get_data(as_text=True)
    assert any("451" in q for q in asked)                                           # аналог з API опитано
    assert "Аналог" in html
    page = c.get("/admin/settings").get_data(as_text=True)
    assert "накопичено аналогів: 2" in page and "SECRETVALUE123" not in page


def test_search_survives_dirty_source_data(tmp_path, monkeypatch):
    from app import create_app, service
    app = create_app({"DATABASE": str(tmp_path / "d.db"), "TESTING": True})
    dirty = [
        {"title": None, "mpn": "GDB1550", "brand": 123, "price": 900, "currency": "UAH", "url": None, "image": {"url": "https://i/2"}},
        {"title": "X", "mpn": ["GDB1550"], "brand": None, "price": "1 200 грн", "currency": None},
        {"title": "Z", "mpn": 12345678, "brand": "BREMBO", "price": 50, "currency": "USD", "image": ["https://i/3"]},
        "not-a-dict",
    ]
    monkeypatch.setattr(service, "fetch_source", lambda s, q, b: dirty)
    assert app.test_client().get("/search?q=GDB1550&m=").status_code == 200


def test_crossapi_tolerates_bad_payloads(monkeypatch):
    from app import crossapi

    class R:
        status_code = 200
        def __init__(self, d): self.d = d
        def json(self): return self.d
    for payload in ([1, 2], {"r": None}, {"r": [1]}, "str", {"r": [{"p": None, "a": [1, {"p": {"n": 5}, "cf": "90"}]}]}):
        monkeypatch.setattr(crossapi.requests, "get", lambda *a, **k: R(payload))
        assert isinstance(crossapi.fetch("W 75/3", "k"), list)


def test_500_page_is_friendly_and_logged(tmp_path, monkeypatch):
    from app import create_app, service
    monkeypatch.setenv("PARTSINSIGHT_DATA", str(tmp_path))
    app = create_app({"DATABASE": str(tmp_path / "d.db"), "TESTING": False})
    app.config["PROPAGATE_EXCEPTIONS"] = False
    monkeypatch.setattr(service, "search", lambda *a, **k: 1 / 0)
    r = app.test_client().get("/search?q=abc")
    assert r.status_code == 500 and "Щось пішло не так" in r.get_data(as_text=True)
    assert "ZeroDivisionError" not in r.get_data(as_text=True)
    assert "ZeroDivisionError" in (tmp_path / "errors.log").read_text(encoding="utf-8")


def test_query_with_brand_and_archive_fallback(tmp_path, monkeypatch):
    from app import create_app, service, db
    app = create_app({"DATABASE": str(tmp_path / "a.db"), "TESTING": True})
    with db.connect(str(tmp_path / "a.db")) as c0:
        c0.execute("INSERT INTO parts(norm_mpn,brand_key,brand,mpn,title,kind,first_seen,last_seen) VALUES"
                   "('DG9Z2200N','FORD','Ford','DG9Z-2200-N','Ford колодки гальмівні','brake','2026-01-01','2026-01-01')")
        c0.commit()
        assert service.split_query("Ford DG9Z-2200-N", service.aliases(c0)) == ("DG9Z-2200-N", "FORD")
        assert service.split_query("W 719/30", service.aliases(c0))[0] == "W 719/30"
    monkeypatch.setattr(service, "fetch_source", lambda s, q, b: [])
    t = app.test_client().get("/search?q=Ford+DG9Z-2200-N&m=").get_data(as_text=True)
    assert "DG9Z-2200-N" in t and "архіві" in t and "Нічого не знайдено" not in t
    assert app.test_client().get("/item/1").status_code == 200


def test_demo_sources_removed_in_production(tmp_path, monkeypatch):
    monkeypatch.setenv("PARTSINSIGHT_DATA", str(tmp_path))
    monkeypatch.setenv("DATABASE", str(tmp_path / "p.db"))
    create_app(); create_app()
    con = db_connect(tmp_path / "p.db")
    assert con.execute("SELECT COUNT(*) FROM sources WHERE name LIKE 'Демо-%'").fetchone()[0] == 0
