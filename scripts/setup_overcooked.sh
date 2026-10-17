#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
# Keep the upstream Python 3.10 environment separate from the host application.
if [[ ! -x .venv-overcooked/bin/python ]]; then
  uv venv --python 3.10 .venv-overcooked
fi
uv pip sync --python .venv-overcooked/bin/python requirements-overcooked.txt
.venv/bin/python -c 'from game_harness.games import get_game; s=get_game("overcooked").create(0,{}); print({"native_tick":s.snapshot()["native_tick"],"score":s.snapshot()["score"]}); s.close()'
