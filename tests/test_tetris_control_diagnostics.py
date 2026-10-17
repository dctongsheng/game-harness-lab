"""Control diagnostics: real rules with explicit fixture Decision receipts."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from game_harness.games import get_game
from game_harness.providers import Settings, digest
from game_harness.runtime import run_episode


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/run_tetris_control_diagnostics.py"
spec = importlib.util.spec_from_file_location("control_diagnostics", SCRIPT)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
SETTINGS = Settings("http://127.0.0.1:1/v1/systemone", "fixture-decision", 1,
                    "https://fixture.invalid/v1", "fixture-coach", "fixture-key-not-real", 1)


def harness(feedback=True):
    config = {"schema": "tetris-context-v1", "structured_facts": True,
              "decision_goal_memory": True, "replan_every_actions": 4}
    if feedback:
        config.update(schema="tetris-context-v2", control_feedback=True)
    return {"id": "diagnostic-fixture", "game_id": "tetris", "decision_model": SETTINGS.decision_model,
            "think_mode": "never", "brief": {"goal": "fixture control task"},
            "candidate": {"decision_instructions": "Fixture policy.", "strategy_notes": "No capability claim."},
            "game_context": get_game("tetris").context({"max_steps": 80}),
            "tetris_context_config": config}


class Decision:
    settings = SETTINGS

    def __init__(self, actions=None, transform=None):
        self.actions = iter(actions) if actions is not None else None
        self.transform = transform
        self.requests = []
        self.closed = False
        self.client = SimpleNamespace(close=lambda: setattr(self, "closed", True))

    def health(self):
        return {"ok": True, "model": SETTINGS.decision_model, "request_think_mode": "never"}

    def decide(self, state, instructions, criteria, request_id):
        self.requests.append(deepcopy((state, instructions, criteria, request_id)))
        if request_id.endswith(":goal"):
            target = state.get("diagnostic_task", {}).get("target", {"leftmost_column": 4, "rotation": 0})
            choice = f"target:{target['leftmost_column']}:{target['rotation']}"
        else:
            choice = next(self.actions) if self.actions is not None else "hard-drop:100"
        answer = {"choice": choice, "confidence": 1,
                  "probabilities": {c["id"]: float(c["id"] == choice) for c in criteria},
                  "details": {"thought": {"used": False, "tokens": 0}}}
        raw = {"model": SETTINGS.decision_model, "answers": {"primary_action": answer}}
        receipt = {"request_id": request_id, "observation_revision": state["step"],
                   "candidate_hash": digest(criteria), "choice_id": choice, "model": SETTINGS.decision_model,
                   "elapsed_ms": .1, "think_mode": "never", "think_used": False, "reasoning_tokens": 0,
                   "raw_response": raw, "response_hash": digest(raw)}
        if self.transform:
            self.transform(receipt)
        return receipt


def fixture(prefix=(), target=None, seed=1):
    session = get_game("tetris").create(seed, {"max_steps": 80})
    try:
        for action in prefix:
            session.step(action)
        before = session.snapshot()
    finally:
        session.close()
    return {"id": "fixture", "seed": seed, "prefix": list(prefix), "before": before,
            "target": target or {"leftmost_column": 4, "rotation": 0}}


def test_prefix_rebuild_is_exact_and_not_counted_as_new_decision_actions():
    task = fixture(["right:100", "noop:100"], {"leftmost_column": 5, "rotation": 0})
    provider = Decision(["hard-drop:100"])
    steps = []
    result = runner.play(provider, harness(), task, "diagnostic-fixture", on_step=steps.append)
    assert result["success"] is True
    assert result["actions"] == 1 and result["goal_calls"] == 1
    assert len(provider.requests) == 2
    assert result["trace"][0]["before"] == task["before"]
    assert result["trace"][0]["before"]["step"] == 2
    assert steps == result["trace"]
    action_request = provider.requests[-1]
    assert action_request[0]["diagnostic_task"]["target"] == task["target"]
    assert action_request[0]["candidates"] == action_request[2]


def test_prefix_drift_stops_before_any_provider_call():
    task = fixture(["right:100"])
    task["before"]["observation"]["horizontalHeldTicks"] += 1
    provider = Decision()
    with pytest.raises(runner.ExperimentError, match="prefix"):
        runner.play(provider, harness(), task, "diagnostic-fixture")
    assert provider.requests == []


@pytest.mark.parametrize("action", ["noop:100", "soft-drop:100"])
def test_natural_lock_at_target_is_not_success_without_explicit_decision_hard_drop(action):
    task = fixture(["soft-drop:1000"] + ["noop:100"] * 4)
    assert task["before"]["observation"]["grounded"] is True
    assert task["before"]["observation"]["lockTicks"] == 25
    result = runner.play(Decision([action]), harness(), task, "natural-lock-fixture")
    assert result["trace"][0]["after"]["observation"]["pieceId"] != task["before"]["observation"]["pieceId"]
    assert result["success"] is False
    assert result["termination_reason"] == "piece_changed_before_target_lock"


def test_hold_at_target_never_counts_as_success():
    result = runner.play(Decision(["hold:100"]), harness(), fixture(), "hold-fixture")
    assert result["success"] is False
    assert result["actions"] == 1


def test_hard_drop_away_from_target_is_not_success():
    task = fixture(target={"leftmost_column": 5, "rotation": 0})
    result = runner.play(Decision(["hard-drop:100"]), harness(), task, "wrong-column-fixture")
    assert result["success"] is False


def test_budget_finishes_without_automatic_final_action():
    provider = Decision(["noop:100"] * 12)
    result = runner.play(provider, harness(), fixture(), "budget-fixture")
    assert result["success"] is False and result["actions"] == 12
    assert result["goal_calls"] == 3
    assert len(provider.requests) == 15
    assert result["termination_reason"] == "action_budget"
    assert all(t["action"] == "noop:100" for t in result["trace"])


@pytest.mark.parametrize("change", ["thinking", "model"])
def test_invalid_decision_receipt_stops_before_native_action(change):
    def mutate(receipt):
        if change == "thinking":
            receipt["think_mode"] = "auto"
        else:
            receipt["model"] = "different-model"
    provider = Decision(transform=mutate)
    with pytest.raises(ValueError):
        runner.play(provider, harness(), fixture(), "invalid-fixture")
    assert len(provider.requests) == 1


def test_archive_and_context_replay_covers_every_live_call(tmp_path):
    task = fixture()
    provider = Decision(["right:100", "left:100", "hard-drop:100"])
    result = runner.play(runner.Recorder(provider, tmp_path), harness(), task, "archive-fixture")
    audit = runner.audit_trial(result, harness(), task, tmp_path)
    assert audit == {"native_replay": True, "actual_inputs_verified": True, "thinking_off_verified_calls": 4}
    assert len(list((tmp_path / "calls").glob("*/request.json"))) == 4
    assert len(list((tmp_path / "calls").glob("*/receipt.json"))) == 4


@pytest.mark.parametrize("corruption", ["request", "receipt", "trace", "success"])
def test_archive_audit_rejects_tampering(tmp_path, corruption):
    task = fixture()
    result = runner.play(runner.Recorder(Decision(["hard-drop:100"]), tmp_path), harness(), task, "audit-fixture")
    if corruption in ("request", "receipt"):
        path = next((tmp_path / "calls").glob(f"*/{corruption}.json"))
        value = json.loads(path.read_text())
        if corruption == "request":
            value["instructions"] += " changed"
        else:
            value["elapsed_ms"] += 1
        path.write_text(json.dumps(value))
    elif corruption == "trace":
        result["trace"][0]["after"]["score"] += 100
    else:
        result["success"] = False
    with pytest.raises(runner.ExperimentError):
        runner.audit_trial(result, harness(), task, tmp_path)


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    parent = harness(False)
    repaired = deepcopy(parent)
    repaired["id"] = "repaired-fixture"
    repaired["candidate"]["decision_instructions"] = "Repaired fixture policy."
    source = tmp_path / "source"
    episode = run_episode(Decision(), parent, episode_id="historical-fixture", seed=1,
                          adapter=get_game("tetris"), config={"max_steps": 80})
    assert episode["infra_error"] is None
    runner.write_new(source / "manifest.json", {"arms": {"both": parent}})
    runner.write_new(source / "episodes/fixture/attempt-01/episode.json", episode)
    parent_path, repaired_path = tmp_path / "parent.json", tmp_path / "repaired.json"
    runner.write_new(parent_path, parent)
    runner.write_new(repaired_path, repaired)
    monkeypatch.setattr(runner, "FIXTURES", (("one", "fixture", 0, 4, 0),))
    monkeypatch.setattr(runner, "source_hashes", lambda: {"fixture-source": "fixed"})
    monkeypatch.setattr(runner.Settings, "load", lambda: SETTINGS)
    output = tmp_path / "diagnostic"
    manifest = runner.prepare(output, parent_path, repaired_path, source)
    return output, manifest


def test_prepare_freezes_real_audited_prefix_and_three_arm_boundary(prepared):
    output, manifest = prepared
    assert manifest["max_actions"] == 12
    assert manifest["protocol"]["full_game_evidence"] is False
    assert set(manifest["arms"]) == {"baseline", "repaired_policy", "repaired_feedback"}
    assert manifest["fixtures"][0]["prefix"] == []
    assert manifest["arms"]["baseline"]["tetris_context_config"]["schema"] == "tetris-context-v1"
    assert manifest["arms"]["repaired_policy"]["tetris_context_config"]["control_feedback"] is False
    assert manifest["arms"]["repaired_feedback"]["tetris_context_config"]["control_feedback"] is True
    assert runner.load(output / "manifest.sha256.json")["sha256"] == digest(manifest)


def test_source_drift_refuses_model_before_health(prepared, monkeypatch):
    output, _ = prepared
    monkeypatch.setattr(runner, "source_hashes", lambda: {"fixture-source": "changed"})
    providers = []
    monkeypatch.setattr(runner, "DecisionProvider", lambda settings: providers.append(settings))
    with pytest.raises(runner.ExperimentError, match="code/provider changed"):
        runner.run(output)
    assert providers == []


def test_run_completes_once_and_reuses_audited_success_without_playing_again(prepared, monkeypatch):
    output, _ = prepared
    providers = []
    def construct(settings):
        provider = Decision()
        providers.append(provider)
        return provider
    monkeypatch.setattr(runner, "DecisionProvider", construct)
    first = runner.run(output)
    second = runner.run(output)
    assert first == second
    assert len(providers[0].requests) == 6
    assert providers[1].requests == []
    assert all(provider.closed for provider in providers)
    assert all(arm["tasks"] == 1 and arm["successes"] == 1 for arm in first["summary"].values())
