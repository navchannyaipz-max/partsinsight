"""Фінальний звіт E1: Hit@1 = підтверджені перші позиції / 20; Precision@5 = підтверджені в топ-5 / 100 для кожного методу.
Звіт створюється лише після повної перевірки таблиці.
Приклад: python metrics.py --run-id e1-2026-12"""
import argparse, pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import e1lib
from e1lib import E1Error, HERE
from app import db


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--runs-dir", default=str(HERE / "runs"))
    a = ap.parse_args()
    try:
        print(e1lib.final_report(f"{a.runs_dir}/{a.run_id}"))
    except E1Error as e:
        print("ПОМИЛКА:", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
