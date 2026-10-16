"""Deterministic tests of the real bundled rules; no model or network calls."""
from collections import Counter
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import time

import pytest

from game_harness.games.arcade import ArcadeSession
from game_harness.games.platformer import PlatformerAdapter
from game_harness.games.tetris import TetrisAdapter


pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="Arcade rules require Node.js")
GAMES = Path(__file__).resolve().parents[1] / "src/game_harness/games"


def action_ids(session):
    return {row["id"] for row in session.candidates()}


def engine_json(code):
    """Exercise public engine exports directly for isolated deterministic fixtures.

    This is not a runtime restore/fixture opcode; production IPC accepts only
    standard initialization and native candidate IDs.
    """
    program = "const {tetris,platformer}=require(" + json.dumps(str(GAMES / "arcade_bridge.cjs")) + ");\n" + code
    result = subprocess.run([shutil.which("node"), "-e", program], check=True,
                            capture_output=True, text=True, timeout=10)
    return json.loads(result.stdout)


def test_vendored_source_and_license_are_unchanged_and_identifiable():
    root = GAMES / "vendor/arena-arcade"
    source = json.loads((root / "SOURCE.json").read_text())
    assert source["license"] == "MIT"
    assert source["upstream_commit"] == "4e2bda1d6c9b2545837761e2c7d19e2ce546aa9d"
    for relative, expected in source["files"].items():
        assert hashlib.sha256((root / relative).read_bytes()).hexdigest() == expected
    assert "Copyright (c) 2026 Dougo" in (root / "LICENSE").read_text()
    assert "Permission is hereby granted" in (root / "LICENSE").read_text()


def test_tetris_exposes_complete_native_catalog_and_hold_is_rule_limited():
    expected = {f"{button}:{duration}" for button in ("left", "right", "soft-drop") for duration in (100, 300, 1000)}
    expected |= {f"{button}:100" for button in ("noop", "rotate-cw", "rotate-ccw", "hard-drop", "hold")}
    with TetrisAdapter().create(1, {"max_steps": 80}) as game:
        assert action_ids(game) == expected
        first = game.snapshot()["observation"]["active"]["type"]
        state = game.step("hold:100")
        assert state["observation"]["hold"] == first
        assert state["observation"]["holdAvailable"] is False
        assert action_ids(game) == expected - {"hold:100"}
        state = game.step("hard-drop:100")
        assert state["observation"]["holdAvailable"] is True
        assert action_ids(game) == expected


def test_platformer_full_catalog_and_seed_level_disclosure():
    expected = {f"{button}:{duration}" for button in ("left", "right", "left+jump", "right+jump") for duration in (100, 300, 1000)}
    expected |= {"noop:100", "jump:100"}
    adapter = PlatformerAdapter()
    for seed in range(3):
        with adapter.create(seed, {}) as game:
            state = game.snapshot()
            assert action_ids(game) == expected
            assert state["render"]["level"] == seed + 1
            assert state["render"]["width"] == 1536 and state["render"]["height"] == 384
            assert state["observation"]["units"]["position"] == "px"
    context = adapter.context({})
    assert context["evaluation"]["seed_modulus"] == 3
    assert context["evaluation"]["minimum_pairs"] == 6
    assert context["score_spec"]["minimum_mean_gain"] == 100
    assert context["score_spec"]["max"] == 11300
    assert "不是原版超级马里奥" in context["rules"]


@pytest.mark.parametrize("adapter,seed,actions", [
    (TetrisAdapter(), 12, ["right:100", "rotate-cw:100", "hard-drop:100", "hold:100", "left:300"]),
    (PlatformerAdapter(), 2, ["right:100", "right+jump:300", "noop:100", "left:100"]),
])
def test_seeded_rules_are_deterministic_and_response_wait_does_not_advance(adapter, seed, actions):
    with adapter.create(seed, {}) as first, adapter.create(seed, {}) as second:
        assert first.snapshot() == second.snapshot()
        initial = first.snapshot()
        time.sleep(.02)
        first.candidates()
        assert first.snapshot() == initial
        for action in actions:
            assert first.step(action) == second.step(action)
            assert first.snapshot()["simulation"]["wall_clock_advances_game"] is False


def test_tetris_seed_controls_bag_but_only_five_previews_are_visible():
    seen = []
    for seed in range(4):
        with TetrisAdapter().create(seed, {}) as game:
            state = game.snapshot()
            seen.append((state["observation"]["active"]["type"], tuple(state["observation"]["next"])))
            assert len(state["observation"]["next"]) == len(state["render"]["next"]) == 5
            assert len(state["render"]["cells"]) == 200
            assert len(state["observation"]["board"]["rows"]) == 22
            encoded = json.dumps(state)
            assert '"rng"' not in encoded and '"bag"' not in encoded
    assert len(set(seen)) > 1


def test_tetris_original_rng_preserves_seven_bag_without_exposing_it_to_players():
    seen = engine_json("""
let state=tetris.create({},'fixture-seven-bag');
const seen=[];
for(let i=0;i<21;i++){
  seen.push(state.active.type);
  state=tetris.step(tetris.input(state,{buttons:['hard-drop'],durationMs:100}),6);
  state.board.fill(0); // Fixture isolates the bag from pile geometry.
}
console.log(JSON.stringify(seen));
""")
    for start in (0, 7, 14):
        assert Counter(seen[start:start + 7]) == Counter("IOTSZJL")


@pytest.mark.parametrize("game_id", ["tetris", "platformer"])
def test_action_simulation_time_matches_native_duration_and_releases_keys(game_id):
    with ArcadeSession(game_id, 0, 10) as game:
        for index, duration in enumerate((100, 300, 1000), 1):
            before = game.snapshot()["tick"]
            state = game.step(f"left:{duration}")
            assert state["step"] == index
            assert state["tick"] - before == duration * 60 // 1000
            assert state["last_input"]["simulated_ticks"] == duration * 60 // 1000
            assert state["observation"]["controls"]["held"] == []
            assert state["observation"]["controls"]["releaseTick"] is None
            assert state["observation"]["controls"]["pendingEdges"] == []


def test_tetris_noop_keeps_gravity_and_rotation_edges_do_not_repeat_within_action():
    with TetrisAdapter().create(0, {"max_steps": 20}) as game:
        initial_y = game.snapshot()["observation"]["active"]["y"]
        for _ in range(7):
            game.step("noop:100")
        assert game.snapshot()["tick"] == 42
        assert game.snapshot()["observation"]["active"]["y"] == initial_y
        state = game.step("noop:100")
        assert state["tick"] == 48
        assert state["observation"]["active"]["y"] == initial_y + 1
        before = state["observation"]["active"]["rotation"]
        state = game.step("rotate-cw:100")
        assert state["observation"]["active"]["rotation"] == (before + 1) % 4
        state = game.step("rotate-cw:100")
        assert state["observation"]["active"]["rotation"] == (before + 2) % 4


def test_platformer_midair_jump_does_not_queue_or_auto_repeat_and_score_has_no_survival_bonus():
    with PlatformerAdapter().create(0, {"max_steps": 30}) as game:
        start = game.snapshot()
        airborne = game.step("jump:100")
        assert not airborne["render"]["player"]["onGround"]
        assert airborne["render"]["player"]["vy"] < 0
        again = game.step("jump:100")
        assert again["render"]["player"]["vy"] > airborne["render"]["player"]["vy"]
        for _ in range(10):
            state = game.step("noop:100")
        assert state["render"]["player"]["onGround"]
        assert state["render"]["player"]["y"] == start["render"]["player"]["y"]
        assert state["metrics"]["survivedTicks"] > 0 and state["score"] == 0


@pytest.mark.parametrize("lines,points", [(1, 100), (2, 300), (3, 500), (4, 800)])
def test_original_tetris_line_scoring(lines, points):
    result = engine_json(f"""
let state=tetris.create({{}},'fixture-lines');
state.active={{type:'I',rotation:1,x:2,y:-2}};
for(let y=20-{lines};y<20;y++)for(let x=0;x<10;x++)if(x!==4)state.board[(y+2)*10+x]=3;
state=tetris.step(tetris.input(state,{{buttons:['hard-drop'],durationMs:100}}),6);
console.log(JSON.stringify(state.score));
""")
    assert result["lines"] == lines and result["points"] == points
    assert result["survivedTicks"] == 6


def test_platformer_points_follow_native_progress_coins_and_completion_formula():
    with PlatformerAdapter().create(0, {}) as game:
        for _ in range(3):
            state = game.step("right:300")
            metrics = state["metrics"]
            assert state["score"] == int(metrics["progress"] * 10) + metrics["coins"] * 100 + (10000 if metrics["completed"] else 0)
        assert state["score"] > 0


@pytest.mark.parametrize("seed,reason", [(1, "FELL_IN_PIT"), (2, "ENEMY_COLLISION")])
def test_platformer_dangerous_inputs_remain_available_and_naturally_end(seed, reason):
    with PlatformerAdapter().create(seed, {}) as game:
        for _ in range(10):
            assert "right:1000" in action_ids(game)
            state = game.step("right:1000")
            if state["done"]:
                break
        assert state["done"] and state["reason"] == reason and not state["truncated"]
        assert not state["metrics"]["completed"]
        assert state["last_input"]["simulated_ticks"] <= 60
        assert game.candidates() == []


def test_tetris_hard_drop_is_not_an_auto_placement_solver_and_top_out_is_natural():
    with TetrisAdapter().create(0, {"max_steps": 100}) as game:
        while not game.snapshot()["done"]:
            state = game.step("hard-drop:100")
        assert state["reason"] in {"TOP_OUT", "SPAWN_BLOCKED"}
        assert not state["truncated"] and state["step"] < 100
        assert state["score"] == 0  # Repeated central drops get no free placement reward.


@pytest.mark.parametrize("adapter", [TetrisAdapter(), PlatformerAdapter()])
def test_decision_budget_truncation_is_distinct_from_natural_finish(adapter):
    with adapter.create(0, {"max_steps": 1}) as game:
        state = game.step("noop:100")
        assert state["done"] and state["truncated"] and state["reason"] == "step_limit"
        assert state["step"] == 1 and state["tick"] == 6
        assert state["render"]["status"] == "playing"
        assert state["score"] == state["metrics"]["points"] == 0
        assert game.candidates() == []
        with pytest.raises(RuntimeError, match="ended"):
            game.step("noop:100")


def test_original_engine_time_limit_is_terminal_even_if_decision_budget_remains():
    with PlatformerAdapter().create(0, {"max_steps": 200}) as game:
        for _ in range(120):
            state = game.step("left:1000")
        assert state["done"] and state["reason"] == "TIME_LIMIT"
        assert state["tick"] == 7200 and state["step"] == 120 and not state["truncated"]
        assert state["score"] == 0


@pytest.mark.parametrize("level,jumps", [(1, [89, 217, 345]), (2, [89, 217, 345]), (3, [109, 231, 377])])
def test_reference_levels_reach_native_goal_with_fixed_recorded_inputs(level, jumps):
    # A rules regression recording, not a policy in any runtime or Harness.
    result = engine_json(f"""
let state=platformer.create({{level:{level}}},'fixture-fixed-recording');
state=platformer.input(state,{{buttons:['right']}});
for(const tick of {json.dumps(jumps)}){{
  state=platformer.step(state,tick-state.tick);
  state=platformer.input(state,{{buttons:['right','jump']}});
  state=platformer.step(state,1);
  state=platformer.input(state,{{buttons:['right']}});
}}
state=platformer.step(state,7200-state.tick);
console.log(JSON.stringify({{status:state.status,reason:state.reason,tick:state.tick,score:state.score}}));
""")
    assert result["status"] == "finished" and result["reason"] == "GOAL_REACHED"
    assert result["tick"] == 452
    assert result["score"]["completed"] is True and result["score"]["progress"] == 100
    assert result["score"]["points"] == 11000 + result["score"]["coins"] * 100
    assert 11000 <= result["score"]["points"] <= 11300


@pytest.mark.parametrize("adapter", [TetrisAdapter(), PlatformerAdapter()])
def test_invalid_actions_and_readback_cannot_mutate_game(adapter):
    with adapter.create(0, {}) as game:
        before = game.snapshot()
        for action in ("auto-solve", "left:999", "left+right:100", None):
            with pytest.raises(ValueError):
                game.step(action)
            assert game.snapshot() == before
        observation, candidates = game.snapshot(), game.candidates()
        observation["metrics"]["points"] = 999999
        candidates[0]["id"] = "injected"
        assert game.snapshot() == before
        assert "injected" not in action_ids(game)
