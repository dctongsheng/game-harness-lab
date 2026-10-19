"""Behavioral fixtures for bounded evolvable components, not capability claims."""
from copy import deepcopy
import json

import pytest

from game_harness.components import (
    CONTEXT_SECTIONS, TEXT_COMPONENTS, TETRIS_COMPONENTS, ComponentValidationError,
    component_names, context_config, resolve_context_config, runtime_contract,
    upgrade_tetris_candidate, validate_candidate,
)
from game_harness.games import get_game
from game_harness.runtime import ReceiptError, _digest, run_episode
from game_harness.tetris_context import (
    TetrisContextError, TetrisDecisionHarness, audit_context_episode, goal_catalog,
)
from test_tetris_context import FixtureProvider, initial_state, call_at
from test_tetris_planning_context import v3


LEGACY = {"decision_instructions": "Select one native action", "strategy_notes": "Observe the actual result"}


def candidate(*, recent=4, period=4, order=CONTEXT_SECTIONS):
    result = upgrade_tetris_candidate(LEGACY, "GOAL_GUIDANCE")
    result["memory_policy"] = json.dumps({"instructions": "MEMORY_GUIDANCE", "recent_actions": recent,
                                           "replan_every_actions": period})
    result["observation_policy"] = json.dumps({"instructions": "OBSERVATION_GUIDANCE", "section_order": list(order)})
    return result


def harness(policy=None):
    return {"id": "component-fixture", "game_id": "tetris", "decision_model": "fixture-decision",
            "candidate": policy or candidate()}


def episode(policy=None, provider=None):
    frozen = harness(policy)
    result = run_episode(provider or FixtureProvider(), frozen, episode_id="components-fixture", seed=21,
                         adapter=get_game("tetris"), config={"max_steps": 9})
    assert result["infra_error"] is None
    return result, frozen


def test_legacy_strip_and_exact_keysets_remain_supported():
    assert validate_candidate({key: "  " + value + "  " for key, value in LEGACY.items()}) == LEGACY
    assert component_names(LEGACY) == TEXT_COMPONENTS
    assert component_names(candidate()) == TETRIS_COMPONENTS
    for invalid in ({**LEGACY, "goal_instructions": "only one extra"}, {},
                    {**candidate(), "code": "choose_a_move()"}):
        with pytest.raises(ComponentValidationError):
            validate_candidate(invalid)


@pytest.mark.parametrize("key,value", [
    ("memory_policy", '{"instructions":"a","recent_actions":2,"recent_actions":4,"replan_every_actions":4}'),
    ("memory_policy", '{"instructions":"a","recent_actions":true,"replan_every_actions":4}'),
    ("memory_policy", '{"instructions":"a","recent_actions":3,"replan_every_actions":4}'),
    ("memory_policy", '{"instructions":"a","recent_actions":4,"replan_every_actions":0}'),
    ("memory_policy", '{"instructions":"a","recent_actions":NaN,"replan_every_actions":4}'),
    ("memory_policy", '{"instructions":"a","recent_actions":4,"replan_every_actions":4,"select":"hard-drop:100"}'),
    ("memory_policy", '{"instructions":" ","recent_actions":4,"replan_every_actions":4}'),
    ("memory_policy", "```json\n{}\n```"),
    ("observation_policy", json.dumps({"instructions": "x", "section_order": list(CONTEXT_SECTIONS[:-1])})),
    ("observation_policy", json.dumps({"instructions": "x", "section_order": [CONTEXT_SECTIONS[0]] * 5})),
    ("observation_policy", json.dumps({"instructions": "x", "section_order": [1, 2, 3, 4, 5]})),
])
def test_invalid_component_json_cannot_change_runtime_contract(key, value):
    with pytest.raises(ComponentValidationError):
        validate_candidate({**candidate(), key: value})


def test_upgrade_defaults_match_v8_memory_and_section_sequence():
    upgraded = upgrade_tetris_candidate(LEGACY, "goal text")
    config = context_config(upgraded)
    assert config["recent_actions"] == config["replan_every_actions"] == 4
    assert config["section_order"] == list(CONTEXT_SECTIONS)
    assert config["structured_facts"] is config["control_feedback"] is config["decision_goal_memory"] is True
    assert "vision" not in config and "image" not in config
    assert {name: upgraded[name] for name in TEXT_COMPONENTS} == LEGACY
    with pytest.raises(ComponentValidationError):
        upgrade_tetris_candidate(upgraded, "new goal")


@pytest.mark.parametrize("recent,period", [(2, 2), (4, 4), (8, 8)])
def test_memory_window_and_fixed_replan_frequency_actually_change_calls(recent, period):
    provider = FixtureProvider()
    result = run_episode(provider, harness(candidate(recent=recent, period=period)),
                         episode_id="actual-memory-cadence", seed=21, adapter=get_game("tetris"),
                         config={"max_steps": 13})
    assert result["infra_error"] is None and result["steps"] == 13
    results = [row["receipt"] for row in result["trace"]]
    goals = [call for call in provider.calls if call[3].endswith(":goal")]
    assert [call[0]["step"] for call in goals] == list(range(0, 13, period))
    memory = results[-1]["tetris_context"]["action_input_state"]["decision_goal_memory"]
    assert [action["step"] for action in memory["recent_actions"]] == list(range(12 - recent, 12))
    assert all(result["choice_id"] == "noop:100" for result in results)


def test_observation_order_changes_actual_input_without_dropping_any_fact_or_catalog():
    state, native = initial_state()
    orders = [CONTEXT_SECTIONS, tuple(reversed(CONTEXT_SECTIONS))]
    inputs = []
    for order in orders:
        provider = FixtureProvider()
        wrapper = TetrisDecisionHarness(provider, context_config(candidate(order=order)))
        receipt = call_at(wrapper, state, native, 0)
        for model_state, instructions, catalog, request_id in provider.calls:
            assert [key for key in model_state if key in CONTEXT_SECTIONS] == list(order)
            assert "MEMORY_GUIDANCE" in instructions and "OBSERVATION_GUIDANCE" in instructions
            assert "所有调用think_mode=never" in instructions
            if request_id.endswith(":goal"):
                assert "GOAL_GUIDANCE" in instructions
                assert [item["id"] for item in catalog] == [item["id"] for item in goal_catalog()]
            else:
                assert "GOAL_GUIDANCE" not in instructions
                assert catalog == native
        inputs.append(receipt["tetris_context"]["calls"])
    for first, second in zip(*inputs):
        assert first["input_state"] == second["input_state"]
        assert first["input_hash"] == second["input_hash"]
        assert first["input_serialized_hash"] != second["input_serialized_hash"]
    # V4 does not invent or remove observations compared with V8's v3 inputs.
    old_provider = FixtureProvider()
    call_at(TetrisDecisionHarness(old_provider, v3()), state, native, 0)
    assert old_provider.calls[-1][0] == inputs[0][-1]["input_state"]


@pytest.mark.parametrize("change", ["piece", "type", "board", "episode"])
def test_component_memory_always_invalidates_on_fixed_scope_changes(change):
    state, native = initial_state()
    wrapper = TetrisDecisionHarness(FixtureProvider(goals=["target:2:1", "keep"]),
                                     context_config(candidate(recent=8, period=8)))
    call_at(wrapper, state, native, 0)
    if change == "piece":
        state["observation"]["pieceId"] += 1
    elif change == "type":
        from game_harness.tetris_landing import _cells, _offsets
        state["observation"]["active"]["type"] = "I" if state["observation"]["active"]["type"] != "I" else "O"
        active = state["observation"]["active"]
        active["cells"] = [{"x": x, "y": y} for x, y in _cells(
            _offsets(active["type"], active["rotation"]), active["x"], active["y"])]
    elif change == "board":
        state["observation"]["board"]["rows"][-1]["cells"] = "I........."
    else:
        state["episode_id"] = "new-episode"
    result = call_at(wrapper, state, native, 0 if change == "episode" else 1)
    memory = result["tetris_context"]["action_input_state"]["decision_goal_memory"]
    assert memory["target"] is None and memory["recent_actions"] == []
    assert result["tetris_context"]["calls"][0]["purpose"] == "goal"


def test_full_candidate_enables_context_and_roundtrips_sorted_json_audit():
    result, frozen = episode(candidate(recent=2, period=2, order=reversed(CONTEXT_SECTIONS)))
    assert result["tetris_context_config"] == context_config(frozen["candidate"])
    assert "tetris_context_config" not in frozen
    persisted = json.loads(json.dumps(result, sort_keys=True))
    audited = audit_context_episode(persisted, frozen)
    assert audited["actual_inputs_verified"] and audited["memory_replay_verified"]
    assert audited["game_actions"] == 9 and audited["goal_calls"] == 5


def test_saved_run_audit_replays_native_rules_and_derived_component_context():
    import importlib.util
    from pathlib import Path
    source = Path(__file__).resolve().parents[1] / "scripts/audit_saved_run.py"
    spec = importlib.util.spec_from_file_location("component_native_auditor", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result, frozen = episode()
    frozen["game_context"] = result["game_context"]
    audit = module._audit_episode(json.loads(json.dumps(result, sort_keys=True)), {frozen["id"]: frozen})
    assert audit["thinking_verified"] == 9
    assert audit["context_audit"]["actual_inputs_verified"] is True


def test_audit_rejects_order_mutation_even_when_sort_key_content_digest_is_unchanged():
    result, frozen = episode()
    changed = deepcopy(frozen)
    observation = json.loads(changed["candidate"]["observation_policy"])
    observation["section_order"].reverse()
    changed["candidate"]["observation_policy"] = json.dumps(observation)
    result["tetris_context_config"] = context_config(changed["candidate"])
    with pytest.raises(TetrisContextError, match="serialization/order"):
        audit_context_episode(result, changed)


@pytest.mark.parametrize("field", ["input_serialized_hash", "input_key_order"])
def test_missing_order_evidence_is_not_accepted_as_audited(field):
    result, frozen = episode()
    del result["trace"][0]["receipt"]["tetris_context"]["calls"][0][field]
    with pytest.raises(TetrisContextError, match="serialization/order"):
        audit_context_episode(result, frozen)


def test_full_profile_refuses_other_games_and_conflicting_context_before_any_decision():
    provider = FixtureProvider()
    with pytest.raises(ComponentValidationError, match="only for Tetris"):
        run_episode(provider, harness(), episode_id="bad-game", seed=0, adapter=get_game("snake"))
    frozen = harness()
    frozen["tetris_context_config"] = v3()
    with pytest.raises(ComponentValidationError, match="conflicts"):
        run_episode(provider, frozen, episode_id="conflict", seed=0, adapter=get_game("tetris"))
    assert not provider.calls
    frozen["tetris_context_config"] = context_config(frozen["candidate"])
    assert resolve_context_config(frozen, "tetris") == frozen["tetris_context_config"]
    with pytest.raises(ComponentValidationError, match="all five"):
        resolve_context_config({"candidate": LEGACY, "tetris_context_config": frozen["tetris_context_config"]}, "tetris")


@pytest.mark.parametrize("bad", [{"choice_id": "not-an-action"}, {"think_mode": "auto"},
                                 {"think_used": True}, {"reasoning_tokens": 1}])
def test_invalid_goal_receipt_stops_without_game_action_or_fallback(bad):
    provider = FixtureProvider(transform=lambda receipt, state, planning: receipt.update(bad) if planning else None)
    result = run_episode(provider, harness(), episode_id="invalid-goal", seed=1,
                         adapter=get_game("tetris"), config={"max_steps": 9})
    assert result["termination_reason"] == "infra_error"
    assert result["steps"] == 0 and result["trace"] == []
    assert len(provider.calls) == 1
    assert result["infra_error"]["tetris_pending_context"]["calls"][0]["status"] == "failed"


def test_contract_describes_actual_memory_and_conditional_projections_only_for_full_profile():
    context = {"game_id": "tetris", "rules_version": "fixture"}
    legacy = runtime_contract(LEGACY, context)
    full = runtime_contract(candidate(recent=2, period=8), context, "fixture-model")
    assert "stateless" in legacy["player"]["memory"]
    assert "stateless" not in full["player"]["memory"]
    assert "最近2" in full["player"]["memory"] and "每8" in full["player"]["memory"]
    assert full["player"]["think_mode"] == "never" and full["player"]["in_game_coach_calls"] is False
    assert "不假设height_profile" not in full["game_guidance"]["design_focus"]
    assert set(CONTEXT_SECTIONS[1:]) <= set(full["runtime_fields"])
    assert full["components"]["component_names"] == list(TETRIS_COMPONENTS)
