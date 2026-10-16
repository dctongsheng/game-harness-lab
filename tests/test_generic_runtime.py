"""Synthetic alternating game verifies generic action authority and lifecycle."""
from copy import deepcopy
import json

import httpx
import pytest

from game_harness.providers import CoachProvider, Settings, digest
from game_harness.runtime import run_episode


HARNESS = {"id": "candidate-v1", "candidate": {
    "decision_instructions": "CANDIDATE_INSTRUCTIONS", "strategy_notes": "candidate notes"}}


class TurnSession:
    def __init__(self):
        self.index = 0
        self.closed = False
        self.invalid_state = None
        self.extra_move = False

    def snapshot(self):
        result = {"game_id": "turn-fixture", "step": self.index, "score": self.index / 4 - 0.5,
                  "done": self.index == 2, "reason": "fixture_end" if self.index == 2 else None,
                  "turn": "red" if self.index % 2 == 0 else "black",
                  "policy_role": "candidate" if self.index % 2 == 0 else "opponent"}
        return {**result, **(self.invalid_state or {})}

    def candidates(self):
        if self.index == 2:
            return []
        result = [{"id": f"move-{self.index}-{i}", "label": f"move {i}"} for i in range(self.index + 2)]
        if self.extra_move:
            result.append({"id": "late-move", "label": "late catalog mutation"})
        return result

    def step(self, choice):
        assert choice in {item['id'] for item in self.candidates()}
        self.index += 1
        return self.snapshot()

    def close(self):
        self.closed = True


class TurnAdapter:
    id = "turn-fixture"
    name = "Synthetic turn game"
    rules_version = "turn-fixture-v1"
    rules = "A two-turn fixture with a fixed opponent."
    opponent_policy = {"decision_instructions": "FIXED_OPPONENT", "strategy_notes": "never mutate"}

    def __init__(self):
        self.session = TurnSession()

    def context(self, config):
        return {"game_id": self.id, "rules": self.rules,
                "score_spec": {"min": -0.5, "max": 0.5, "direction": "maximize"},
                "opponent_policy": deepcopy(self.opponent_policy)}

    def create(self, seed, config):
        return self.session


class ChoiceProvider:
    def __init__(self, hook=None):
        self.calls = []
        self.hook = hook

    def decide(self, state, instructions, criteria, request_id):
        self.calls.append(deepcopy((state, instructions, criteria)))
        response = {"choice_id": criteria[-1]["id"], "model": "fixture-model",
                    "request_id": request_id, "observation_revision": state["step"],
                    "candidate_hash": digest(criteria), "elapsed_ms": 0.1,
                    "think_mode": "never", "think_used": False, "reasoning_tokens": 0,
                    "raw_response": {"fixture": True}}
        if self.hook:
            self.hook(response)
        return response


def run(adapter, provider=None, **kwargs):
    return run_episode(provider or ChoiceProvider(), HARNESS, adapter=adapter,
                       episode_id="fixture-generic", seed=0, config={"max_steps": 2}, **kwargs)


def test_each_actor_uses_model_and_current_complete_legal_catalog():
    adapter, provider = TurnAdapter(), ChoiceProvider()
    result = run(adapter, provider)
    assert result['infra_error'] is None
    assert result['score'] == 0.0
    assert result['game_id'] == 'turn-fixture'
    assert result['game_context']['score_spec']['min'] == -0.5
    assert result['config']['max_steps'] == 2
    assert len(provider.calls) == 2
    assert 'CANDIDATE_INSTRUCTIONS' in provider.calls[0][1]
    assert 'FIXED_OPPONENT' not in provider.calls[0][1]
    assert 'FIXED_OPPONENT' in provider.calls[1][1]
    assert 'CANDIDATE_INSTRUCTIONS' not in provider.calls[1][1]
    assert [len(call[2]) for call in provider.calls] == [2, 3]
    assert [step['actor'] for step in result['trace']] == ['red', 'black']
    assert [step['policy_role'] for step in result['trace']] == ['candidate', 'opponent']
    for record, call in zip(result['trace'], provider.calls):
        assert record['criteria'] == call[2]
        assert record['receipt']['candidate_hash'] == digest(record['criteria'])
    assert result['trace'][0]['candidate_hash'] != result['trace'][1]['candidate_hash']
    assert adapter.session.closed


def test_action_catalog_change_during_request_prevents_execution():
    adapter = TurnAdapter()
    provider = ChoiceProvider(lambda _: setattr(adapter.session, 'extra_move', True))
    result = run(adapter, provider)
    assert result['termination_reason'] == 'infra_error'
    assert 'catalog changed' in result['failure']['message']
    assert adapter.session.index == 0
    assert adapter.session.closed


def test_previous_turn_move_cannot_be_reused_in_different_catalog():
    adapter = TurnAdapter()
    provider = ChoiceProvider()
    provider.hook = lambda receipt: receipt.update(choice_id='move-0-0') if len(provider.calls) == 2 else None
    result = run(adapter, provider)
    assert result['steps'] == 1
    assert result['failure']['type'] == 'ReceiptError'
    assert adapter.session.closed


def test_frozen_opponent_cannot_be_changed_by_external_mutation_mid_episode():
    adapter = TurnAdapter()
    adapter.opponent_policy = deepcopy(adapter.opponent_policy)
    provider = ChoiceProvider(lambda _: adapter.opponent_policy.update(decision_instructions='MUTATED'))
    result = run(adapter, provider)
    assert result['infra_error'] is None
    assert 'FIXED_OPPONENT' in provider.calls[1][1]
    assert 'MUTATED' not in provider.calls[1][1]


@pytest.mark.parametrize('state', [{'policy_role': 'script'}, {'cells': {object()}}, {'score': float('nan')}])
def test_bad_adapter_state_closes_without_any_decision(state):
    adapter, provider = TurnAdapter(), ChoiceProvider()
    adapter.session.invalid_state = state
    result = run(adapter, provider)
    assert result['termination_reason'] == 'infra_error'
    assert adapter.session.closed
    assert provider.calls == []
    json.dumps(result, allow_nan=False)


def test_missing_opponent_policy_stops_instead_of_falling_back():
    adapter, provider = TurnAdapter(), ChoiceProvider()
    adapter.opponent_policy = None
    result = run(adapter, provider)
    assert result['steps'] == 1 and len(provider.calls) == 1
    assert 'no frozen opponent policy' in result['failure']['message']
    assert adapter.session.closed


def test_close_failure_is_infrastructure_failure_not_a_game_score():
    adapter = TurnAdapter()

    def close():
        adapter.session.closed = True
        raise RuntimeError('fixture process shutdown failed')

    adapter.session.close = close
    result = run(adapter)
    assert result['infra_error']['stage'] == 'close_session'
    assert result['termination_reason'] == 'infra_error'


def test_initialization_uses_adapter_context_without_snake_assumptions():
    captured = []
    brief = {key: 'fixture' for key in ('background', 'goal', 'rules', 'precautions', 'strategy')}

    def transport(request):
        captured.append(json.loads(request.content))
        content = json.dumps({'brief': brief, 'candidate': HARNESS['candidate']})
        return httpx.Response(200, json={'model': 'fixture-coach', 'choices': [
            {'message': {'content': content}, 'finish_reason': 'stop'}]})

    settings = Settings('http://localhost/v1/systemone', 'fixture-decision', 1,
                        'https://fixture.invalid/v1', 'fixture-coach', 'fixture-key', 1)
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        coach = CoachProvider(settings, client=client)
        actual_brief, candidate = coach.initialize(game_context={
            'game_id': 'xiangqi', 'rules': 'Chinese chess rules',
            'score_spec': {'min': 0, 'max': 100, 'direction': 'maximize'},
            'opponent_policy': {'decision_instructions': 'FROZEN_CHESS_OPPONENT', 'strategy_notes': 'fixed'}})
    assert actual_brief == brief and candidate == HARNESS['candidate']
    prompt = captured[0]['messages'][0]['content']
    assert 'Chinese chess rules' in prompt and 'FROZEN_CHESS_OPPONENT' in prompt
    assert '贪吃蛇' not in prompt and '食物' not in prompt
