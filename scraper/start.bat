@echo off
chcp 65001 >nul
cd /d %~dp0
if not exist .venv (
  echo Първо пускане: инсталиране на зависимостите...
  py -3 -m venv .venv
  .venv\Scripts\python -m pip install -r requirements.txt
)
if not exist config.json copy config.example.json config.json >nul
start "" http://127.0.0.1:8000
.venv\Scripts\python -m uvicorn pharma.app:app --host 127.0.0.1 --port 8000
