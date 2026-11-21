"""Спільна бібліотека експерименту E1: заморожування умов, збір, офлайн-відтворення, розмітка, метрики.

Запуск = окрема папка runs/<run_id>/ (frozen.json, копії queries.csv і config.json, responses/, evaluation/).
Зміна умов = новий запуск; попередні знімки й розмітка не змінюються.
"""
from __future__ import annotations
import csv, datetime, hashlib, json, os, pathlib, re, shutil, sqlite3, sys, tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app import analytics, db, fetcher, matching, service          # noqa: E402
from app.normalize import norm_mpn                                    # noqa: E402

COMPONENTS = ["app/matching.py", "app/analytics.py", "app/normalize.py", "app/service.py", "app/crossapi.py", "app/partmeta.py"]
CATEGORIES = ("full", "variant", "partial", "text")
APPROVED = "ЗАТВЕРДЖЕНО"
METHODS = ("baseline", "partsinsight")
TOP_K = 5
DECISIONS = ("так", "ні", "сумнівно")
EMPTY = "порожньо"
TABLE_FIELDS = ["id", "запит", "метод", "позиція", "бренд", "артикул", "URL", "рішення", "підстава", "джерело", "рівень_системи", "підстава_системи"]
INTERNAL_FIELDS = ["id", "запит", "метод", "позиція", "offer_id", "бренд", "артикул", "URL", "джерело", "рівень_системи", "підстава_системи"]
SHEET_FIELDS = ["offer_id", "id", "запит", "правило", "бренд", "артикул", "URL", "рішення", "підстава"]


class E1Error(Exception):
    """Помилка умов експерименту: повідомлення призначене для користувача."""


# ---------------------------------------------------------------- допоміжне
def sha256_file(path) -> str:
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def slug(s: str) -> str:
    return re.sub(r"[^0-9A-Za-zА-Яа-яІіЇїЄє]+", "_", s).strip("_")[:60] or "src"


def read_csv(path) -> list[dict]:
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path, fields, rows) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


_SECRET = re.compile(r"secret|token|password|api_?key|authorization", re.I)


def redact(obj):
    """Секрети з профілю джерела в знімок не потрапляють (посилання $ENV:ІМ’Я залишаються)."""
    if isinstance(obj, dict):
        return {k: ("***" if _SECRET.search(str(k)) and isinstance(v, str) and not v.startswith("$ENV:") else redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(x) for x in obj]
    return obj


# ---------------------------------------------------------------- запити
def validate_queries(rows: list[dict]) -> list[str]:
    p = []
    ids = [r.get("id", "").strip() for r in rows]
    if len(rows) != 20:
        p.append(f"має бути рівно 20 запитів, є {len(rows)}")
    if len(set(ids)) != len(ids):
        p.append("ідентифікатори запитів не унікальні: " + ", ".join(sorted({i for i in ids if ids.count(i) > 1})))
    if set(ids) != {f"Q{i:02d}" for i in range(1, 21)}:
        p.append("ідентифікатори мають бути Q01–Q20")
    for c in CATEGORIES:
        n = sum(r.get("type") == c for r in rows)
        if n != 5:
            p.append(f"категорія «{c}»: {n} запитів, потрібно 5")
    bad_type = sorted({r.get("type", "") for r in rows} - set(CATEGORIES))
    if bad_type:
        p.append(f"невідомі категорії: {bad_type}")
    for r in rows:
        i = r.get("id", "?")
        if not (r.get("query") or "").strip():
            p.append(f"{i}: порожній запит")
        if not (r.get("expected_rule") or "").strip():
            p.append(f"{i}: не заповнено правило релевантності")
        if not (r.get("rationale") or "").strip():
            p.append(f"{i}: не заповнено обґрунтування очікуваного результату")
        if (r.get("status") or "").strip() != APPROVED:
            p.append(f"{i}: статус «{(r.get('status') or '').strip()}», потрібно «{APPROVED}»")
    return p


# ---------------------------------------------------------------- знімок умов
def snapshot_from_db(db_path: str, source_names: list[str], allow_demo: bool) -> dict:
    con = db.connect(db_path)
    try:
        sources, ph = [], {}
        for n in source_names:
            r = con.execute("SELECT * FROM sources WHERE name=?", (n,)).fetchone()
            if not r:
                raise E1Error(f"Джерело «{n}» не знайдено в базі")
            if n.startswith("Демо") and not allow_demo:
                raise E1Error(f"«{n}» — демоджерело; для реального запуску його використовувати не можна")
            d = dict(r)
            ph[n] = sha256_text(json.dumps({k: d[k] for k in ("kind", "search_url", "profile", "currency")}, ensure_ascii=False, sort_keys=True))
            d["profile"] = json.dumps(redact(json.loads(d["profile"] or "{}")), ensure_ascii=False)
            sources.append({k: d[k] for k in ("name", "kind", "search_url", "profile", "currency", "delay_ms", "allow_local", "note")})
        return {
            "sources": sources, "source_hash": ph,
            "aliases": [list(r) for r in con.execute("SELECT alias, canonical FROM aliases ORDER BY alias")],
            "cross_refs": [list(r) for r in con.execute(
                "SELECT mfr_a,mpn_a,mfr_b,mpn_b,ref_source FROM cross_refs WHERE verified=1 ORDER BY id")],
            "rates": dict(service.RATES),
        }
    finally:
        con.close()


def build_snapshot_db(frozen: dict) -> sqlite3.Connection:
    """Чиста тимчасова база лише із зафіксованих даних: без поточного кешу, архіву й пізніших правок довідника."""
    path = pathlib.Path(tempfile.mkdtemp()) / "e1_snapshot.db"
    con = db.connect(str(path))
    db.init(con, "http://127.0.0.1:5050")
    for t in ("sources", "cache", "cross_refs", "aliases", "settings", "parts", "vehicle_parts", "price_snapshots", "watchlist"):
        con.execute(f"DELETE FROM {t}")
    for i, s in enumerate(frozen["sources"], 1):
        con.execute("INSERT INTO sources(id,name,kind,enabled,search_url,profile,currency,delay_ms,allow_local,note) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (i, s["name"], s["kind"], 1, s["search_url"], s["profile"], s["currency"], s["delay_ms"], s["allow_local"], s["note"]))
    con.executemany("INSERT INTO aliases(alias,canonical) VALUES(?,?)", frozen["aliases"])
    con.executemany("INSERT INTO cross_refs(mfr_a,mpn_a,mfr_b,mpn_b,ref_source,verified) VALUES(?,?,?,?,?,1)", frozen["cross_refs"])
    con.commit()
    return con


def plan_requests(frozen: dict) -> list[dict]:
    """Усі звернення до джерел: початковий запит і запити за аналогами, які сформувала система (спільна логіка service.plan_queries)."""
    con = build_snapshot_db(frozen)
    try:
        out = []
        for q in frozen["queries"]:
            plan = service.plan_queries(con, q["query"])
            for s in frozen["sources"]:
                for k, text in enumerate(plan["queries"]):
                    out.append({"req_id": f"{q['id']}__{slug(s['name'])}__{k}", "qid": q["id"], "source": s["name"], "text": text,
                                "role": "original" if k == 0 else "analog"})
        return out
    finally:
        con.close()


# ---------------------------------------------------------------- freeze
def freeze(run_id: str, queries_path, config_path, db_path, source_names: list[str], runs_dir, test_run: bool = False) -> pathlib.Path:
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,60}", run_id):
        raise E1Error("run_id: латиниця, цифри, _ . - (3–60 знаків)")
    run = pathlib.Path(runs_dir) / run_id
    if run.exists():
        raise E1Error(f"Запуск {run_id} уже існує. Зміну умов оформлюйте як новий запуск з іншою назвою.")
    rows = read_csv(queries_path)
    problems = validate_queries(rows)
    cfg = json.loads(pathlib.Path(config_path).read_text(encoding="utf-8"))
    if cfg.get("ALPHA") != matching.ALPHA or cfg.get("THETA") != matching.THETA or cfg.get("WEIGHTS") != analytics.WEIGHTS:
        problems.append("config.json розходиться з кодом (ALPHA/THETA/WEIGHTS)")
    if not source_names:
        problems.append("не вказано жодного джерела")
    if problems:
        raise E1Error("Фіксацію заблоковано:\n - " + "\n - ".join(problems))
    snap = snapshot_from_db(db_path, source_names, allow_demo=test_run)
    frozen = {
        "run_id": run_id, "test_run": bool(test_run),
        "label": "ТЕСТОВИЙ ЗАПУСК (контрольні дані, не результат експерименту)" if test_run else "робочий запуск",
        "frozen_at": now_iso(), "config": cfg, "queries": rows, **snap,
        "sha256": {"queries.csv": sha256_file(queries_path), "config.json": sha256_file(config_path),
                   "components": {c: sha256_file(ROOT / c) for c in COMPONENTS}},
    }
    frozen["requests"] = plan_requests(frozen)
    run.mkdir(parents=True)
    shutil.copy(queries_path, run / "queries.csv")
    shutil.copy(config_path, run / "config.json")
    (run / "frozen.json").write_text(json.dumps(frozen, ensure_ascii=False, indent=1), encoding="utf-8")
    return run


def load_frozen(run) -> dict:
    p = pathlib.Path(run) / "frozen.json"
    if not p.exists():
        raise E1Error(f"{run}: немає frozen.json — спершу виконайте freeze")
    return json.loads(p.read_text(encoding="utf-8"))


def verify_integrity(run, frozen: dict | None = None) -> None:
    run = pathlib.Path(run)
    frozen = frozen or load_frozen(run)
    h = frozen["sha256"]
    bad = []
    if sha256_file(run / "queries.csv") != h["queries.csv"]:
        bad.append("queries.csv у папці запуску змінено")
    if sha256_file(run / "config.json") != h["config.json"]:
        bad.append("config.json у папці запуску змінено")
    for c, v in h["components"].items():
        if sha256_file(ROOT / c) != v:
            bad.append(f"змінено компонент пошуку {c}")
    if bad:
        raise E1Error("Умови експерименту змінилися після фіксації:\n - " + "\n - ".join(bad) +
                      "\nСтворіть НОВИЙ запуск (freeze з іншою назвою); попередні знімки й розмітка лишаються без змін.")


# ---------------------------------------------------------------- collect
def _req_file(run, frozen, r) -> pathlib.Path:
    return pathlib.Path(run) / "responses" / slug(r["source"]) / f"{r['req_id']}.json"


def read_status(run, frozen, r) -> str | None:
    f = _req_file(run, frozen, r)
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8")).get("status")
    except ValueError:
        return None


def missing_requests(run, frozen) -> list[dict]:
    return [r for r in frozen["requests"] if read_status(run, frozen, r) not in ("ok", "ok_empty")]


def collect(run, db_path, fetch_fn=None, pause: bool = True) -> dict:
    """Збирає лише відсутні або невдалі звернення. Розрізняє: ok (є пропозиції), ok_empty (успішна нульова видача), failed (збій)."""
    run = pathlib.Path(run)
    frozen = load_frozen(run)
    verify_integrity(run, frozen)
    con = db.connect(db_path)
    try:
        fetcher.set_secrets({r["key"]: r["value"] for r in con.execute("SELECT key,value FROM settings")})
        live = {}
        for s in frozen["sources"]:
            row = con.execute("SELECT * FROM sources WHERE name=?", (s["name"],)).fetchone()
            if not row:
                raise E1Error(f"Джерело «{s['name']}» зникло з бази")
            d = dict(row)
            cur_hash = sha256_text(json.dumps({k: d[k] for k in ("kind", "search_url", "profile", "currency")}, ensure_ascii=False, sort_keys=True))
            if cur_hash != frozen["source_hash"][s["name"]]:
                raise E1Error(f"Налаштування джерела «{s['name']}» змінено після фіксації — створіть новий запуск")
            live[s["name"]] = d
    finally:
        con.close()
    fn = fetch_fn or (lambda src, text: fetcher.fetch_source(src, text, "http://127.0.0.1:5050"))
    log = run / "collect_log.csv"
    new = not log.exists()
    done = failed = skipped = 0
    with open(log, "a", newline="", encoding="utf-8") as lf:
        w = csv.writer(lf)
        if new:
            w.writerow(["час", "джерело", "запит_id", "роль", "фактичний_текст", "статус", "записів", "помилка"])
        for r in frozen["requests"]:
            f = _req_file(run, frozen, r)
            if read_status(run, frozen, r) in ("ok", "ok_empty"):
                skipped += 1
                continue
            f.parent.mkdir(parents=True, exist_ok=True)
            at = now_iso()
            try:
                items = fn(live[r["source"]], r["text"])
                status, err = ("ok" if items else "ok_empty"), ""
            except Exception as e:                      # будь-який збій фіксується; успіх і збій не змішуються
                items, status, err = [], "failed", f"{type(e).__name__}: {e}"[:300]
            f.write_text(json.dumps({"req_id": r["req_id"], "qid": r["qid"], "role": r["role"], "source": r["source"],
                                     "query_text": r["text"], "collected_at": at, "status": status, "error": err,
                                     "items": items}, ensure_ascii=False, indent=1), encoding="utf-8")
            w.writerow([at, r["source"], r["qid"], r["role"], r["text"], status, len(items), err])
            done += status != "failed"
            failed += status == "failed"
            if pause and live[r["source"]].get("delay_ms"):
                import time
                time.sleep(max(live[r["source"]]["delay_ms"], 300) / 1000)
    return {"collected": done, "failed": failed, "already_done": skipped, "missing": len(missing_requests(run, frozen))}


# ---------------------------------------------------------------- evaluate
def _items_key(source: str, raw_or_offer: dict, mpn: str) -> str:
    """Стабільний ідентифікатор пропозиції в межах запиту (однаковий для обох методів)."""
    url = service._s(raw_or_offer.get("url"))
    title = service._s(raw_or_offer.get("title"))
    return hashlib.sha1(f"{source}|{url or title}|{norm_mpn(mpn)}".encode()).hexdigest()[:10]


def _load_responses(run, frozen) -> dict:
    out = {}
    for r in frozen["requests"]:
        d = json.loads(_req_file(run, frozen, r).read_text(encoding="utf-8"))
        out[(r["source"], r["text"])] = d
    return out


def evaluate(run) -> pathlib.Path:
    run = pathlib.Path(run)
    frozen = load_frozen(run)
    verify_integrity(run, frozen)
    miss = missing_requests(run, frozen)
    if miss:
        lines = [f"{r['qid']} «{next(q['query'] for q in frozen['queries'] if q['id'] == r['qid'])}» — джерело «{r['source']}», "
                 f"запит «{r['text']}» ({'збій' if read_status(run, frozen, r) == 'failed' else 'відповіді немає'})" for r in miss]
        raise E1Error(f"Збір неповний ({len(miss)} звернень). Оцінювання не виконано, попередні таблиці збережено.\n - " + "\n - ".join(lines) +
                      "\nПовторіть збір (collect): буде повторено лише пропущені звернення.")
    resp = _load_responses(run, frozen)
    con = build_snapshot_db(frozen)
    if plan_requests(frozen) != frozen["requests"]:
        raise E1Error("Перелік звернень, відтворений із зафіксованих зв’язків, не збігається із замороженим")
    anchor = max(datetime.datetime.fromisoformat(d["collected_at"]) for d in resp.values())
    saved_env = os.environ.pop(service.crossapi.KEY_NAME, None)          # безкоштовний API аналогів не підмішується
    saved_secrets = dict(fetcher.SECRETS)
    fetcher.set_secrets({})
    old_now, service.NOW = service.NOW, anchor

    def replay(source, text):
        d = resp.get((source, text))
        if d is None:
            raise E1Error(f"Відтворення звернулось до незбереженого запиту: джерело «{source}», текст «{text}»")
        return d["items"], d["collected_at"]

    internal = []
    try:
        for q in frozen["queries"]:
            plan = service.plan_queries(con, q["query"])
            nq = norm_mpn(plan["mq"])
            base = []
            for s in frozen["sources"]:
                d = resp[(s["name"], q["query"])]                        # базовий метод: лише відповідь на початковий запит
                for it in d["items"]:
                    if nq and norm_mpn(service._s(it.get("mpn"))) == nq:
                        base.append({"brand": service._s(it.get("brand")), "mpn": service._s(it.get("mpn")), "url": service._s(it.get("url")),
                                     "source": s["name"], "level": "", "basis": "точний нормалізований артикул",
                                     "offer_id": _items_key(s["name"], it, it.get("mpn"))})
            res = service.search(con, q["query"], "http://x", None, True, replay=replay)
            full = []
            for g in res["groups"]:
                for o in g["offers"]:
                    full.append({"brand": o["brand"], "mpn": o["mpn_raw"], "url": o["url"], "source": o["source"], "level": g["level"],
                                 "basis": g["evidence"]["basis"], "offer_id": _items_key(o["source"], o, o["mpn_raw"])})
            for method, lst in (("baseline", base), ("partsinsight", full)):
                for pos in range(1, TOP_K + 1):
                    x = lst[pos - 1] if pos <= len(lst) else None
                    internal.append({"id": q["id"], "запит": q["query"], "метод": method, "позиція": pos,
                                     "offer_id": x["offer_id"] if x else "", "бренд": x["brand"] if x else "",
                                     "артикул": x["mpn"] if x else "", "URL": x["url"] if x else "", "джерело": x["source"] if x else "",
                                     "рівень_системи": x["level"] if x else "", "підстава_системи": x["basis"] if x else ""})
    finally:
        service.NOW = old_now
        fetcher.set_secrets(saved_secrets)
        if saved_env is not None:
            os.environ[service.crossapi.KEY_NAME] = saved_env
        con.close()
    out = run / "evaluation"
    out.mkdir(exist_ok=True)
    # аркуш експерта: кожна пропозиція запиту — один раз, без методу, позиції й висновку алгоритму; порядок не розкриває ранг
    rules = {q["id"]: q["expected_rule"] for q in frozen["queries"]}
    seen, sheet = set(), []
    for r in internal:
        if r["offer_id"] and (r["id"], r["offer_id"]) not in seen:
            seen.add((r["id"], r["offer_id"]))
            sheet.append({"offer_id": r["offer_id"], "id": r["id"], "запит": r["запит"], "правило": rules[r["id"]], "бренд": r["бренд"],
                          "артикул": r["артикул"], "URL": r["URL"], "рішення": "", "підстава": ""})
    sheet.sort(key=lambda x: (x["id"], hashlib.sha1((x["offer_id"] + "salt").encode()).hexdigest()))
    sheet_path = out / "expert_sheet.csv"
    if sheet_path.exists():                                              # повторне оцінювання не втрачає вже зроблену розмітку
        old = {(r["id"], r["offer_id"]): r for r in read_csv(sheet_path)}
        for r in sheet:
            o = old.get((r["id"], r["offer_id"]))
            if o:
                r["рішення"], r["підстава"] = o["рішення"], o["підстава"]
    tmp = out / ".tmp"
    tmp.mkdir(exist_ok=True)
    write_csv(tmp / "methods_internal.csv", INTERNAL_FIELDS, internal)
    write_csv(tmp / "expert_sheet.csv", SHEET_FIELDS, sheet)
    shutil.move(str(tmp / "methods_internal.csv"), str(out / "methods_internal.csv"))
    shutil.move(str(tmp / "expert_sheet.csv"), str(sheet_path))
    tmp.rmdir()
    (out / "evaluation.json").write_text(json.dumps({
        "evaluated_at_anchor": anchor.isoformat(), "complete": True, "test_run": frozen["test_run"],
        "positions_sha256": sha256_text(json.dumps([[r["id"], r["метод"], r["позиція"], r["offer_id"]] for r in internal])),
        "n_queries": len(frozen["queries"]), "n_expert_rows": len(sheet)}, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


# ---------------------------------------------------------------- розмітка
def apply_labels(run) -> dict:
    """Переносить рішення експерта на позиції ОБОХ методів: e1_table.csv."""
    run = pathlib.Path(run)
    out = run / "evaluation"
    if not (out / "evaluation.json").exists():
        raise E1Error("Спершу виконайте evaluate")
    sheet = {(r["id"], r["offer_id"]): r for r in read_csv(out / "expert_sheet.csv")}
    rows, unl = [], 0
    for r in read_csv(out / "methods_internal.csv"):
        if not r["offer_id"]:
            dec, why = EMPTY, ""
        else:
            s = sheet.get((r["id"], r["offer_id"]))
            dec = (s["рішення"].strip().lower() if s else "")
            why = s["підстава"] if s else ""
            unl += dec == ""
        rows.append({**r, "рішення": dec, "підстава": why})
    write_csv(out / "e1_table.csv", TABLE_FIELDS, rows)
    return {"rows": len(rows), "unlabeled_positions": unl}


# ---------------------------------------------------------------- метрики
def validate_table(rows: list[dict], expected_qids: list[str]) -> list[str]:
    p = []
    count = {}
    for r in rows:
        try:
            key = (r["метод"], r["id"], int(r["позиція"]))
        except (KeyError, ValueError):
            p.append(f"некоректний рядок: {r}")
            continue
        count[key] = count.get(key, 0) + 1
    for m in METHODS:
        for q in expected_qids:
            for pos in range(1, TOP_K + 1):
                n = count.get((m, q, pos), 0)
                if n == 0:
                    p.append(f"відсутній рядок: {m} {q} позиція {pos}")
                elif n > 1:
                    p.append(f"дублікат ({n} рядки): {m} {q} позиція {pos}")
    extra = {k for k in count if k[0] not in METHODS or k[1] not in expected_qids or not 1 <= k[2] <= TOP_K}
    for k in sorted(extra, key=str):
        p.append(f"зайвий рядок: {k}")
    for r in rows:
        d = (r.get("рішення") or "").strip().lower()
        loc = f"{r.get('метод')} {r.get('id')} позиція {r.get('позиція')}"
        has = bool((r.get("артикул") or "").strip() or (r.get("URL") or "").strip() or (r.get("offer_id") or "").strip())
        if d == "":
            p.append(f"нерозмічена пропозиція: {loc}")
        elif d not in DECISIONS + (EMPTY,):
            p.append(f"невідоме рішення «{d}»: {loc}")
        elif d == EMPTY and has:
            p.append(f"позначено «порожньо», але пропозиція є: {loc}")
        elif d in DECISIONS and not has:
            p.append(f"рішення «{d}» для порожньої позиції: {loc}")
    return p


def compute(rows: list[dict], n_queries: int = 20) -> dict:
    out = {}
    for m in METHODS:
        by = {}
        for r in rows:
            if r["метод"] == m:
                by.setdefault(r["id"], {})[int(r["позиція"])] = (r["рішення"] or "").strip().lower()
        hit = conf = empty_q = doubt = 0
        for q, pos in by.items():
            ds = [pos.get(i, EMPTY) for i in range(1, TOP_K + 1)]
            hit += ds[0] == "так"
            conf += sum(d == "так" for d in ds)
            doubt += sum(d == "сумнівно" for d in ds)
            empty_q += all(d == EMPTY for d in ds)
        out[m] = {"hit1": hit / n_queries, "hits": hit, "p5": conf / (TOP_K * n_queries), "confirmed": conf,
                  "empty_queries": empty_q, "doubtful": doubt}
    return out


def final_report(run) -> str:
    """Фінальний звіт лише після повної перевірки таблиці; інакше E1Error і файл звіту не створюється."""
    run = pathlib.Path(run)
    frozen = load_frozen(run)
    verify_integrity(run, frozen)
    out = run / "evaluation"
    if not (out / "e1_table.csv").exists() or not (out / "evaluation.json").exists():
        raise E1Error("Немає e1_table.csv: виконайте evaluate і labels")
    if not json.loads((out / "evaluation.json").read_text(encoding="utf-8")).get("complete"):
        raise E1Error("Оцінювання не позначене як повне")
    if missing_requests(run, frozen):
        raise E1Error("Збір неповний — фінальний звіт заблоковано")
    rows = read_csv(out / "e1_table.csv")
    qids = [q["id"] for q in frozen["queries"]]
    problems = validate_table(rows, qids)
    if problems:
        raise E1Error(f"Фінальний звіт заблоковано ({len(problems)} проблем):\n - " + "\n - ".join(problems[:40]) +
                      ("\n - …" if len(problems) > 40 else ""))
    res = compute(rows, len(qids))
    lines = [f"# Звіт E1 · запуск {frozen['run_id']}", "", f"**{frozen['label']}**", "",
             f"Зафіксовано: {frozen['frozen_at']} · запитів: {len(qids)} · позицій на запит: {TOP_K}", "",
             "| Метод | Hit@1 | Precision@5 | Підтверджених у топ-5 | Порожніх видач | Сумнівних оцінок |", "|---|---|---|---|---|---|"]
    for m in METHODS:
        v = res[m]
        lines.append(f"| {m} | {v['hit1']:.2f} ({v['hits']}/{len(qids)}) | {v['p5']:.2f} ({v['confirmed']}/{TOP_K * len(qids)}) | "
                     f"{v['confirmed']} | {v['empty_queries']} | {v['doubtful']} |")
    txt = "\n".join(lines) + "\n"
    (run / "e1_report.md").write_text(txt, encoding="utf-8")
    return txt
