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
