"""Post-hoc analyzer unit tests; fixtures are not game-performance evidence."""
from copy import deepcopy
import fcntl
import hashlib
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import analyze_tetris_vision_goals as analyzer


def snapshot(*, locked=False, step=0):
    active = {"type": "O", "rotation": 0, "cells": [
        {"x": 8, "y": 0}, {"x": 9, "y": 0}, {"x": 8, "y": 1}, {"x": 9, "y": 1}]}
    rows = [{"y": y, "cells": "........OO" if locked and y in (18, 19) else "." * 10} for y in range(-2, 20)]
    return {"step": step, "observation": {"pieceId": 1, "active": active, "board": {"rows": rows}},
            "metrics": {"lines": 0}}


def row(action="right:100", *, lock=False, target=None):
    before, after = snapshot(), snapshot(locked=lock, step=1)
    return {"before": before, "after": after, "action": action,
            "receipt": {"request_id": "fixture-decision", "response_hash": "fixture-response",
                "tetris_context": {"action_input_state": {**deepcopy(before), "decision_goal_memory": {
                    "target": target if target is not None else {"leftmost_column": 9, "rotation": 0}}}}}}


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def checksum(path):
    return {str(p.relative_to(path)): hashlib.sha256(p.read_bytes()).hexdigest() for p in path.rglob("*") if p.is_file()}


@pytest.fixture
def study(tmp_path, monkeypatch):
    path = tmp_path / "study"; path.mkdir(); (path / ".run.lock").touch()
    scenarios = [{"id": f"evaluation-{i:02d}", "seed": i + 700, "order": list(analyzer.ARMS)} for i in range(24)]
    harness = {"id": "frozen-test-policy"}
    manifest = {"schema": "tetris-vision-experiment/v1", "id": "fixture-study", "arm_order": list(analyzer.ARMS),
                "arms": dict.fromkeys(analyzer.ARMS, harness), "scenarios": {"evaluation": scenarios}}
    result = {"per_seed": []}
    for index, scenario in enumerate(scenarios):
        result_row = {**scenario, "arms": {}}
        for arm in analyzer.ARMS:
            score = 100 if (index in (0, 3) and arm == "vision") or (index == 1 and arm == "text") else 0
            terminal = "TOP_OUT" if index == 2 and arm == "vision" else "step_limit"
            step = row("hard-drop:100", lock=True)
            step["receipt"]["tetris_context"]["calls"] = [
                {"purpose": "goal", "receipt": {"choice_id": "target:3:0" if index < 12 else "target:8:1"}},
                {"purpose": "game_action", "receipt": {"choice_id": "hard-drop:100"}},
            ]
            episode = {"episode_id": f"fixture-{index}", "logical_trial": scenario["id"], "seed": scenario["seed"],
                "purpose": "evaluation", "experiment_arm": arm, "score": score, "steps": 1,
                "termination_reason": terminal, "trace": [step], "initial_state": step["before"], "final_state": step["after"]}
            directory = path / "episodes" / arm / scenario["id"] / "attempt-01"
            write(directory / "episode.json", episode)
            write(directory / "complete.json", {"episode_hash": analyzer.digest(episode), "harness_hash": analyzer.digest(harness)})
            result_row["arms"][arm] = {"points": score, "termination_reason": terminal}
        result["per_seed"].append(result_row)
    write(path / "manifest.json", manifest)
    write(path / "evaluation_result.json", result)
    write(path / "evaluation_result.sha256.json", {"sha256": analyzer.digest(result)})
    # Deliberately failing calibration evidence must never be analyzed as formal games.
    write(path / "episodes/vision/calibration-00/attempt-01/episode.json", {"not_a_formal_game": True})
    calls = []
    def audit_under_lock(received):
        assert received == path
        with (path / ".run.lock").open("rb") as other:
            with pytest.raises(BlockingIOError):
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        calls.append(received)
        return {"status": "completed", "manifest_hash": analyzer.digest(manifest), "audit": {"fixture_only": True}}
    monkeypatch.setattr(analyzer, "audit_study", audit_under_lock)
    return path, calls


def test_geometry_reuses_current_orientation_only_and_stall_has_explicit_boundary():
    impossible = row()
    noop = row("noop:100")
    other_rotation = row(target={"leftmost_column": 9, "rotation": 1})
    inside = row()
    for cell in inside["receipt"]["tetris_context"]["action_input_state"]["observation"]["active"]["cells"]:
        cell["x"] -= 2
    for cell in inside["after"]["observation"]["active"]["cells"]:
        cell["x"] -= 2
    episode = {"trace": [impossible, noop, other_rotation, inside]}
    inherited = analyzer.inspect_episode(episode)
    extra = analyzer.inspect_extra(episode)
    assert inherited["counts"]["same_rotation_target_actions"] == 3
    assert inherited["counts"]["outside_horizontal_bounds_actions"] == 3
    assert inherited["counts"]["outside_bounds_noop_actions"] == 1
    assert inherited["counts"]["horizontal_no_x_change_actions"] == 3
    assert extra == {"noop_actions": 1, "horizontal_actions": 3, "boundary_no_x_change_actions": 2}


def test_hidden_rows_height_holes_and_active_cells_excluded():
    state = snapshot()
    state["observation"]["board"]["rows"][0]["cells"] = "I........."
    profile = analyzer.board_profile(state)
    assert profile["locked_cells"] == 1 and profile["maximum_height"] == 22
    assert profile["holes"] == 21 and profile["column_heights"] == [22] + [0] * 9
    assert analyzer.board_profile(snapshot())["locked_cells"] == 0


def test_lock_timeline_observes_cell_conservation_not_hold_or_harddrop_label():
    episode = {"trace": [row("hold:100"), row("hard-drop:100"), row("soft-drop:100", lock=True)]}
    events = analyzer.lock_timeline(episode)
    assert len(events) == 1 and events[0]["new_locked_pieces"] == 1
    assert events[0]["action"] == "soft-drop:100"
    assert events[0]["board_after"]["maximum_height"] == 2
    assert events[0]["board_after"]["holes"] == 0
    assert events[0]["target_reached_before_action"] is False
    broken = row("hard-drop:100", lock=True)
    broken["after"]["observation"]["board"]["rows"][-1]["cells"] = ".........O"
    with pytest.raises(analyzer.ReportError, match="conservation"):
        analyzer.lock_timeline({"trace": [broken]})


def test_complete_audit_first_only_48_formal_games_fixed_examples_readonly(study):
    path, calls = study
    before = checksum(path)
    report = analyzer.analyze(path)
    assert len(calls) == 1 and checksum(path) == before
    assert report["formal_episodes"] == 48 and report["paired_bags"] == 24
    assert all(arm["episodes"] == 24 for arm in report["arms"].values())
    assert all(arm["counts"]["actions"] == 24 for arm in report["arms"].values())
    assert all(arm["goal_choice_counts"] == {"target:3:0": 12, "target:8:1": 12} for arm in report["arms"].values())
    assert all(arm["native_action_counts"] == {"hard-drop:100": 24} for arm in report["arms"].values())
    assert report["categories"]["positive_gain"][0]["pair_id"] == "evaluation-00"
    assert report["categories"]["positive_gain"][0]["eligible_pair_count"] == 2
    assert report["categories"]["negative_gain"][0]["pair_id"] == "evaluation-01"
    assert report["categories"]["zero_gain_vision_top_out"][0]["pair_id"] == "evaluation-02"
    for category in report["categories"].values():
        assert len(category[0]["episodes"]) == 2
        assert len(category[0]["episodes"][0]["lock_events"]) == 1
    serialized = json.dumps(report)
    assert str(path) not in serialized and "independent samples" in serialized
    assert "causal mechanism" in serialized


def test_missing_case_categories_remain_empty(study):
    path, _ = study
    result = analyzer.load(path / "evaluation_result.json")
    for row in result["per_seed"]:
        for arm in analyzer.ARMS:
            row["arms"][arm].update(points=0, termination_reason="step_limit")
    write(path / "evaluation_result.json", result)
    write(path / "evaluation_result.sha256.json", {"sha256": analyzer.digest(result)})
    assert analyzer.analyze(path)["categories"] == dict.fromkeys(
        ("positive_gain", "negative_gain", "zero_gain_vision_top_out"), [])


def test_audited_episode_hash_rechecked_before_analysis(study):
    path, _ = study
    source = path / "episodes/vision/evaluation-00/attempt-01/episode.json"
    episode = analyzer.load(source); episode["score"] += 1; write(source, episode)
    with pytest.raises(analyzer.ReportError, match="changed after full audit"):
        analyzer.analyze(path)


def test_incomplete_or_mutated_result_rejected(study, monkeypatch):
    path, _ = study
    monkeypatch.setattr(analyzer, "audit_study", lambda p: {"status": "incomplete"})
    with pytest.raises(analyzer.ReportError, match="complete audited"):
        analyzer.analyze(path)
    monkeypatch.setattr(analyzer, "audit_study", lambda p: {"status": "completed"})
    result = analyzer.load(path / "evaluation_result.json"); result["per_seed"][0]["seed"] = 9
    write(path / "evaluation_result.json", result)
    with pytest.raises(analyzer.ReportError, match="result changed"):
        analyzer.analyze(path)


def test_lock_blocks_running_study_and_missing_lock_never_created(study, tmp_path):
    path, calls = study
    missing = tmp_path / "missing"; missing.mkdir()
    with pytest.raises(analyzer.ReportError, match="missing"):
        analyzer.analyze(missing)
    assert not (missing / ".run.lock").exists()
    with (path / ".run.lock").open("rb") as active:
        fcntl.flock(active, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(analyzer.ReportError, match="running"):
            analyzer.analyze(path)
    assert not calls


def test_choice_distributions_preserve_every_full_id_and_duration():
    first, second, third = row("left:100"), row("left:300"), row("noop:100")
    first["receipt"]["tetris_context"]["calls"] = [
        {"purpose": "goal", "receipt": {"choice_id": "target:3:0"}},
        {"purpose": "game_action", "receipt": {"choice_id": "left:100"}},
    ]
    second["receipt"]["tetris_context"]["calls"] = [
        {"purpose": "goal", "receipt": {"choice_id": "keep"}},
        {"purpose": "goal", "receipt": {"choice_id": "target:9:3"}},
    ]
    third["receipt"]["tetris_context"]["calls"] = [
        {"purpose": "goal", "receipt": {"choice_id": "clear"}},
        {"purpose": "goal", "receipt": {"choice_id": "target:3:0"}},
    ]
    assert analyzer.inspect_choices({"trace": [first, second, third]}) == {
        "goal_choice_counts": {"target:3:0": 2, "keep": 1, "target:9:3": 1, "clear": 1},
        "native_action_counts": {"left:100": 1, "left:300": 1, "noop:100": 1},
    }
