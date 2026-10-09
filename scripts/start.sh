#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_dir"
if [[ ! -f .env ]]; then
  echo "缺少 .env：请复制 .env.example 并填写所需配置。" >&2
  exit 1
fi
if [[ ! -x .venv/bin/uvicorn ]]; then
  echo "缺少 .venv/bin/uvicorn：请先按 README 安装依赖。" >&2
  exit 1
fi
exec .venv/bin/uvicorn src.server:app --host "${HOST:-127.0.0.1}" --port "${PORT:-8000}" --env-file .env
