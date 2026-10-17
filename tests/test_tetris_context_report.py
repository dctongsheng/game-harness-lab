"""Public diagnostics use saved synthetic calls and actual native rule replay."""
from copy import deepcopy
import fcntl
import importlib.util
import json
from pathlib import Path

import pytest

from game_harness.games import get_game
from game_harness.runtime import run_episode

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/report_tetris_context_experiment.py"
spec = importlib.util.spec_from_file_location("tetris_context_report", SCRIPT)
reporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reporter)


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def study(tmp_path):
    (tmp_path / ".run.lock").write_bytes(b"")
    arms = ["baseline", "facts", "memory", "both"]
    context = get_game("tetris").context({"max_steps": 2})
    parent = {"candidate": {"decision_instructions": "fixture policy", "strategy_notes": "fixture notes"},
              "brief": {"goal": "fixture"}}
    manifest = {"schema": "tetris-context-experiment/v1", "id": "fixture-report", "arm_order": arms,
                "config": {"max_steps": 2}, "parent": parent,
                "candidate_hash": reporter.digest(parent["candidate"]), "brief_hash": reporter.digest(parent["brief"]),
                "providers": {"decision_model": "fixture-decision", "decision_url": "http://private.example/v1",
                              "coach_api_key": "sk-secret-example-never-include"},
                "constraints": {"publish": False, "registry_writes": False, "in_game_coach": False, "arm_policy_text_changes": False},
                "scenarios": {"calibration": [{"id": "calibration-00", "seed": 91, "order": arms}],
                              "evaluation": [{"id": "evaluation-00", "seed": 92, "order": arms}]},
                "arms": {arm: {"id": "h-" + arm, "candidate": parent["candidate"], "brief": parent["brief"],
                               "game_id": "tetris", "game_context": context, "decision_model": "fixture-decision",
                               "tetris_context_config": {"schema": "tetris-context-v1", "structured_facts": arm in {"facts", "both"},
                                                         "decision_goal_memory": arm in {"memory", "both"}, "replan_every_actions": 4}}
                         for arm in arms}}
    write(tmp_path / "manifest.json", manifest)
    write(tmp_path / "manifest.sha256.json", {"sha256": reporter.digest(manifest)})
    return manifest


class RecordingFixture:
    def __init__(self, root, *, fail=False, hold_first=False):
        self.root, self.fail, self.hold_first = root, fail, hold_first

    def decide(self, state, instructions, criteria, request_id):
        purpose = "goal" if request_id.endswith(":goal") else "game_action"
        directory = self.root / "calls" / reporter.digest(request_id)[:32]
        write(directory / "request.json", {"request_id": request_id, "purpose": purpose, "state": deepcopy(state),
              "instructions": instructions, "criteria": deepcopy(criteria), "think_mode": "never", "expected_model": "fixture-decision"})
        with (self.root / "calls.jsonl").open("a") as stream:
            stream.write(json.dumps({"kind": "decision_started", "purpose": purpose, "request_id": request_id}) + "\n")
        if self.fail and purpose == "game_action":
            write(directory / "failed.json", {"error_type": "RuntimeError"})
            raise RuntimeError("synthetic failure")
        choice = "target:3:0" if purpose == "goal" else "hold:100" if self.hold_first and state["step"] == 0 else "hard-drop:100"
        raw = {"model": "fixture-decision", "answers": {"primary_action": {
            "choice": choice, "confidence": 1., "probabilities": {item["id"]: float(item["id"] == choice) for item in criteria},
            "details": {"thought": {"used": False, "tokens": 0}}}}}
        receipt = {"choice_id": choice, "model": "fixture-decision", "elapsed_ms": 2., "raw_response": raw,
                   "response_hash": reporter.digest(raw), "request_id": request_id, "observation_revision": state["step"],
                   "candidate_hash": reporter.digest(criteria), "think_mode": "never", "think_used": False, "reasoning_tokens": 0}
        write(directory / "receipt.json", receipt)
        return receipt


def episode(root, manifest, phase="calibration", arm="memory", *, fail=False, hold_first=False):
    scenario = manifest["scenarios"][phase][0]
    logical_id = f"{scenario['id']}-{arm}"
    attempt = root / "episodes" / logical_id / "attempt-01"
    episode_id = "exp-" + reporter.digest({"experiment": manifest["id"], "logical_id": logical_id, "attempt": "attempt-01"})[:24]
    write(attempt / "started.json", {})
    harness = manifest["arms"][arm]
    value = run_episode(RecordingFixture(root, fail=fail, hold_first=hold_first), harness,
                        episode_id=episode_id, seed=scenario["seed"], adapter=get_game("tetris"), config={"max_steps": 2})
    value.update(purpose=phase, logical_trial=logical_id, experiment_id=manifest["id"])
    write(attempt / "episode.json", value)
    if fail:
        write(attempt / "failed.json", {"error_type": "RuntimeError"})
    else:
        assert value["infra_error"] is None
        write(attempt / "complete.json", {"episode_hash": reporter.digest(value), "harness_hash": reporter.digest(harness)})
    return value


def finish_phase(root, manifest, phase, episodes):
    scenario = manifest["scenarios"][phase][0]
    rows = {}
    for arm, value in episodes.items():
        diagnostics = reporter.episode_diagnostics(value)
        rows[arm] = {"episode_id": value["episode_id"], "points": value["score"], "lines": value["final_state"]["metrics"]["lines"],
                     "steps": value["steps"], "termination_reason": value["termination_reason"],
                     "goal_calls": sum(diagnostics["goal_choice_counts"].values())}
    result = {"status": "completed", "phase": phase, "per_seed": [{**scenario, "arms": rows}],
              "arms": {"fixture": "frozen summary"}, "comparisons": {"fixture": {"mean_gain": 0, "screening_gate_pass": False}},
              "interaction": {"mean_gain": 0}}
    write(root / f"{phase}_result.json", result)
    write(root / f"{phase}_result.sha256.json", {"sha256": reporter.digest(result)})


def test_unstarted_or_health_failure_is_not_reported_complete(tmp_path):
    study(tmp_path)
    (tmp_path / "calls.jsonl").write_text(json.dumps({"kind": "health_failed", "error": "private endpoint"}) + "\n")
    result = reporter.report(tmp_path)
    assert result["status"] == "incomplete"
    assert result["phases"]["calibration"]["status"] == "not_started"
    assert result["phases"]["evaluation"]["arms"]["baseline"]["mean_points"] is None
    assert result["call_archive"]["event_counts"]["health_failed"] == 1
    text = json.dumps(result)
    assert "private.example" not in text and "sk-secret" not in text and "private endpoint" not in text


def test_report_rejects_running_experiment_without_blocking_or_modifying_lock(tmp_path):
    study(tmp_path)
    lock_path = tmp_path / ".run.lock"
    before = lock_path.stat()
    with lock_path.open("a") as runner_lock:
        fcntl.flock(runner_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with pytest.raises(reporter.ReportError, match="Experiment is running"):
                reporter.report(tmp_path)
        finally:
            fcntl.flock(runner_lock, fcntl.LOCK_UN)
    assert reporter.report(tmp_path)["status"] == "incomplete"
    after = lock_path.stat()
    assert (before.st_mtime_ns, before.st_size) == (after.st_mtime_ns, after.st_size)


def test_report_allows_other_readers_and_never_creates_missing_lock(tmp_path):
    study(tmp_path)
    lock_path = tmp_path / ".run.lock"
    with lock_path.open("rb") as reader_lock:
        fcntl.flock(reader_lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        try:
            assert reporter.report(tmp_path)["status"] == "incomplete"
        finally:
            fcntl.flock(reader_lock, fcntl.LOCK_UN)
    lock_path.unlink()
    with pytest.raises(reporter.ReportError, match="Existing .run.lock is missing"):
        reporter.report(tmp_path)
    assert not lock_path.exists()


def test_full_content_archive_match_and_hold_not_counted_as_lock(tmp_path):
    manifest = study(tmp_path)
    value = episode(tmp_path, manifest, hold_first=True)
    result = reporter.report(tmp_path)
    stats = result["phases"]["calibration"]["arms"]["memory"]
    assert stats["episodes"] == 1 and stats["locked_pieces"] == 1
    assert stats["goal_choice_counts"] == {"target": 2}
    assert stats["latency_ms"]["goal"]["calls"] == 2
    assert result["audit"]["ledger_calls_matched_to_archived_requests_and_receipts"] == 4
    assert result["phases"]["calibration"]["status"] == "partial"
    assert result["phases"]["calibration"]["frozen_registered_result"] is None


@pytest.mark.parametrize("kind", ["state", "instructions", "criteria", "receipt", "missing"])
def test_rejects_request_or_receipt_archive_disagreement(tmp_path, kind):
    manifest = study(tmp_path)
    value = episode(tmp_path, manifest)
    call = value["trace"][0]["receipt"]["tetris_context"]["calls"][0]
    directory = tmp_path / "calls" / reporter.digest(call["request_id"])[:32]
    if kind == "missing":
        (directory / "receipt.json").unlink()
    elif kind == "receipt":
        receipt = reporter.load(directory / "receipt.json")
        receipt["elapsed_ms"] = 999
        write(directory / "receipt.json", receipt)
    else:
        request = reporter.load(directory / "request.json")
        request[kind] = "tampered"
        write(directory / "request.json", request)
    with pytest.raises(reporter.ReportError):
        reporter.report(tmp_path)


def test_failed_attempt_preserves_costs_without_inventing_zero_score(tmp_path):
    manifest = study(tmp_path)
    episode(tmp_path, manifest, fail=True)
    result = reporter.report(tmp_path)
    assert result["incomplete_attempts"][0]["status"] == "failed"
    assert result["phases"]["calibration"]["arms"]["memory"]["episodes"] == 0
    assert result["phases"]["calibration"]["arms"]["memory"]["mean_points"] is None
    assert result["call_archive"]["requests_saved"] == 2
    assert result["call_archive"]["status_counts"] == {"receipt_saved": 1, "failed": 1}
    assert result["call_archive"]["calls_not_in_completed_episodes"] == 2


def test_completed_phase_copies_frozen_statistics_only_after_all_games(tmp_path):
    manifest = study(tmp_path)
    for phase in manifest["scenarios"]:
        values = {arm: episode(tmp_path, manifest, phase, arm) for arm in manifest["arm_order"]}
        finish_phase(tmp_path, manifest, phase, values)
    result = reporter.report(tmp_path)
    assert result["status"] == "completed"
    assert result["audit"]["completed_episodes_replayed"] == 8
    assert result["phases"]["evaluation"]["frozen_registered_result"]["comparisons"]["fixture"]["screening_gate_pass"] is False
    saved = reporter.load(tmp_path / "evaluation_result.json")
    saved["per_seed"][0]["arms"]["baseline"]["points"] = 999
    write(tmp_path / "evaluation_result.json", saved)
    write(tmp_path / "evaluation_result.sha256.json", {"sha256": reporter.digest(saved)})
    with pytest.raises(reporter.ReportError, match="differs"):
        reporter.report(tmp_path)


def test_exploratory_goal_arrival_and_horizontal_reversal_are_observational():
    board = {"rows": [{"cells": "." * 10} for _ in range(22)]}
    before = {"observation": {"board": board, "pieceId": 1, "active": {
        "type": "T", "rotation": 0, "cells": [{"x": 3, "y": 0}]}}, "metrics": {"lines": 0}}
    trace = [{"before": deepcopy(before), "after": deepcopy(before), "action": action, "receipt": {"tetris_context": {
        "action_input_state": {"decision_goal_memory": {"target": {"leftmost_column": 3, "rotation": 0}}}, "calls": []}}}
        for action in ("left:100", "rotate-cw:100", "right:100", "hard-drop:100")]
    result = reporter.episode_diagnostics({"trace": trace})
    assert result["same_piece_horizontal_reversals"] == 1
    assert result["goal_already_reached_before_actions"] == 4
    assert result["actions_when_goal_already_reached"]["hard-drop:100"] == 1
