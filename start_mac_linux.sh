#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
[ -e .env ] || cp .env.example .env
python3 -m pip install -r requirements.txt
python3 app.py
