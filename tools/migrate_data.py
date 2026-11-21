"""Перенесення вже зібраних даних (архів деталей, ціни, довідники) без втрат і без ключів.

  python tools/migrate_data.py export [--out ПАПКА] [--with-secrets]   — чиста копія вашої бази для завантаження на хостинг
  python tools/migrate_data.py merge ФАЙЛ.db                           — додати дані з іншої бази до поточної (нічого не перезаписує)
  python tools/migrate_data.py info [ФАЙЛ.db]                          — скільки записів у базі

За замовчуванням експорт НЕ містить ключів доступу й пароля адміністратора (таблиця settings очищується),
а також журнал запитів. Шлях до бази береться з PARTSINSIGHT_DATA (за замовчуванням ~/PartsInsightData).
"""
from __future__ import annotations
import argparse, pathlib, sqlite3, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import db  # noqa: E402

TABLES = ["sources", "parts", "price_snapshots", "vehicle_parts", "cache", "brands", "aliases", "cross_refs", "car_models", "part_types", "watchlist", "searches", "settings"]


def counts(path) -> dict:
    c = sqlite3.connect(path); out = {}
    for t in TABLES:
        try: out[t] = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except sqlite3.Error: out[t] = None
    c.close(); return out


def export(out_dir, with_secrets=False) -> pathlib.Path:
    src = pathlib.Path(db.default_path())
    if not src.exists():
        raise SystemExit("Базу не знайдено. Спершу запустіть програму й виконайте кілька пошуків.")
    out = pathlib.Path(out_dir).expanduser(); out.mkdir(parents=True, exist_ok=True)
    dst = out / "partsinsight.db"
    if dst.exists(): dst.unlink()
    a = sqlite3.connect(src); b = sqlite3.connect(dst); a.backup(b); a.close()
    if not with_secrets:
        b.execute("DELETE FROM settings"); b.execute("DELETE FROM searches")
    b.commit(); b.execute("VACUUM"); b.close()
    return dst


def merge(other) -> dict:
    other = pathlib.Path(other)
    if not other.exists(): raise SystemExit(f"Файл не знайдено: {other}")
    con = db.connect(db.default_path()); con.execute("ATTACH DATABASE ? AS o", (str(other),))
    n = {}
    def cnt(sql, *p): return con.execute(sql, p).rowcount
    n["brands"] = cnt("INSERT OR IGNORE INTO brands(canonical,display,verified,source,first_seen,seen_count) SELECT canonical,display,verified,source,first_seen,seen_count FROM o.brands")
    n["aliases"] = cnt("INSERT OR IGNORE INTO aliases SELECT alias,canonical FROM o.aliases")
    n["car_models"] = cnt("INSERT OR IGNORE INTO car_models(make,model,year_from,year_to) SELECT make,model,year_from,year_to FROM o.car_models")
    n["cross_refs"] = cnt("INSERT INTO cross_refs(mfr_a,mpn_a,mfr_b,mpn_b,ref_source,verified) SELECT mfr_a,mpn_a,mfr_b,mpn_b,ref_source,verified FROM o.cross_refs x "
                          "WHERE NOT EXISTS(SELECT 1 FROM cross_refs c WHERE c.mpn_a=x.mpn_a AND c.mpn_b=x.mpn_b)")
    n["parts"] = cnt("INSERT OR IGNORE INTO parts(norm_mpn,brand_key,brand,mpn,title,kind,image,first_seen,last_seen,seen_count) "
                     "SELECT norm_mpn,brand_key,brand,mpn,title,kind,image,first_seen,last_seen,seen_count FROM o.parts")
    cols = [r[1] for r in con.execute("PRAGMA o.table_info(price_snapshots)") if r[1] not in ("id", "part_id")]
    ccols = [c for c in cols if c in {r[1] for r in con.execute("PRAGMA main.table_info(price_snapshots)")}]
    lst = ",".join("s." + c for c in ccols); cl = ",".join(ccols)
    n["price_snapshots"] = cnt(f"INSERT INTO price_snapshots(part_id,{cl}) SELECT p.id,{lst} FROM o.price_snapshots s "
                               "JOIN o.parts op ON op.id=s.part_id JOIN parts p ON p.norm_mpn=op.norm_mpn AND p.brand_key=op.brand_key "
                               "WHERE NOT EXISTS(SELECT 1 FROM price_snapshots x WHERE x.part_id=p.id AND x.source=s.source AND x.price=s.price AND x.fetched_at=s.fetched_at AND COALESCE(x.url,'')=COALESCE(s.url,''))")
    n["vehicle_parts"] = cnt("INSERT OR IGNORE INTO vehicle_parts(make,model,year,engine,part_type,part_id,first_seen,last_seen,times) "
                             "SELECT v.make,v.model,v.year,v.engine,v.part_type,p.id,v.first_seen,v.last_seen,v.times FROM o.vehicle_parts v "
                             "JOIN o.parts op ON op.id=v.part_id JOIN parts p ON p.norm_mpn=op.norm_mpn AND p.brand_key=op.brand_key")
    con.commit(); con.execute("DETACH DATABASE o"); con.close()
    return n


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    e = sp.add_parser("export"); e.add_argument("--out", default=str(pathlib.Path.home() / "PartsInsightExport")); e.add_argument("--with-secrets", action="store_true")
    m = sp.add_parser("merge"); m.add_argument("file")
    i = sp.add_parser("info"); i.add_argument("file", nargs="?")
    a = ap.parse_args()
    if a.cmd == "export":
        p = export(a.out, a.with_secrets)
        print(f"Готово: {p}\nЗаписів: {counts(p)}")
        print("Ключі й пароль адміністратора " + ("ВКЛЮЧЕНО (обережно!)" if a.with_secrets else "не включено") + ". Завантажте файл на хостинг (docs/DEPLOY.md, розділ «Перенесення даних»).")
    elif a.cmd == "merge":
        print("Додано записів:", merge(a.file))
    else:
        print(counts(a.file or db.default_path()))


if __name__ == "__main__":
    main()
