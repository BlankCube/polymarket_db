#!/bin/bash
# Long-running indexer script - run with nohup
cd /home/ubuntu/polymarket-db
source venv/bin/activate
exec python3 run.py index-all 2>&1 | tee -a indexer.log
