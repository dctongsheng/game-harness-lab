# Third-party notices

## xiangqi.js

- Upstream: https://github.com/lengyanyu258/xiangqi.js
- Pinned upstream commit: `f9019ac2303d4b80ef0b82fd0515bfb55a80a62b`
- License: **BSD-2-Clause**. This dependency is not relicensed by this project.
- Source and full notice: `src/game_harness/games/vendor/xiangqi.cjs` and `LICENSE.xiangqi`.
- Imported unchanged from the user-supplied `decision-arena/packages/games/src/vendor` snapshot, whose third-party notice records this upstream commit.

The Python adapter and Node JSON bridge use the public rules API for complete legal
move generation, move application, and result detection. They do not use the
upstream `perft` helper or an engine search policy. All gameplay moves are chosen
by the configured Decision model. The original copyright and disclaimer remain
inside the distributed vendor directory.

## GEPA and Python dependencies

GEPA is pinned by source commit in `pyproject.toml`; its MIT notice and other
dependencies' license notices remain in their installed distributions.

## Decision Arena board and arcade rules

- Source repository: https://github.com/dctongsheng/decison-arena
- Reference checkout supplied by the user; pinned commit `4e2bda1d6c9b2545837761e2c7d19e2ce546aa9d`.
- Copyright (c) 2026 Dougo, **MIT**. Full licenses are retained in `games/vendor/arena-board/LICENSE` and `games/vendor/arena-arcade/LICENSE` under `src/game_harness/`.
- Military Chess and Gomoku rules, plus Tetris and original platformer rules and required common/core definitions, are copied without source changes. Each vendor directory includes a `SOURCE.json` with file checksums and source provenance.
- `board_bridge.ts` and `arcade_bridge.ts` implement the local paused adapter contract. Compiled `.cjs` files bundle these MIT rules for Node 18+; no external packages or network are used at gameplay runtime.
- These adapters do not copy the reference project's legacy GPL or unlicensed demos, Nintendo ROMs, or Nintendo assets. Platformer levels and graphics are the reference project's original game.

## Overcooked-AI (external, optional)

- Upstream: https://github.com/HumanCompatibleAI/overcooked_ai
- Pinned commit: `739950a079cdaed5a44fcc662efc40244c205d06`; package version 2.0.0; license **MIT**. The upstream license remains with the installed source distribution.
- `requirements-overcooked.txt` pins the independent Python 3.10 environment. No pretrained policy, human gameplay dataset, or game art is vendored.
- The local JSONL worker calls native `OvercookedGridworld.get_state_transition` with `display_phi=False` and `motion_planner=None`. It does not construct a planner or choose actions. The dashboard draws its own simple kitchen visualization from public native state.

## poke-env and Pokemon Showdown (external, optional)

- poke-env upstream: https://github.com/hsahovic/poke-env, commit `7ff6212e4e738efd46f1ea52035b37a8bba1e386`, version 0.16.1, **MIT**. Installed license retained in the distribution.
- Pokemon Showdown upstream: https://github.com/smogon/pokemon-showdown, commit `7244fe8e28fa8bc4d0809ae30d183c608b62bb2b`, **MIT**. Source, LICENSE and package lock remain in the ignored local `.runtime/pokemon-showdown/` checkout.
- `requirements-pokemon.lock` pins the optional Python environment. `scripts/setup_pokemon.sh` builds the fixed local native simulator with Node 22.18+. No public battle server or account is used.
- Our bridge calls native `Battle.choose` and exposes separately filtered player requests; poke-env Battle instances parse each player's view. No upstream Player bot, random player, move planner or default-choice recovery is used.
- The dashboard draws text and HP bars only. No Pokemon ROM, sprite, audio or other game art is redistributed. Pokemon names are used to identify simulator rule entities.
