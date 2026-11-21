@echo off
chcp 65001 >nul
cd /d "%~dp0"
if "%PARTSINSIGHT_DATA%"=="" set PARTSINSIGHT_DATA=%USERPROFILE%\PartsInsightData
if exist "%PARTSINSIGHT_DATA%\venv\Scripts\activate.bat" call "%PARTSINSIGHT_DATA%\venv\Scripts\activate.bat"
python tools\publish.py
pause
