#!/bin/bash
set -e
export PYTHONUNBUFFERED=1
export PYTHONPATH=.
pip install --upgrade pip
pip install -r requirements.txt
# bind via gunicorn; Render provides $PORT
gunicorn --workers 1 --bind 0.0.0.0:$PORT advanced_bot_full:app
