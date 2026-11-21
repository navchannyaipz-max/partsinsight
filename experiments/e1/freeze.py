"""Фіксує умови запуску E1: 20 затверджених запитів, вибрані джерела, зв’язки аналогів, налаштування й контрольні суми коду.
Приклад: python freeze.py --run-id e1-2026-12 --sources «Google Shopping через SerpApi (потрібен ключ)»
"""
import argparse, pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import e1lib
from e1lib import E1Error, HERE
from app import db


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--sources", required=True, help="назви джерел через «;»")
    ap.add_argument("--queries", default=str(HERE / "queries.csv"))
    ap.add_argument("--config", default=str(HERE / "config.json"))
    ap.add_argument("--db", default=db.default_path())
    ap.add_argument("--runs-dir", default=str(HERE / "runs"))
    ap.add_argument("--test-run", action="store_true", help="тестовий запуск на контрольних даних (дозволяє демоджерела)")
    a = ap.parse_args()
    try:
        run = e1lib.freeze(a.run_id, a.queries, a.config, a.db, [s.strip() for s in a.sources.split(";") if s.strip()], a.runs_dir, a.test_run)
        import json
        fr = json.loads((run / "frozen.json").read_text(encoding="utf-8"))
        print(f"Зафіксовано запуск {a.run_id}: {len(fr['queries'])} запитів, {len(fr['sources'])} джерел, {len(fr['requests'])} звернень "
              f"(з них за аналогами: {sum(r['role'] == 'analog' for r in fr['requests'])}). Папка: {run}")
    except E1Error as e:
        print("ПОМИЛКА:", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
