"""Experiment scheduling, real GEPA, receipt audit and bounded recovery; no network."""
from collections import Counter
from copy import deepcopy
import functools
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from game_harness.games import get_game
from game_harness.providers import Settings, digest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/run_feedback_experiment.py"
spec = importlib.util.spec_from_file_location("feedback_experiment", SCRIPT)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
SETTINGS = Settings("http://127.0.0.1:1/v1/systemone", "fixture-decision", 1,
                    "https://fixture.invalid/v1", "fixture-coach", "test-only", 1)
BASE = {"decision_instructions": "choose a valid action", "strategy_notes": "baseline"}
NEW = {"decision_instructions": "updated policy, choose a valid action", "strategy_notes": "new"}


class Decision:
    def __init__(self, fail_at=None):
        self.count = 0
        self.fail_at = fail_at

    def health(self):
        return {"ok": True, "model": SETTINGS.decision_model, "request_think_mode": "never"}

    def decide(self, state, instructions, criteria, request_id):
        self.count += 1
        if self.count == self.fail_at:
            raise RuntimeError("fixture transport failure")
        action = criteria[0]["id"]
        answer = {"choice": action, "confidence": 1, "probabilities": {item["id"]: float(item["id"] == action) for item in criteria},
                  "details": {"thought": {"used": False, "tokens": 0}}}
        raw = {"model": SETTINGS.decision_model, "answers": {"primary_action": answer}, "usage": {"total_tokens": 10}}
        return {"request_id": request_id, "observation_revision": state["step"], "candidate_hash": digest(criteria),
                "choice_id": action, "model": SETTINGS.decision_model, "elapsed_ms": .1,
                "think_mode": "never", "think_used": False, "reasoning_tokens": 0,
                "raw_response": raw, "response_hash": digest(raw)}


class Coach:
    def __init__(self, invalid=False):
        self.calls = []
        self.invalid = invalid

    def complete(self, messages, purpose):
        self.calls.append({"usage": {"total_tokens": 20}, "messages": deepcopy(messages)})
        return "invalid JSON" if self.invalid else json.dumps(NEW)


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    # Unrelated collaborating source edits must not cause a test flake.
    fixture_hashes = {"pyproject.toml": hashlib.sha256((runner.ROOT / "pyproject.toml").read_bytes()).hexdigest()}
    monkeypatch.setattr(runner, "code_hashes", lambda: fixture_hashes)
    config = {"max_steps": 1, "gomoku_size": 9}
    parent = tmp_path / "parent.json"
    parent.write_text(json.dumps({"brief": {"goal": "win"}, "candidate": BASE,
                                  "bootstrap": {"source": "fixture"},
                                  "game_context": get_game("gomoku").context(config)}))
    manifest = runner.prepare(identity="test", game="gomoku", parent_path=parent,
                              settings=SETTINGS, root=tmp_path)
    return tmp_path / "test", manifest


def experiment(path, **kwargs):
    return runner.Experiment(path, settings=SETTINGS, **kwargs)


def test_prepare_has_disjoint_balanced_splits_and_no_secret_or_registry(prepared):
    path, manifest = prepared
    plan = manifest["scenarios"]
    assert len(plan["development"]) == 4
    assert plan["development"] == plan["train"] + plan["selection_validation"]
    all_unique = [plan["trigger"], *plan["development"], *(r["seed"] for r in plan["final"])]
    assert len(all_unique) == len(set(all_unique))
    for name in ("train", "selection_validation"):
        assert sorted(seed % 2 for seed in plan[name]) == [0, 1]
    assert Counter(row["seed"] % 2 for row in plan["final"]) == {0: 6, 1: 6}
    for parity in (0, 1):
        assert Counter(row["order"][0] for row in plan["final"] if row["seed"] % 2 == parity) == {"parent": 3, "candidate": 3}
    assert manifest["budget"]["final_pairs"] == 12
    assert manifest["budget"]["final_episodes_max"] == 24
    assert "test-only" not in (path / "manifest.json").read_text()
    assert not list(path.rglob("*.sqlite"))
    assert not (path / "calls.jsonl").exists()
    assert (path / "README.md").exists()


def test_tetris_final_uses_twelve_fresh_bags_repeated_twice():
    plan = runner.scenario_plan("tetris", 100)
    counts = Counter(row["seed"] for row in plan["final"])
    assert len(counts) == 12 and set(counts.values()) == {2}
    assert not set(counts) & set([plan["trigger"], *plan["development"]])
    for seed in counts:
        rows = [r for r in plan["final"] if r["seed"] == seed]
        assert rows[0]["order"] == list(reversed(rows[1]["order"]))
    assert plan == runner.scenario_plan("tetris", 100)


def test_real_gepa_all_four_proposals_get_dev_trials_without_pool_admission(prepared, monkeypatch):
    path, manifest = prepared
    real = runner.evolve_harness
    calls = []

    @functools.wraps(real)
    def spy(**kwargs):
        calls.append(kwargs["feedback_format"])
        return real(**kwargs)

    monkeypatch.setattr(runner, "evolve_harness", spy)
    decision, coach = Decision(), Coach()
    with experiment(path, decision=decision, coach=coach) as run:
        assert run.proposal_coach(path / "proposals" / "legacy-1").settings.decision_model == run.settings.decision_model
        result = run.run_dev()
        assert run.run_dev() == result  # Idempotent completion: no extra actions.
    assert calls == [runner.FORMATS[0], runner.FORMATS[1], runner.FORMATS[1], runner.FORMATS[0]]
    assert decision.count == 21 and len(coach.calls) == 4
    assert all(p["status"] == "proposed" and len(p["development"]["pairs"]) == 4 for p in result["proposals"])
    assert all(not p["proposal"]["entered_gepa_pool"] for p in result["proposals"])
    assert result["costs"]["completed_episodes"] == 21
    assert result["costs"]["coach_started"] == 4
    assert result["costs"]["health_started"] == 1
    assert result["costs"]["decision_started"] == 21
    assert result["costs"]["gepa_evaluation_callbacks"] >= 24
    assert not result["final_allowed"] and result["selected_proposal_id"] is None
    assert not result["published"]
    # Repeated identical candidate text is independently played in each proposal.
    candidate_ids = [pair["candidate_episode_id"] for p in result["proposals"] for pair in p["development"]["pairs"]]
    assert len(set(candidate_ids)) == 16
    for result_file in path.glob("proposals/*/gepa/attempt-*/evolution_result.json"):
        assert runner.load(result_file)["gepa"]["engine"] == "gepa.optimize"
    with experiment(path, decision=decision, coach=coach) as run:
        with pytest.raises(runner.ExperimentError, match="No candidate passed"):
            run.run_final()
    assert decision.count == 21


def test_invalid_proposals_are_not_parent_candidate_successes(prepared):
    path, _ = prepared
    decision, coach = Decision(), Coach(invalid=True)
    with experiment(path, decision=decision, coach=coach) as run:
        result = run.run_dev()
    assert decision.count == 5 and len(coach.calls) == 4
    assert all(p["status"] == "rejected_schema" and p["development"] is None for p in result["proposals"])
    assert all(arm["valid_proposals"] == 0 and arm["mean_gain"] is None for arm in result["arms"].values())
    assert not result["final_allowed"]


def test_failed_candidate_resumes_saved_coach_and_successful_trials(prepared):
    path, _ = prepared
    decision, coach = Decision(fail_at=6), Coach()
    with experiment(path, decision=decision, coach=coach) as run:
        with pytest.raises(Exception):
            run.run_dev()
    assert len(coach.calls) == 1
    assert not (path / "development_result.json").exists()
    with experiment(path, decision=decision, coach=coach) as run:
        with pytest.raises(runner.ExperimentError, match="Previous attempt"):
            run.run_dev()
    assert decision.count == 6
    with experiment(path, decision=decision, coach=coach, retry_failed=True) as run:
        result = run.run_dev()
    assert len(coach.calls) == 4  # Recovery did not buy another reflection.
    assert decision.count == 22
    assert result["costs"]["decision_failed"] == 1
    assert result["costs"]["episode_attempts"] == 22
    assert result["costs"]["completed_episodes"] == 21
    failed = list(path.glob("episodes/*/attempt-*/failed.json"))
    assert len(failed) == 1 and failed[0].exists()


def test_completed_episode_crash_window_recovers_without_decision(prepared):
    path, manifest = prepared
    decision = Decision()
    with experiment(path, decision=decision, coach=Coach()) as run:
        original = run.episode("one", run.parent, 4, "test")
        receipt = next(path.glob("episodes/one/attempt-*/complete.json"))
        receipt.unlink()  # Fixture: crash after episode save, before audit write.
        recovered = run.episode("one", run.parent, 4, "test")
    assert recovered == original and decision.count == 1
    assert receipt.exists()


def test_receipt_and_episode_tampering_are_rejected(prepared):
    path, _ = prepared
    with experiment(path, decision=Decision(), coach=Coach()) as run:
        result = run.episode("audit", run.parent, 4, "test")
        bad = deepcopy(result)
        bad["trace"][0]["receipt"]["raw_response"]["answers"]["primary_action"]["choice"] = "different"
        with pytest.raises(Exception, match="hash mismatch"):
            run.verify_episode(bad, run.parent)
        bad = deepcopy(result)
        bad["trace"][0]["receipt"]["think_used"] = True
        with pytest.raises(Exception, match="telemetry"):
            run.verify_episode(bad, run.parent)
        bad = deepcopy(result)
        bad["score"] = 100
        with pytest.raises(Exception, match="score differs"):
            run.verify_episode(bad, run.parent)


def test_retry_budget_is_finite_and_attempts_never_overwritten(prepared):
    path, _ = prepared
    with experiment(path, decision=Decision(), coach=Coach(), retry_failed=True) as run:
        directory = path / "isolated"
        first = run.attempt(directory, 2)
        second = run.attempt(directory, 2)
        assert first != second and (first / "started.json").exists()
        with pytest.raises(runner.ExperimentError, match="exhausted"):
            run.attempt(directory, 2)


def test_manifest_source_or_provider_drift_stops_before_network(prepared, monkeypatch):
    path, manifest = prepared
    monkeypatch.setattr(runner, "code_hashes", lambda: {"pyproject.toml": "changed"})
    with pytest.raises(runner.ExperimentError, match="Source changed"):
        experiment(path)
    monkeypatch.setattr(runner, "code_hashes", lambda: manifest["code_hashes"])
    data = runner.load(path / "manifest.json")
    data["config"]["max_steps"] = 2
    (path / "manifest.json").write_text(json.dumps(data))
    with pytest.raises(runner.ExperimentError, match="Manifest changed"):
        experiment(path)


def test_cluster_bootstrap_keeps_repeated_seed_together_and_is_deterministic():
    pairs = [{"seed": 8, "parent_score": 0, "candidate_score": 10},
             {"seed": 8, "parent_score": 0, "candidate_score": 30},
             {"seed": 9, "parent_score": 0, "candidate_score": 40},
             {"seed": 9, "parent_score": 0, "candidate_score": 60}]
    stats = runner.paired_stats(pairs, "tetris", resamples=200)
    assert stats == runner.paired_stats(pairs, "tetris", resamples=200)
    assert stats["pair_count"] == 4 and stats["unique_seed_count"] == 2
    assert stats["bootstrap_95"] == [20, 50]
    assert stats["mean_gain"] == 35
    assert runner.passes_gate(stats, {"minimum_mean_gain": 30, "bootstrap_95_lower_greater_than": 0}, final=True)


def test_final_runs_only_frozen_selected_candidate_with_paired_order(prepared, monkeypatch):
    path, manifest = prepared
    calls = []
    with experiment(path, decision=Decision(), coach=Coach()) as run:
        winner = run.harness(NEW, "complete-1")
        runner.write_new(path / "development_result.json", {"final_allowed": True, "selected_proposal_id": "complete-1",
                         "proposals": [{"id": "complete-1", "candidate_harness": winner}]})

        def fake_episode(logical_id, harness, seed, purpose):
            calls.append((logical_id, harness["id"], seed))
            return {"score": 100 if harness["id"] == winner["id"] else 0, "episode_id": logical_id}

        monkeypatch.setattr(run, "episode", fake_episode)
        result = run.run_final()
        assert run.run_final() == result
    assert len(calls) == 24
    assert result["passed"] and result["statistics"]["pair_count"] == 12
    assert not result["published"]
    expected = [(scenario["seed"], label) for scenario in manifest["scenarios"]["final"] for label in scenario["order"]]
    assert [(seed, "candidate" if hid == winner["id"] else "parent") for _, hid, seed in calls] == expected


def test_prepare_source_snapshot_matches_every_manifest_hash(prepared):
    path, manifest = prepared
    snapshot = path / "source_snapshot"
    actual = {str(file.relative_to(snapshot)): hashlib.sha256(file.read_bytes()).hexdigest()
              for file in snapshot.rglob("*") if file.is_file()}
    assert actual == manifest["code_hashes"]
    assert not (path / "calls.jsonl").exists()


def test_snapshot_rechecks_bytes_before_writing_and_retains_original(tmp_path, monkeypatch):
    source = tmp_path / "repository"
    source.mkdir()
    (source / "one.py").write_bytes(b"original source\n")
    (source / "nested").mkdir()
    (source / "nested/two.cjs").write_bytes(b"second source\n")
    hashes = {name: hashlib.sha256((source / name).read_bytes()).hexdigest()
              for name in ("one.py", "nested/two.cjs")}
    monkeypatch.setattr(runner, "ROOT", source)
    snapshot = tmp_path / "snapshot"
    runner.snapshot_sources(snapshot, hashes)
    assert {str(file.relative_to(snapshot)): hashlib.sha256(file.read_bytes()).hexdigest()
            for file in snapshot.rglob("*") if file.is_file()} == hashes
    (source / "nested/two.cjs").write_bytes(b"changed after manifest hash\n")
    rejected = tmp_path / "rejected"
    with pytest.raises(runner.ExperimentError, match="Source changed while preparing snapshot"):
        runner.snapshot_sources(rejected, hashes)
    assert not rejected.exists()  # No partial snapshot is presented as complete.
    assert (snapshot / "nested/two.cjs").read_bytes() == b"second source\n"
    with pytest.raises(FileExistsError):
        runner.snapshot_sources(snapshot, {"one.py": hashes["one.py"]})
