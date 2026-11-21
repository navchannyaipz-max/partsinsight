from __future__ import annotations
import json
import os
import secrets
import time
from datetime import datetime, timezone
from functools import wraps

from urllib.parse import quote, quote_plus

from werkzeug.security import check_password_hash, generate_password_hash
import sqlite3
from flask import (Flask, Response, send_file, abort, flash, g, jsonify, redirect, render_template, request, session, url_for)

from . import analytics, archive, db, fetcher, service, vehicles
from .normalize import alias_key


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.update(
        DATABASE=os.environ.get("DATABASE") or (test_config or {}).get("DATABASE") or db.default_path(),
        BASE_URL=os.environ.get("BASE_URL", "http://127.0.0.1:5000"),
        ADMIN_PASSWORD=os.environ.get("ADMIN_PASSWORD", "admin"),
    )
    if test_config:
        app.config.update(test_config)
    app.config["SECRET_KEY"] = (os.environ.get("SECRET_KEY") or (test_config or {}).get("SECRET_KEY")
                                or (secrets.token_hex(16) if test_config else db.stable_secret_key()))

    with db.connect(app.config["DATABASE"]) as con:
        db.init(con, app.config["BASE_URL"])

    def load_settings():
        with db.connect(app.config["DATABASE"]) as c0:
            fetcher.set_secrets({r["key"]: r["value"] for r in c0.execute("SELECT key,value FROM settings")})

    load_settings()
    if not test_config and os.environ.get("PARTSINSIGHT_DEMO") != "1":
        with db.connect(app.config["DATABASE"]) as c0:      # робоча установка: демонстраційних даних немає
            ids = [r[0] for r in c0.execute("SELECT id FROM sources WHERE name LIKE 'Демо-%'")]
            for i in ids:
                c0.execute("DELETE FROM cache WHERE source_id=?", (i,))
                c0.execute("DELETE FROM sources WHERE id=?", (i,))
            c0.execute("DELETE FROM cross_refs WHERE ref_source LIKE 'Демонстраційний%'")
            c0.commit()
    if not test_config:                       # щодобова копія бази поруч із нею
        try:
            db.backup_db(app.config["DATABASE"])
        except Exception:
            pass

    from .mock_web import bp as mock_bp
    app.register_blueprint(mock_bp)

    def con():
        if "con" not in g:
            g.con = db.connect(app.config["DATABASE"])
        return g.con

    @app.teardown_appcontext
    def _close(_):
        c = g.pop("con", None)
        if c:
            c.close()

    # ---------- допоміжне ----------
    def csrf_token() -> str:
        if "csrf" not in session:
            session["csrf"] = secrets.token_hex(16)
        return session["csrf"]

    @app.before_request
    def _csrf():
        if request.method == "POST" and not request.path.startswith("/mock"):
            tok = session.get("csrf")
            if not tok or not secrets.compare_digest(request.form.get("csrf", ""), tok):
                abort(400, "CSRF")

    _hits: dict = {}

    @app.before_request
    def _rate_limit():
        """Захист квот платних API від зловживань: не більше 40 запитів пошуку на хвилину з однієї адреси."""
        if app.config.get("TESTING") or request.endpoint not in ("search", "part", "vehicle_pick", "api_search", "compare"):
            return None
        now = time.time()
        q = [t for t in _hits.get(request.remote_addr, []) if now - t < 60]
        if len(q) >= 40:
            abort(429)
        q.append(now)
        _hits[request.remote_addr] = q
        return None

    def admin_required(f):
        @wraps(f)
        def w(*a, **k):
            if not session.get("admin"):
                return redirect(url_for("login", next=request.path))
            return f(*a, **k)
        return w

    @app.context_processor
    def _ctx():
        def brand_site(brand):
            from .partmeta import BRAND_SITES
            from .normalize import brand_key as bk
            return BRAND_SITES.get(bk(brand or "", service.aliases(con())), "")
        return {"csrf": csrf_token, "is_admin": session.get("admin", False), "brand_site": brand_site}

    @app.template_filter("money")
    def money(v):
        return f"{v:,.0f}".replace(",", " ") if v is not None else "—"

    @app.template_filter("ago")
    def ago(iso):
        if not iso:
            return "—"
        try:
            dt = datetime.fromisoformat(iso)
        except (TypeError, ValueError):
            return "невідомо"
        if dt.tzinfo is None:                     # старі записи можуть бути без часового поясу
            dt = dt.replace(tzinfo=timezone.utc)
        s = (datetime.now(timezone.utc) - dt).total_seconds()
        if s < 60:
            return "щойно"
        if s < 3600:
            return f"{int(s // 60)} хв тому"
        if s < 86400:
            return f"{int(s // 3600)} год тому"
        return f"{int(s // 86400)} дн тому"

    LEVELS = {
        "IDENTICAL": ("За артикулом", "Збігається нормалізований артикул"),
        "CROSS_REFERENCE": ("Аналоги", "Пара деталей із довідника аналогів"),
        "CANDIDATE": ("Можливі збіги", "Схожі артикул або назва, потребує перевірки"),
    }
    BADGES = {"IDENTICAL": "За артикулом", "CROSS_REFERENCE": "Аналог за довідником", "CANDIDATE": "Можливий збіг"}
    app.jinja_env.globals["LEVELS"] = LEVELS
    app.jinja_env.globals["BADGES"] = BADGES
    ORIGINS = {"live": "живий пошук", "cache": "кеш", "stale": "збережені дані (оновлення не вдалося)", "archive": "архів"}
    app.jinja_env.globals["ORIGINS"] = ORIGINS
    STATES = {"live": "оновлено", "cache": "з кешу", "empty": "відповіло, збігів немає", "stale": "недоступне, показано збережене",
              "error": "недоступне"}
    app.jinja_env.globals["STATES"] = STATES

    # ---------- публічні сторінки ----------
    @app.get("/")
    def index():
        recent = con().execute(
            "SELECT q, MAX(at) at FROM searches GROUP BY q ORDER BY at DESC LIMIT 6").fetchall()
        n = con().execute("SELECT COUNT(*) FROM sources WHERE enabled=1").fetchone()[0]
        return render_template("index.html", recent=recent, n_sources=n)

    def run_search(q, m, force=False):
        return service.search(con(), q, app.config["BASE_URL"], m or None, force)

    @app.get("/search")
    def search():
        q = request.args.get("q", "").strip()
        if len(q) < 2:
            return redirect(url_for("index"))
        m = request.args.get("m", "").strip()
        res = run_search(q, m, force=request.args.get("refresh") == "1")
        level = request.args.get("level", "")
        sort = request.args.get("sort", "")
        stock = request.args.get("stock") == "1"
        counts = {k: sum(1 for x in res["groups"] if x["level"] == k) for k in LEVELS}
        groups = [x for x in res["groups"] if not level or x["level"] == level]
        if stock:
            groups = [x for x in groups if any(o["avail"] for o in x["offers"])]
        if sort == "price":
            groups.sort(key=lambda x: x["stats"]["min"] if x["stats"] else float("inf"))
        elif sort == "score":
            groups.sort(key=lambda x: -(x["best"]["score"] or 0))
        back = request.args.get("back", "")
        back = back if back.startswith("/vehicle?") else ""
        return render_template("results.html", res=res, groups=groups, counts=counts, level=level, m=m,
                               sort=sort, stock=stock, back=back, car=request.args.get("car", "")[:80])

    def _ctx_args(a):
        """Стан сторінки результатів, який переноситься в деталі й назад (запит, виробник, рівень, сортування, наявність)."""
        back = a.get("back", "")
        return {"q": a.get("q", ""), "m": a.get("m", "") or None, "level": a.get("level") or None,
                "sort": a.get("sort") or None, "stock": 1 if a.get("stock") == "1" else None,
                "back": back if back.startswith("/vehicle?") else None, "car": (a.get("car") or "")[:80] or None}

    def _breakdown(o):
        if not o or o.get("score") is None:
            return None
        u = o["utilities"]
        wsum = sum(analytics.WEIGHTS[k] for k in u)
        names = {"price": "Ціна", "avail": "Наявність", "delivery": "Строк доставки", "fresh": "Свіжість"}
        rows = []
        for k, w in analytics.WEIGHTS.items():
            if k in u:
                eff = w / wsum
                rows.append({"label": names[k], "w": w, "eff": eff, "u": u[k], "part": eff * u[k]})
            else:
                rows.append({"label": names[k], "w": w, "eff": 0.0, "u": None, "part": None})
        return rows

    @app.get("/part")
    def part():
        a = request.args
        q, m, pid = a.get("q", ""), a.get("m", ""), a.get("id", "")
        res = run_search(q, m)
        grp = next((x for x in res["groups"] if x["id"] == pid), None)
        if not grp:
            abort(404)
        others = [x for x in res["groups"] if x["id"] != pid][:4]
        c_ = con()
        db_pid = archive.part_id(c_, grp["key"][1], grp["key"][0])
        hist = archive.history(c_, db_pid) if db_pid else []
        ctx = _ctx_args(a)
        sel = next((o for o in grp["offers"] if o["oid"] == a.get("offer")), None) or grp["offers"][0]
        return render_template("part.html", g=grp, res=res, q=q, m=m, others=others, strip=_strip(grp),
                               db_pid=db_pid, hist=hist, hchart=_hist_chart(hist), ctx=ctx, sel=sel, bd=_breakdown(sel),
                               back_url=url_for("search", **ctx),
                               watched=db_pid in archive.watched_ids(c_) if db_pid else False)

    @app.get("/compare")
    def compare():
        a = request.args
        q, m, pid = a.get("q", ""), a.get("m", ""), a.get("id", "")
        res = run_search(q, m)
        grp = next((x for x in res["groups"] if x["id"] == pid), None)
        if not grp:
            abort(404)
        ids = list(dict.fromkeys(a.getlist("o")))[:3]
        chosen = [o for o in grp["offers"] if o["oid"] in ids]
        chosen.sort(key=lambda o: ids.index(o["oid"]))
        ctx = _ctx_args(a)
        part_url = url_for("part", id=pid, **ctx)
        if len(chosen) < 2:
            flash("Для порівняння оберіть щонайменше двох продавців.", "error")
            return redirect(part_url)
        known = [o["unit_price"] for o in chosen if o["unit_price"] is not None]
        low = min(known) if known else None
        for o in chosen:
            o["is_low"] = low is not None and o["unit_price"] is not None and abs(o["unit_price"] - low) < 0.005
        def without(oid):
            return url_for("compare", id=pid, o=[x for x in ids if x != oid], **ctx)
        return render_template("compare.html", g=grp, chosen=chosen, part_url=part_url, without=without, ctx=ctx)

    @app.get("/item/<int:pid>")
    def item_page(pid):
        c_ = con()
        it = archive.item(c_, pid)
        if not it:
            abort(404)
        hist = archive.history(c_, pid)
        return render_template("item.html", it=it, hist=hist, hchart=_hist_chart(hist),
                               analogs=service.analogs_for(c_, it["mpn"]),
                               watched=pid in archive.watched_ids(c_))

    @app.get("/api/search")
    def api_search():
        q = request.args.get("q", "").strip()
        if len(q) < 2:
            return jsonify(error="q"), 400
        res = run_search(q, request.args.get("m", ""))
        slim = [{"id": x["id"], "level": x["level"], "brand": x["brand"], "mpn": x["mpn"], "title": x["title"],
                 "stats": x["stats"], "offers": len(x["offers"])} for x in res["groups"]]
        return jsonify(q=q, ms=res["ms"], sources=res["sources"], groups=slim)

    # ---------- підбір за авто ----------
    @app.get("/vehicle")
    def vehicle_pick():
        a = request.args
        make, model = a.get("make", ""), a.get("model", "")
        c_ = con()
        if make not in vehicles.db_makes(c_):
            make = model = ""
        elif model not in vehicles.db_models(c_, make):
            model = ""
        ys = vehicles.db_years(c_, make, model) if model else []
        year = a.get("year", type=int)
        year = year if year in ys else None
        engine = a.get("engine", "").strip()[:30]
        part = a.get("part", "").strip()
        types = vehicles.db_part_types(c_)
        valid_parts = {p for ps in types.values() for p in ps}
        part = part if part in valid_parts else ""
        ctx = {"makes": vehicles.db_makes(c_), "make": make, "model": model, "year": year, "engine": engine, "part": part,
               "models": vehicles.db_models(c_, make) if make else [], "years": ys, "types": types,
               "found": None, "archived": [], "q": "", "car": "", "back": ""}
        if make and model and year:
            ctx["car"] = f"{make.split(' (')[0]} {model} {year}" + (f" {engine}" if engine else "")
            if part:
                ctx["q"] = vehicles.search_query(part, make, model, year, engine)
                from urllib.parse import urlencode
                ctx["back"] = "/vehicle?" + urlencode({"make": make, "model": model, "year": year,
                                                        "engine": engine, "part": part})
                refresh = a.get("refresh") == "1"
                ctx["archived"] = archive.for_vehicle(con(), make, model, part, year, engine)
                if refresh or not ctx["archived"]:        # в Інтернет ідемо лише коли в архіві порожньо або просять оновити
                    ctx["found"] = service.discover(
                        con(), ctx["q"], app.config["BASE_URL"], vehicles.exclude_tokens(make, model, year, engine),
                        force=refresh, vehicle={"make": make, "model": model, "year": year, "engine": engine, "part_type": part})
                    ctx["archived"] = archive.for_vehicle(con(), make, model, part, year, engine)
                for it in ctx["archived"]:
                    it["analogs"] = vehicles.analogs_of(con(), it["brand"], it["mpn"])
        return render_template("vehicle.html", **ctx)

    @app.get("/api/vehicle/<level>")
    def api_vehicle(level):
        a = request.args
        if level == "makes":
            return jsonify(vehicles.db_makes(con()))
        if level == "models":
            return jsonify(vehicles.db_models(con(), a.get("make", "")))
        if level == "years":
            return jsonify(vehicles.db_years(con(), a.get("make", ""), a.get("model", "")))
        if level == "types":
            return jsonify(vehicles.db_part_types(con()))
        abort(404)

    @app.post("/watch/<int:pid>")
    def watch_toggle(pid):
        if not con().execute("SELECT 1 FROM parts WHERE id=?", (pid,)).fetchone():
            abort(404)
        on = archive.toggle_watch(con(), pid)
        flash("Додано до обраного." if on else "Прибрано з обраного.", "ok")
        nxt = request.form.get("next", "")
        return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("watchlist_page"))

    @app.get("/watchlist")
    def watchlist_page():
        return render_template("watchlist.html", rows=archive.watchlist(con()))

    @app.route("/admin/dictionaries", methods=["GET", "POST"])
    @admin_required
    def dictionaries():
        c = con()
        if request.method == "POST":
            f, act = request.form, request.form.get("act")
            if act == "brand_ok":
                c.execute("UPDATE brands SET verified=1 WHERE id=?", (f.get("id", type=int),))
                flash("Бренд підтверджено.", "ok")
            elif act == "brand_merge":
                b = c.execute("SELECT * FROM brands WHERE id=?", (f.get("id", type=int),)).fetchone()
                target = f.get("target", "").strip()
                if b and target and target != b["canonical"] and c.execute("SELECT 1 FROM brands WHERE canonical=?", (target,)).fetchone():
                    c.execute("UPDATE aliases SET canonical=? WHERE canonical=?", (target, b["canonical"]))
                    c.execute("INSERT OR REPLACE INTO aliases VALUES(?,?)", (b["canonical"], target))
                    c.execute("DELETE FROM brands WHERE id=?", (b["id"],))
                    flash(f"«{b['display']}» об’єднано з {target}. Нові пошуки вже використовують це правило.", "ok")
                else:
                    flash("Оберіть існуючий бренд для об’єднання.", "error")
            elif act == "model_add":
                try:
                    mk, md = f["make"].strip(), f["model"].strip()
                    y0, y1 = int(f["year_from"]), int(f["year_to"])
                    assert mk and md and 1950 <= y0 <= y1 <= 2035
                    c.execute("INSERT INTO car_models(make,model,year_from,year_to) VALUES(?,?,?,?)", (mk, md, y0, y1))
                    flash("Модель додано до каталогу.", "ok")
                except (KeyError, ValueError, AssertionError):
                    flash("Перевірте марку, модель і роки (1950–2035).", "error")
                except sqlite3.IntegrityError:
                    flash("Така модель уже є.", "error")
            elif act == "type_add":
                cat, nm = f.get("category", "").strip(), f.get("name", "").strip()
                if cat and nm:
                    try:
                        c.execute("INSERT INTO part_types(category,name,sort) VALUES(?,?,(SELECT COALESCE(MAX(sort),0)+1 FROM part_types))", (cat, nm))
                        flash("Тип деталі додано.", "ok")
                    except sqlite3.IntegrityError:
                        flash("Такий тип уже є.", "error")
            elif act == "demo":
                on = f.get("on") == "1"
                c.execute("UPDATE sources SET enabled=? WHERE name LIKE 'Демо-%'", (1 if on else 0,))
                flash("Демонстраційні джерела " + ("увімкнено." if on else "вимкнено."), "ok")
            c.commit()
            return redirect(url_for("dictionaries"))
        demo_on = bool(c.execute("SELECT 1 FROM sources WHERE name LIKE 'Демо-%' AND enabled=1").fetchone())
        return render_template(
            "dictionaries.html", demo_on=demo_on,
            brands=c.execute("SELECT * FROM brands ORDER BY verified, seen_count DESC, canonical").fetchall(),
            verified_brands=[r[0] for r in c.execute("SELECT canonical FROM brands WHERE verified=1 ORDER BY canonical")],
            models=c.execute("SELECT * FROM car_models ORDER BY make, model").fetchall(),
            types=vehicles.db_part_types(c))

    # ---------- архів і дані ----------
    @app.get("/archive")
    def archive_page():
        a = request.args
        page = max(a.get("page", 1, type=int), 1)
        rows, total = archive.list_parts(con(), a.get("q", "").strip(), a.get("make", ""), a.get("model", ""),
                                         a.get("kind", ""), limit=50, offset=(page - 1) * 50)
        makes_ = [r[0] for r in con().execute("SELECT DISTINCT make FROM vehicle_parts ORDER BY make")]
        models_ = [r[0] for r in con().execute("SELECT DISTINCT model FROM vehicle_parts WHERE make=? ORDER BY model",
                                               (a.get("make", ""),))] if a.get("make") else []
        return render_template("archive.html", rows=rows, total=total, page=page, st=archive.stats(con()),
                               q=a.get("q", ""), make=a.get("make", ""), model=a.get("model", ""), kind=a.get("kind", ""),
                               makes=makes_, models=models_)

    @app.get("/archive/export.csv")
    @admin_required
    def archive_csv():
        return Response(archive.export_csv(con()), mimetype="text/csv; charset=utf-8",
                        headers={"Content-Disposition": "attachment; filename=partsinsight-archive.csv"})

    @app.get("/admin/data")
    @admin_required
    def data_page():
        path = app.config["DATABASE"]
        from pathlib import Path
        bdir = Path(path).parent / "backups"
        backups = sorted(bdir.glob("partsinsight-*.db"), reverse=True) if bdir.exists() else []
        size = Path(path).stat().st_size if Path(path).exists() else 0
        return render_template("data.html", path=path, size=size, st=archive.stats(con()),
                               backups=[(b.name, b.stat().st_size) for b in backups[:7]], bdir=str(bdir))

    @app.post("/admin/data/backup")
    @admin_required
    def data_backup():
        try:
            dst = db.backup_db(app.config["DATABASE"], force=True)
            flash(f"Копію створено: {dst}", "ok")
        except Exception as e:
            flash(f"Не вдалося створити копію: {e}", "error")
        return redirect(url_for("data_page"))

    @app.get("/admin/data/download")
    @admin_required
    def data_download():
        import tempfile
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        src = sqlite3.connect(app.config["DATABASE"])
        out = sqlite3.connect(tmp.name)
        src.backup(out)
        out.close(); src.close()
        return send_file(tmp.name, as_attachment=True, download_name="partsinsight-backup.db")

    # ---------- адмін ----------
    @app.route("/admin/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            row = con().execute("SELECT value FROM settings WHERE key='admin_password_hash'").fetchone()
            pw = request.form.get("password", "")
            ok = check_password_hash(row["value"], pw) if row else secrets.compare_digest(pw, app.config["ADMIN_PASSWORD"])
            if ok:
                session["admin"] = True
                return redirect(request.args.get("next") or url_for("admin"))
            flash("Невірний пароль", "error")
            time.sleep(0.5)
        return render_template("login.html")

    @app.post("/admin/logout")
    def logout():
        session.pop("admin", None)
        return redirect(url_for("index"))

    @app.get("/admin")
    @admin_required
    def admin():
        c = con()
        return render_template(
            "admin.html",
            sources=c.execute("SELECT * FROM sources ORDER BY id").fetchall(),
            xrefs=c.execute("SELECT * FROM cross_refs ORDER BY id DESC").fetchall(),
            log=c.execute("SELECT * FROM searches ORDER BY id DESC LIMIT 8").fetchall(),
            rates=service.RATES)

    def _source_form(sid=None):
        c = con()
        if request.method == "POST":
            f = request.form
            try:
                prof = json.loads(f.get("profile", "{}"))
                if not prof.get("auto"):
                    assert isinstance(prof.get("fields"), dict) and "price" in prof["fields"] \
                        and ("mpn" in prof["fields"] or prof.get("mpn_from_title"))
                    if f["kind"] == "html":
                        assert prof.get("item")
            except (ValueError, AssertionError):
                flash("Профіль має бути валідним JSON з fields.price і fields.mpn (або mpn_from_title: true) (та item для HTML).", "error")
                return render_template("source_form.html", s=dict(f), sid=sid)
            if f["kind"] not in ("html", "json") or not f["search_url"].startswith(("http://", "https://", "{base}")) \
                    or "{q}" not in f["search_url"]:
                flash("URL має починатися з http(s):// і містити {q}.", "error")
                return render_template("source_form.html", s=dict(f), sid=sid)
            vals = (f["name"].strip(), f["kind"], f["search_url"].strip(), json.dumps(prof, ensure_ascii=False),
                    f.get("currency", "UAH"), max(int(f.get("delay_ms") or 1000), 0), f.get("note", ""))
            if sid:
                c.execute("UPDATE sources SET name=?,kind=?,search_url=?,profile=?,currency=?,delay_ms=?,note=? WHERE id=?",
                          (*vals, sid))
            else:
                c.execute("INSERT INTO sources(name,kind,search_url,profile,currency,delay_ms,note,enabled)"
                          " VALUES(?,?,?,?,?,?,?,0)", vals)
            c.commit()
            flash("Джерело збережено. Увімкніть його після перевірки.", "ok")
            return redirect(url_for("admin"))
        s = dict(c.execute("SELECT * FROM sources WHERE id=?", (sid,)).fetchone()) if sid else {
            "kind": "html", "currency": "UAH", "delay_ms": 1500, "profile": json.dumps(
                {"item": "div.product", "fields": {"title": "a.title", "url": "a.title@href", "mpn": ".sku",
                                                   "brand": ".brand", "price": ".price"}}, ensure_ascii=False, indent=2)}
        if sid:
            s["profile"] = json.dumps(json.loads(s["profile"]), ensure_ascii=False, indent=2)
        return render_template("source_form.html", s=s, sid=sid)

    @app.route("/admin/source/new", methods=["GET", "POST"])
    @admin_required
    def source_new():
        return _source_form()

    @app.route("/admin/source/<int:sid>", methods=["GET", "POST"])
    @admin_required
    def source_edit(sid):
        if not con().execute("SELECT 1 FROM sources WHERE id=?", (sid,)).fetchone():
            abort(404)
        return _source_form(sid)

    @app.post("/admin/source/<int:sid>/toggle")
    @admin_required
    def source_toggle(sid):
        con().execute("UPDATE sources SET enabled=1-enabled WHERE id=?", (sid,))
        con().commit()
        return redirect(url_for("admin"))

    @app.post("/admin/source/<int:sid>/delete")
    @admin_required
    def source_delete(sid):
        con().execute("DELETE FROM sources WHERE id=?", (sid,))
        con().execute("DELETE FROM cache WHERE source_id=?", (sid,))
        con().commit()
        return redirect(url_for("admin"))

    @app.post("/admin/source/<int:sid>/test")
    @admin_required
    def source_test(sid):
        row = con().execute("SELECT * FROM sources WHERE id=?", (sid,)).fetchone()
        if not row:
            abort(404)
        probe = request.form.get("probe", "").strip() or "0986452041"
        t = time.time()
        meta, items, err = {}, [], ""
        try:
            items, meta = fetcher.fetch_source_ex(dict(row), probe, app.config["BASE_URL"])
        except Exception as e:
            err = str(e) or type(e).__name__
        ms = int((time.time() - t) * 1000)
        con().execute("UPDATE sources SET last_status=?, last_error=?, last_ms=?, last_items=?, last_at=? WHERE id=?",
                      ("error" if err else "ok", err, ms, len(items), db.now_iso(), sid))
        con().commit()
        al = service.aliases(con())
        sample = []
        for it in items[:6]:
            n = service._normalize(it, dict(row), al, db.now_iso())
            sample.append({"raw": it, "ok": n is not None, "norm": n})
        return render_template("test_result.html", s=row, probe=probe, meta=meta, err=err, ms=ms,
                               n=len(items), sample=sample)

    def run_probe(sid, probe):
        row = con().execute("SELECT * FROM sources WHERE id=?", (sid,)).fetchone()
        t = time.time()
        meta, items, err = {}, [], ""
        try:
            items, meta = fetcher.fetch_source_ex(dict(row), probe, app.config["BASE_URL"])
        except Exception as e:
            err = str(e) or type(e).__name__
        ms = int((time.time() - t) * 1000)
        con().execute("UPDATE sources SET last_status=?, last_error=?, last_ms=?, last_items=?, last_at=? WHERE id=?",
                      ("error" if err else "ok", err, ms, len(items), db.now_iso(), sid))
        con().commit()
        return row, meta, items, err, ms

    PROVIDERS = [
        {"id": "ebay", "name": "eBay Browse API", "source": "eBay Browse API (потрібні ключі)",
         "keys": [("EBAY_CLIENT_ID", "App ID (Client ID)", "Production-ключ, містить «-PRD-»: назва-…-PRD-…"),
                  ("EBAY_CLIENT_SECRET", "Cert ID (Client Secret)", "Починається з «PRD-». Не плутайте з App ID.")],
         "link": "https://developer.ebay.com/my/keys", "help": "developer.ebay.com → My Account → Application Keys → набір Production."},
        {"id": "xref", "name": "Каталог аналогів (Cross-Reference API)", "kind": "xref", "source": "",
         "keys": [("RAPIDAPI_KEY", "RapidAPI key", "Безкоштовно до 300 запитів на день; потрібна підписка на API (план Basic)")],
         "link": "https://rapidapi.com/aser4giam-BpC-7ne9l62/api/auto-parts-cross-reference-api",
         "help": "rapidapi.com → увійти → відкрити API «Auto Parts Cross-Reference» → Subscribe (Basic, безкоштовно) → скопіювати ключ X-RapidAPI-Key."},
        {"id": "serpapi", "name": "Google Shopping (SerpApi)", "source": "Google Shopping через SerpApi (потрібен ключ)",
         "keys": [("SERPAPI_KEY", "API key", "Безкоштовно 250 запитів на місяць")],
         "link": "https://serpapi.com/manage-api-key", "help": "serpapi.com → Sign up → Dashboard → API key."},
    ]

    @app.route("/admin/settings", methods=["GET", "POST"])
    @admin_required
    def settings():
        c = con()
        if request.method == "POST":
            if request.form.get("act") == "password":
                pw = request.form.get("new_password", "")
                if len(pw) < 6:
                    flash("Пароль має містити щонайменше 6 символів.", "error")
                else:
                    c.execute("INSERT OR REPLACE INTO settings VALUES('admin_password_hash',?)", (generate_password_hash(pw),))
                    c.commit()
                    flash("Пароль змінено.", "ok")
                return redirect(url_for("settings"))
            for k in [k for p in PROVIDERS for k, _, _ in p["keys"]]:
                v = request.form.get(k, "").strip().strip("\"'").strip()
                if request.form.get("clear_" + k):
                    c.execute("DELETE FROM settings WHERE key=?", (k,))
                elif v:
                    c.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (k, v))
            c.commit()
            load_settings()
            prov = next((p for p in PROVIDERS if p["id"] == request.form.get("verify")), None)
            if not prov:
                flash("Ключі збережено.", "ok")
                return redirect(url_for("settings"))
            if prov.get("kind") == "xref":
                from . import crossapi
                try:
                    found = crossapi.fetch("W 75/3", fetcher.resolve("$ENV:" + crossapi.KEY_NAME))
                    flash(f"{prov['name']}: працює, для тестового артикула знайдено аналогів: {len(found)}. "
                          "Далі аналоги підтягуються автоматично під час пошуку й зберігаються в базі.", "ok")
                except crossapi.CrossApiError as e:
                    flash(f"{prov['name']}: {e}", "error")
                return redirect(url_for("settings"))
            srow = c.execute("SELECT id FROM sources WHERE name=?", (prov["source"],)).fetchone()
            if not srow:
                flash("Джерело не знайдено в списку.", "error")
                return redirect(url_for("settings"))
            _, meta, items, err, ms = run_probe(srow["id"], "0986452041")
            if err:
                flash(f"{prov['name']}: {err}", "error")
            elif not items:
                flash(f"{prov['name']}: ключі прийнято, але за тестовим запитом нічого не знайдено ({ms} мс). Джерело не увімкнено.", "error")
            else:
                c.execute("UPDATE sources SET enabled=1 WHERE id=?", (srow["id"],))
                c.commit()
                flash(f"{prov['name']}: працює, знайдено записів: {len(items)}. Джерело увімкнено.", "ok")
            return redirect(url_for("settings"))
        have = {r["key"]: r["value"] for r in c.execute("SELECT key,value FROM settings")}
        view = []
        for p in PROVIDERS:
            keys = [{"name": k, "label": l, "hint": h, "set": bool(have.get(k)),
                     "tail": ("••••" + have[k][-4:]) if have.get(k) else ""} for k, l, h in p["keys"]]
            srow = c.execute("SELECT enabled,last_status,last_error,last_items FROM sources WHERE name=?", (p["source"],)).fetchone() \
                if p["source"] else None
            if p.get("kind") == "xref":
                n = c.execute("SELECT COUNT(*) FROM cross_refs WHERE ref_source LIKE 'Cross-Reference API%'").fetchone()[0]
                srow = {"enabled": bool(have.get("RAPIDAPI_KEY")), "last_status": "ok", "last_error": "", "last_items": n}
            view.append({**p, "fields": keys, "src": srow})
        return render_template("settings.html", providers=view)

    @app.route("/admin/source/quick", methods=["GET", "POST"])
    @admin_required
    def source_quick():
        if request.method == "POST":
            f = request.form
            name, link, term = f.get("name", "").strip(), f.get("url", "").strip(), f.get("term", "").strip()
            if not (name and term and link.startswith(("http://", "https://"))):
                flash("Вкажіть назву, повне посилання (http…) та слово, яке ви шукали.", "error")
                return render_template("source_quick.html", f=f)
            tpl = link
            for v in (quote(term, safe=""), quote_plus(term), term):
                if v in tpl:
                    tpl = tpl.replace(v, "{q}")
                    break
            else:
                flash(f"У посиланні не знайдено «{term}». Виконайте пошук на сайті цим словом і скопіюйте адресу сторінки результатів.", "error")
                return render_template("source_quick.html", f=f)
            cur = f.get("currency", "UAH")
            cur = cur if cur in ("UAH", "USD", "EUR") else "UAH"
            sid = con().execute(
                "INSERT INTO sources(name,kind,enabled,search_url,profile,currency,delay_ms,note) VALUES(?,?,?,?,?,?,?,?)",
                (name, "html", 0, tpl, json.dumps({"auto": True, "currency": cur}), cur, 1500,
                 "Додано за посиланням: авторозбір сторінки.")).lastrowid
            con().commit()
            row, meta, items, err, ms = run_probe(sid, term)
            al, sample = service.aliases(con()), []
            for it in items[:6]:
                nm = service._normalize(it, dict(row), al, db.now_iso())
                sample.append({"raw": it, "ok": nm is not None, "norm": nm})
            if not err and items:
                flash(f"Знайдено товарів: {len(items)}. Джерело додано вимкненим: перегляньте приклади й увімкніть його у списку.", "ok")
            return render_template("test_result.html", s=row, probe=term, meta=meta, err=err, ms=ms, n=len(items), sample=sample)
        return render_template("source_quick.html", f={"term": "0986452041", "currency": "UAH"})

    @app.post("/admin/xref")
    @admin_required
    def xref_add():
        f = request.form
        if not (f.get("mpn_a") and f.get("mpn_b") and f.get("ref_source")):
            flash("Заповніть обидва артикули та джерело довідника.", "error")
        else:
            con().execute("INSERT INTO cross_refs(mfr_a,mpn_a,mfr_b,mpn_b,ref_source) VALUES(?,?,?,?,?)",
                          (f.get("mfr_a", ""), f["mpn_a"], f.get("mfr_b", ""), f["mpn_b"], f["ref_source"]))
            if f.get("mfr_a"):
                con().execute("INSERT OR IGNORE INTO aliases VALUES(?,?)", (alias_key(f["mfr_a"]), alias_key(f["mfr_a"])))
            if f.get("mfr_b"):
                con().execute("INSERT OR IGNORE INTO aliases VALUES(?,?)", (alias_key(f["mfr_b"]), alias_key(f["mfr_b"])))
            con().commit()
            flash("Аналог додано.", "ok")
        return redirect(url_for("admin") + "#xrefs")

    @app.post("/admin/xref/<int:xid>/delete")
    @admin_required
    def xref_delete(xid):
        con().execute("DELETE FROM cross_refs WHERE id=?", (xid,))
        con().commit()
        return redirect(url_for("admin") + "#xrefs")

    @app.errorhandler(500)
    @app.errorhandler(Exception)
    def _server_error(e):
        import traceback, os
        from werkzeug.exceptions import HTTPException
        if isinstance(e, HTTPException) and e.code and e.code < 500:
            return e
        tb = traceback.format_exc()
        try:
            with open(os.path.join(db.data_dir(), "errors.log"), "a", encoding="utf-8") as f:
                f.write(f"--- {db.now_iso()} {request.method} {request.full_path}\n{tb}\n")
        except Exception:
            pass
        detail = tb if (session.get("admin") or app.config.get("TESTING")) else None
        return render_template("error.html", detail=detail), 500

    return app


def _hist_chart(h: list[dict]) -> dict | None:
    """SVG-графік історії мінімальної ціни за днями."""
    if len(h) < 2:
        return None
    W, H, px, py = 640, 190, 44, 24
    lo, hi = min(p["min"] for p in h), max(p["median"] for p in h)
    span = (hi - lo) or 1.0

    def X(i):
        return px + i * (W - 2 * px) / (len(h) - 1)

    def Y(v):
        return H - py - (v - lo) / span * (H - 2 * py)

    return {"W": W, "H": H, "min": " ".join(f"{X(i):.1f},{Y(p['min']):.1f}" for i, p in enumerate(h)),
            "med": " ".join(f"{X(i):.1f},{Y(p['median']):.1f}" for i, p in enumerate(h)),
            "pts": [{"x": round(X(i), 1), "y": round(Y(p["min"]), 1), "d": p["date"], "v": p["min"]} for i, p in enumerate(h)],
            "lo": lo, "hi": hi, "px": px, "d0": h[0]["date"], "d1": h[-1]["date"]}


def _strip(g: dict) -> dict:
    """Дані для SVG-діаграми розкиду цін (точки, медіана, межі)."""
    st = g["stats"]
    if not st:
        return None
    lo, hi = st["min"], st["max"]
    span = (hi - lo) or 1.0
    W, pad = 640, 28

    def x(v):
        return pad + (v - lo) / span * (W - 2 * pad) if hi != lo else W / 2

    pts = [{"x": round(x(o["unit_price"]), 1), "outlier": o["outlier"], "label": o["source"],
            "price": o["unit_price"]} for o in g["offers"] if o["unit_price"] is not None]
    placed: list[tuple[float, int]] = []
    for p in sorted(pts, key=lambda p: p["x"]):
        lvl = 0
        while any(l == lvl and abs(p["x"] - px) < 16 for px, l in placed):
            lvl += 1
        p["lvl"] = lvl
        placed.append((p["x"], lvl))
    return {"W": W, "pts": pts, "med": round(x(st["median"]), 1), "lo": round(x(lo), 1), "hi": round(x(hi), 1),
            "H": 70 + 18 * max((p["lvl"] for p in pts), default=0)}
