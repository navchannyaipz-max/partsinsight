"""КОНТРОЛЬНИЙ (ТЕСТОВИЙ) прогін E1 на синтетичних даних: фіксація → збір → відмова на неповному зборі → повтор → офлайн-оцінювання → розмітка → звіт.

Усе, що створює цей скрипт, позначене «ТЕСТОВИЙ ЗАПУСК» і НЕ є результатом експерименту. Розмітка тут автоматична (за простим тестовим правилом),
у справжньому запуску її виконує експерт. Запуск: python control_run.py [--out папка]
"""
import argparse, pathlib, shutil, sys, tempfile
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import e1lib
from e1lib import E1Error
from app import create_app

QUERIES = ([("full", q) for q in ("GDB1550", "W 719/30", "0986452041", "P 85 114", "OC 47")]
           + [("variant", q) for q in ("GDB-1550", "w719/30", "0 986 452 041", "p85114", "Ford DG9Z-2200-N")]
           + [("partial", q) for q in ("GDB15", "W 719", "0986452", "P 85", "DG9Z-22")]
           + [("text", q) for q in ("колодки trw", "фільтр bosch", "фільтр mann", "свічка ngk", "амортизатор kyb")])


def item(mpn, brand):
    return {"title": f"Колодки {brand} {mpn}", "mpn": mpn, "brand": brand, "price": 300, "currency": "UAH", "pack": 1, "url": f"https://control.test/{mpn}"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(pathlib.Path(tempfile.mkdtemp())))
    out = pathlib.Path(ap.parse_args().out)
    out.mkdir(parents=True, exist_ok=True)
    dbp = str(out / "control.db")
    create_app({"DATABASE": dbp, "TESTING": True})
    from app import db
    src = db.connect(dbp).execute("SELECT name FROM sources ORDER BY id LIMIT 1").fetchone()[0]
    qp = out / "queries.csv"
    e1lib.write_csv(qp, ["id", "type", "query", "expected_rule", "rationale", "evidence_source", "status"],
                    [{"id": f"Q{i:02d}", "type": t, "query": q, "expected_rule": "ТЕСТОВЕ правило", "rationale": "ТЕСТОВИЙ ЗАПУСК",
                      "evidence_source": "контрольні дані", "status": "ЗАТВЕРДЖЕНО"} for i, (t, q) in enumerate(QUERIES, 1)])
    fail = {"on": True}

    def fetch(s, text):
        if fail["on"] and text == "0986AB1234":
            raise RuntimeError("тестовий збій")
        return {"0986AB1234": [item("0986AB1234", "Bosch")], "0986452041": [item("0986452041", "Bosch")]}.get(text, [])

    run = e1lib.freeze("CONTROL-TEST", qp, e1lib.HERE / "config.json", dbp, [src], out / "runs", test_run=True)
    print("1. ФІКСАЦІЯ (ТЕСТОВИЙ ЗАПУСК):", run)
    print("2. ЗБІР із імітованим збоєм:", e1lib.collect(run, dbp, fetch, pause=False))
    try:
        e1lib.evaluate(run)
    except E1Error as e:
        print("3. ВІДМОВА на неповному зборі (очікувано):\n", str(e).splitlines()[0], "\n", "\n".join(str(e).splitlines()[1:3]))
    fail["on"] = False
    print("4. ПОВТОР лише пропущених:", e1lib.collect(run, dbp, fetch, pause=False))
    e1lib.evaluate(run)
    print("5. ОФЛАЙН-ОЦІНЮВАННЯ виконано; аркуш експерта:", run / "evaluation" / "expert_sheet.csv")
    try:
        e1lib.apply_labels(run); e1lib.final_report(run)
    except E1Error as e:
        print("6. ЗВІТ заблоковано до розмітки (очікувано):", str(e).splitlines()[0])
    p = run / "evaluation" / "expert_sheet.csv"
    sheet = e1lib.read_csv(p)
    for r in sheet:
        r["рішення"] = "так" if r["артикул"] in ("0986AB1234", "0986452041") else "ні"
        r["підстава"] = "АВТОМАТИЧНА тестова розмітка (не експерт)"
    e1lib.write_csv(p, e1lib.SHEET_FIELDS, sheet)
    e1lib.apply_labels(run)
    print("7. ФІНАЛЬНИЙ ЗВІТ (ТЕСТОВИЙ, не результат експерименту):\n" + e1lib.final_report(run))
    print("Папка прогону:", out)


if __name__ == "__main__":
    main()
