from copy import deepcopy
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from analyze_tetris_control_goals import inspect_episode


def row(target=None, action="right:100"):
    active = {"type": "S", "rotation": 0,
              "cells": [{"x": 7, "y": 0}, {"x": 8, "y": 0}, {"x": 8, "y": 1}, {"x": 9, "y": 1}]}
    observation = {"pieceId": 1, "active": active}
    return {"action": action, "receipt": {"request_id": "fixture", "tetris_context": {
        "action_input_state": {"step": 3, "observation": observation,
                               "decision_goal_memory": {"target": target}}}},
        "after": {"observation": deepcopy(observation)}}


def test_width_three_at_column_eight_is_impossible_but_seven_fits():
    bad = row({"leftmost_column": 8, "rotation": 0})
    good = row({"leftmost_column": 7, "rotation": 0})
    result = inspect_episode({"trace": [bad, good]})
    assert result["counts"]["same_rotation_target_actions"] == 2
    assert result["counts"]["outside_horizontal_bounds_actions"] == 1
    assert result["first_three_outside_bounds_examples"][0]["width"] == 3


def test_different_orientation_or_missing_goal_is_not_inferred():
    result = inspect_episode({"trace": [row({"leftmost_column": 9, "rotation": 1}), row()]})
    assert result["counts"]["same_rotation_target_actions"] == 0
    assert result["counts"]["outside_horizontal_bounds_actions"] == 0


def test_noop_count_is_specific_to_impossible_target():
    result = inspect_episode({"trace": [row({"leftmost_column": 8, "rotation": 0}, "noop:100"),
                                        row({"leftmost_column": 7, "rotation": 0}, "noop:100")]})
    assert result["counts"]["outside_bounds_noop_actions"] == 1
    assert result["actions_at_outside_bounds_target"] == {"noop:100": 1}


def test_horizontal_stall_allows_falling_but_excludes_new_piece():
    falling = row()
    for cell in falling["after"]["observation"]["active"]["cells"]:
        cell["y"] += 1
    changed = deepcopy(falling)
    changed["after"]["observation"]["pieceId"] += 1
    result = inspect_episode({"trace": [falling, changed]})
    assert result["counts"]["horizontal_no_x_change_actions"] == 1
