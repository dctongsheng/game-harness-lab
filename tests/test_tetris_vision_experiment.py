"""Image-only ablation boundary tests: native rules plus synthetic HTTP transport."""
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
import run_tetris_vision_experiment as runner
import report_tetris_vision_experiment as reporter
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
        return VisionDecisionProvider(SETTINGS, enabled=arm == "vision", archive_dir=archive_dir,
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
    manifest = runner.prepare(identity="vision-test", parent_path=source, settings=SETTINGS,
                              root=data / "experiments", data_root=data)
    return data / "experiments/vision-test", manifest, source


def experiment(path, **kwargs):
    return runner.TetrisVisionExperiment(path, settings=SETTINGS, decision=Health(), **kwargs)


def test_schedule_fixed_fresh_balanced_and_identical_model_visible_metadata():
    first = runner.scenario_plan(7)
    excluded = {first["calibration"][0]["seed"], first["evaluation"][0]["seed"]}
    plan = runner.scenario_plan(7, excluded)
    assert plan == runner.scenario_plan(7, excluded)
    assert [len(plan[p]) for p in ("calibration", "evaluation")] == [2, 24]
    seeds = [row["seed"] for phase in plan.values() for row in phase]
    assert len(set(seeds)) == 26 and not set(seeds) & excluded
    for phase, n in (("calibration", 1), ("evaluation", 12)):
        for position in range(2):
            assert Counter(row["order"][position] for row in plan[phase]) == dict.fromkeys(runner.ARMS, n)
    assert runner.shared_episode_id("study", "calibration-00", "attempt-01") == runner.shared_episode_id("study", "calibration-00", "attempt-02")


def test_manifest_is_single_policy_fixed_budget_dependency_and_no_secrets(prepared):
    path, manifest, _ = prepared
    assert manifest["arms"]["text"] == manifest["arms"]["vision"] == manifest["parent"]
    assert manifest["budget"]["completed_episodes"] == 52
    assert manifest["budget"]["decision_request_attempts_max"] == 16640
    assert manifest["runtime_dependencies"]["Pillow"] == "12.0.0"
    assert manifest["statistics"]["bootstrap_resamples"] == 10000
    assert manifest["statistics"]["primary_comparisons"] == ["vision-text"]
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
        runner.prepare(identity="bad", parent_path=source, settings=SETTINGS, root=path.parent, data_root=path.parent.parent)
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


def test_all_52_trials_mandatory_after_negative_calibration_and_cached(prepared, monkeypatch):
    path, m, _ = prepared
    seen, cache = [], {}
    def fake_episode(scenario_id, arm, seed, purpose):
        key = scenario_id, arm
        if key not in cache:
            seen.append((purpose, seed, arm))
            points = 100 if arm == "text" else 0
            cache[key] = {"episode_id": scenario_id, "score": points, "steps": 0, "trace": [],
                "final_state": {"metrics": {"lines": points // 100}}, "termination_reason": "step_limit"}
        return cache[key]
    with experiment(path) as run:
        monkeypatch.setattr(run, "episode", fake_episode)
        with pytest.raises(runner.ExperimentError, match="run-calibration"):
            run.run_evaluation()
        calibration = run.run_calibration()
        assert len(seen) == 4 and calibration["evaluation_allowed"]
        assert not calibration["comparisons"]["vision-text"]["screening_gate_pass"]
        result = run.run_evaluation()
        assert len(seen) == 52 and result["comparisons"]["vision-text"]["mean_gain"] == -100
        assert run.run_evaluation() == result and len(seen) == 52
    assert seen == [(phase, scenario["seed"], arm) for phase in ("calibration", "evaluation")
                   for scenario in m["scenarios"][phase] for arm in scenario["order"]]


@pytest.mark.skipif(not shutil.which("node"), reason="Native Tetris needs Node")
def test_native_pair_is_image_only_every_goal_action_archived_replayed_cached(prepared):
    path, m, _ = prepared
    scenario, providers, episodes = m["scenarios"]["calibration"][0], Providers(), {}
    with experiment(path, provider_factory=providers) as run:
        for arm in runner.ARMS:
            episodes[arm] = run.episode(scenario["id"], arm, scenario["seed"], "calibration")
            before = len(providers.requests)
            assert run.episode(scenario["id"], arm, scenario["seed"], "calibration") == episodes[arm]
            assert len(providers.requests) == before
    assert episodes["text"]["episode_id"] == episodes["vision"]["episode_id"]
    assert episodes["text"]["harness_id"] == episodes["vision"]["harness_id"]
    text = [payload for arm, payload in providers.requests if arm == "text"]
    visual = [payload for arm, payload in providers.requests if arm == "vision"]
    assert len(text) == len(visual) > 1
    assert all({key: value for key, value in b.items() if key != "images"} == a for a, b in zip(text, visual))
    assert all("images" not in a and len(b["images"]) == 1 for a, b in zip(text, visual))
    report = reporter.report(path)
    assert report["status"] == "incomplete"
    assert report["audit"]["completed_episodes_replayed"] == 2
    assert report["audit"]["image_requests_verified"] == len(visual)
    assert report["audit"]["ledger_calls_matched_to_archived_requests_and_receipts"] == len(providers.requests)
    assert report["call_archive"]["calls_not_in_completed_episodes"] == 0
    assert len(list((path / "images").glob("*.png"))) < len(visual)  # goal/action share pixels
    request_file = next((path / "calls" / "vision").glob("*/*/*/request.json"))
    request, receipt = runner.load(request_file), runner.load(request_file.parent / "receipt.json")
    contradictory = deepcopy(receipt)
    contradictory["raw_response"]["usage"]["output_tokens"] = 1
    with pytest.raises(reporter.ReportError, match="thinking telemetry"):
        reporter.verify_transport_archive(path, request_file.parent, request, contradictory, "vision")
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
            run.episode(s["id"], "vision", s["seed"], "calibration")
    with experiment(path, provider_factory=Providers()) as run:
        with pytest.raises(runner.ExperimentError, match="Previous attempt"):
            run.episode(s["id"], "vision", s["seed"], "calibration")
    providers = Providers()
    with experiment(path, provider_factory=providers, retry_failed=True) as run:
        episode = run.episode(s["id"], "vision", s["seed"], "calibration")
        complete = next(path.glob("episodes/vision/*/attempt-*/complete.json")); complete.unlink()
        count = len(providers.requests)
        assert run.episode(s["id"], "vision", s["seed"], "calibration") == episode
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
    rows = [{"seed": n, "arms": {arm: {"points": 100 if n == 0 and arm == "vision" else 0,
             "lines": int(n == 0 and arm == "vision"), "steps": 80, "goal_calls": 10,
             "termination_reason": "STEP_LIMIT"} for arm in runner.ARMS}} for n in range(24)]
    result = runner.summarize(rows, {"bootstrap_seed": 74107, "bootstrap_resamples": 10000})
    stats = result["comparisons"]["vision-text"]
    assert stats["mean_gain"] > 0 and stats["bootstrap_95"][0] == 0 and not stats["screening_gate_pass"]
