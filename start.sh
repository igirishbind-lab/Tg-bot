#!/bin/bash
pip install --upgrade pip
pip install -r requirements.txt
gunicorn --bind 0.0.0.0:$PORT advanced_bot_full:app
