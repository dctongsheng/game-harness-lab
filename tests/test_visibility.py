"""Hidden-information evidence must not be merged across player viewpoints."""
from copy import deepcopy
import json
import pytest

from game_harness.evolution import _summary, evolve_harness
from game_harness.visibility import public_payload


POLICY = {'feedback_visibility': 'candidate_before_only', 'seed_is_private': True}
CONTEXT = {'game_id': 'hidden_fixture', 'information_policy': POLICY,
           'rules': 'Only current player information is observable.',
           'score_spec': {'min': 0, 'max': 100, 'direction': 'maximize'}}
BASE = {'decision_instructions': 'Choose a legal move.', 'strategy_notes': 'Use observable evidence only.'}


def episode(seed):
    return {'episode_id': 'opaque-trial-id', 'game_id': 'hidden_fixture', 'seed': seed,
            'harness_id': 'baseline', 'score': 50, 'steps': 3, 'termination_reason': 'step_limit',
            'model': 'fixture', 'infra_error': None,
            'trace': [
                {'policy_role': 'candidate', 'before': {'observation': 'candidate-known'},
                 'after': {'observation': 'NEVER_OPPONENT_PRIVATE'}, 'action': 'move:0:1'},
                {'policy_role': 'opponent', 'before': {'observation': 'NEVER_OPPONENT_PRIVATE'},
                 'after': {'observation': 'candidate-known'}, 'action': 'move:5:6'},
                {'policy_role': 'candidate', 'before': {'observation': 'candidate-known'},
                 'after': {'observation': 'NEVER_OPPONENT_PRIVATE'}, 'action': 'move:1:2'},
            ]}


def test_private_reflection_uses_only_candidate_before_and_omits_seed():
    value = episode(175319)
    before = deepcopy(value)
    result = _summary(value, include_trace=True, game_context=CONTEXT)
    text = json.dumps(result)
    assert 'NEVER_OPPONENT_PRIVATE' not in text and '175319' not in text
    assert 'candidate-known' in text
    assert result['trace_sampling']['retained_indices'] == [0, 2]
    assert all('after' not in item for item in result['trace_sample'])
    assert value == before


def test_real_gepa_coach_messages_do_not_receive_hidden_opponent_or_seed(tmp_path):
    calls = []

    class Coach:
        def complete(self, messages, purpose):
            calls.append(deepcopy(messages))
            return json.dumps(BASE)

    evolve_harness(candidate=BASE, trigger_episode=episode(175319),
                   train_seeds=[175320, 175321], validation_seeds=[175322, 175323],
                   evaluate_episode=lambda candidate, seed, purpose: episode(seed), coach=Coach(),
                   run_dir=tmp_path / 'job', game_context=CONTEXT)
    assert calls
    text = json.dumps(calls)
    assert 'NEVER_OPPONENT_PRIVATE' not in text
    for seed in range(175319, 175324):
        assert str(seed) not in text


def test_public_state_and_replay_use_only_common_knowledge_without_mutating_evidence():
    view = {'rows': 1, 'cols': 2, 'cells': [{'label': '?', 'hidden': True}, None]}
    snapshot = {'game_id': 'hidden_fixture', 'step': 3, 'score': 50, 'done': True,
                'observation': {'private': 'NEVER_OPPONENT_PRIVATE'},
                'cells': [{'label': 'NEVER_OPPONENT_PRIVATE'}],
                'render': {'private': 'NEVER_OPPONENT_PRIVATE'}, 'public_view': view}
    record = {'game_id': 'hidden_fixture', 'seed': 175319, 'config': {'seed': 175319},
              'final_state': snapshot, 'trace': [{'before': snapshot, 'after': snapshot,
              'criteria': [{'id': 'reveals-engineer-mobility'}], 'action': 'move:0:1',
              'receipt': {'raw_response': {'probabilities': {'reveals-engineer-mobility': 1}},
                          'model': 'fixture', 'choice_id': 'move:0:1'}}]}
    before = deepcopy(record)
    result = public_payload({'episodes': [record], 'current_episode': record}, {'hidden_fixture'})
    text = json.dumps(result)
    assert 'NEVER_OPPONENT_PRIVATE' not in text and '175319' not in text
    assert 'reveals-engineer-mobility' not in text
    assert result['episodes'][0]['final_state']['cells'] == view['cells']
    assert result['episodes'][0]['trace'][0]['receipt']['choice_id'] == 'move:0:1'
    assert before == record


def test_public_projection_leaves_full_information_games_unchanged():
    record = {'game_id': 'snake', 'seed': 42, 'criteria': [{'id': 'up'}], 'raw_response': {'x': 1}}
    assert public_payload(record, {'hidden_fixture'}) == record


@pytest.mark.parametrize('projection', [None, 'NEVER_PRIVATE', ['NEVER_PRIVATE']])
def test_missing_hidden_game_public_projection_does_not_fall_back_to_private_cells(projection):
    state = {'game_id': 'hidden_fixture', 'snapshot': {'step': 1,
             'cells': ['NEVER_PRIVATE'], 'observation': 'NEVER_PRIVATE', 'render': {'cells': ['NEVER_PRIVATE']}}}
    state['snapshot']['public_view'] = projection
    result = public_payload(state, {'hidden_fixture'})
    assert result['snapshot'] == {'step': 1}
    assert 'NEVER_PRIVATE' not in json.dumps(result)
