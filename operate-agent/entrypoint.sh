#!/bin/sh
set -e

mkdir -p /data/claude /data/sessions /data/workspace/scripts
ln -sfn /data/claude /root/.claude

cp -f /app/harness/CLAUDE.md /data/workspace/CLAUDE.md

exec python3 /app/bridge.py
