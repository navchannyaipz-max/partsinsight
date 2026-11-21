"""Автоматичний прогін E1 «одним кліком» з безпечними зупинками.
Скрипт сам: перевіряє запити → фіксує умови → збирає відповіді (кілька спроб) → готує аркуш для експерта →
(після розмітки) переносить мітки й рахує Hit@1 / Precision@5. Його можна запускати знову: він продовжує з місця зупинки.
ЛЮДИНА лишається потрібною у двох місцях, і скрипт їх не обходить: (1) статус запитів «ЗАТВЕРДЖЕНО» у queries.csv,
(2) заповнення колонок «рішення» та «підстава» в expert_sheet.csv."""
import argparse, datetime, pathlib, sys, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import e1lib
from e1lib import E1Error, HERE
from app import db


def say(m):
    print(m, flush=True)


def stop(code, m):
    say("\n" + m)
    sys.exit(code)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=db.default_path())
    ap.add_argument("--runs-dir", default=str(HERE / "runs"))
    ap.add_argument("--run-id", help="за замовчуванням — останній запуск або новий e1-РРРР-ММ-ДД")
    ap.add_argument("--sources", help="назви через «;» (за замовчуванням усі увімкнені не демо-джерела)")
    ap.add_argument("--passes", type=int, default=3, help="скільки разів повторювати збір пропущених звернень")
    ap.add_argument("--wait", type=int, default=30, help="пауза між спробами, с")
    a = ap.parse_args()
    runs = pathlib.Path(a.runs_dir)
    cur = runs / "CURRENT"
    try:
        # 1. запити
        rows = e1lib.read_csv(HERE / "queries.csv")
        prob = e1lib.validate_queries(rows)
        run_id = a.run_id or (cur.read_text().strip() if cur.exists() else None)
        if prob and not (run_id and (runs / run_id / "frozen.json").exists()):
            stop(2, "КРОК 1 з 5 не пройдено: запити ще не затверджені.\n" + "\n".join(" • " + p for p in prob[:8]) +
                 ("\n …" if len(prob) > 8 else "") +
                 "\nВідкрийте experiments/e1/queries.csv, перевірте кожен запит і в колонці status впишіть ЗАТВЕРДЖЕНО. "
                 "Це робить людина — скрипт не має права затверджувати запити замість вас.")
        # 2. фіксація
        if not (run_id and (runs / run_id / "frozen.json").exists()):
            run_id = a.run_id or "e1-" + datetime.date.today().isoformat()
            if a.sources:
                names = [s.strip() for s in a.sources.split(";") if s.strip()]
            else:
                con = db.connect(a.db)
                names = [r["name"] for r in con.execute("SELECT name FROM sources WHERE enabled=1 ORDER BY id") if not r["name"].startswith("Демо")]
                con.close()
            if not names:
                stop(2, "КРОК 2 з 5: немає жодного увімкненого реального джерела. Додайте джерело в адмінці (розділ «Джерела») і запустіть знову.")
            e1lib.freeze(run_id, HERE / "queries.csv", HERE / "config.json", a.db, names, a.runs_dir)
            runs.mkdir(parents=True, exist_ok=True)
            cur.write_text(run_id)
            say(f"КРОК 2 з 5: умови зафіксовано, запуск {run_id}, джерел: {len(names)}.")
        else:
            say(f"Продовжую запуск {run_id}.")
        run = runs / run_id
        cur.write_text(run_id)
        # 3. збір
        frozen = e1lib.load_frozen(run)
        for p in range(1, a.passes + 1):
            r = e1lib.collect(run, a.db)
            say(f"КРОК 3 з 5: спроба {p}/{a.passes}: {r}")
            if not r["missing"]:
                break
            if p < a.passes:
                time.sleep(a.wait)
        left = e1lib.missing_requests(run, frozen)
        if left:
            stop(3, f"Не вдалося зібрати {len(left)} звернень (джерело недоступне або відмовило). Це НЕ приховується й не рахується як «нічого не знайдено». "
                    "Перевірте ключі/мережу й запустіть цей самий файл ще раз — буде повторено лише пропущене.")
        # 4. оцінка
        if not (run / "evaluation" / "evaluation.json").exists():
            e1lib.evaluate(run)
            say("КРОК 4 з 5: обчислено ранжування обох методів, створено аркуш експерта.")
        sheet = run / "evaluation" / "expert_sheet.csv"
        empty = [x for x in e1lib.read_csv(sheet) if not (x.get("рішення") or "").strip()]
        if empty:
            stop(4, f"КРОК 4: потрібна ваша експертна розмітка — незаповнених рядків: {len(empty)}.\nВідкрийте файл:\n  {sheet}\n"
                    "і в колонці «рішення» вкажіть оцінку, а в «підстава» — чому (за каталогом/сторінкою товару). Збережіть як CSV і запустіть цей файл знову.")
        # 5. мітки й метрики
        r = e1lib.apply_labels(run)
        say(f"КРОК 5 з 5: мітки перенесено: {r}")
        say(e1lib.final_report(run))
        say(f"\nГотово. Результати: {run / 'evaluation'} та e1_report.md")
    except E1Error as e:
        stop(1, "ПОМИЛКА: " + str(e))


if __name__ == "__main__":
    main()
