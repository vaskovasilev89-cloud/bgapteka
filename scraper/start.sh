#!/usr/bin/env sh
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
fi
echo "Отвори http://127.0.0.1:8000"
.venv/bin/python -m uvicorn pharma.app:app --host 127.0.0.1 --port 8000
