@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist venv (
  echo Birinchi marta: kerakli narsalar o`rnatilmoqda, kuting...
  python -m venv venv
)
call venv\Scripts\activate
pip install -q -r requirements.txt
echo Bot ishga tushdi. Oynani yopmang!
python bot.py
pause
