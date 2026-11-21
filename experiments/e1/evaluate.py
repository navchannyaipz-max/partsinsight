"""Офлайн-оцінювання запуску E1 на збереженому знімку: baseline і PartsInsight → methods_internal.csv та аркуш експерта expert_sheet.csv.
Приклад: python evaluate.py --run-id e1-2026-12"""
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
        out = e1lib.evaluate(f"{a.runs_dir}/{a.run_id}")
        print("Готово. Розмітьте файл:", out / "expert_sheet.csv", "(колонки «рішення»: так / ні / сумнівно, та «підстава»)")
    except E1Error as e:
        print("ПОМИЛКА:", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
