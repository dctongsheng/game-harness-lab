"""Synthetic audit rejection tests; these do not measure model playing ability."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sqlite3

import pytest

from game_harness.providers import digest
from game_harness.runtime import run_episode


_spec = importlib.util.spec_from_file_location("audit_pokemon_run", Path(__file__).parents[1] / "scripts/audit_pokemon_run.py")
audit_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(audit_module)


class FixtureSession:
    """Alternating private views and staged actions, not a Pokemon simulator."""

    def __init__(self, seed, config):
        self.seed, self.config, self.count = seed, config, 0
        self.pending = None

    def snapshot(self):
        native = self.count >= 4
        truncated = not native and self.count >= self.config["max_steps"]
        done = native or truncated
        candidate_seat = self.seed % 2
        actor = self.count % 2
        result = self.config.get("fixture_outcome", "win") if native else "unresolved" if truncated else "ongoing"
        winner = (None if result not in {"win", "loss"} else candidate_seat if result == "win" else 1 - candidate_seat)
        public = {"players": [{"seat": 0, "team": [{"species": "VisibleA"}], "unrevealed_count": 2},
                              {"seat": 1, "team": [{"species": "VisibleB"}], "unrevealed_count": 2}],
                  "native_turn": self.count // 2 + 1, "recent_events": ["public turn " + str(self.count // 2)]}
        return {"game_id": "pokemon", "step": self.count, "max_steps": self.config["max_steps"],
                "done": done, "native_terminal": native, "truncated": truncated,
                "reason": ("native_tie" if result == "draw" else "native_win") if native else "step_limit" if truncated else None,
                "result": result, "winner": winner,
                "score": audit_module.OUTCOME_SCORES[result] if native else 50,
                "native_turn": self.count // 2 + 1,
                "actor": "p" + str(actor + 1), "turn": actor,
                "policy_role": "candidate" if actor == candidate_seat else "opponent",
                "candidate_seat": candidate_seat,
                "observation": {"private_side": actor, "own_team": ["PRIVATE_" + str(actor)],
                                "revealed_opponent": ["VisibleA" if actor else "VisibleB"]},
                "public_view": public}

    def candidates(self):
        if self.count % 2:
            return [{"id": "move:1", "label": "Opponent visible move"}, {"id": "switch:2", "label": "Opponent bench"}]
        return [{"id": "move:1", "label": "Candidate visible move"}, {"id": "move:2", "label": "Alternative move"},
                {"id": "switch:3", "label": "Candidate bench"}]

    def step(self, action):
        if self.count % 2 == 0:
            self.pending = action
        else:
            self.pending = None
        self.count += 1
        return self.snapshot()

    def close(self):
        pass


class FixtureAdapter:
    id = "pokemon"
    rules_version = "pokemon-transaction-fixture-v1"
    rules = "Fixture rules with private simultaneous actions"
    opponent_policy = {"decision_instructions": "Frozen opponent", "strategy_notes": "Opponent notes"}

    def context(self, config):
        return {"game_id": self.id, "rules_version": self.rules_version, "rules": self.rules,
                "config": deepcopy(config), "opponent_policy": deepcopy(self.opponent_policy),
                "evaluation": {"require_native_terminal": True},
                "information_policy": {"feedback_visibility": "candidate_before_only", "seed_is_private": True}}

    def create(self, seed, config):
        return FixtureSession(seed, config)


class FixtureProvider:
    def __init__(self, fail_at=None):
        self.fail_at = fail_at

    def decide(self, state, instructions, criteria, request_id):
        if self.fail_at == state["step"]:
            raise RuntimeError("fixture provider interruption")
        action = criteria[0]["id"]
        raw = {"model": "fixture-decision", "answers": {"primary_action": {
            "choice": action, "confidence": 1.0,
            "probabilities": {item["id"]: float(item["id"] == action) for item in criteria},
            "details": {"thought": {"used": False, "tokens": 0}}}}}
        return {"choice_id": action, "model": "fixture-decision", "elapsed_ms": 1.0,
                "raw_response": raw, "response_hash": digest(raw), "think_mode": "never",
                "think_used": False, "reasoning_tokens": 0,
                "candidate_hash": digest(criteria), "observation_revision": state["step"], "request_id": request_id,
                "request_evidence": {"state": deepcopy(state), "instructions": instructions,
                    "criteria": deepcopy(criteria), "request_id": request_id, "think_mode": "never", "allow_none": False},
                "request_payload": {"model": "fixture-decision", "state": json.dumps(state),
                    "questions": {"primary_action": {"type": "choice", "instructions": instructions,
                        "criteria": {item["id"]: item["label"] for item in criteria}, "allow_none": False}},
                    "think_mode": "never", "return_details": True, "allow_none": False}}


@pytest.fixture
def evidence(monkeypatch):
    adapter = FixtureAdapter()
    monkeypatch.setattr(audit_module, "get_game", lambda _: adapter)
    config = {"game_id": "pokemon", "max_steps": 8, "board_size": 8}
    harness = {"id": "h-fixture", "game_id": "pokemon", "think_mode": "never",
               "candidate": {"decision_instructions": "Candidate policy", "strategy_notes": "Candidate notes"},
               "game_context": adapter.context(config), "decision_model": "fixture-decision"}
    return adapter, config, harness


def make_episode(evidence, fail_at=None, seed=123, **options):
    adapter, config, harness = deepcopy(evidence)
    config.update(options)
    harness["game_context"] = adapter.context(config)
    episode = run_episode(FixtureProvider(fail_at), harness, episode_id="ep-fixture", seed=seed,
                          adapter=adapter, config=config, max_steps=config["max_steps"])
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


@pytest.mark.parametrize("seed", [42, 43])
@pytest.mark.parametrize("outcome,score", [("win", 100), ("loss", 0), ("draw", 50)])
def test_native_outcomes_replayed_from_both_seats(evidence, seed, outcome, score):
    episode, harness = make_episode(evidence, seed=seed, fixture_outcome=outcome)
    result = audit_module.audit_episode(episode, harness)
    assert result["valid_for_evaluation"] and result["native_terminal"]
    assert result["result"] == outcome and result["score"] == score
    assert result["roles"] == {"candidate": 2, "opponent": 2}
    assert result["candidate_seat"] == seed % 2
    assert result["decision_calls"] == result["recorded_request_count"] == 4
    assert str(seed) not in json.dumps({k: v for k, v in result.items() if k != "source_hash"})


def test_cutoff_is_replayed_but_never_enters_score_or_win_rate(evidence, tmp_path):
    episode, harness = make_episode(evidence, max_steps=2)
    save(tmp_path, episode, harness)
    result = audit_module.audit(tmp_path)
    assert result["native_completed_episodes"] == 0
    assert result["excluded_cutoff_episodes"] == 1
    assert result["episodes"][0]["score"] is None
    assert result["episodes"][0]["observed_compatibility_score"] == 50
    group = result["by_harness"][harness["id"]]
    assert group["cutoffs"] == 1 and group["draws"] == 0
    assert group["mean_score"] is None and group["native_win_rate"] is None


@pytest.mark.parametrize("fail_at", [0, 1, 2, 3])
def test_infrastructure_failures_audit_prefix_and_exclude_score(evidence, fail_at):
    episode, harness = make_episode(evidence, fail_at)
    result = audit_module.audit_episode(episode, harness)
    assert result["status"] == "excluded_incomplete"
    assert result["valid_for_evaluation"] is False and result["score"] is None
    assert result["decision_calls"] == fail_at


def test_read_only_aggregate_exposes_no_seed_or_private_request(evidence, tmp_path):
    episode, harness = make_episode(evidence)
    save(tmp_path, episode, harness)
    original = (tmp_path / "registry.sqlite").read_bytes()
    result = audit_module.audit(tmp_path)
    assert (tmp_path / "registry.sqlite").read_bytes() == original
    assert result["native_completed_episodes"] == 1
    assert result["by_harness"][harness["id"]]["wins"] == 1
    assert result["model_requests_made_by_audit"] == 0
    assert "PRIVATE_" not in json.dumps(result)
    assert "request_payload" not in json.dumps(result)


@pytest.mark.parametrize("tamper", ["before", "after", "score", "winner", "model", "catalog", "catalog_hash",
    "raw_action", "raw_hash", "request_id", "revision", "actor", "role", "policy", "opponent_policy",
    "request_state", "request_prompt", "request_criteria", "request_thinking", "opponent_prompt",
    "thinking_used", "thinking_tokens", "thinking_missing", "probabilities", "final_state", "latency",
    "incomplete_mask", "payload_state", "payload_catalog", "payload_thinking", "payload_missing"])
def test_tampered_evidence_rejected(evidence, tamper):
    episode, harness = make_episode(evidence)
    step = episode["trace"][0]
    receipt = step["receipt"]
    if tamper in {"before", "after"}:
        step[tamper]["score"] = 999
    elif tamper == "score":
        episode["score"] = 999
    elif tamper == "winner":
        episode["final_state"]["winner"] = 0
    elif tamper == "model":
        episode["model"] = "different-model"
    elif tamper == "catalog":
        step["criteria"] = step["criteria"][:1]
    elif tamper == "catalog_hash":
        receipt["candidate_hash"] = "bad"
    elif tamper == "raw_action":
        receipt["raw_response"]["answers"]["primary_action"]["choice"] = "switch:2"
        receipt["response_hash"] = digest(receipt["raw_response"])
    elif tamper == "raw_hash":
        receipt["response_hash"] = "bad"
    elif tamper == "request_id":
        receipt["request_id"] = "unbound"
    elif tamper == "revision":
        receipt["observation_revision"] = True
    elif tamper == "actor":
        step["actor"] = "p9"
    elif tamper == "role":
        step["policy_role"] = "candidate"  # first turn is opponent for seed123
    elif tamper == "policy":
        step["policy_hash"] = digest(harness["candidate"])
    elif tamper == "opponent_policy":
        episode["policy_hashes"]["opponent"] = "not-frozen"
    elif tamper == "request_state":
        receipt["request_evidence"]["state"]["opponent_private"] = "LEAK"
    elif tamper == "request_prompt":
        receipt["request_evidence"]["instructions"] = "different policy"
    elif tamper == "request_criteria":
        receipt["request_evidence"]["criteria"] = step["criteria"][:1]
    elif tamper == "request_thinking":
        receipt["request_evidence"]["think_mode"] = "auto"
    elif tamper == "opponent_prompt":
        receipt["request_evidence"]["instructions"] = episode["trace"][1]["receipt"]["request_evidence"]["instructions"]
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
        receipt["raw_response"]["answers"]["primary_action"]["probabilities"].pop("move:1")
        receipt["response_hash"] = digest(receipt["raw_response"])
    elif tamper == "final_state":
        episode["final_state"]["done"] = False
    elif tamper == "latency":
        episode["latencies_ms"] = [99] * 4
    elif tamper == "payload_state":
        receipt["request_payload"]["state"] = json.dumps({"made_up": True})
    elif tamper == "payload_catalog":
        receipt["request_payload"]["questions"]["primary_action"]["criteria"].pop("move:1")
    elif tamper == "payload_thinking":
        receipt["request_payload"]["think_mode"] = "auto"
    elif tamper == "payload_missing":
        receipt.pop("request_payload")
    else:
        episode["infra_error"] = {"type": "fixture network failure"}
        step["after"]["score"] = 999
    with pytest.raises(audit_module.AuditError):
        audit_module.audit_episode(episode, harness)


def test_missing_actual_requests_requires_explicit_weaker_mode(evidence):
    episode, harness = make_episode(evidence)
    for step in episode["trace"]:
        step["receipt"].pop("request_evidence")
    with pytest.raises(audit_module.AuditError, match="request evidence is missing"):
        audit_module.audit_episode(episode, harness)
    result = audit_module.audit_episode(episode, harness, require_requests=False)
    assert result["recorded_request_count"] == 0


def test_adapter_with_private_pending_choice_in_public_view_is_rejected(evidence, monkeypatch):
    adapter, config, harness = evidence
    class LeakySession(FixtureSession):
        def snapshot(self):
            state = super().snapshot()
            state["public_view"]["pending_action"] = self.pending
            return state
    monkeypatch.setattr(FixtureAdapter, "create", lambda self, seed, config: LeakySession(seed, config))
    episode, harness = make_episode(evidence)
    with pytest.raises(audit_module.AuditError, match="private request or pending choice"):
        audit_module.audit_episode(episode, harness)


@pytest.mark.skipif(not (Path(__file__).parents[1] / ".runtime/pokemon-showdown/dist/sim/index.js").is_file(),
                    reason="optional pinned Pokemon native engine is not installed")
def test_native_replay_of_fixture_choices_and_forced_switch_has_no_model_calls(monkeypatch):
    """Real engine mechanics with synthetic receipts, never a capability score."""
    pytest.importorskip("poke_env")
    from game_harness.games.pokemon import PokemonAdapter
    adapter = PokemonAdapter()
    monkeypatch.setattr(audit_module, "get_game", lambda _: adapter)
    config = {"game_id": "pokemon", "board_size": 8, "max_steps": 8}
    harness = {"id": "h-native-audit-fixture", "game_id": "pokemon", "think_mode": "never",
               "candidate": {"decision_instructions": "Fixture", "strategy_notes": "Fixture"},
               "game_context": adapter.context(config), "decision_model": "fixture-decision"}
    episode = run_episode(FixtureProvider(), harness, episode_id="ep-native-audit-fixture", seed=42,
                          adapter=adapter, config=config, max_steps=config["max_steps"])
    assert episode["infra_error"] is None
    result = audit_module.audit_episode(episode, harness)
    assert result["decision_calls"] == len(episode["trace"]) > 2
    assert set(result["roles"]) == {"candidate", "opponent"}
    assert any(row["action"].startswith("switch:") for row in episode["trace"])
    assert result["status"] == "excluded_cutoff" or result["native_terminal"]
