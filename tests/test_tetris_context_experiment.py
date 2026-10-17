"""Frozen Tetris factorial schedule and evidence recovery; no model/network calls."""
from collections import Counter
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil

import pytest

from game_harness.games import get_game
from game_harness.providers import Settings, digest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/run_tetris_context_experiment.py"
spec = importlib.util.spec_from_file_location("tetris_context_experiment", SCRIPT)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
SETTINGS = Settings("http://127.0.0.1:1/v1/systemone", "fixture-decision", 1,
                    "https://fixture.invalid/v1", "fixture-coach", "never-publish-this-test-key", 1)
CANDIDATE = {"decision_instructions": "Use the same frozen policy in every condition.",
             "strategy_notes": "Fixtures do not establish game skill."}


class Decision:
    """Explicitly synthetic receipts with actual Tetris rules underneath."""
    settings = SETTINGS

    def __init__(self, fail_at=None):
        self.count = 0
        self.fail_at = fail_at
        self.requests = []

    def health(self):
        return {"ok": True, "model": SETTINGS.decision_model, "request_think_mode": "never"}

    def decide(self, state, instructions, criteria, request_id):
        self.count += 1
        self.requests.append(deepcopy({"state": state, "request_id": request_id, "criteria": criteria}))
        if self.count == self.fail_at:
            raise RuntimeError("fixture transport failure")
        action = "target:0:0" if request_id.endswith(":goal") else "hard-drop:100"
        assert action in {choice["id"] for choice in criteria}
        answer = {"choice": action, "confidence": 1,
                  "probabilities": {choice["id"]: float(choice["id"] == action) for choice in criteria},
                  "details": {"thought": {"used": False, "tokens": 0}}}
        raw = {"model": SETTINGS.decision_model, "answers": {"primary_action": answer},
               "usage": {"total_tokens": 10}}
        return {"request_id": request_id, "observation_revision": state["step"],
                "candidate_hash": digest(criteria), "choice_id": action, "model": SETTINGS.decision_model,
                "elapsed_ms": .1, "think_mode": "never", "think_used": False, "reasoning_tokens": 0,
                "raw_response": raw, "response_hash": digest(raw)}


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    # Collaborator edits do not make tests flaky. The source-freeze behavior is
    # separately tested with an intentional drift.
    hashes = {"pyproject.toml": hashlib.sha256((runner.ROOT / "pyproject.toml").read_bytes()).hexdigest()}
    monkeypatch.setattr(runner, "code_hashes", lambda: hashes)
    parent = tmp_path / "parent.json"
    parent.write_text(json.dumps({"game_id": "tetris", "brief": {"goal": "native points"}, "candidate": CANDIDATE,
                                  "game_context": get_game("tetris").context({"max_steps": 80})}))
    data_root = tmp_path / "data"
    manifest = runner.prepare(identity="test", parent_path=parent, settings=SETTINGS,
                              root=data_root / "experiments", data_root=data_root)
    return data_root / "experiments/test", manifest


def experiment(path, **kwargs):
    return runner.TetrisContextExperiment(path, settings=SETTINGS, **kwargs)


def test_plan_is_disjoint_latin_square_balanced_and_deterministic():
    first = runner.scenario_plan(10)
    blocked = {first["calibration"][0]["seed"], first["evaluation"][0]["seed"]}
    plan = runner.scenario_plan(10, blocked)
    assert plan == runner.scenario_plan(10, blocked)
    assert len(plan["calibration"]) == 2 and len(plan["evaluation"]) == 12
    seeds = [row["seed"] for rows in plan.values() for row in rows]
    assert len(set(seeds)) == 14 and not set(seeds) & blocked
    for position in range(4):
        assert Counter(row["order"][position] for row in plan["evaluation"]) == dict.fromkeys(runner.ARMS, 3)
    assert all(set(row["order"]) == set(runner.ARMS) for rows in plan.values() for row in rows)


def test_prior_seed_inventory_includes_failed_and_scheduled_and_trace_crash(tmp_path):
    runner.write_new(tmp_path / "episodes/one.json", {"game_id": "tetris", "seed": 111, "infra_error": {"type": "fixture"}})
    runner.write_new(tmp_path / "episodes/other.json", {"game_id": "gomoku", "seed": 222})
    runner.write_new(tmp_path / "experiments/old/manifest.json", {
        "game_id": "tetris", "seed": 999999, "scenarios": {"trigger": 112, "train": [113, 114],
        "final": [{"seed": 115, "repeat": 0, "stratum": 0}]}})
    runner.write_new(tmp_path / "experiments/new/manifest.json", {
        "game_id": "tetris", "scenarios": {"calibration": [{"seed": 116}], "evaluation": [{"seed": 117}]}})
    runner.write_new(tmp_path / "experiments/old/episodes/a/attempt-01/episode.json", {"game_id": "tetris", "seed": 118})
    runner.write_new(tmp_path / "experiments/old/source_snapshot/manifest.json", {"game_id": "tetris", "scenarios": {"trigger": 444}})
    trace = tmp_path / "experiments/crash/episodes/a/attempt-01/trace.jsonl"
    trace.parent.mkdir(parents=True)
    trace.write_text(json.dumps({"type": "episode_start", "game_id": "tetris", "seed": 119}) + "\ninvalid unfinished second line")
    used, evidence = runner.prior_tetris_seeds(tmp_path)
    assert used == set(range(111, 120))
    assert all("source_snapshot" not in row["path"] for row in evidence)
    assert not {0, 222, 444, 999999} & used


def test_prepare_freezes_shared_policy_full_budget_and_no_secret_or_publication(prepared):
    path, manifest = prepared
    assert manifest["config"] == {"max_steps": 80}
    assert manifest["budget"]["completed_episodes"] == 56
    assert manifest["budget"]["decision_request_attempts_without_episode_retries_max"] == 8960
    assert manifest["budget"]["decision_request_attempts_max"] == 17920
    for arm in runner.ARMS:
        harness = manifest["arms"][arm]
        assert harness["candidate"] == CANDIDATE and harness["brief"] == manifest["parent"]["brief"]
        assert harness["tetris_context_config"] == runner.context_config(arm)
    assert manifest["constraints"]["publish"] is False and manifest["constraints"]["registry_writes"] is False
    assert manifest["budget"]["coach_calls"] == 0
    assert "never-publish-this-test-key" not in (path / "manifest.json").read_text()
    assert not list(path.rglob("*.sqlite")) and not (path / "calls.jsonl").exists()
    hashes = {str(file.relative_to(path / "source_snapshot")): hashlib.sha256(file.read_bytes()).hexdigest()
              for file in (path / "source_snapshot").rglob("*") if file.is_file()}
    assert hashes == manifest["code_hashes"]


def test_source_inventory_includes_both_runners_audit_every_source_and_assets():
    hashes = runner.code_hashes()
    assert {"scripts/run_tetris_context_experiment.py", "scripts/run_feedback_experiment.py",
            "scripts/audit_saved_run.py", "src/game_harness/tetris_features.py", "pyproject.toml"} <= set(hashes)
    for path in (runner.ROOT / "src").rglob("*"):
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
            assert str(path.relative_to(runner.ROOT)) in hashes


def test_prepare_rejects_wrong_budget_and_arm_specific_parent(tmp_path, monkeypatch):
    parent = tmp_path / "parent.json"
    value = {"brief": {"goal": "points"}, "candidate": CANDIDATE,
             "game_context": get_game("tetris").context({"max_steps": 1})}
    parent.write_text(json.dumps(value))
    with pytest.raises(runner.ExperimentError, match="fixed 80"):
        runner.prepare(identity="bad", parent_path=parent, settings=SETTINGS, root=tmp_path, data_root=tmp_path)
    value["game_context"] = get_game("tetris").context({"max_steps": 80})
    value["tetris_context_config"] = runner.context_config("facts")
    parent.write_text(json.dumps(value))
    with pytest.raises(runner.ExperimentError, match="already be assigned"):
        runner.prepare(identity="bad", parent_path=parent, settings=SETTINGS, root=tmp_path, data_root=tmp_path)
    assert not (tmp_path / "bad").exists()


def test_all_four_arms_run_every_seed_without_score_gate_and_never_publish(prepared, monkeypatch):
    path, manifest = prepared
    calls = []
    fake_cache = {}

    def fake_episode(logical_id, harness, seed, purpose):
        # This test isolates orchestration. Receipt/replay tests below use the
        # real rule engine; synthetic negative scores here must not stop a run.
        if logical_id not in fake_cache:
            arm = next(arm for arm in runner.ARMS if harness["id"] == manifest["arms"][arm]["id"])
            calls.append((purpose, seed, arm))
            score = 100 if arm == "baseline" else 0
            fake_cache[logical_id] = {"episode_id": logical_id, "score": score, "steps": 0,
                                      "final_state": {"metrics": {"lines": score // 100}},
                                      "termination_reason": "step_limit", "trace": []}
        return fake_cache[logical_id]

    decision = Decision()
    with experiment(path, decision=decision) as run:
        monkeypatch.setattr(run, "episode", fake_episode)
        with pytest.raises(runner.ExperimentError, match="run-calibration"):
            run.run_evaluation()
        calibration = run.run_calibration()
        assert len(calls) == 8 and calibration["evaluation_allowed"] is True
        assert all(not row["screening_gate_pass"] for row in calibration["comparisons"].values())
        result_hash = path / "calibration_result.sha256.json"
        result_hash.unlink()  # Fixture: result reached disk just before a crash.
        assert run.run_calibration() == calibration and result_hash.exists()
        assert len(calls) == 8
        evaluation = run.run_evaluation()
        assert len(calls) == 56 and evaluation["status"] == "completed"
        assert all(row["mean_gain"] == -100 for row in evaluation["comparisons"].values())
        assert all(not row["screening_gate_pass"] for row in evaluation["comparisons"].values())
        assert run.run_evaluation() == evaluation and len(calls) == 56
    expected = [(phase, row["seed"], arm) for phase in ("calibration", "evaluation")
                for row in manifest["scenarios"][phase] for arm in row["order"]]
    assert calls == expected
    assert evaluation["published"] is False and evaluation["policy_changed"] is False
    assert evaluation["costs"]["health_started"] == 1
    assert decision.count == 0 and not list(path.rglob("*.sqlite"))


@pytest.mark.skipif(not shutil.which("node"), reason="Real Tetris adapter requires Node.js")
@pytest.mark.parametrize("arm", runner.ARMS)
def test_actual_rules_mock_model_receipts_full_context_audit_and_cached_resume(prepared, arm):
    path, manifest = prepared
    decision = Decision()
    with experiment(path, decision=decision) as run:
        harness = manifest["arms"][arm]
        episode = run.episode("receipt-fixture", harness, 12, "fixture")
        audit = run.verify_episode(episode, harness)
        count = decision.count
        assert run.episode("receipt-fixture", harness, 12, "fixture") == episode
        assert decision.count == count
        assert episode["config"]["max_steps"] == 80
        assert episode["steps"] > 0 and episode["score"] == 0
        assert audit["actions"] == audit["thinking_off_actions"] == episode["steps"]
        context = audit["context_audit"]
        assert context["actual_inputs_verified"] and context["memory_replay_verified"]
        assert context["goal_calls"] == context["thinking_off_goal_calls"]
        assert bool(context["goal_calls"]) == (arm in {"memory", "both"})
        costs = run.costs()
        assert costs["decision_started"] == episode["steps"] + context["goal_calls"]
        assert costs["by_purpose"]["goal"]["decision_started"] == context["goal_calls"]
        assert costs["completed_episodes"] == 1 and costs["coach_calls"] == 0
    assert len(list((path / "calls").glob("*/request.json"))) == count
    assert len(list((path / "calls").glob("*/receipt.json"))) == count
    for request in decision.requests:
        assert "arm" not in request["state"] and "baseline" not in request["state"]
        assert ("tetris_features" in request["state"]) == (arm in {"facts", "both"})


@pytest.mark.skipif(not shutil.which("node"), reason="Real Tetris adapter requires Node.js")
def test_infrastructure_failure_stops_then_explicit_retry_preserves_all_evidence(prepared):
    path, manifest = prepared
    harness = manifest["arms"]["baseline"]
    failing = Decision(fail_at=2)
    with experiment(path, decision=failing) as run:
        with pytest.raises(runner.ExperimentError, match="Incomplete"):
            run.episode("failed", harness, 12, "fixture")
    assert len(list(path.glob("episodes/failed/attempt-*/failed.json"))) == 1
    recovery = Decision()
    with experiment(path, decision=recovery) as run:
        with pytest.raises(runner.ExperimentError, match="Previous attempt"):
            run.episode("failed", harness, 12, "fixture")
    assert recovery.count == 0
    with experiment(path, decision=recovery, retry_failed=True) as run:
        episode = run.episode("failed", harness, 12, "fixture")
        costs = run.costs()
    assert episode["score"] == 0 and not episode["infra_error"]
    assert costs["completed_episodes"] == 1 and costs["episode_attempts"] == 2
    assert costs["decision_failed"] == 1
    failed_episode = runner.load(path / "episodes/failed/attempt-01/episode.json")
    assert failed_episode["infra_error"] and failed_episode["episode_id"] != episode["episode_id"]


@pytest.mark.skipif(not shutil.which("node"), reason="Real Tetris adapter requires Node.js")
def test_crash_after_episode_save_recovers_without_new_model_calls_and_detects_tampering(prepared):
    path, manifest = prepared
    decision, harness = Decision(), manifest["arms"]["both"]
    with experiment(path, decision=decision) as run:
        episode = run.episode("one", harness, 12, "fixture")
        complete = next(path.glob("episodes/one/attempt-*/complete.json"))
        complete.unlink()  # Simulated crash window after durable episode save.
        count = decision.count
        assert run.episode("one", harness, 12, "fixture") == episode
        assert decision.count == count and complete.exists()
        bad = deepcopy(episode)
        bad["trace"][0]["receipt"]["tetris_context"]["calls"][0]["input_state"]["step"] += 1
        with pytest.raises(Exception, match="input differs"):
            run.verify_episode(bad, harness)
        bad = deepcopy(episode)
        bad["trace"][0]["receipt"]["think_used"] = True
        with pytest.raises(Exception, match="telemetry"):
            run.verify_episode(bad, harness)
        episode_path = complete.parent / "episode.json"
        bad = deepcopy(episode)
        bad["score"] += 100
        episode_path.write_text(json.dumps(bad))
        with pytest.raises(runner.ExperimentError, match="identity changed"):
            run.episode("one", harness, 12, "fixture")


def test_attempt_budget_is_bounded(prepared):
    path, _ = prepared
    with experiment(path, decision=Decision(), retry_failed=True) as run:
        first = run.attempt(path / "fixture-failure", 2)
        second = run.attempt(path / "fixture-failure", 2)
        assert first != second
        with pytest.raises(runner.ExperimentError, match="exhausted"):
            run.attempt(path / "fixture-failure", 2)


def test_provider_manifest_or_source_drift_refuses_resume_before_network(prepared, monkeypatch):
    path, manifest = prepared
    monkeypatch.setattr(runner, "code_hashes", lambda: {"pyproject.toml": "changed"})
    with pytest.raises(runner.ExperimentError, match="Source changed"):
        experiment(path)
    monkeypatch.setattr(runner, "code_hashes", lambda: manifest["code_hashes"])
    changed = Settings(SETTINGS.decision_url, "other-model", 1, SETTINGS.coach_base_url, SETTINGS.coach_model, "ignored", 1)
    with pytest.raises(runner.ExperimentError, match="Provider settings"):
        runner.TetrisContextExperiment(path, settings=changed, decision=Decision())
    value = runner.load(path / "manifest.json")
    value["config"]["max_steps"] = 1
    (path / "manifest.json").write_text(json.dumps(value))
    with pytest.raises(runner.ExperimentError, match="Manifest changed"):
        experiment(path)


def test_contrasts_pair_by_seed_bonferroni_interval_and_screening_gate():
    statistics_config = {"bootstrap_resamples": 500, "bootstrap_seed": 3}
    gate = {"minimum_mean_gain": 100, "no_pair_regression": True, "familywise_bootstrap_lower_greater_than": 0}
    def rows(scores):
        return [{"seed": seed, "arms": {arm: {"points": scores[arm], "lines": scores[arm] // 100,
                                              "termination_reason": "step_limit", "steps": 80, "goal_calls": 2}
                                         for arm in runner.ARMS}} for seed in range(12)]
    result = runner.summarize(rows({"baseline": 0, "facts": 100, "memory": 0, "both": 200}), statistics_config, gate)
    assert result["comparisons"]["facts-baseline"]["screening_gate_pass"] is True
    assert result["comparisons"]["memory-baseline"]["screening_gate_pass"] is False
    assert result["comparisons"]["both-baseline"]["bootstrap_98_333"] == [200, 200]
    assert result["interaction"]["mean_gain"] == 100
    assert result["arms"]["both"]["any_line_episodes"] == 12
    noisy = [{"seed": seed, "difference": (seed - 5) * 100} for seed in range(12)]
    first = runner.contrast_stats(noisy, resamples=500, bootstrap_seed=3)
    assert first == runner.contrast_stats(noisy, resamples=500, bootstrap_seed=3)
    assert first["bootstrap_98_333"][0] <= first["bootstrap_95"][0]
    assert first["bootstrap_98_333"][1] >= first["bootstrap_95"][1]
    with pytest.raises(runner.ExperimentError, match="unique bag"):
        runner.contrast_stats([{"seed": 1, "difference": 100}, {"seed": 1, "difference": 200}])
