#!/bin/bash
# Подвійний клік у Finder запускає PartsInsight.
# Усі дані (база, ключі, архів, копії) та середовище Python лежать у ~/PartsInsightData і не залежать від папки програми.
cd "$(dirname "$0")" || exit 1
DATA="${PARTSINSIGHT_DATA:-$HOME/PartsInsightData}"
mkdir -p "$DATA"
export PARTSINSIGHT_DATA="$DATA"

PY=$(command -v python3)
if [ -z "$PY" ]; then
  echo "Python 3 не знайдено. Встановіть його з https://www.python.org/downloads і запустіть файл знову."
  read -r -p "Натисніть Enter, щоб закрити…"; exit 1
fi

if [ ! -d "$DATA/venv" ]; then
  echo "Перший запуск: створюю середовище Python…"
  "$PY" -m venv "$DATA/venv" || { echo "Не вдалося створити середовище."; read -r -p "Enter…"; exit 1; }
fi
# shellcheck disable=SC1091
source "$DATA/venv/bin/activate"

# Пакети ставимо лише коли змінився requirements.txt
if [ ! -f "$DATA/venv/.deps_ok" ] || [ requirements.txt -nt "$DATA/venv/.deps_ok" ]; then
  echo "Встановлюю пакети (потрібен інтернет)…"
  pip install -q -r requirements.txt && touch "$DATA/venv/.deps_ok" || { echo "Помилка встановлення пакетів."; read -r -p "Enter…"; exit 1; }
fi

export PARTSINSIGHT_DEMO="${PARTSINSIGHT_DEMO:-0}"
export PORT="${PORT:-5050}"
export BASE_URL="http://127.0.0.1:$PORT"
echo "Дані: $DATA"
echo "Запускаю на http://127.0.0.1:$PORT  (зупинити: Ctrl+C або закрийте це вікно)"
( sleep 2; open "http://127.0.0.1:$PORT" ) &
python run.py
