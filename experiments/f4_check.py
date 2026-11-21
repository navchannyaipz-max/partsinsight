"""Автоматична перевірка F4 (живе джерело → кеш → збій із попередніми даними). Працює на ТИМЧАСОВІЙ копії бази: ваші дані не змінюються.
Результат — файл experiments/f4_result.md. Крок «збій» ЗМОДЕЛЬОВАНИЙ (джерело вимикається штучно) і так і позначений у звіті.
Потрібен інтернет і хоча б одне увімкнене реальне джерело. Приклад: python f4_check.py "GDB1550" """
import argparse, os, pathlib, shutil, sqlite3, sys, tempfile, datetime
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import db, service, fetcher  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("query", nargs="?", default="GDB1550")
    ap.add_argument("--db", default=db.default_path())
    ap.add_argument("--source", help="назва джерела (за замовчуванням перше увімкнене не демо)")
    a = ap.parse_args()
    def _has_schema(path):
        try:
            c = sqlite3.connect(path); ok = c.execute("SELECT 1 FROM sqlite_master WHERE name='settings'").fetchone() is not None; c.close(); return ok
        except sqlite3.Error:
            return False
    if not os.path.exists(a.db) or not _has_schema(a.db):
        print("База ще не створена: запустіть програму (start.command / start.bat), додайте джерело й ключі, потім повторіть."); sys.exit(2)
    tmp = pathlib.Path(tempfile.mkdtemp()) / "f4.db"
    src = sqlite3.connect(a.db)
    dst = sqlite3.connect(tmp)
    src.backup(dst); src.close(); dst.close()
    con = db.connect(str(tmp))
    fetcher.set_secrets({r["key"]: r["value"] for r in con.execute("SELECT key,value FROM settings")})
    rows = con.execute("SELECT name FROM sources WHERE enabled=1 ORDER BY id").fetchall()
    names = [r["name"] for r in rows if not r["name"].startswith("Демо")]
    name = a.source or (names[0] if names else None)
    if not name:
        print("Немає увімкнених реальних джерел: додайте джерело в адмінці й повторіть."); sys.exit(2)
    con.execute("UPDATE sources SET enabled=0 WHERE name<>?", (name,)); con.commit()
    con.execute("DELETE FROM cache") if con.execute("SELECT name FROM sqlite_master WHERE name='cache'").fetchone() else None
    con.commit()

    def run(label, force):
        r = service.search(con, a.query, "http://127.0.0.1", force=force)
        st = [(s.get("name"), s.get("state"), s.get("fetched_at"), s.get("error")) for s in r.get("sources", [])]
        return label, r.get("state"), sum(len(g.get("offers", [])) for g in r.get("groups", [])), st

    out = [run("1. Живий запит", True), run("2. Повторний запит (кеш)", False)]
    con.execute("UPDATE sources SET search_url='http://127.0.0.1:9/unreachable?q={q}' WHERE name=?", (name,)); con.commit()
    if con.execute("SELECT name FROM sqlite_master WHERE name='cache'").fetchone():
        con.execute("UPDATE cache SET fetched_at=datetime('now','-3 day')") ; con.commit()
    out.append(run("3. ЗМОДЕЛЬОВАНИЙ збій джерела (кеш застарів)", True))
    ok = out[0][1] in ("ok", "partial") and out[0][2] > 0
    lines = [f"# Перевірка F4 — {datetime.datetime.now():%Y-%m-%d %H:%M}", f"Запит: `{a.query}`; джерело: «{name}». Виконано на тимчасовій копії бази.",
             "Крок 3 — штучний збій (адреса джерела замінена на недосяжну); це симуляція, а не реальна аварія джерела.", ""]
    for label, state, n, st in out:
        lines += [f"## {label}", f"- стан відповіді: `{state}`, пропозицій: {n}"] + [f"- {s}" for s in st] + [""]
    lines.append("**Висновок:** " + ("крок 1 отримав реальні пропозиції; перегляньте кроки 2–3 на відповідність очікуванню (кеш → «кеш»; збій → «застарілі дані» з початковою датою)."
                                    if ok else "крок 1 НЕ отримав реальних пропозицій — F4 на живому джерелі не підтверджено (перевірте ключі/мережу)."))
    (ROOT / "experiments" / "f4_result.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    shutil.rmtree(tmp.parent, ignore_errors=True)


if __name__ == "__main__":
    main()
