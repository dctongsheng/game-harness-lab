"""Native-engine and recorded-provider checks, never model capability tests."""
from copy import deepcopy

import pytest

from game_harness.games import get_game
from game_harness.runtime import _digest, run_episode
from game_harness.tetris_context import (
    CONTEXT_CONTRACT, GOAL_INSTRUCTIONS, TetrisContextError,
    TetrisDecisionHarness, audit_context_episode, context_instructions, validate_config,
)
from game_harness.tetris_control import CONTROL_CONTRACT, TetrisControlFeedback


def v2(enabled=True):
    return {"schema": "tetris-context-v2", "structured_facts": True,
            "decision_goal_memory": True, "control_feedback": enabled,
            "replan_every_actions": 4}


def v1():
    return {"schema": "tetris-context-v1", "structured_facts": True,
            "decision_goal_memory": True, "replan_every_actions": 4}


class RecordedProvider:
    def __init__(self, actions, goals=None):
        self.actions = iter(actions)
        self.goals = iter(goals or ["target:6:0"] * 50)
        self.calls = []

    def decide(self, state, instructions, criteria, request_id):
        choice = next(self.goals if request_id.endswith(":goal") else self.actions)
        self.calls.append(deepcopy((state, instructions, criteria, request_id)))
        probabilities = {item["id"]: float(item["id"] == choice) for item in criteria}
        raw = {"model": "fixture-decision", "answers": {"primary_action": {
            "choice": choice, "confidence": 1., "probabilities": probabilities,
            "details": {"thought": {"used": False, "tokens": 0}},
        }}}
        return {"request_id": request_id, "observation_revision": state["step"],
                "choice_id": choice, "candidate_hash": _digest(criteria), "model": "fixture-decision",
                "elapsed_ms": 1., "think_mode": "never", "think_used": False, "reasoning_tokens": 0,
                "raw_response": raw, "response_hash": _digest(raw)}


def fixture_episode(actions, goals=None, configuration=None, seed=1256368473):
    harness = {"id": "control-fixture", "decision_model": "fixture-decision", "game_id": "tetris",
               "candidate": {"decision_instructions": "fixture policy", "strategy_notes": "fixture notes"},
               "tetris_context_config": configuration or v2()}
    provider = RecordedProvider(actions, goals)
    episode = run_episode(provider, harness, episode_id="control-fixture-episode", seed=seed,
                          adapter=get_game("tetris"), config={"max_steps": len(actions)})
    assert episode["infra_error"] is None
    return episode, harness, provider


def facts_at(episode, step):
    return episode["trace"][step]["receipt"]["tetris_context"]["action_input_state"]["tetris_control_facts"]


@pytest.mark.parametrize("alteration", [
    {"control_feedback": 1}, {"control_feedback": None}, {"structured_facts": False},
    {"decision_goal_memory": False}, {"replan_every_actions": 3}, {"extra": True},
    {"schema": "tetris-context-v3"},
])
def test_v2_configuration_is_exact(alteration):
    with pytest.raises(TetrisContextError):
        validate_config({**v2(), **alteration})
    missing = v2()
    missing.pop("control_feedback")
    with pytest.raises(TetrisContextError):
        validate_config(missing)


def test_v2_configuration_copy_and_instruction_contract():
    original = v2()
    saved = validate_config(original)
    original["control_feedback"] = False
    assert saved["control_feedback"] is True
    old = "frozen\n" + CONTEXT_CONTRACT
    assert context_instructions("frozen", v1()) == old
    assert context_instructions("frozen", v2(False)) == old
    assert context_instructions("frozen", v2()) == old + "\n" + CONTROL_CONTRACT


def test_disabled_v2_is_identical_to_v1_actual_calls_and_ledger_except_config():
    actions = ["right:100", "noop:100", "left:100", "rotate-cw:100", "noop:100", "hard-drop:100"]
    old, old_harness, old_provider = fixture_episode(actions, configuration=v1())
    new, new_harness, new_provider = fixture_episode(actions, configuration=v2(False))
    assert old_provider.calls == new_provider.calls
    for before, after in zip(old["trace"], new["trace"]):
        equivalent = deepcopy(after)
        equivalent["receipt"]["tetris_context"]["config"] = v1()
        assert before == equivalent
    assert audit_context_episode(old, old_harness) == audit_context_episode(new, new_harness)


@pytest.mark.parametrize("actions,positions,timers", [
    (["left:100"] * 3, [4, 3, 2, 0], [0, 5, 11, 17]),
    (["left:100", "noop:100", "left:100"], [4, 3, 3, 2], [0, 5, 0, 5]),
    (["left:100", "right:100"], [4, 3, 4], [0, 5, 5]),
    (["right:300", "left:300"], [4, 8, 4], [0, 17, 17]),
    (["right:100", "rotate-cw:100", "right:100"], [4, 5, 5, 6], [0, 5, 0, 5]),
])
def test_real_native_timing_without_model(actions, positions, timers):
    session = get_game("tetris").create(1256368473, {"max_steps": 80})
    try:
        snapshots = [session.snapshot()] + [session.step(action) for action in actions]
    finally:
        session.close()
    observations = [item["observation"] for item in snapshots]
    assert [min(cell["x"] for cell in item["active"]["cells"]) for item in observations] == positions
    assert [item["horizontalHeldTicks"] for item in observations] == timers
    assert len({item["pieceId"] for item in observations}) == 1
    assert all(item["controls"]["held"] == [] for item in observations)


def test_actual_motion_and_crossing_only_appear_on_following_observation():
    episode, harness, _ = fixture_episode(["right:300", "left:300", "noop:100"])
    initial = facts_at(episode, 0)
    assert initial["last_transition"] is None
    assert initial["recent_window"]["confirmed_actions"] == 0
    assert initial["target_relation"] == {"target": {"leftmost_column": 6, "rotation": 0},
                                          "column_error": 2, "rotation_equal": True, "at_target": False}
    first = facts_at(episode, 1)
    assert first["current"] == {"leftmost_column": 8, "rotation": 0,
                                 "horizontal_direction": 1, "horizontal_held_ticks": 17}
    assert first["last_transition"] == {
        "step": 0, "action": "right:300", "physical_ticks": 18,
        "before_leftmost_column": 4, "after_leftmost_column": 8, "actual_column_delta": 4,
        "before_rotation": 0, "after_rotation": 0, "rotation_unchanged": True,
        "target_at_action": {"leftmost_column": 6, "rotation": 0},
        "target_unchanged": True, "crossing_comparison_applicable": True,
        "strictly_crossed_target_column": True,
    }
    assert first["recent_window"]["horizontal_command_reversals"] == 0
    second = facts_at(episode, 2)
    assert second["last_transition"]["actual_column_delta"] == -4
    assert second["last_transition"]["strictly_crossed_target_column"] is True
    assert second["recent_window"]["horizontal_command_reversals"] == 1
    assert audit_context_episode(episode, harness)["actual_inputs_verified"] is True


def test_reaching_target_is_not_crossing_and_does_not_execute_an_automatic_drop():
    episode, _, _ = fixture_episode(["right:100", "noop:100"], goals=["target:5:0"])
    reached = facts_at(episode, 1)
    assert reached["target_relation"]["at_target"] is True
    assert reached["last_transition"]["crossing_comparison_applicable"] is True
    assert reached["last_transition"]["strictly_crossed_target_column"] is False
    assert episode["trace"][1]["action"] == "noop:100"
    assert episode["trace"][-1]["after"]["observation"]["pieceId"] == 1


def test_rotation_changes_leftmost_column_without_false_horizontal_crossing():
    episode, _, _ = fixture_episode(["rotate-cw:100", "noop:100"], goals=["target:4:1"], seed=0)
    moved = facts_at(episode, 1)["last_transition"]
    assert moved["before_leftmost_column"] == 3
    assert moved["after_leftmost_column"] == 5
    assert moved["actual_column_delta"] == 2
    assert moved["rotation_unchanged"] is False
    assert moved["crossing_comparison_applicable"] is False
    assert moved["strictly_crossed_target_column"] is False


def test_goal_replacement_recomputes_relation_and_crossing_before_action():
    episode, _, provider = fixture_episode(
        ["right:300", "left:300", "right:300", "left:300", "noop:100"],
        goals=["target:6:0", "target:2:0"])
    goal_call = next(call for call in provider.calls if call[0]["step"] == 4 and call[3].endswith(":goal"))
    before = goal_call[0]["tetris_control_facts"]
    after = facts_at(episode, 4)
    assert before["target_relation"]["column_error"] == 2
    assert before["last_transition"]["strictly_crossed_target_column"] is True
    assert after["target_relation"]["column_error"] == -2
    assert after["last_transition"]["target_unchanged"] is False
    assert after["last_transition"]["crossing_comparison_applicable"] is False
    assert after["last_transition"]["strictly_crossed_target_column"] is False
    assert after["recent_window"]["horizontal_command_reversals"] == 3
    assert goal_call[1].endswith(CONTROL_CONTRACT + "\n" + GOAL_INSTRUCTIONS)


def test_no_target_has_null_relation_and_clear_does_not_auto_replan():
    episode, _, provider = fixture_episode(["noop:100"] * 6, goals=["target:6:0", "clear"])
    empty = facts_at(episode, 4)
    assert empty["target_relation"] == {"target": None, "column_error": None,
                                        "rotation_equal": None, "at_target": None}
    assert empty["last_transition"]["crossing_comparison_applicable"] is False
    assert facts_at(episode, 5)["target_relation"]["target"] is None
    assert len([call for call in provider.calls if call[3].endswith(":goal")]) == 2


def test_recent_window_counts_four_confirmed_actions_not_four_horizontal_actions():
    episode, _, _ = fixture_episode(["right:100", "noop:100", "left:100", "noop:100", "right:100", "noop:100"])
    window = facts_at(episode, 5)["recent_window"]
    assert window == {"max_actions": 4, "confirmed_actions": 4,
                      "horizontal_commands": [{"step": 2, "action": "left:100"},
                                              {"step": 4, "action": "right:100"}],
                      "horizontal_command_reversals": 1}


@pytest.mark.parametrize("action", ["hold:100", "hard-drop:100"])
def test_real_spawn_resets_confirmed_history_and_pending_transition(action):
    episode, harness, _ = fixture_episode(["right:100", action, "noop:100"])
    assert episode["trace"][2]["before"]["observation"]["pieceId"] == 2
    fresh = facts_at(episode, 2)
    assert fresh["last_transition"] is None
    assert fresh["recent_window"]["confirmed_actions"] == 0
    assert audit_context_episode(episode, harness)["goal_calls"] == 2


def test_new_episode_on_reused_wrapper_starts_fresh():
    provider = RecordedProvider(["right:100", "noop:100"], ["target:6:0", "keep"])
    wrapper = TetrisDecisionHarness(provider, v2())
    session = get_game("tetris").create(1256368473, {"max_steps": 80})
    try:
        before, criteria = session.snapshot(), session.candidates()
    finally:
        session.close()
    for episode_id in ("first", "second"):
        state = deepcopy(before)
        state["episode_id"] = episode_id
        wrapper.decide(state, "fixture", criteria, episode_id)
    facts = provider.calls[-1][0]["tetris_control_facts"]
    assert facts["last_transition"] is None
    assert facts["recent_window"]["confirmed_actions"] == 0
    assert facts["target_relation"]["target"] is None


def test_tracker_rejects_unexecuted_transition_and_copies_inputs():
    session = get_game("tetris").create(1256368473, {"max_steps": 80})
    try:
        state = session.snapshot()
    finally:
        session.close()
    tracker = TetrisControlFeedback()
    scope = {"episode_id": "fixture", "piece_id": 1, "locked_board_hash": "fixture"}
    tracker.observe(state, scope)
    target = {"leftmost_column": 6, "rotation": 0}
    tracker.selected("right:100", target)
    target["leftmost_column"] = 0
    state["step"] = 1
    with pytest.raises(ValueError, match="not confirmed"):
        tracker.observe(state, scope)


@pytest.mark.parametrize("field,value", [("horizontalDirection", True), ("horizontalHeldTicks", -1),
                                         ("horizontalHeldTicks", 0.0), ("tick", None)])
def test_tracker_rejects_invalid_public_timers(field, value):
    session = get_game("tetris").create(1256368473, {"max_steps": 80})
    try:
        state = session.snapshot()
    finally:
        session.close()
    state["observation"][field] = value
    with pytest.raises(ValueError):
        TetrisControlFeedback().observe(state, {"fixture": True})


@pytest.mark.parametrize("field", ["actual_column_delta", "horizontal_command_reversals", "column_error"])
def test_audit_rejects_tampered_control_fact(field):
    episode, harness, _ = fixture_episode(["right:300", "left:300", "noop:100"])
    call = episode["trace"][1]["receipt"]["tetris_context"]["calls"][0]
    facts = call["input_state"]["tetris_control_facts"]
    if field == "actual_column_delta":
        facts["last_transition"][field] += 1
    elif field == "horizontal_command_reversals":
        facts["recent_window"][field] += 1
    else:
        facts["target_relation"][field] += 1
    call["input_hash"] = _digest(call["input_state"])
    with pytest.raises(TetrisContextError, match="input differs"):
        audit_context_episode(episode, harness)


def test_feedback_keeps_complete_native_catalog_for_each_action():
    episode, _, provider = fixture_episode(["right:100", "noop:100", "hold:100", "noop:100"])
    native_calls = [call for call in provider.calls if not call[3].endswith(":goal")]
    for step, call in zip(episode["trace"], native_calls):
        assert call[2] == step["criteria"]
        assert call[0]["candidates"] == step["criteria"]
    assert len(provider.calls) == len(episode["trace"]) + 2
