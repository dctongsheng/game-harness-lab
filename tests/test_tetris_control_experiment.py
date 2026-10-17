"""V6 frozen package experiment: synthetic providers, actual rules, zero network."""
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import run_tetris_control_experiment as runner
import report_tetris_control_experiment as reporter
from game_harness.games import get_game
from game_harness.providers import Settings, digest

SETTINGS = Settings("http://127.0.0.1:1/v1/systemone", "fixture-decision", 1,
                    "https://fixture.invalid/v1", "fixture-coach", "never-publish-this-test-key", 1)
CANDIDATE = {"decision_instructions": "Frozen V5 policy", "strategy_notes": "Synthetic provider only"}
REPAIRED = {"decision_instructions": "LLM repair fixture; observe actual displacement", "strategy_notes": "No strength claims"}


class Decision:
    settings = SETTINGS

    def __init__(self, fail_at=None):
        self.count, self.fail_at, self.requests = 0, fail_at, []

    def health(self):
        return {"ok": True, "model": SETTINGS.decision_model, "request_think_mode": "never"}

    def decide(self, state, instructions, criteria, request_id):
        self.count += 1
        self.requests.append(deepcopy({"state": state, "instructions": instructions, "criteria": criteria, "request_id": request_id}))
        if self.count == self.fail_at:
            raise RuntimeError("fixture transport failure")
        action = "target:0:0" if request_id.endswith(":goal") else "hard-drop:100"
        answer = {"choice": action, "confidence": 1,
                  "probabilities": {choice["id"]: float(choice["id"] == action) for choice in criteria},
                  "details": {"thought": {"used": False, "tokens": 0}}}
        raw = {"model": SETTINGS.decision_model, "answers": {"primary_action": answer}, "usage": {"total_tokens": 10}}
        return {"request_id": request_id, "observation_revision": state["step"],
                "candidate_hash": digest(criteria), "choice_id": action, "model": SETTINGS.decision_model,
                "elapsed_ms": .1, "think_mode": "never", "think_used": False, "reasoning_tokens": 0,
                "raw_response": raw, "response_hash": digest(raw)}


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    hashes = {"pyproject.toml": hashlib.sha256((ROOT / "pyproject.toml").read_bytes()).hexdigest()}
    monkeypatch.setattr(runner, "code_hashes", lambda: hashes)
    parent = {"game_id": "tetris", "brief": {"goal": "native points"}, "candidate": CANDIDATE,
              "game_context": get_game("tetris").context({"max_steps": 80})}
    repair = {**parent, "candidate": REPAIRED}
    parent_path, repaired_path = tmp_path / "parent.json", tmp_path / "repaired.json"
    parent_path.write_text(json.dumps(parent)); repaired_path.write_text(json.dumps(repair))
    data_root = tmp_path / "data"
    manifest = runner.prepare(identity="v6-test", parent_path=parent_path, repaired_path=repaired_path,
                              settings=SETTINGS, root=data_root / "experiments", data_root=data_root)
    return data_root / "experiments/v6-test", manifest, parent_path, repaired_path


def experiment(path, **kwargs):
    return runner.TetrisControlExperiment(path, settings=SETTINGS, **kwargs)


def test_plan_is_disjoint_deterministic_balanced_and_fixes_52_games():
    first = runner.scenario_plan(20)
    excluded = {first["calibration"][0]["seed"], first["evaluation"][0]["seed"]}
    plan = runner.scenario_plan(20, excluded)
    assert plan == runner.scenario_plan(20, excluded)
    assert [len(plan[p]) for p in ("calibration", "evaluation")] == [2, 24]
    seeds = [row["seed"] for rows in plan.values() for row in rows]
    assert len(seeds) == len(set(seeds)) == 26 and not set(seeds) & excluded
    for phase, count in (("calibration", 1), ("evaluation", 12)):
        for position in range(2):
            assert Counter(row["order"][position] for row in plan[phase]) == dict.fromkeys(runner.ARMS, count)


def test_manifest_freezes_distinct_candidates_shared_brief_v1_baseline_v2_repair_no_secret(prepared):
    path, m, _, _ = prepared
    assert m["budget"]["completed_episodes"] == 52
    assert m["budget"]["decision_request_attempts_without_episode_retries_max"] == 8320
    assert m["budget"]["decision_request_attempts_max"] == 16640
    assert m["arms"]["baseline"]["candidate"] == CANDIDATE
    assert m["arms"]["repaired"]["candidate"] == REPAIRED
    assert m["arms"]["baseline"]["tetris_context_config"] == runner.v5_context_config("both")
    assert m["arms"]["repaired"]["tetris_context_config"] == {**runner.v5_context_config("both"), "schema": "tetris-context-v2", "control_feedback": True}
    assert all(h["brief"] == m["parent"]["brief"] for h in m["arms"].values())
    assert m["statistics"]["primary_comparisons"] == ["repaired-baseline"]
    assert m["constraints"]["publish"] is False and m["constraints"]["registry_writes"] is False
    assert "never-publish-this-test-key" not in (path / "manifest.json").read_text()
    assert not list(path.rglob("*.sqlite"))


@pytest.mark.parametrize("mutation,match", [
    (lambda p: p.update(brief={"goal": "changed"}), "exact parent brief"),
    (lambda p: p.update(candidate=CANDIDATE), "actually revised"),
    (lambda p: p.update(think_mode="auto"), "thinking mode"),
    (lambda p: p.update(game_context=get_game("tetris").context({"max_steps": 1})), "fixed 80"),
    (lambda p: p.update(tetris_context_config=runner.context_config("baseline")), "fixed v2"),
])
def test_repair_scope_mismatches_rejected_before_network(prepared, mutation, match):
    path, _, parent_path, repaired_path = prepared
    repair = runner.load(repaired_path); mutation(repair); repaired_path.write_text(json.dumps(repair))
    with pytest.raises(runner.ExperimentError, match=match):
        runner.prepare(identity="rejected", parent_path=parent_path, repaired_path=repaired_path,
                       settings=SETTINGS, root=path.parent, data_root=path.parent.parent)
    assert not (path.parent / "rejected").exists()


def test_source_provider_and_manifest_drift_rejected(prepared, monkeypatch):
    path, manifest, _, _ = prepared
    monkeypatch.setattr(runner, "code_hashes", lambda: {"pyproject.toml": "different"})
    with pytest.raises(runner.ExperimentError, match="Source changed"):
        experiment(path)
    monkeypatch.setattr(runner, "code_hashes", lambda: manifest["code_hashes"])
    other = Settings(SETTINGS.decision_url, "other", 1, SETTINGS.coach_base_url, SETTINGS.coach_model, "ignored", 1)
    with pytest.raises(runner.ExperimentError, match="Provider settings"):
        runner.TetrisControlExperiment(path, settings=other, decision=Decision())
    manifest["config"]["max_steps"] = 1
    (path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(runner.ExperimentError, match="Manifest changed"):
        experiment(path)


def test_all_52_trials_run_irrespective_of_negative_calibration_and_cache(prepared, monkeypatch):
    path, manifest, _, _ = prepared
    calls, cache = [], {}
    def fake_episode(logical_id, harness, seed, purpose):
        if logical_id not in cache:
            arm = next(arm for arm in runner.ARMS if harness["id"] == manifest["arms"][arm]["id"])
            calls.append((purpose, seed, arm))
            points = 100 if arm == "baseline" else 0
            cache[logical_id] = {"episode_id": logical_id, "score": points, "steps": 0,
                                 "final_state": {"metrics": {"lines": points // 100}},
                                 "termination_reason": "step_limit", "trace": []}
        return cache[logical_id]
    with experiment(path, decision=Decision()) as run:
        monkeypatch.setattr(run, "episode", fake_episode)
        with pytest.raises(runner.ExperimentError, match="run-calibration"):
            run.run_evaluation()
        calibration = run.run_calibration()
        assert len(calls) == 4 and calibration["evaluation_allowed"]
        (path / "calibration_result.sha256.json").unlink()
        assert run.run_calibration() == calibration and len(calls) == 4
        result = run.run_evaluation()
        assert len(calls) == 52 and result["comparisons"]["repaired-baseline"]["mean_gain"] == -100
        assert result["comparisons"]["repaired-baseline"]["screening_gate_pass"] is False
        assert run.run_evaluation() == result and len(calls) == 52
    assert calls == [(phase, scenario["seed"], arm) for phase in ("calibration", "evaluation")
                     for scenario in manifest["scenarios"][phase] for arm in scenario["order"]]
    assert result["published"] is False and result["costs"]["coach_calls"] == 0


@pytest.mark.skipif(not shutil.which("node"), reason="Native Tetris adapter needs Node")
@pytest.mark.parametrize("arm", runner.ARMS)
def test_native_rules_receipts_archive_context_replay_and_cache(prepared, arm):
    path, m, _, _ = prepared
    scenario = m["scenarios"]["calibration"][0]
    logical = f"{scenario['id']}-{arm}"
    decision = Decision()
    with experiment(path, decision=decision) as run:
        episode = run.episode(logical, m["arms"][arm], scenario["seed"], "calibration")
        audit = run.verify_episode(episode, m["arms"][arm])
        count = decision.count
        assert run.episode(logical, m["arms"][arm], scenario["seed"], "calibration") == episode
        assert decision.count == count
        assert audit["context_audit"]["actual_inputs_verified"]
        assert audit["context_audit"]["memory_replay_verified"]
        assert audit["context_audit"]["goal_calls"] == audit["context_audit"]["thinking_off_goal_calls"]
        assert run.costs()["decision_started"] == episode["steps"] + audit["context_audit"]["goal_calls"]
    report = reporter.report(path)
    assert report["status"] == "incomplete"
    assert report["audit"]["completed_episodes_replayed"] == 1
    assert report["audit"]["ledger_calls_matched_to_archived_requests_and_receipts"] == count
    assert report["call_archive"]["calls_not_in_completed_episodes"] == 0
    assert sum(values["total_tokens"] for values in report["call_archive"]["reported_usage_by_purpose"].values()) == 10 * count
    assert all(("tetris_control_facts" in req["state"]) == (arm == "repaired") for req in decision.requests)
    assert all("arm" not in req["state"] for req in decision.requests)
    request_file = next((path / "calls").glob("*/request.json"))
    request = runner.load(request_file); request["instructions"] += " tampered"
    request_file.write_text(json.dumps(request))
    with pytest.raises(reporter.ReportError, match="outbound request"):
        reporter.report(path)


@pytest.mark.skipif(not shutil.which("node"), reason="Native Tetris adapter needs Node")
def test_failure_explicit_retry_and_crash_after_save_recovery(prepared):
    path, m, _, _ = prepared
    scenario, arm = m["scenarios"]["calibration"][0], "repaired"
    logical, harness = f"{scenario['id']}-{arm}", m["arms"][arm]
    with experiment(path, decision=Decision(fail_at=2)) as run:
        with pytest.raises(runner.ExperimentError, match="Incomplete"):
            run.episode(logical, harness, scenario["seed"], "calibration")
    with experiment(path, decision=Decision()) as run:
        with pytest.raises(runner.ExperimentError, match="Previous attempt"):
            run.episode(logical, harness, scenario["seed"], "calibration")
    decision = Decision()
    with experiment(path, decision=decision, retry_failed=True) as run:
        episode = run.episode(logical, harness, scenario["seed"], "calibration")
        complete = next(path.glob(f"episodes/{logical}/attempt-*/complete.json"))
        complete.unlink()
        count = decision.count
        assert run.episode(logical, harness, scenario["seed"], "calibration") == episode
        assert decision.count == count
        assert run.costs()["decision_failed"] == 1
        with pytest.raises(runner.ExperimentError, match="exhausted"):
            run.attempt(path / "episodes" / logical, 2)
    report = reporter.report(path)
    assert len(report["incomplete_attempts"]) == 1
    assert report["call_archive"]["calls_not_in_completed_episodes"] == 2
    assert report["call_archive"]["status_counts"]["failed"] == 1


def test_report_respects_existing_lock_and_never_creates_lock(prepared):
    path, _, _, _ = prepared
    with pytest.raises(reporter.ReportError, match="missing"):
        reporter.report(path)
    assert not (path / ".run.lock").exists()
    with experiment(path, decision=Decision()):
        with pytest.raises(reporter.ReportError, match="running"):
            reporter.report(path)
    before = {str(p.relative_to(path)): p.read_bytes() for p in path.rglob("*") if p.is_file()}
    assert reporter.report(path)["status"] == "incomplete"
    after = {str(p.relative_to(path)): p.read_bytes() for p in path.rglob("*") if p.is_file()}
    assert before == after


def test_statistics_use_single_primary_comparison_paired_any_line_and_strict_gate():
    config = {"bootstrap_resamples": 1000, "bootstrap_seed": 3}
    gate = {"minimum_mean_gain": 100, "no_pair_regression": True, "bootstrap_lower_greater_than": 0}
    def rows(pairs):
        return [{"seed": i, "arms": {arm: {"points": points, "lines": points // 100,
                  "termination_reason": "step_limit", "steps": 80, "goal_calls": 4,
                  "control_diagnostics": {key: 4 for key in runner.CONTROL_KEYS}}
                  for arm, points in zip(runner.ARMS, pair)}} for i, pair in enumerate(pairs)]
    result = runner.summarize(rows([(0, 100)] * 24), config, gate)
    comparison = result["comparisons"]["repaired-baseline"]
    assert comparison["bootstrap_95"] == [100, 100] and comparison["screening_gate_pass"]
    assert "bootstrap_98_333" not in comparison
    assert result["any_line_comparison"]["n01_baseline_no_repaired_yes"] == 24
    assert result["any_line_comparison"]["n10_baseline_yes_repaired_no"] == 0
    assert result["any_line_comparison"]["bootstrap_95"] == [1, 1]
    assert result["arms"]["repaired"]["steps_per_locked_piece"] == 20
    assert not runner.summarize(rows([(100, 0)] + [(0, 200)] * 23), config, gate)["comparisons"]["repaired-baseline"]["screening_gate_pass"]
    tied = runner.summarize(rows([(0, 0)] * 24), config, gate)
    assert tied["any_line_comparison"]["exact_mcnemar_two_sided_p"] == 1
    assert not tied["comparisons"]["repaired-baseline"]["screening_gate_pass"]
    with pytest.raises(runner.ExperimentError, match="unique bag"):
        runner.contrast_stats([{"seed": 1, "difference": 0}, {"seed": 1, "difference": 1}])


def transition(x, after_x, action, *, target=6, rotation=0, after_rotation=None, piece=1, after_piece=None):
    def snapshot(column, orientation, pid):
        return {"observation": {"pieceId": pid, "active": {"type": "S", "rotation": orientation, "cells": [{"x": column, "y": 1}]},
                                "board": {"rows": [{"cells": ["."] * 10}]}}, "metrics": {"lines": 0}}
    return {"before": snapshot(x, rotation, piece), "after": snapshot(after_x, rotation if after_rotation is None else after_rotation, piece if after_piece is None else after_piece),
            "action": action, "receipt": {"tetris_context": {"action_input_state": {"decision_goal_memory": {"target": {"leftmost_column": target, "rotation": rotation}}}, "calls": []}}}


def test_overshoot_counts_actual_crossings_and_excludes_rotation_new_piece_and_target_switch_reversal():
    steps = [transition(3, 7, "right:300"), transition(7, 3, "left:300"),
             transition(3, 4, "right:100", target=4),  # changed target resets reversal scope
             transition(4, 7, "right:300", target=6, after_rotation=1),
             transition(3, 7, "right:300", after_piece=2)]
    result = reporter.episode_diagnostics({"trace": steps})
    assert result["horizontal_actions"] == 5
    assert result["strict_overshoot_opportunities"] == 3
    assert result["strict_overshoots"] == 2
    assert result["same_piece_horizontal_reversals"] == 1
    assert result["horizontal_reversal_opportunities"] == 2


@pytest.mark.skipif(not shutil.which("node"), reason="Native Tetris adapter needs Node")
def test_completed_calibration_report_recalculates_stats_and_rejects_tamper(prepared):
    path, manifest, _, _ = prepared
    with experiment(path, decision=Decision()) as run:
        result = run.run_calibration()
    public = reporter.report(path)
    assert public["phases"]["calibration"]["status"] == "completed"
    assert public["phases"]["evaluation"]["status"] == "not_started"
    assert public["audit"]["completed_episodes_replayed"] == 4
    assert public["phases"]["calibration"]["frozen_registered_result"]["comparisons"] == result["comparisons"]
    result["comparisons"]["repaired-baseline"]["mean_gain"] = 999
    (path / "calibration_result.json").write_text(json.dumps(result))
    (path / "calibration_result.sha256.json").write_text(json.dumps({"sha256": digest(result)}))
    with pytest.raises(reporter.ReportError, match="statistics differ"):
        reporter.report(path)


def test_source_inventory_contains_full_v6_stack():
    hashes = runner.code_hashes()
    required = {"scripts/run_tetris_control_experiment.py", "scripts/report_tetris_control_experiment.py",
                "scripts/repair_tetris_control_harness.py", "scripts/run_tetris_control_diagnostics.py",
                "scripts/run_tetris_context_experiment.py", "scripts/report_tetris_context_experiment.py",
                "scripts/run_feedback_experiment.py", "scripts/audit_saved_run.py",
                "src/game_harness/tetris_context.py", "src/game_harness/tetris_control.py", "pyproject.toml"}
    assert required <= set(hashes)
    for path in (ROOT / "src").rglob("*"):
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
            assert str(path.relative_to(ROOT)) in hashes
