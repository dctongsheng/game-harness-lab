"""Protocol-only fixtures; these tests make no model capability claims."""
from copy import deepcopy

import pytest

from game_harness.games import get_game
from game_harness.runtime import ReceiptError, _digest, run_episode
from game_harness.tetris_context import (
    CONTEXT_CONTRACT, TetrisContextError, TetrisDecisionHarness,
    audit_context_episode, goal_catalog, validate_config,
)


def config(facts=False, memory=True):
    return {"schema": "tetris-context-v1", "structured_facts": facts,
            "decision_goal_memory": memory, "replan_every_actions": 4}


class FixtureProvider:
    def __init__(self, goals=None, action="noop:100", transform=None):
        self.goals = iter(goals or ["target:2:1"] * 100)
        self.action = action
        self.transform = transform
        self.calls = []

    def decide(self, state, instructions, criteria, request_id):
        planning = request_id.endswith(":goal")
        choice = next(self.goals) if planning else self.action
        self.calls.append(deepcopy((state, instructions, criteria, request_id)))
        probabilities = {item["id"]: float(item["id"] == choice) for item in criteria}
        raw = {"model": "fixture-decision", "answers": {"primary_action": {
            "choice": choice, "confidence": 1.0, "probabilities": probabilities,
            "details": {"thought": {"used": False, "tokens": 0}},
        }}}
        receipt = {"request_id": request_id, "observation_revision": state["step"],
                   "choice_id": choice, "candidate_hash": _digest(criteria), "model": "fixture-decision",
                   "elapsed_ms": 1., "think_mode": "never", "think_used": False, "reasoning_tokens": 0,
                   "raw_response": raw, "response_hash": _digest(raw)}
        if self.transform:
            self.transform(receipt, state, planning)
        return receipt


def initial_state():
    session = get_game("tetris").create(77, {"max_steps": 80})
    try:
        before = session.snapshot()
        criteria = session.candidates()
    finally:
        session.close()
    state = deepcopy(before)
    state.update({"episode_id": "fixture", "observation_hash": _digest(before),
                  "candidates": deepcopy(criteria), "remaining_steps": 80})
    return state, criteria


def call_at(wrapper, state, criteria, step):
    state = deepcopy(state)
    state["step"] = step
    return wrapper.decide(state, "frozen strategy", criteria, f"fixture:step:{step}")


def test_configuration_is_strict_and_copied():
    supplied = config()
    frozen = validate_config(supplied)
    supplied["decision_goal_memory"] = False
    assert frozen["decision_goal_memory"] is True
    for bad in ({}, {**config(), "unlisted": True}, {**config(), "structured_facts": 1},
                {**config(), "replan_every_actions": True}, {**config(), "replan_every_actions": 2}):
        with pytest.raises(TetrisContextError):
            validate_config(bad)


def test_goal_catalog_complete_unranked_and_fresh():
    values = goal_catalog()
    assert len(values) == 42
    assert {item["id"] for item in values} == {"keep", "clear"} | {
        f"target:{c}:{r}" for c in range(10) for r in range(4)}
    values[0]["id"] = "fake"
    assert goal_catalog()[0]["id"] == "keep"


def test_all_arms_preserve_native_catalog_and_shared_instructions():
    state, criteria = initial_state()
    original = deepcopy(state)
    action_inputs = {}
    for facts, memory in ((False, False), (True, False), (False, True), (True, True)):
        provider = FixtureProvider()
        wrapper = TetrisDecisionHarness(provider, config(facts, memory))
        receipt = call_at(wrapper, state, criteria, 0)
        action_state, instructions, action_catalog, _ = provider.calls[-1]
        assert action_catalog == criteria
        assert receipt["candidate_hash"] == _digest(criteria)
        assert instructions == "frozen strategy\n" + CONTEXT_CONTRACT
        assert ("tetris_features" in action_state) is facts
        assert ("decision_goal_memory" in action_state) is memory
        assert len(provider.calls) == 1 + int(memory)
        assert receipt["tetris_context"]["action_input_state"] == action_state
        action_inputs[(facts, memory)] = action_state
    assert state == original
    assert action_inputs[(False, False)] == original


def test_goal_period_keep_clear_and_history_do_not_trigger_native_macros():
    state, criteria = initial_state()
    provider = FixtureProvider(goals=["target:2:1", "clear", "keep", "target:5:2"])
    wrapper = TetrisDecisionHarness(provider, config())
    records = [call_at(wrapper, state, criteria, step) for step in range(13)]
    goals = [call for call in provider.calls if call[3].endswith(":goal")]
    assert [call[0]["step"] for call in goals] == [0, 4, 8, 12]
    assert all(record["choice_id"] == "noop:100" for record in records)
    assert records[3]["tetris_context"]["action_input_state"]["decision_goal_memory"]["target"] == {
        "leftmost_column": 2, "rotation": 1}
    assert records[4]["tetris_context"]["action_input_state"]["decision_goal_memory"]["target"] is None
    assert records[8]["tetris_context"]["action_input_state"]["decision_goal_memory"]["target"] is None
    assert len(records[12]["tetris_context"]["action_input_state"]["decision_goal_memory"]["recent_actions"]) == 4
    assert [item["step"] for item in records[12]["tetris_context"]["action_input_state"]["decision_goal_memory"]["recent_actions"]] == [8, 9, 10, 11]


@pytest.mark.parametrize("change", ["piece", "type", "board", "episode"])
def test_goal_invalidates_on_scope_changes_including_same_type_hold(change):
    state, criteria = initial_state()
    provider = FixtureProvider(goals=["target:2:1", "keep"])
    wrapper = TetrisDecisionHarness(provider, config())
    call_at(wrapper, state, criteria, 0)
    altered = deepcopy(state)
    if change == "piece":
        altered["observation"]["pieceId"] += 1
    elif change == "type":
        altered["observation"]["active"]["type"] = "I" if state["observation"]["active"]["type"] != "I" else "O"
    elif change == "board":
        altered["observation"]["board"]["rows"][-1]["cells"] = "I........."
    else:
        altered["episode_id"] = "another-episode"
    receipt = call_at(wrapper, altered, criteria, 1 if change != "episode" else 0)
    memory = receipt["tetris_context"]["action_input_state"]["decision_goal_memory"]
    assert memory["target"] is None
    assert memory["recent_actions"] == []
    assert receipt["tetris_context"]["calls"][0]["purpose"] == "goal"


def test_motion_rotation_and_time_do_not_invalidate_goal():
    state, criteria = initial_state()
    provider = FixtureProvider()
    wrapper = TetrisDecisionHarness(provider, config())
    call_at(wrapper, state, criteria, 0)
    state["observation"]["active"].update({"x": 7, "y": 5, "rotation": 3})
    state["observation"]["horizontalHeldTicks"] = 22
    receipt = call_at(wrapper, state, criteria, 1)
    assert len(receipt["tetris_context"]["calls"]) == 1
    assert receipt["tetris_context"]["memory_after_planning"]["target"] == {"leftmost_column": 2, "rotation": 1}


def test_explicit_keep_preserves_target_and_its_age():
    state, criteria = initial_state()
    wrapper = TetrisDecisionHarness(FixtureProvider(goals=["target:2:1", "keep"]), config())
    for step in range(5):
        receipt = call_at(wrapper, state, criteria, step)
    memory = receipt["tetris_context"]["action_input_state"]["decision_goal_memory"]
    assert memory["target"] == {"leftmost_column": 2, "rotation": 1}
    assert memory["age_actions"] == 4


def test_observation_or_catalog_mutation_by_goal_provider_is_rejected():
    state, criteria = initial_state()
    def mutate(receipt, model_state, planning):
        model_state["step"] = 999
    provider = FixtureProvider(transform=mutate)
    wrapper = TetrisDecisionHarness(provider, config())
    with pytest.raises(TetrisContextError, match="mutated"):
        call_at(wrapper, state, criteria, 0)
    assert len(provider.calls) == 1


@pytest.mark.parametrize("field,value", [("choice_id", "left:100"), ("candidate_hash", "fake"),
                                         ("think_mode", "auto"), ("think_used", True),
                                         ("request_id", "wrong"), ("response_hash", "wrong")])
def test_invalid_goal_stops_before_action_and_retains_pending_evidence(field, value):
    state, criteria = initial_state()
    provider = FixtureProvider(transform=lambda r, s, p: r.update({field: value}) if p else None)
    wrapper = TetrisDecisionHarness(provider, config())
    with pytest.raises(ReceiptError):
        call_at(wrapper, state, criteria, 0)
    assert len(provider.calls) == 1
    assert wrapper.pending_context["calls"][0]["status"] == "failed"
    assert "receipt" in wrapper.pending_context["calls"][0]


def test_model_change_between_goal_and_action_is_rejected():
    state, criteria = initial_state()
    def change_model(receipt, state, planning):
        if not planning:
            receipt["model"] = receipt["raw_response"]["model"] = "other-model"
            receipt["response_hash"] = _digest(receipt["raw_response"])
    wrapper = TetrisDecisionHarness(FixtureProvider(transform=change_model), config())
    with pytest.raises(TetrisContextError, match="model changed"):
        call_at(wrapper, state, criteria, 0)
    assert [call["status"] for call in wrapper.pending_context["calls"]] == ["validated", "failed"]


def test_provider_failure_after_goal_preserves_goal_receipt():
    state, criteria = initial_state()
    class Failing(FixtureProvider):
        def decide(self, state, instructions, criteria, request_id):
            if not request_id.endswith(":goal"):
                raise ConnectionError("fixture unavailable")
            return super().decide(state, instructions, criteria, request_id)
    wrapper = TetrisDecisionHarness(Failing(), config())
    with pytest.raises(ConnectionError):
        call_at(wrapper, state, criteria, 0)
    assert wrapper.pending_context["calls"][0]["receipt"]["choice_id"] == "target:2:1"
    assert wrapper.pending_context["calls"][1]["status"] == "failed"


def real_fixture_episode(facts=True, memory=True):
    adapter = get_game("tetris")
    harness = {"id": "fixture-harness", "decision_model": "fixture-decision", "game_id": "tetris",
               "candidate": {"decision_instructions": "fixture", "strategy_notes": "fixture"},
               "tetris_context_config": config(facts, memory)}
    # The runtime integration creates the wrapper from the frozen harness field.
    episode = run_episode(FixtureProvider(), harness, episode_id="fixture-replay", seed=21,
                          adapter=adapter, config={"max_steps": 9})
    assert episode["infra_error"] is None
    return episode, harness


def test_saved_context_reconstructs_actual_inputs_goals_and_history_without_network():
    episode, harness = real_fixture_episode()
    result = audit_context_episode(episode, harness)
    assert result == {"game_actions": 9, "goal_calls": 3, "thinking_off_goal_calls": 3,
                      "actual_inputs_verified": True, "memory_replay_verified": True}


def test_real_hold_spawn_requests_new_goal_and_history_starts_empty():
    class HoldThenNoop(FixtureProvider):
        def decide(self, state, instructions, criteria, request_id):
            if not request_id.endswith(":goal"):
                self.action = "hold:100" if state["step"] == 0 else "noop:100"
            return super().decide(state, instructions, criteria, request_id)
    _, harness = real_fixture_episode()
    episode = run_episode(HoldThenNoop(goals=["target:2:1", "target:6:2"]), harness,
                          episode_id="fixture-hold", seed=21, adapter=get_game("tetris"),
                          config={"max_steps": 3})
    assert episode["infra_error"] is None
    assert episode["trace"][1]["before"]["observation"]["pieceId"] == episode["trace"][0]["before"]["observation"]["pieceId"] + 1
    next_memory = episode["trace"][1]["receipt"]["tetris_context"]["action_input_state"]["decision_goal_memory"]
    assert next_memory["recent_actions"] == []
    assert next_memory["target"] == {"leftmost_column": 6, "rotation": 2}
    assert audit_context_episode(episode, harness)["goal_calls"] == 2


@pytest.mark.parametrize("corrupt", ["memory", "feature", "instruction", "goal_receipt", "action_state", "extra_call", "config"])
def test_audit_rejects_tampered_context(corrupt):
    episode, harness = real_fixture_episode()
    ledger = episode["trace"][0]["receipt"]["tetris_context"]
    if corrupt == "memory":
        ledger["memory_after_action"]["target"]["leftmost_column"] = 9
    elif corrupt == "feature":
        ledger["calls"][0]["input_state"]["tetris_features"] = {"fake": True}
    elif corrupt == "instruction":
        ledger["calls"][0]["instructions_hash"] = "fake"
    elif corrupt == "goal_receipt":
        ledger["calls"][0]["receipt"]["choice_id"] = "target:9:3"
    elif corrupt == "action_state":
        ledger["action_input_state"]["decision_goal_memory"]["target"] = None
    elif corrupt == "extra_call":
        ledger["calls"].append(deepcopy(ledger["calls"][0]))
    else:
        episode["tetris_context_config"]["structured_facts"] = False
    with pytest.raises((TetrisContextError, ReceiptError)):
        audit_context_episode(episode, harness)
