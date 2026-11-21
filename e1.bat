@echo off
chcp 65001 >nul
cd /d "%~dp0"
if "%PARTSINSIGHT_DATA%"=="" set PARTSINSIGHT_DATA=%USERPROFILE%\PartsInsightData
if exist "%PARTSINSIGHT_DATA%\venv\Scripts\activate.bat" call "%PARTSINSIGHT_DATA%\venv\Scripts\activate.bat"
python experiments\e1\run_all.py
echo.
echo Перевірка F4 (живе джерело - кеш - збій):
python experiments\f4_check.py
pause
