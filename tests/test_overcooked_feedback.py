from copy import deepcopy

from game_harness.evolution import _summary
from game_harness.feedback import chars, compact_state


def test_kitchen_projection_keeps_geometry_and_both_chefs_without_history_duplication():
    frame = {"step": 9, "native_tick": 4, "actor": "chef_1", "acting_player": 1,
             "score": 0, "players": [{"index": i, "position": [i, 1],
                 "orientation": [0, -1], "held_object": None} for i in (0, 1)],
             "pots": [{"position": [1, 0], "ingredients": ["onion"], "is_ready": False}],
             "objects": [], "terrain": ["XPX", "O D"], "orders": [],
             "joint_history": [{"tick": i, "actions": ["west", "east"]} for i in range(4)],
             "last_joint_actions": ["west", "east"], "deliveries": 0}
    compact = compact_state(frame, "overcooked", {"terrain": frame["terrain"], "orders": []})
    assert compact["players"] == frame["players"] and compact["pots"] == frame["pots"]
    assert compact["native_tick"] == 4 and compact["acting_player"] == 1
    assert "joint_history" not in compact and "terrain" not in compact


def test_reflection_contains_complete_zero_score_kitchen_transition():
    before = {"step": 1, "native_tick": 0, "score": 0, "actor": "chef_1", "acting_player": 1,
              "terrain": ["XPX", "O D"], "players": [], "pots": [], "objects": [], "orders": []}
    after = deepcopy(before)
    after.update(step=2, native_tick=1, done=True, reason="step_limit",
                 pots=[{"position": [1, 0], "ingredients": ["onion"], "is_ready": False}])
    source = {"episode_id": "fixture", "game_id": "overcooked", "harness_id": "h-fixture", "seed": 1,
              "score": 0, "steps": 1, "termination_reason": "step_limit", "model": "fixture",
              "infra_error": None, "trace": [{"before": before, "after": after, "action": "interact",
                 "actor": "chef_1", "policy_role": "candidate", "criteria": [{"id": "interact", "label": "交互"}]}]}
    result = _summary(source, include_trace=True, game_context={"game_id": "overcooked"})
    assert result["observation_context"]["terrain"] == before["terrain"]
    event = result["trace_sample"][0]
    assert event["before"]["native_tick"] == 0 and event["after"]["native_tick"] == 1
    assert event["after"]["pots"] == after["pots"]
    assert "kitchen_state_changed" in event["selection_reasons"]
    assert chars(result) <= 8000
