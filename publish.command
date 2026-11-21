#!/bin/bash
# Подвійний клік: програма сама запитає потрібні дані й опублікує проєкт на GitHub.
cd "$(dirname "$0")" || exit 1
DATA="${PARTSINSIGHT_DATA:-$HOME/PartsInsightData}"
[ -d "$DATA/venv" ] && source "$DATA/venv/bin/activate"
python3 tools/publish.py
read -r -p "Натисніть Enter, щоб закрити…"
