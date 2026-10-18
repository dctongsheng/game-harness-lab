"""V3 adds facts and an independent planner, never a native-action selector."""
from copy import deepcopy

import pytest

from game_harness.tetris_context import (
    GOAL_INSTRUCTIONS, TetrisContextError, TetrisDecisionHarness,
    audit_context_episode, context_instructions, goal_catalog,
    goal_instructions, validate_config,
)
from test_tetris_context import FixtureProvider, initial_state
from test_tetris_control import fixture_episode, v2


def v3():
    return {**v2(), "schema": "tetris-context-v3", "goal_instructions": "独立规划测试提示"}


@pytest.mark.parametrize("change", [{"goal_instructions": ""}, {"goal_instructions": " "},
    {"goal_instructions": 3}, {"goal_instructions": "x" * 12001}, {"control_feedback": False},
    {"structured_facts": False}, {"decision_goal_memory": False}, {"extra": True}])
def test_configuration_rejects_unfrozen_or_missing_features(change):
    with pytest.raises(TetrisContextError):
        validate_config({**v3(), **change})


def test_old_goal_prompt_bytes_and_new_prompt_separation():
    assert goal_instructions("action policy", v2()) == context_instructions("action policy", v2()) + "\n" + GOAL_INSTRUCTIONS
    assert "action policy" not in goal_instructions("action policy", v3())
    assert "独立规划测试提示" in goal_instructions("action policy", v3())
    assert "action policy" in context_instructions("action policy", v3())
    assert "独立规划测试提示" not in context_instructions("action policy", v3())


def test_projection_does_not_filter_catalogs_or_override_decision_choice():
    state, native = initial_state()
    original = deepcopy(state)
    # An infeasible target is intentionally allowed, and the action choice
    # remains the raw provider choice even though projections can flag it.
    provider = FixtureProvider(goals=["target:9:0"], action="noop:100")
    wrapper = TetrisDecisionHarness(provider, v3())
    receipt = wrapper.decide(state, "action policy", native, "fixture:step:0")
    assert state == original
    assert receipt["choice_id"] == "noop:100"
    assert [c["id"] for c in provider.calls[0][2]] == [c["id"] for c in goal_catalog()]
    assert all("原生按键路径未验证" in c["label"] for c in provider.calls[0][2][2:])
    assert provider.calls[0][2][:2] == goal_catalog()[:2]
    assert provider.calls[1][2] == native
    assert len(provider.calls) == 2
    assert provider.calls[1][0]["decision_goal_memory"]["target"] == {"leftmost_column": 9, "rotation": 0}
    for request, _, _, _ in provider.calls:
        projected = request["tetris_landing_projections"]["candidates"]
        assert [item["id"] for item in projected] == [g["id"] for g in goal_catalog()[2:]]


def test_goal_and_projection_reconstruction_and_stale_scope():
    actions = ["right:100", "hard-drop:100", "noop:100", "hold:100", "noop:100"]
    episode, harness, provider = fixture_episode(actions, configuration=v3())
    assert audit_context_episode(episode, harness)["actual_inputs_verified"] is True
    first_goal = provider.calls[0][0]["tetris_landing_projections"]
    subsequent_goal = next(call[0] for call in provider.calls if call[0]["step"] == 2 and call[3].endswith(":goal"))
    assert subsequent_goal["decision_goal_memory"]["target"] is None
    assert subsequent_goal["tetris_landing_projections"] != first_goal
    tampered = deepcopy(episode)
    record = tampered["trace"][0]["receipt"]["tetris_context"]["calls"][0]
    record["input_state"]["tetris_landing_projections"]["candidates"][0]["points_delta"] = 999
    with pytest.raises(TetrisContextError):
        audit_context_episode(tampered, harness)


def test_injected_projection_is_rejected_before_any_provider_call():
    state, native = initial_state()
    state["tetris_landing_projections"] = {}
    provider = FixtureProvider()
    with pytest.raises(TetrisContextError, match="already contains"):
        TetrisDecisionHarness(provider, v3()).decide(state, "test", native, "fixture:step:0")
    assert not provider.calls
