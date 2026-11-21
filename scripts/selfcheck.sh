#!/usr/bin/env bash
# 关键自测脚本：在临时 SQLite 上跑完整 pytest 套件。
# 用法：./scripts/selfcheck.sh
set -euo pipefail
cd "$(dirname "$0")/.."

export PYTHONPATH="$(pwd):${PYTHONPATH:-}"
export DATABASE_URL="sqlite:////tmp/biodiversity_selfcheck.db"
export SEED_DEMO=0
rm -f /tmp/biodiversity_selfcheck.db

python3 -m pytest tests/ -v
