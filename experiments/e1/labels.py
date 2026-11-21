"""Переносить розмітку експерта (expert_sheet.csv) на позиції обох методів → evaluation/e1_table.csv.
Приклад: python labels.py --run-id e1-2026-12"""
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
        r = e1lib.apply_labels(f"{a.runs_dir}/{a.run_id}")
        print(r)
        if r["unlabeled_positions"]:
            print("Є нерозмічені позиції — фінальний звіт буде заблоковано, доки експерт не заповнить усі рядки.")
    except E1Error as e:
        print("ПОМИЛКА:", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
