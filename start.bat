@echo off
chcp 65001 >nul
cd /d "%~dp0"
if "%PARTSINSIGHT_DATA%"=="" set "PARTSINSIGHT_DATA=%USERPROFILE%\PartsInsightData"
if not exist "%PARTSINSIGHT_DATA%" mkdir "%PARTSINSIGHT_DATA%"
where python >nul 2>nul || (echo Python не знайдено. Встановіть з python.org і запустіть знову. & pause & exit /b 1)
if not exist "%PARTSINSIGHT_DATA%\venv" (echo Перший запуск: створюю середовище... & python -m venv "%PARTSINSIGHT_DATA%\venv")
call "%PARTSINSIGHT_DATA%\venv\Scripts\activate"
if not exist "%PARTSINSIGHT_DATA%\venv\.deps_ok" (echo Встановлюю пакети... & pip install -q -r requirements.txt && type nul > "%PARTSINSIGHT_DATA%\venv\.deps_ok")
if "%PARTSINSIGHT_DEMO%"=="" set PARTSINSIGHT_DEMO=0
if "%PORT%"=="" set PORT=5050
set BASE_URL=http://127.0.0.1:%PORT%
echo Дані: %PARTSINSIGHT_DATA%
echo Запускаю на http://127.0.0.1:%PORT%  (зупинити: Ctrl+C)
start "" http://127.0.0.1:%PORT%
python run.py
