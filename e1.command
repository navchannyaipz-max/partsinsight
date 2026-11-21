#!/bin/bash
# Подвійний клік: автоматичний прогін експерименту E1 (продовжується з місця зупинки). Потрібен інтернет і налаштовані ключі в програмі.
cd "$(dirname "$0")" || exit 1
export PARTSINSIGHT_DATA="${PARTSINSIGHT_DATA:-$HOME/PartsInsightData}"
[ -d "$PARTSINSIGHT_DATA/venv" ] && source "$PARTSINSIGHT_DATA/venv/bin/activate"
python3 experiments/e1/run_all.py
echo; echo "Перевірка F4 (живе джерело → кеш → збій):"
python3 experiments/f4_check.py
read -r -p "Натисніть Enter, щоб закрити…"
