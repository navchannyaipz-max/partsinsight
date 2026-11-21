"""Збір відповідей для запуску E1 (початковий запит і запити за аналогами). Повторний запуск повторює лише пропущені або невдалі звернення.
Приклад: python collect.py --run-id e1-2026-12"""
import argparse, pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import e1lib
from e1lib import E1Error, HERE
from app import db


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--db", default=db.default_path())
    ap.add_argument("--runs-dir", default=str(HERE / "runs"))
    a = ap.parse_args()
    try:
        r = e1lib.collect(f"{a.runs_dir}/{a.run_id}", a.db)
        print(r)
        if r["missing"]:
            print("Збір неповний: повторіть команду — буде повторено лише пропущені звернення.")
            sys.exit(2)
    except E1Error as e:
        print("ПОМИЛКА:", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
