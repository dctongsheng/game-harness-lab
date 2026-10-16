"""Rules and adapter tests; no provider calls or paid model traffic."""
from pathlib import Path
import shutil

import pytest

from game_harness.games.xiangqi import (
    MATERIAL_WEIGHTS, RULES_VERSION, XiangqiAdapter, XiangqiEngineError, XiangqiSession,
)


pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="Node.js required by Chinese chess adapter")


def ids(game):
    return {candidate["id"] for candidate in game.candidates()}


def test_standard_opening_full_catalog_and_coordinates():
    with XiangqiSession() as game:
        state = game.snapshot()
        assert len(ids(game)) == 44
        assert len([cell for cell in state["cells"] if cell]) == 32
        assert state["cells"][4] == {"type": "k", "kind": "k", "color": "b", "label": "将"}
        assert state["cells"][85] == {"type": "k", "kind": "k", "color": "r", "label": "帅"}
        assert state["turn"] == "r" and state["policy_role"] == "candidate"
        assert state["score"] == 50 and not state["done"]
        assert state["rules_version"] == RULES_VERSION


def test_seed_only_changes_candidate_side_not_initial_position():
    with XiangqiSession(seed=10) as red, XiangqiSession(seed=11) as black:
        assert red.snapshot()["fen"] == black.snapshot()["fen"]
        assert red.snapshot()["candidate_color"] == "r"
        assert black.snapshot()["candidate_color"] == "b"
        assert black.snapshot()["policy_role"] == "opponent"
        black.step("h0g2")
        assert black.snapshot()["policy_role"] == "candidate"
        assert "repeated_trials" in black.snapshot()["trial_design"]


@pytest.mark.parametrize("fen,present,absent", [
    (None, {"h2h9"}, {"h2h8"}),
    ("4k4/9/9/9/4p4/9/9/9/1R7/1N2K4 r - - 0 1", {"b0d1"}, {"b0a2", "b0c2"}),
    ("4k4/9/9/9/4p4/9/9/9/3R5/2B1K4 r - - 0 1", {"c0a2"}, {"c0e2"}),
    ("4k4/9/9/9/4p4/2B6/9/9/9/4K4 r - - 0 1", {"c4a2"}, {"c4a6", "c4e6"}),
    ("4k4/9/9/9/4R4/9/9/9/9/4K4 r - - 0 1", {"e5e6"}, {"e5d5"}),
])
def test_rules_cannon_horse_elephant_facing_generals(fen, present, absent):
    with XiangqiSession(fen=fen) as game:
        assert present <= ids(game)
        assert not absent & ids(game)


def test_stalemate_is_loss_and_not_draw():
    with XiangqiSession(seed=1, fen="3k5/R8/9/9/9/9/9/9/9/4K4 b - - 0 1") as game:
        state = game.snapshot()
        assert not state["in_check"]
        assert state["done"] and state["reason"] == "stalemate"
        assert state["score"] == 0 and state["result"] == "loss"
        assert game.candidates() == []


@pytest.mark.parametrize("seed,score,result", [(0, 100, "win"), (1, 0, "loss")])
def test_real_mating_line_and_no_move_after_terminal(seed, score, result):
    with XiangqiSession(seed=seed) as game:
        for move in ["h2h4", "d9e8", "h4g4", "g6g5", "g4g9"]:
            state = game.step(move)
        assert state["reason"] == "checkmate" and state["done"]
        assert state["score"] == score and state["result"] == result
        assert not state["truncated"]
        with pytest.raises(RuntimeError, match="ended"):
            game.step("e9d9")


def test_threefold_uses_persistent_history_not_just_fen():
    with XiangqiSession() as game:
        cycle = ["h0g2", "h9g7", "g2h0", "g7h9"]
        for action in cycle:
            game.step(action)
        assert not game.snapshot()["done"]
        before = game.snapshot()
        game.candidates()
        assert game.snapshot() == before
        for action in cycle:
            state = game.step(action)
        assert state["done"] and state["reason"] == "threefold_repetition"
        assert state["result"] == "draw" and state["score"] == 50


def test_noncapture_clock_is_authoritative_draw():
    fen = "rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR r - - 119 1"
    with XiangqiSession(fen=fen) as game:
        state = game.step("h0g2")
        assert state["done"] and state["reason"] == "noncapture_120"
        assert state["result"] == "draw"


def test_cutoff_material_is_separate_from_game_outcome():
    with XiangqiSession(seed=0, max_steps=1) as red, XiangqiSession(seed=1, max_steps=1) as black:
        red_state = red.step("h2h9")
        black_state = black.step("h2h9")
        assert red_state["score"] == 54  # red cannon captured a black horse
        assert black_state["score"] == 46
        for state in (red_state, black_state):
            assert state["done"] and state["truncated"] and state["reason"] == "step_limit"
            assert state["result"] is None and state["winner"] is None
            assert state["score_kind"] == "material_proxy"
        assert red.candidates() == []


def test_cutoff_proxy_is_bounded_and_weights_fixed():
    # Use a controlled cached rules state solely to test the score cap.
    with XiangqiSession(max_steps=1) as game:
        game._engine["cells"] = [{"type": "r", "color": "r"}] * 10
        game.step_count = 1
        assert game.snapshot()["score"] == 90
        game._engine["cells"] = [{"type": "r", "color": "b"}] * 10
        assert game.snapshot()["score"] == 10
        assert MATERIAL_WEIGHTS == {"r": 9, "n": 4, "c": 4, "b": 2, "a": 2, "p": 1, "k": 0}


@pytest.mark.parametrize("action", ["e0e9", "h9g7", "h2h8", "bad", 1, None])
def test_illegal_action_never_changes_state(action):
    with XiangqiSession() as game:
        before = game.snapshot()
        with pytest.raises(ValueError):
            game.step(action)
        assert game.snapshot() == before


def test_snapshots_and_candidates_are_detached():
    with XiangqiSession() as game:
        snapshot, candidates = game.snapshot(), game.candidates()
        snapshot["cells"][4]["color"] = "r"
        candidates[0]["id"] = "fake"
        assert game.snapshot()["cells"][4]["color"] == "b"
        assert "fake" not in ids(game)


def test_close_and_timeout_cleanup_child_process():
    game = XiangqiSession()
    process = game._process
    game.close()
    game.close()
    assert process.poll() is not None
    with pytest.raises(XiangqiEngineError, match="超时"):
        XiangqiSession(timeout=0.000001)


def test_adapter_contract_explains_evaluation_limits():
    adapter = XiangqiAdapter()
    context = adapter.context({"max_steps": 20})
    assert context["score_spec"]["cutoff_is_proxy_not_win"]
    assert context["opponent_policy"] == adapter.opponent_policy
    assert context["score_spec"]["minimum_mean_gain"] == 2.0
    assert context["evaluation"]["seed_modulus"] == 2
    assert "不代表不同棋盘" in context["trial_design"]
    assert context["config"]["max_steps"] == 20
    with adapter.create(0, {"max_steps": 20}) as game:
        assert len(game.candidates()) == 44
    for bad in (0, -1, 2001, True, "80"):
        with pytest.raises(ValueError):
            adapter.validate_config({"max_steps": bad})


def test_vendor_license_is_distributed():
    import game_harness.games.xiangqi as module
    assets = Path(module.__file__).parent
    assert "BSD-2-Clause" in (assets / "vendor/xiangqi.cjs").read_text()
    assert "Redistribution and use" in (assets / "vendor/LICENSE.xiangqi").read_text()
