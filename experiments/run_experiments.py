"""Експериментальна перевірка (розділ 4) на локальному «Інтернеті» (mock). Усі числа вимірюються, а не задаються."""
from __future__ import annotations
import json, os, socket, statistics as st, sys, tempfile, threading, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import logging; logging.getLogger("werkzeug").setLevel(logging.ERROR)
from werkzeug.serving import make_server
from app import create_app, db, service, analytics
from app.fetcher import fetch_source
from app.mock_web import CATALOG, DECOYS
import json as _json
ALL = CATALOG + DECOYS
from app.normalize import norm_mpn, norm_text, trigram_sim, looks_like_mpn, brand_key

sk = socket.socket(); sk.bind(("127.0.0.1", 0)); port = sk.getsockname()[1]; sk.close()
BASE = f"http://127.0.0.1:{port}"
dbp = os.path.join(tempfile.mkdtemp(), "exp.db")
app = create_app({"DATABASE": dbp, "BASE_URL": BASE, "TESTING": True})
srv = make_server("127.0.0.1", port, app, threaded=True); threading.Thread(target=srv.serve_forever, daemon=True).start()
con = db.connect(dbp)
_prof = {"item": "article.item", "fields": {"title": ".item-title", "url": ".item-title@href", "mpn": ".sku", "brand": ".brand",
         "price": ".price", "availability": ".stock", "delivery": ".delivery"}}
con.execute("INSERT INTO sources(name,kind,enabled,search_url,profile,currency,delay_ms,allow_local,note) VALUES(?,?,?,?,?,?,?,?,?)",
            ("Нечіткий магазин В (HTML)", "html", 1, "{base}/mock/shop-c/fuzzy?q={q}", _json.dumps(_prof, ensure_ascii=False), "UAH", 0, 1, "експеримент"))
con.commit()
al = service.aliases(con)
sources = [dict(r) for r in con.execute("SELECT * FROM sources WHERE enabled=1 ORDER BY id")]
NORM = [norm_mpn(m) for _, _, m, _ in ALL]
IDX = {n: i for i, n in enumerate(NORM)}

def pool(q):
    out = []
    for s in sources:
        for raw in fetch_source(s, q, BASE):
            o = service._normalize(raw, s, al, db.now_iso())
            if o: out.append(o)
    return out

# ---- всесвіт пропозицій: кожна пропозиція отримує індекс деталі каталогу
universe = {}
for i, (_, _, m, _) in enumerate(ALL):
    for o in pool(m):
        universe[(o["source_id"], o["url"])] = IDX.get(o["norm_mpn"], -1)
assert -1 not in universe.values(), "не вдалося віднести пропозицію до деталі"
by_part = {}
for k, i in universe.items(): by_part.setdefault(i, set()).add(k)

# ---- довідник аналогів (істина)
CROSS = {}
for a, am, b, bm, _ in db.DEFAULT_CROSS:
    CROSS.setdefault(norm_mpn(am), set()).add(IDX[norm_mpn(bm)]); CROSS.setdefault(norm_mpn(bm), set()).add(IDX[norm_mpn(am)])

# ---- контрольний набір запитів
H = str.maketrans("BKCOEAMHPTX", "ВКСОЕАМНРТХ")
Q = []  # (тип, запит, множина деталей-істина)
def parts_prefix(p): return {i for i, n in enumerate(NORM) if n.startswith(p)}
for i, (_, _, m, _) in enumerate(CATALOG):
    Q.append(("A", m, {i} | CROSS.get(NORM[i], set())))
for i, (_, _, m, _) in enumerate(CATALOG[:12]):
    v = [m.replace(" ", "").lower(), m.replace(" ", "-"), m.upper().replace(" ", "").translate(H)][i % 3]
    if v == m.replace(" ", "-") and i % 3 == 1 and "-" in m: v = m.replace(" ", "").lower()
    Q.append(("B", v, {i} | CROSS.get(NORM[i], set())))
for p in ["0986452", "0986", "F026", "315", "GDB", "BKR", "09A", "5560"]:
    Q.append(("C", p, parts_prefix(norm_mpn(p))))
for words, brand in [("масляний фільтр", None), ("гальмівні колодки", None), ("свічка запалювання", None), ("повітряний фільтр", None),
                     ("амортизатор", None), ("підшипник маточини", None), ("фільтр", None), ("bosch фільтр", "Bosch")]:
    ws = norm_text(words).split()
    Q.append(("D", words, {i for i, (t, b, m, _) in enumerate(ALL) if all(w in norm_text(f"{t} {b}") for w in ws)}))
for q in ["ZX9999", "00000", "насос гур", "лампа h7"]:
    Q.append(("F", q, set()))

def keys(parts): return {k for p in parts for k in by_part.get(p, set())}

def methods(q):
    nq = norm_mpn(q); P = pool(q)
    mp = looks_like_mpn(q)
    def sim(o): return trigram_sim(nq, o["norm_mpn"]) if mp else trigram_sim(norm_text(q), norm_text(o["title"]))
    r = {}
    r["exact"] = [o for o in P if nq and mp and o["norm_mpn"] == nq]
    r["prefix"] = [o for o in P if nq and mp and o["norm_mpn"].startswith(nq)]
    r["trigram"] = sorted([o for o in P if sim(o) >= 0.5], key=lambda o: -sim(o))
    r["source"] = P
    res = service.search(con, q, BASE, force=True)
    r["combined"] = [o for g in res["groups"] for o in g["offers"]]
    return r, res

NAMES = ["exact", "prefix", "trigram", "source", "combined"]
acc = {n: {"tp": 0, "fp": 0, "fn": 0} for n in NAMES}
typ = {t: {n: {"tp": 0, "fp": 0, "fn": 0} for n in NAMES} for t in "ABCDF"}
rr = {n: [] for n in NAMES}; p5 = {n: [] for n in NAMES}
levels = {"IDENTICAL": [0, 0], "CROSS_REFERENCE": [0, 0], "CANDIDATE": [0, 0]}  # [правильних, усього]
group_err = 0; group_tot = 0; merge_err = 0; merge_tot = 0; ident_bad = 0; ident_tot = 0
for t, q, tp_parts in Q:
    truth = keys(tp_parts)
    r, res = methods(q)
    for n in NAMES:
        got = {(o["source_id"], o["url"]) for o in r[n]}
        tp, fp, fn = len(got & truth), len(got - truth), len(truth - got)
        for d in (acc[n], typ[t][n]): d["tp"] += tp; d["fp"] += fp; d["fn"] += fn
        if truth:
            order = [(o["source_id"], o["url"]) for o in r[n]]
            rank = next((i + 1 for i, k in enumerate(order) if k in truth), None)
            rr[n].append(1 / rank if rank else 0.0)
            p5[n].append(sum(1 for k in order[:5] if k in truth) / 5)
    for g in res["groups"]:
        part = IDX[g["key"][1]]
        ok = part in tp_parts
        levels[g["level"]][1] += 1; levels[g["level"]][0] += ok
        group_tot += 1; group_err += (not ok)
        for o in g["offers"]:
            merge_tot += 1; merge_err += (universe[(o["source_id"], o["url"])] != part)
        if g["level"] == "IDENTICAL":
            ident_tot += 1; ident_bad += (part not in tp_parts)

def prf(d):
    p = d["tp"] / (d["tp"] + d["fp"]) if d["tp"] + d["fp"] else 0.0
    r = d["tp"] / (d["tp"] + d["fn"]) if d["tp"] + d["fn"] else 0.0
    return p, r, (2 * p * r / (p + r) if p + r else 0.0)

out = {"n_queries": len(Q), "by_type_n": {t: sum(1 for x in Q if x[0] == t) for t in "ABCDF"},
       "n_universe": len(universe), "n_parts": len(ALL), "n_sources": len(sources),
       "overall": {n: dict(zip(("P", "R", "F1"), prf(acc[n])), MRR=st.mean(rr[n]), P5=st.mean(p5[n]), FP=acc[n]["fp"]) for n in NAMES},
       "by_type": {t: {n: dict(zip(("P", "R", "F1"), prf(typ[t][n])), FP=typ[t][n]["fp"], FN=typ[t][n]["fn"]) for n in NAMES} for t in "ABCDF"},
       "levels": {k: {"correct": v[0], "total": v[1]} for k, v in levels.items()},
       "wrong_groups": group_err, "total_groups": group_tot, "merge_err": merge_err, "merge_tot": merge_tot, "ident_bad": ident_bad, "ident_tot": ident_tot}

# ---- швидкодія: холодний пошук (мережа) і з кешем (власні витрати)
qs = [q for t, q, _ in Q if t in "ABD"][:30]
cold, warm = [], []
for q in qs:
    t0 = time.time(); service.search(con, q, BASE, force=True); cold.append((time.time() - t0) * 1000)
for q in qs:
    service.search(con, q, BASE, force=True)
    t0 = time.time(); service.search(con, q, BASE); warm.append((time.time() - t0) * 1000)
def pct(a, p): a = sorted(a); return a[min(len(a) - 1, int(round(p * (len(a) - 1))))]
out["timing_ms"] = {"cold_median": st.median(cold), "cold_p95": pct(cold, .95), "warm_median": st.median(warm), "warm_p95": pct(warm, .95), "n": len(qs)}
# масштабування за кількістю джерел
scal = {}
allsrc = [r["id"] for r in con.execute("SELECT id FROM sources WHERE enabled=1 ORDER BY id")]
for k in range(1, len(allsrc) + 1):
    con.execute("UPDATE sources SET enabled=0 WHERE enabled=1 AND id NOT IN (%s)" % ",".join(map(str, allsrc[:k])))
    for i in allsrc[:k]: con.execute("UPDATE sources SET enabled=1 WHERE id=?", (i,))
    con.commit(); ts = []
    for q in qs[:12]:
        t0 = time.time(); service.search(con, q, BASE, force=True); ts.append((time.time() - t0) * 1000)
    scal[k] = st.median(ts)
for i in allsrc: con.execute("UPDATE sources SET enabled=1 WHERE id=?", (i,))
con.commit(); out["scaling_ms"] = scal

# ---- викиди: синтетичне введення аномалії у групи з n>=5
import random; random.seed(7)
groups = []
for i, (_, _, m, _) in enumerate(CATALOG):
    res = service.search(con, m, BASE, force=True)
    for g in res["groups"]:
        if g["level"] == "IDENTICAL" and len(g["offers"]) >= 5:
            groups.append([o["unit_price"] for o in g["offers"]])
det = {}
fp = sum(1 for ps in groups for z in analytics.modified_z(ps) if z is not None and abs(z) > analytics.Z_LIMIT)
det["groups_n5"] = len(groups); det["fp_clean"] = fp; det["offers_clean"] = sum(len(p) for p in groups)
for mult in (3.0, 2.0, 1.5):
    hit = 0
    for ps in groups:
        j = random.randrange(len(ps)); q2 = list(ps); q2[j] = q2[j] * mult
        z = analytics.modified_z(q2)[j]; hit += (z is not None and abs(z) > analytics.Z_LIMIT)
    det[f"detect_x{mult}"] = hit / len(groups) if groups else None
out["outliers"] = det
json.dump(out, open(os.path.join(os.path.dirname(__file__), "results.json"), "w"), ensure_ascii=False, indent=1)
print(json.dumps(out, ensure_ascii=False, indent=1))
