"""Audit rejection tests use synthetic evidence, never real model capability claims."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sqlite3

import pytest

from game_harness.providers import digest
from game_harness.runtime import run_episode


_spec = importlib.util.spec_from_file_location("audit_overcooked_run", Path(__file__).parents[1] / "scripts/audit_overcooked_run.py")
audit_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(audit_module)


class FixtureSession:
    """Small transaction fixture; not the Overcooked engine or a capability test."""

    def __init__(self, config):
        self.config = config
        self.count = self.score = self.tick = 0
        self.pending = self.last = None
        self.delivery_counts = [0, 0]
        self.transition = None

    def snapshot(self):
        done = self.count >= self.config["max_steps"]
        return {"step": self.count, "done": done, "reason": "step_limit" if done else None,
                "native_tick": self.tick, "score": self.score, "acting_player": self.count % 2,
                "actor": "chef_" + str(self.count % 2), "policy_role": "candidate",
                "last_joint_actions": deepcopy(self.last), "delivery_counts": self.delivery_counts[:],
                "deliveries": sum(self.delivery_counts), "last_transition": deepcopy(self.transition)}

    def candidates(self):
        return [{"id": action, "label": action} for action in sorted(audit_module.ACTIONS)]

    def step(self, action):
        if self.count % 2 == 0:
            self.pending = action
        else:
            self.tick += 1
            self.last = [self.pending, action]
            delivered = self.last == ["interact", "interact"]
            reward = 20 if delivered else 0
            self.score += reward
            self.delivery_counts[0] += int(delivered)
            self.transition = {"native_tick": self.tick, "sparse_reward": reward,
                               "sparse_reward_by_player": [reward, 0],
                               "delivery_by_player": [delivered, False]}
            self.pending = None
        self.count += 1
        return self.snapshot()

    def close(self):
        pass


class FixtureAdapter:
    id = "overcooked"
    rules_version = "transaction-fixture-v1"
    rules = "fixture instructions"
    opponent_policy = None

    def context(self, config):
        return {"game_id": self.id, "rules_version": self.rules_version,
                "rules": self.rules, "config": deepcopy(config)}

    def create(self, seed, config):
        return FixtureSession(config)


class FixtureProvider:
    def __init__(self, fail_at=None):
        self.fail_at = fail_at

    def decide(self, state, instructions, criteria, request_id):
        if self.fail_at == state["step"]:
            raise RuntimeError("fixture provider interruption")
        action = "interact"
        raw = {"model": "fixture-decision", "answers": {"primary_action": {
            "choice": action, "confidence": 1.0,
            "probabilities": {item["id"]: float(item["id"] == action) for item in criteria},
            "details": {"thought": {"used": False, "tokens": 0}}}}}
        return {"choice_id": action, "model": "fixture-decision", "elapsed_ms": 1.0,
                "raw_response": raw, "response_hash": digest(raw), "think_mode": "never",
                "think_used": False, "reasoning_tokens": 0,
                "candidate_hash": digest(criteria), "observation_revision": state["step"],
                "request_id": request_id, "request_evidence": {
                    "state": deepcopy(state), "instructions": instructions, "criteria": deepcopy(criteria),
                    "request_id": request_id, "think_mode": "never", "allow_none": False},
                "request_payload": {"model": "fixture-decision", "state": json.dumps(state),
                    "questions": {"primary_action": {"type": "choice", "instructions": instructions,
                        "criteria": {item["id"]: item["label"] for item in criteria}, "allow_none": False}},
                    "think_mode": "never", "return_details": True, "allow_none": False}}


@pytest.fixture
def evidence(monkeypatch):
    adapter = FixtureAdapter()
    monkeypatch.setattr(audit_module, "get_game", lambda _: adapter)
    config = {"game_id": "overcooked", "max_steps": 4, "board_size": 8, "overcooked_layout": "fixture"}
    harness = {"id": "h-fixture", "game_id": "overcooked", "think_mode": "never",
               "candidate": {"decision_instructions": "fixture", "strategy_notes": "fixture"},
               "game_context": adapter.context(config), "decision_model": "fixture-decision"}
    return adapter, config, harness


def make_episode(evidence, fail_at=None):
    adapter, config, harness = evidence
    episode = run_episode(FixtureProvider(fail_at), harness, episode_id="ep-fixture", seed=123,
                          adapter=adapter, config=config)
    episode["purpose"] = "search_train"
    return episode, harness


def save(root, episode, harness):
    (root / "episodes").mkdir()
    db = sqlite3.connect(root / "registry.sqlite")
    db.execute("CREATE TABLE records(kind TEXT,id TEXT,body TEXT)")
    db.execute("INSERT INTO records VALUES (?,?,?)", ("version", harness["id"], json.dumps(harness)))
    db.commit()
    db.close()
    (root / "episodes" / f"{episode['episode_id']}.json").write_text(json.dumps(episode))


def test_two_decision_calls_commit_one_tick_and_record_requests(evidence, tmp_path):
    episode, harness = make_episode(evidence)
    save(tmp_path, episode, harness)
    result = audit_module.audit(tmp_path)
    assert result["completed_episodes"] == 1
    assert result["audited_decision_calls"] == result["recorded_request_count"] == 4
    assert result["native_joint_ticks"] == 2
    assert result["by_harness"][harness["id"]]["scores"] == [40]
    assert result["episodes"][0]["actors"] == {"chef_0": 2, "chef_1": 2}
    assert result["episodes"][0]["native_sparse_reward_deltas"] == [20, 20]
    assert result["episodes"][0]["scoring_deliveries"] == 2
    assert result["model_requests_made_by_audit"] == 0


@pytest.mark.parametrize("fail_at", [0, 1, 2, 3])
def test_provider_failures_audit_prefix_but_never_become_zero_score(evidence, tmp_path, fail_at):
    episode, harness = make_episode(evidence, fail_at)
    save(tmp_path, episode, harness)
    result = audit_module.audit(tmp_path)
    assert result["completed_episodes"] == 0
    assert result["excluded_incomplete_episodes"] == 1
    assert result["by_harness"][harness["id"]]["mean_score"] is None
    assert result["episodes"][0]["score"] is None
    assert result["audited_decision_calls"] == fail_at
    assert result["native_joint_ticks"] == fail_at // 2


@pytest.mark.parametrize("tamper", ["before", "after", "score", "model", "catalog", "catalog_hash",
                                    "raw_action", "raw_hash", "request_id", "revision", "actor", "policy",
                                    "request_state", "request_prompt", "request_criteria", "request_thinking",
                                    "thinking_used", "thinking_tokens", "thinking_missing", "probabilities",
                                    "native_tick", "final_state", "joint_action", "latency", "incomplete_mask",
                                    "payload_state", "payload_catalog", "payload_thinking", "payload_missing"])
def test_tampered_evidence_rejected(evidence, tamper):
    episode, harness = make_episode(evidence)
    step = episode["trace"][0]
    receipt = step["receipt"]
    if tamper in {"before", "after"}:
        step[tamper]["score"] = 999
    elif tamper == "score":
        episode["score"] = 999
    elif tamper == "model":
        episode["model"] = "another-model"
    elif tamper == "catalog":
        step["criteria"] = step["criteria"][:1]
    elif tamper == "catalog_hash":
        receipt["candidate_hash"] = "bad"
    elif tamper == "raw_action":
        receipt["raw_response"]["answers"]["primary_action"]["choice"] = "west"
        receipt["response_hash"] = digest(receipt["raw_response"])
    elif tamper == "raw_hash":
        receipt["response_hash"] = "bad"
    elif tamper == "request_id":
        receipt["request_id"] = "unbound"
    elif tamper == "revision":
        receipt["observation_revision"] = True
    elif tamper == "actor":
        step["actor"] = "chef_1"
    elif tamper == "policy":
        step["policy_hash"] = "bad"
    elif tamper == "request_state":
        episode["trace"][1]["receipt"]["request_evidence"]["state"]["pending_action"] = "interact"
    elif tamper == "request_prompt":
        receipt["request_evidence"]["instructions"] = "different prompt"
    elif tamper == "request_criteria":
        receipt["request_evidence"]["criteria"] = step["criteria"][:1]
    elif tamper == "request_thinking":
        receipt["request_evidence"]["think_mode"] = "auto"
    elif tamper.startswith("thinking_"):
        thought = receipt["raw_response"]["answers"]["primary_action"]["details"]["thought"]
        if tamper == "thinking_used":
            thought["used"] = True
        elif tamper == "thinking_tokens":
            thought["tokens"] = 1
        else:
            thought.clear()
        receipt["response_hash"] = digest(receipt["raw_response"])
    elif tamper == "probabilities":
        receipt["raw_response"]["answers"]["primary_action"]["probabilities"].pop("north")
        receipt["response_hash"] = digest(receipt["raw_response"])
    elif tamper == "native_tick":
        step["after"]["native_tick"] = 1
    elif tamper == "final_state":
        episode["final_state"]["done"] = False
    elif tamper == "joint_action":
        episode["trace"][1]["after"]["last_joint_actions"] = ["stay", "stay"]
    elif tamper == "latency":
        episode["latencies_ms"] = [99, 99, 99, 99]
    elif tamper == "payload_state":
        receipt["request_payload"]["state"] = json.dumps({"made_up": True})
    elif tamper == "payload_catalog":
        receipt["request_payload"]["questions"]["primary_action"]["criteria"].pop("north")
    elif tamper == "payload_thinking":
        receipt["request_payload"]["think_mode"] = "auto"
    elif tamper == "payload_missing":
        receipt.pop("request_payload")
    else:
        # Infrastructure labels do not exempt an artifact's existing trace from audit.
        episode["infra_error"] = {"type": "network"}
        step["after"]["score"] = 999
    with pytest.raises(audit_module.AuditError):
        audit_module.audit_episode(episode, harness)


def test_unrecorded_requests_require_explicit_weaker_audit_mode(evidence):
    episode, harness = make_episode(evidence)
    for step in episode["trace"]:
        step["receipt"].pop("request_evidence")
    with pytest.raises(audit_module.AuditError, match="request evidence is missing"):
        audit_module.audit_episode(episode, harness)
    result = audit_module.audit_episode(episode, harness, require_requests=False)
    assert result["recorded_request_count"] == 0
    assert result["decision_calls"] == 4


def test_audit_does_not_open_database_for_writes(evidence, tmp_path):
    episode, harness = make_episode(evidence)
    save(tmp_path, episode, harness)
    before = (tmp_path / "registry.sqlite").read_bytes()
    audit_module.audit(tmp_path)
    assert (tmp_path / "registry.sqlite").read_bytes() == before


def test_first_chef_pending_action_cannot_be_public_even_if_replay_agrees(evidence, monkeypatch):
    adapter, config, harness = evidence

    class LeakySession(FixtureSession):
        def snapshot(self):
            return {**super().snapshot(), "pending_action": self.pending}

    monkeypatch.setattr(adapter, "create", lambda seed, config: LeakySession(config))
    episode, harness = make_episode(evidence)
    with pytest.raises(audit_module.AuditError, match="leaked pending action"):
        audit_module.audit_episode(episode, harness)


@pytest.mark.skipif(not (Path(__file__).parents[1] / ".venv-overcooked/bin/python").is_file(),
                    reason="optional pinned native Overcooked environment is not installed")
def test_saved_native_game_replays_both_chefs_without_model_calls(monkeypatch):
    from game_harness.games.overcooked import OvercookedAdapter
    adapter = OvercookedAdapter()
    monkeypatch.setattr(audit_module, "get_game", lambda _: adapter)
    config = {"max_steps": 4, "board_size": 8, "overcooked_layout": "cramped_room"}
    harness = {"id": "h-native-fixture", "game_id": "overcooked", "think_mode": "never",
               "candidate": {"decision_instructions": "fixture", "strategy_notes": "fixture"},
               "game_context": adapter.context(config), "decision_model": "fixture-decision"}
    episode = run_episode(FixtureProvider(), harness, episode_id="native-fixture", seed=123,
                          adapter=adapter, config=config)
    assert episode["infra_error"] is None
    result = audit_module.audit_episode(episode, harness)
    assert result["status"] == "audited_completed"
    assert result["actors"] == {"chef_0": 2, "chef_1": 2}
    assert result["decision_calls"] == result["recorded_request_count"] == 4
    assert result["native_ticks"] == 2
    assert result["deliveries"] == 0
