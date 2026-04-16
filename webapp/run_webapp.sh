#!/bin/bash
cd /home/ubuntu/polymarket-db/webapp
source ../venv/bin/activate
export ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-your-key-here}"
exec uvicorn app:app --host 0.0.0.0 --port 8080 --workers 1
