#!/bin/sh
set -eu
cd "$(dirname "$0")"
if command -v uv >/dev/null 2>&1; then
  if [ ! -x .venv/bin/python ]; then uv venv --python 3.13 .venv; fi
  uv pip install --python .venv/bin/python -r requirements.lock -e . --no-deps
else
  python3 -m venv .venv
  .venv/bin/python -m pip install -r requirements.lock -e . --no-deps
fi
if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
fi
echo '安装完成。配置 .env 后运行 ./run.sh'
