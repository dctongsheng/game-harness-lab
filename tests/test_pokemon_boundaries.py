"""Pokemon native-result gating and strict separation of private player information."""
from __future__ import annotations

from copy import deepcopy
import json

import pytest

from game_harness.evolution import _summary
from game_harness.scoring import IncompleteGameResult, require_evaluable_result
from game_harness.visibility import public_payload


CONTEXT = {
    'game_id': 'pokemon',
    'score_spec': {'min': 0, 'max': 100, 'direction': 'maximize', 'win': 100, 'loss': 0, 'draw': 50},
    'evaluation': {'require_native_terminal': True},
    'information_policy': {'feedback_visibility': 'candidate_before_only', 'seed_is_private': True},
}


def native_episode(result='win'):
    return {
        'episode_id': 'opaque-pokemon-episode', 'game_id': 'pokemon', 'seed': 1827359,
        'harness_id': 'frozen-candidate', 'score': {'win': 100, 'loss': 0, 'draw': 50}[result],
        'steps': 2, 'termination_reason': 'native_end', 'model': 'fixture-model',
        'infra_error': None, 'trace': [],
        'final_state': {'game_id': 'pokemon', 'native_terminal': True, 'truncated': False,
                        'done': True, 'result': result, 'score': {'win': 100, 'loss': 0, 'draw': 50}[result]},
    }


@pytest.mark.parametrize('result', ['win', 'loss', 'draw'])
def test_native_terminal_results_are_evaluable_and_source_is_immutable(result):
    episode = native_episode(result)
    before = deepcopy(episode)
    assert require_evaluable_result(episode, CONTEXT) is None
    summary = _summary(episode, include_trace=False, game_context=CONTEXT)
    assert summary['score'] == episode['score']
    assert episode == before


@pytest.mark.parametrize('final_state', [
    {}, None, False, [], 'NOT_A_RESULT',
    {'done': True, 'native_terminal': False, 'truncated': True, 'result': 'unresolved'},
    {'done': True, 'native_terminal': True, 'truncated': True, 'result': 'win'},
    {'done': True, 'native_terminal': True, 'result': 'win'},
    {'done': True, 'truncated': False, 'result': 'win'},
    {'done': True, 'native_terminal': True, 'truncated': False},
    {'done': True, 'native_terminal': True, 'truncated': False, 'result': None},
    {'done': True, 'native_terminal': True, 'truncated': False, 'result': 'unresolved'},
    {'done': True, 'native_terminal': True, 'truncated': False, 'result': 'timeout_win'},
    {'done': True, 'native_terminal': True, 'truncated': False, 'result': ['win']},
    {'done': True, 'native_terminal': 1, 'truncated': False, 'result': 'win'},
    {'done': True, 'native_terminal': True, 'truncated': 0, 'result': 'win'},
])
def test_native_gate_and_search_summary_reject_missing_or_invalid_outcomes(final_state):
    episode = native_episode()
    episode['final_state'] = final_state
    for operation in (
        lambda: require_evaluable_result(episode, CONTEXT),
        lambda: _summary(episode, include_trace=False, game_context=CONTEXT),
        lambda: _summary(episode, include_trace=True, game_context=CONTEXT),
    ):
        with pytest.raises(IncompleteGameResult):
            operation()


@pytest.mark.parametrize('change', [
    {'done': False}, {'done': 1}, {'done': None},
    {'score': 50}, {'score': 0}, {'score': True}, {'score': float('nan')},
    {'score': float('inf')}, {'score': '100'}, {'result': {'win': True}},
])
def test_native_gate_rejects_nonterminal_or_inconsistent_final_result(change):
    episode = native_episode('win')
    episode['final_state'].update(change)
    with pytest.raises(IncompleteGameResult):
        require_evaluable_result(episode, CONTEXT)
    with pytest.raises(IncompleteGameResult):
        _summary(episode, include_trace=False, game_context=CONTEXT)


@pytest.mark.parametrize('score', [0, 50, True, float('nan'), float('inf'), '100', None])
def test_native_gate_rejects_episode_score_inconsistent_with_final_native_win(score):
    episode = native_episode('win')
    episode['score'] = score
    with pytest.raises(IncompleteGameResult):
        require_evaluable_result(episode, CONTEXT)
    with pytest.raises(IncompleteGameResult):
        _summary(episode, include_trace=False, game_context=CONTEXT)


def test_native_gate_cannot_accept_top_level_outcome_in_place_of_final_native_evidence():
    episode = native_episode()
    episode.pop('final_state')
    episode.update(native_terminal=True, truncated=False, result='win')
    with pytest.raises(IncompleteGameResult):
        require_evaluable_result(episode, CONTEXT)
    with pytest.raises(IncompleteGameResult):
        _summary(episode, include_trace=False, game_context=CONTEXT)


def test_non_native_games_keep_existing_evaluation_contract():
    episode = {'game_id': 'snake', 'score': 1}
    assert require_evaluable_result(episode, {'game_id': 'snake'}) is None


def public_view(turn=1):
    return {'game_id': 'pokemon', 'format_label': '第三世代单打 3v3', 'native_turn': turn,
            'players': [{'seat': 0, 'unrevealed_count': 2,
                         'team': [{'species': 'PUBLIC_CHARIZARD', 'hp_fraction': .5, 'moves': []}]},
                        {'seat': 1, 'unrevealed_count': 2,
                         'team': [{'species': 'PUBLIC_BLASTOISE', 'hp_fraction': 1., 'moves': []}]}],
            'recent_events': ['|turn|' + str(turn)]}


def private_snapshot(step=0, secret='PRIVATE_OPPONENT'):
    return {'game_id': 'pokemon', 'step': step, 'max_steps': 160, 'native_turn': 1,
            'score': 50, 'done': False, 'native_terminal': False, 'truncated': False,
            'result': None, 'candidate_seat': 0, 'actor': step % 2,
            'team': [{'species': secret, 'moves': [{'id': secret + '_MOVE'}]}],
            'opponent_team': [{'species': secret + '_OPPONENT'}],
            'observation': {'team': secret}, 'request': {'team': secret},
            'raw_request': {'state': secret}, 'request_payload': {'state': secret},
            'request_evidence': {'state': secret},
            'render': {'team': secret}, 'public_view': public_view(),
            'applied_action': secret + '_ACTION', 'pending_choice': secret + '_PENDING',
            'criteria': [{'id': secret + '_ACTION'}]}


def test_public_state_and_replay_drop_private_teams_requests_and_choices_even_with_projection():
    snapshot = private_snapshot()
    record = {'game_id': 'pokemon', 'seed': 1827359, 'final_state': snapshot,
              'last_action': 'PRIVATE_LAST_ACTION', 'trace': [
                  {'before': snapshot, 'after': snapshot, 'action': 'PRIVATE_ACTION',
                   'applied_action': 'PRIVATE_APPLIED_ACTION', 'criteria': [{'id': 'PRIVATE_LEGAL'}],
                   'receipt': {'model': 'fixture-model', 'choice_id': 'PRIVATE_CHOICE',
                               'request_payload': {'state': 'PRIVATE_PROMPT'},
                               'raw_response': {'probabilities': {'PRIVATE_LEGAL': 1}}}}]}
    source = {'episodes': [record], 'current_episode': record}
    before = deepcopy(source)
    result = public_payload(source, {'pokemon'})
    serialized = json.dumps(result)
    assert 'PRIVATE' not in serialized
    assert '1827359' not in serialized
    assert 'PUBLIC_CHARIZARD' in serialized and 'PUBLIC_BLASTOISE' in serialized
    assert result['episodes'][0]['final_state']['public_view'] == public_view()
    assert result['episodes'][0]['trace'][0]['receipt']['model'] == 'fixture-model'
    assert source == before


@pytest.mark.parametrize('projection', [None, False, [], 'PRIVATE_PROJECTION'])
def test_missing_public_projection_fails_closed_for_all_pokemon_private_fields(projection):
    snapshot = private_snapshot()
    snapshot['public_view'] = projection
    source = {'game_id': 'pokemon', 'snapshot': snapshot}
    result = public_payload(source, {'pokemon'})
    serialized = json.dumps(result)
    assert 'PRIVATE' not in serialized
    assert 'PUBLIC_CHARIZARD' not in serialized
    projected = result['snapshot']
    assert not {'team', 'opponent_team', 'observation', 'request', 'render', 'public_view',
                'pending_choice', 'criteria', 'applied_action'} & projected.keys()
    assert projected['step'] == 0 and projected['score'] == 50


def feedback_episode():
    candidate = private_snapshot(0, 'KNOWN_CANDIDATE_PARTY')
    candidate['opponent_team'] = [{'species': 'PUBLIC_BLASTOISE', 'moves': []}]
    waiting = private_snapshot(1)
    final = private_snapshot(2, 'PRIVATE_TERMINAL_PARTY')
    final.update(done=True, native_terminal=True, truncated=False, result='loss', score=0,
                 reason='native_end', public_view=public_view(2))
    final['public_view']['recent_events'] = ['|move|p2a: Blastoise|Surf|p1a: Charizard', '|win|Opponent']
    source = native_episode('loss')
    source['final_state'] = final
    source['trace'] = [
        {'policy_role': 'candidate', 'actor': 0, 'before': candidate, 'after': waiting,
         'action': 'move:1', 'criteria': [{'id': 'move:1', 'label': 'Known candidate move'}],
         'receipt': {'raw_request': {'body': 'PRIVATE_TRANSPORT'}, 'model': 'fixture-model'}},
        {'policy_role': 'opponent', 'actor': 1, 'before': waiting, 'after': final,
         'action': 'PRIVATE_OPPONENT_ACTION_SLOT_4',
         'criteria': [{'id': 'PRIVATE_OPPONENT_CATALOG', 'label': 'Hidden move'}],
         'receipt': {'choice_id': 'PRIVATE_OPPONENT_ACTION_SLOT_4'}},
    ]
    return source


def test_reflection_receives_candidate_before_and_public_resolution_not_opponent_private_data():
    source = feedback_episode()
    before = deepcopy(source)
    result = _summary(source, include_trace=True, game_context=CONTEXT)
    serialized = json.dumps(result)
    assert 'KNOWN_CANDIDATE_PARTY' in serialized
    assert 'PUBLIC_BLASTOISE' in serialized and 'Surf' in serialized
    assert 'PRIVATE' not in serialized and '1827359' not in serialized
    assert result['trace_sampling']['retained_indices'] == [0]
    assert result['trace_sampling']['public_followup_indices'] == [1]
    event = result['trace_sample'][0]
    assert event['action'] == 'move:1'
    assert 'after' not in event
    assert event['public_result']['visibility'] == 'public_common_knowledge'
    assert event['public_followups'][0]['public_result']['done'] is True
    assert 'action' not in event['public_followups'][0]
    assert 'criteria' not in event['public_followups'][0]
    assert source == before


def test_opponent_private_counterfactuals_cannot_change_reflection_pack():
    source = feedback_episode()
    expected = _summary(source, include_trace=True, game_context=CONTEXT)
    changed = deepcopy(source)
    for state in (changed['trace'][0]['after'], changed['trace'][1]['before'], changed['trace'][1]['after']):
        state['team'] = [{'species': 'DIFFERENT_PRIVATE_TEAM', 'moves': ['DIFFERENT_MOVE']}]
        state['opponent_team'] = [{'species': 'DIFFERENT_PRIVATE_OTHER_TEAM'}]
        state['observation'] = {'unseen': 'DIFFERENT_PRIVATE_STATE'}
        state['applied_action'] = 'DIFFERENT_SLOT'
        state['request'] = {'side': {'pokemon': 'DIFFERENT_REQUEST'}}
    changed['trace'][1]['action'] = 'DIFFERENT_SLOT'
    changed['trace'][1]['criteria'] = [{'id': 'DIFFERENT_CATALOG'}]
    changed['trace'][1]['receipt'] = {'choice_id': 'DIFFERENT_SLOT'}
    assert _summary(changed, include_trace=True, game_context=CONTEXT) == expected


def test_missing_public_resolution_never_falls_back_to_opponent_private_state():
    source = feedback_episode()
    source['trace'][0]['after'].pop('public_view')
    source['trace'][1]['after'].pop('public_view')
    result = _summary(source, include_trace=True, game_context=CONTEXT)
    assert 'PRIVATE' not in json.dumps(result)
    event = result['trace_sample'][0]
    assert event['public_result']['unavailable'] is True
    assert event['public_followups'] == []
    assert result['trace_sampling']['last_source_step_covered'] is False
