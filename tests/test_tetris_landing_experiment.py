"""Planning-package boundaries: frozen policy, native replay and synthetic HTTP."""
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import sys

import httpx
import pytest

pytest.importorskip('PIL', reason='Install .[vision] to run visual tests')

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import run_tetris_landing_experiment as runner
import report_tetris_landing_experiment as reporter
from game_harness.games import get_game
from game_harness.providers import Settings
from game_harness.vision_provider import VisionDecisionProvider

SETTINGS = Settings("http://127.0.0.1:1/v1/systemone", "fixture-decision", 1,
                    "https://fixture.invalid/v1", "fixture-coach", "never-publish-this-test-key", 1)
CANDIDATE = {"decision_instructions": "Frozen V6 fixture policy", "strategy_notes": "No strength claims"}


class Health:
    def health(self):
        return {"ok": True, "model": SETTINGS.decision_model, "request_think_mode": "never"}


class Providers:
    def __init__(self, fail_at=None):
        self.requests, self.fail_at = [], fail_at
    def __call__(self, arm, archive_dir, image_dir):
        def response(request):
            payload = json.loads(request.content)
            self.requests.append((arm, deepcopy(payload)))
            if len(self.requests) == self.fail_at:
                raise httpx.ConnectError("fixture offline")
            criteria = payload["questions"]["primary_action"]["criteria"]
            choice = "target:0:0" if "target:0:0" in criteria else "hard-drop:100"
            answer = {"choice": choice, "confidence": 1,
                "probabilities": {key: float(key == choice) for key in criteria},
                "details": {"thought": {"used": False, "tokens": 0}}}
            return httpx.Response(200, json={"model": SETTINGS.decision_model,
                "answers": {"primary_action": answer}, "usage": {"total_tokens": 10}})
        return VisionDecisionProvider(SETTINGS, enabled=True, archive_dir=archive_dir,
            image_store=image_dir, client=httpx.Client(transport=httpx.MockTransport(response)))


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    hashes = {"pyproject.toml": hashlib.sha256((ROOT / "pyproject.toml").read_bytes()).hexdigest()}
    monkeypatch.setattr(runner, "code_hashes", lambda: hashes)
    parent = {"game_id": "tetris", "brief": {"goal": "native points"}, "candidate": CANDIDATE,
              "game_context": get_game("tetris").context({"max_steps": 80}),
              "tetris_context_config": runner.context_config()}
    source = tmp_path / "v6.json"; source.write_text(json.dumps(parent))
    data = tmp_path / "data"
    planner = tmp_path / "planner.json"
    planner.write_text(json.dumps({"schema": "tetris-landing-planner/v1", "goal_instructions": "Fixture independent goal prompt", "provenance": {"fixture": True}}))
    manifest = runner.prepare(identity="landing-test", parent_path=source, planner_path=planner, settings=SETTINGS,
                              root=data / "experiments", data_root=data)
    return data / "experiments/landing-test", manifest, source


def experiment(path, **kwargs):
    return runner.TetrisLandingExperiment(path, settings=SETTINGS, decision=Health(), **kwargs)


def test_schedule_fixed_fresh_balanced_and_identical_model_visible_metadata():
    first = runner.scenario_plan(7)
    excluded = {first["calibration"][0]["seed"], first["evaluation"][0]["seed"]}
    plan = runner.scenario_plan(7, excluded)
    assert plan == runner.scenario_plan(7, excluded)
    assert [len(plan[p]) for p in ("calibration", "evaluation")] == [2, 12]
    seeds = [row["seed"] for phase in plan.values() for row in phase]
    assert len(set(seeds)) == 14 and not set(seeds) & excluded
    for phase, n in (("calibration", 1), ("evaluation", 6)):
        for position in range(2):
            assert Counter(row["order"][position] for row in plan[phase]) == dict.fromkeys(runner.ARMS, n)
    assert runner.shared_episode_id("study", "calibration-00", "attempt-01") == runner.shared_episode_id("study", "calibration-00", "attempt-02")


def test_manifest_is_single_policy_fixed_budget_dependency_and_no_secrets(prepared):
    path, manifest, _ = prepared
    assert manifest["arms"]["baseline"] == manifest["parent"]
    assert manifest["arms"]["baseline"]["candidate"] == manifest["arms"]["planning"]["candidate"]
    assert manifest["arms"]["planning"]["tetris_context_config"]["goal_instructions"] == manifest["planner"]["goal_instructions"]
    assert manifest["arms"]["planning"]["tetris_context_config"]["schema"] == "tetris-context-v3"
    assert runner.validate_manifest_arms(manifest, path) == manifest["planner"]
    assert manifest["budget"]["completed_episodes"] == 28
    assert manifest["budget"]["decision_request_attempts_max"] == 8960
    assert manifest["runtime_dependencies"]["Pillow"] == "12.0.0"
    assert manifest["statistics"]["bootstrap_resamples"] == 10000
    assert manifest["statistics"]["primary_comparisons"] == ["planning-baseline"]
    assert manifest["constraints"]["registry_writes"] is False
    assert "never-publish-this-test-key" not in (path / "manifest.json").read_text()
    assert not list(path.rglob("*.sqlite"))


@pytest.mark.parametrize("change,match", [
    (lambda p: p.update(tetris_context_config={}), "V6"),
    (lambda p: p.update(think_mode="auto"), "thinking"),
    (lambda p: p.update(brief={}), "brief"),
    (lambda p: p.update(game_context=get_game("tetris").context({"max_steps": 1})), "80-action"),
])
def test_parent_contract_rejected_before_network(prepared, change, match):
    path, _, source = prepared
    value = runner.load(source); change(value); source.write_text(json.dumps(value))
    with pytest.raises(runner.ExperimentError, match=match):
        runner.prepare(identity="bad", parent_path=source, planner_path=path / "planner-source.json", settings=SETTINGS, root=path.parent, data_root=path.parent.parent)
    assert not (path.parent / "bad").exists()


def test_manifest_source_and_runtime_drift_rejected(prepared, monkeypatch):
    path, m, _ = prepared
    monkeypatch.setattr(runner, "runtime_dependencies", lambda: {"Pillow": "other"})
    with pytest.raises(runner.ExperimentError, match="dependencies"):
        experiment(path)
    monkeypatch.setattr(runner, "runtime_dependencies", lambda: m["runtime_dependencies"])
    monkeypatch.setattr(runner, "code_hashes", lambda: {})
    with pytest.raises(runner.ExperimentError, match="Source changed"):
        experiment(path)


def test_all_28_trials_mandatory_after_negative_calibration_and_cached(prepared, monkeypatch):
    path, m, _ = prepared
    seen, cache = [], {}
    def fake_episode(scenario_id, arm, seed, purpose):
        key = scenario_id, arm
        if key not in cache:
            seen.append((purpose, seed, arm))
            points = 100 if arm == "baseline" else 0
            cache[key] = {"episode_id": scenario_id, "score": points, "steps": 0, "trace": [],
                "final_state": {"metrics": {"lines": points // 100}}, "termination_reason": "step_limit"}
        return cache[key]
    with experiment(path) as run:
        monkeypatch.setattr(run, "episode", fake_episode)
        with pytest.raises(runner.ExperimentError, match="run-calibration"):
            run.run_evaluation()
        calibration = run.run_calibration()
        assert len(seen) == 4 and calibration["evaluation_allowed"]
        assert not calibration["comparisons"]["planning-baseline"]["screening_gate_pass"]
        result = run.run_evaluation()
        assert len(seen) == 28 and result["comparisons"]["planning-baseline"]["mean_gain"] == -100
        assert run.run_evaluation() == result and len(seen) == 28
    assert seen == [(phase, scenario["seed"], arm) for phase in ("calibration", "evaluation")
                   for scenario in m["scenarios"][phase] for arm in scenario["order"]]


@pytest.mark.skipif(not shutil.which("node"), reason="Native Tetris needs Node")
def test_native_pair_retains_actions_and_images_but_changes_only_declared_planning_package(prepared):
    path, m, _ = prepared
    scenario, providers, episodes = m["scenarios"]["calibration"][0], Providers(), {}
    with experiment(path, provider_factory=providers) as run:
        for arm in runner.ARMS:
            episodes[arm] = run.episode(scenario["id"], arm, scenario["seed"], "calibration")
            before = len(providers.requests)
            assert run.episode(scenario["id"], arm, scenario["seed"], "calibration") == episodes[arm]
            assert len(providers.requests) == before
    assert episodes["baseline"]["episode_id"] == episodes["planning"]["episode_id"]
    assert episodes["baseline"]["harness_id"] != episodes["planning"]["harness_id"]
    baseline = [payload for arm, payload in providers.requests if arm == "baseline"]
    planning = [payload for arm, payload in providers.requests if arm == "planning"]
    assert len(baseline) == len(planning) > 1
    for old, new in zip(baseline, planning):
        assert old["images"] == new["images"]  # same mocked choices mean identical native states
        old_state, new_state = json.loads(old["state"]), json.loads(new["state"])
        assert "tetris_landing_projections" not in old_state and "tetris_landing_projections" in new_state
        new_state.pop("tetris_landing_projections")
        old_criteria = old["questions"]["primary_action"]["criteria"]
        new_criteria = new["questions"]["primary_action"]["criteria"]
        assert list(old_criteria) == list(new_criteria)
        if old_state.get("selection_purpose") == "update_goal_memory_only":
            assert old_criteria != new_criteria
            assert old_criteria["keep"] == new_criteria["keep"] and old_criteria["clear"] == new_criteria["clear"]
            assert [c["id"] for c in old_state["candidates"]] == [c["id"] for c in new_state["candidates"]]
            old_state.pop("candidates"); new_state.pop("candidates")
        else:
            assert old_criteria == new_criteria
        assert old_state == new_state
        assert old["think_mode"] == new["think_mode"] == "never"
        assert old["questions"]["primary_action"]["instructions"] != new["questions"]["primary_action"]["instructions"]
        if old_state.get("selection_purpose") == "update_goal_memory_only":
            assert CANDIDATE["decision_instructions"] not in new["questions"]["primary_action"]["instructions"]
        else:
            assert CANDIDATE["decision_instructions"] in new["questions"]["primary_action"]["instructions"]
    report = reporter.report(path)
    assert report["status"] == "incomplete"
    assert report["audit"]["completed_episodes_replayed"] == 2
    assert report["audit"]["image_requests_verified"] == len(providers.requests)
    assert report["audit"]["ledger_calls_matched_to_archived_requests_and_receipts"] == len(providers.requests)
    assert report["call_archive"]["calls_not_in_completed_episodes"] == 0
    assert len(list((path / "images").glob("*.png"))) < len(providers.requests)  # goal/action share pixels
    request_file = next((path / "calls" / "planning").glob("*/*/*/request.json"))
    request, receipt = runner.load(request_file), runner.load(request_file.parent / "receipt.json")
    contradictory = deepcopy(receipt)
    contradictory["raw_response"]["usage"]["output_tokens"] = 1
    with pytest.raises(reporter.ReportError, match="thinking telemetry"):
        reporter.verify_transport_archive(path, request_file.parent, request, contradictory, "planning")
    image = next((path / "images").glob("*.png"))
    image.write_bytes(image.read_bytes() + b"tampered")
    with pytest.raises(reporter.ReportError, match="hash mismatch"):
        reporter.report(path)


@pytest.mark.skipif(not shutil.which("node"), reason="Native Tetris needs Node")
def test_failed_attempt_explicit_retry_no_zero_score_and_recovery(prepared):
    path, m, _ = prepared
    s = m["scenarios"]["calibration"][0]
    with experiment(path, provider_factory=Providers(fail_at=2)) as run:
        with pytest.raises(runner.ExperimentError, match="Incomplete"):
            run.episode(s["id"], "planning", s["seed"], "calibration")
    with experiment(path, provider_factory=Providers()) as run:
        with pytest.raises(runner.ExperimentError, match="Previous attempt"):
            run.episode(s["id"], "planning", s["seed"], "calibration")
    providers = Providers()
    with experiment(path, provider_factory=providers, retry_failed=True) as run:
        episode = run.episode(s["id"], "planning", s["seed"], "calibration")
        complete = next(path.glob("episodes/planning/*/attempt-*/complete.json")); complete.unlink()
        count = len(providers.requests)
        assert run.episode(s["id"], "planning", s["seed"], "calibration") == episode
        assert len(providers.requests) == count
    report = reporter.report(path)
    assert len(report["incomplete_attempts"]) == 1
    assert report["audit"]["completed_episodes_replayed"] == 1
    assert report["call_archive"]["status_counts"]["failed"] == 1


def test_report_lock_no_mutating_while_active(prepared):
    path, _, _ = prepared
    with pytest.raises(reporter.ReportError, match="missing"):
        reporter.report(path)
    with experiment(path):
        with pytest.raises(reporter.ReportError, match="running"):
            reporter.report(path)


def test_zero_including_interval_cannot_be_called_clear_improvement():
    rows = [{"seed": n, "arms": {arm: {"points": 100 if n == 0 and arm == "planning" else 0,
             "lines": int(n == 0 and arm == "planning"), "steps": 80, "goal_calls": 10,
             "termination_reason": "STEP_LIMIT"} for arm in runner.ARMS}} for n in range(12)]
    result = runner.summarize(rows, {"bootstrap_seed": 74107, "bootstrap_resamples": 10000})
    stats = result["comparisons"]["planning-baseline"]
    assert stats["mean_gain"] > 0 and stats["bootstrap_95"][0] == 0 and not stats["screening_gate_pass"]


@pytest.mark.parametrize("change", [
    lambda p: p.update(goal_instructions=""),
    lambda p: p.update(provenance={}),
    lambda p: p.update(schema="anything"),
    lambda p: p.update(unknown=True),
])
def test_planner_contract_rejected_before_experiment_directory(prepared, change):
    path, manifest, source = prepared
    planner = deepcopy(manifest["planner"]); change(planner)
    invalid = path.parent / "invalid-planner.json"; invalid.write_text(json.dumps(planner))
    with pytest.raises(runner.ExperimentError, match="Planner"):
        runner.prepare(identity="bad-planner", parent_path=source, planner_path=invalid,
                       settings=SETTINGS, root=path.parent, data_root=path.parent.parent)
    assert not (path.parent / "bad-planner").exists()


def test_frozen_planner_bytes_and_action_policy_cannot_drift(prepared):
    path, manifest, _ = prepared
    altered = deepcopy(manifest)
    altered["arms"]["planning"]["candidate"]["strategy_notes"] = "changed execution policy"
    with pytest.raises(runner.ExperimentError, match="beyond"):
        runner.validate_manifest_arms(altered, path)
    source = path / "planner-source.json"
    source.write_text(source.read_text() + "\n")
    with pytest.raises(runner.ExperimentError, match="source/hash"):
        experiment(path)


@pytest.mark.skipif(not shutil.which("node"), reason="Native Tetris needs Node")
def test_report_recomputes_results_from_native_episodes_even_if_result_checksum_rewritten(prepared, monkeypatch):
    path, manifest, _ = prepared
    # Reduce only this mocked fixture's schedule; real prepare always fixes 2 + 12.
    for phase in manifest["scenarios"]:
        manifest["scenarios"][phase] = manifest["scenarios"][phase][:1]
    (path / "manifest.json").write_text(json.dumps(manifest))
    (path / "manifest.sha256.json").write_text(json.dumps({"sha256": runner.digest(manifest)}))
    with experiment(path, provider_factory=Providers()) as run:
        run.run_calibration(); run.run_evaluation()
    result = reporter.report(path)
    assert result["status"] == "completed" and result["audit"]["completed_episodes_replayed"] == 4
    saved = path / "evaluation_result.json"
    altered = runner.load(saved)
    altered["arms"]["planning"]["mean_points"] += 100
    saved.write_text(json.dumps(altered))
    saved.with_suffix(".sha256.json").write_text(json.dumps({"sha256": runner.digest(altered)}))
    with pytest.raises(reporter.ReportError, match="Registered statistics"):
        reporter.report(path)
