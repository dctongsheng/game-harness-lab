#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
node -e 'const v=process.versions.node.split(".").map(Number); if(v[0]<22 || (v[0]===22 && v[1]<18)) throw Error("Node >=22.18.0 required")'
[[ -x .venv/bin/python ]] || uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -r requirements-pokemon.lock
uv pip install --python .venv/bin/python --no-deps -e .
pokemon_source=.runtime/pokemon-showdown
pokemon_commit=7244fe8e28fa8bc4d0809ae30d183c608b62bb2b
if [[ ! -d "$pokemon_source" ]]; then
  git init "$pokemon_source"
  git -C "$pokemon_source" remote add origin https://github.com/smogon/pokemon-showdown.git
  git -C "$pokemon_source" fetch --depth 1 origin "$pokemon_commit"
  git -C "$pokemon_source" checkout --detach FETCH_HEAD
fi
[[ "$(git -C "$pokemon_source" rev-parse HEAD)" == "$pokemon_commit" ]] || { echo 'Showdown checkout differs from pinned commit; preserve it and inspect manually.' >&2; exit 1; }
[[ -z "$(git -C "$pokemon_source" status --porcelain --untracked-files=no)" ]] || { echo 'Showdown has source edits; preserve them and inspect manually.' >&2; exit 1; }
npm ci --ignore-scripts --omit=dev --prefix "$pokemon_source"
node "$pokemon_source/build" --force
.venv/bin/python -c 'from game_harness.games import get_game; s=get_game("pokemon").create(0,{}); print({"game": "pokemon", "native_turn": s.snapshot()["native_turn"], "legal_choices": len(s.candidates())}); s.close()'
