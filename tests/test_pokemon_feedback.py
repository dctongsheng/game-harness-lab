"""Pokemon coach projections preserve actionable complete events, not raw views."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path

import pytest

from game_harness.evolution import _summary
from game_harness.feedback import chars, complete_episode, compact_state, fit_pack, _pokemon_public_transition


CONTEXT = {'game_id': 'pokemon', 'information_policy': {
    'feedback_visibility': 'candidate_before_only', 'seed_is_private': True}}


def mon(name, own=False, move='visiblemove'):
    return {'ident': 'p1: ' + name, 'species': name.lower(), 'active': True, 'fainted': False,
            'current_hp': 100, 'max_hp': 150 if own else 100, 'hp_fraction': 2/3 if own else 1,
            'types': ['fire'], 'status': None, 'ability': None, 'item': None,
            'boosts': {'atk': 0, 'spa': 1}, 'stats': {'atk': 80, 'spa': 120} if own else None,
            'slot': 1 if own else None,
            'moves': [{'id': move, 'type': 'fire', 'category': 'special', 'base_power': 95,
                       'accuracy': 1, 'priority': 0, 'target': 'normal', 'boosts': None,
                       'status': None, 'heal': 0, 'drain': 0, 'recoil': 0, 'protect': False,
                       **({'pp': 14, 'max_pp': 15} if own else {})}]}


def state(index, candidate=True, done=False):
    team = [mon('Charizard', own=True, move='flamethrower')] if candidate else [mon('NEVER_OPPONENT_TEAM', own=True, move='NEVER_PRIVATE_MOVE')]
    view = {'players': [{'seat': 0, 'team': [mon('Charizard')], 'unrevealed_count': 2},
                        {'seat': 1, 'team': [mon('Blastoise')], 'unrevealed_count': 2}],
            'native_turn': 1 + index // 2, 'recent_events': ['|turn|1'] + (['|win|Seat 1'] if done else [])}
    return {'game_id': 'pokemon', 'format': 'gen3customgame', 'step': index,
            'candidate_seat': 0, 'viewer_seat': 0 if candidate else 1, 'actor': 'p1' if candidate else 'p2',
            'team': team, 'opponent_team': [mon('Blastoise', move='surf')],
            'native_turn': view['native_turn'], 'native_terminal': done, 'truncated': False,
            'score': 100 if done else 50, 'done': done, 'result': 'win' if done else 'ongoing',
            'winner': 0 if done else None, 'reason': 'native_win' if done else None,
            'public_view': view, 'recent_events': view['recent_events'], 'force_switch': False,
            'weather': {}, 'fields': {}, 'side_conditions': {}, 'opponent_side_conditions': {}}


def test_only_authorized_move_facts_and_public_followups_reach_coach():
    before, hidden, final = state(0), state(1, False), state(2, False, True)
    trace = [{'before': before, 'after': hidden, 'policy_role': 'candidate', 'action': 'move:1',
              'criteria': [{'id': 'move:1', 'label': 'Known move'}]},
             {'before': hidden, 'after': final, 'policy_role': 'opponent', 'action': 'NEVER_PRIVATE_SLOT',
              'criteria': [{'id': 'NEVER_PRIVATE_SLOT', 'label': 'NEVER_PRIVATE_LABEL'}],
              'receipt': {'request_payload': {'state': 'NEVER_HTTP_BODY'}}}]
    episode = {'game_id': 'pokemon', 'trace': trace}
    original = deepcopy(episode)
    result = complete_episode(episode, {'score': 100}, CONTEXT)
    encoded = json.dumps(result)
    assert 'NEVER_' not in encoded
    assert {'flamethrower', 'surf'} <= set(result['observation_context']['move_rules'])
    assert result['trace_sampling']['retained_indices'] == [0]
    assert result['trace_sampling']['public_followup_indices'] == [1]
    assert result['trace_sampling']['last_source_step_covered']
    event = result['trace_sample'][0]
    assert event['before']['team'][0]['moves'] == [{'id': 'flamethrower', 'pp': 14, 'max_pp': 15}]
    assert event['before']['team'][0]['stats'] == {'atk': 80, 'spa': 120}
    assert event['before']['team'][0]['boosts'] == {'spa': 1}
    assert event['before']['team'][0]['hp'] == [100, 150]
    assert 'action' not in event['public_followups'][0]
    assert event['public_followups'][0]['public_result']['new_public_events'] == ['|win|Seat 1']
    assert chars(result) <= 8000
    assert episode == original


def test_public_projection_cannot_copy_private_stats_pp_or_slot():
    bad_public = mon('Charizard', own=True)
    result = compact_state({'players': [{'seat': 0, 'team': [bad_public]}]}, 'pokemon',
                           {'move_rules': {'visiblemove': ['fire', 'special', 95, 1, 0]}})
    public = result['players'][0]['team'][0]
    assert 'stats' not in public and 'slot' not in public
    assert public['moves'] == [{'id': 'visiblemove'}]


def test_public_transition_removes_only_exact_overlap_and_marks_unchanged_party():
    old, new = state(0), state(1)
    old['public_view']['recent_events'] = ['a', 'b', 'c']
    new['public_view']['recent_events'] = ['b', 'c', 'd', 'e']
    result = _pokemon_public_transition(old, new, {})
    assert result['new_public_events'] == ['d', 'e']
    assert result['players_unchanged'] is True and 'players' not in result
    new['public_view']['players'][0]['team'][0]['current_hp'] = 50
    changed = _pokemon_public_transition(old, new, {})
    assert changed['players'][0]['team'][0]['hp'][0] == 50
    assert 'players_unchanged' not in changed


def test_variable_move_facts_remain_with_each_event_instead_of_false_static_rule():
    first, second = state(0), state(1)
    second['team'][0]['moves'][0]['base_power'] = 120
    trace = [{'before': first, 'after': second, 'policy_role': 'candidate', 'action': 'move:1'},
             {'before': second, 'after': state(2, done=True), 'policy_role': 'candidate', 'action': 'move:1'}]
    result = complete_episode({'trace': trace}, {'score': 100}, CONTEXT)
    assert 'flamethrower' not in result['observation_context']['move_rules']
    assert result['trace_sample']
    for row in result['trace_sample']:
        expected = trace[row['trace_index']]['before']['team'][0]['moves'][0]['base_power']
        assert row['before']['team'][0]['moves'][0]['base_power'] == expected


@pytest.mark.skipif(not (Path(__file__).parents[1] / '.runtime/pokemon-showdown/dist/sim/index.js').is_file(),
                    reason='optional pinned native engine is not installed')
@pytest.mark.parametrize('seed', [42, 43, 4243, 1042, 2043, 3042, 4043])
def test_native_complete_terminal_event_survives_8000_char_budget(seed):
    """Native mechanical fixtures, using synthetic choices rather than an LLM."""
    pytest.importorskip('poke_env')
    from game_harness.games.pokemon import PokemonAdapter
    from game_harness.runtime import run_episode
    spec = importlib.util.spec_from_file_location('pokemon_audit_fixture', Path(__file__).with_name('test_pokemon_audit.py'))
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    adapter = PokemonAdapter()
    config = {'game_id': 'pokemon', 'board_size': 8, 'max_steps': 160}
    harness = {'id': 'h-native-feedback-fixture', 'game_id': 'pokemon', 'think_mode': 'never',
               'candidate': {'decision_instructions': 'Fixture', 'strategy_notes': 'Fixture'},
               'game_context': adapter.context(config), 'decision_model': 'fixture-decision'}
    episode = run_episode(fixture.FixtureProvider(), harness, episode_id='ep-native-feedback-fixture', seed=seed,
                          adapter=adapter, config=config, max_steps=160)
    assert episode['infra_error'] is None and episode['final_state']['native_terminal']
    original = deepcopy(episode)
    result = _summary(episode, include_trace=True, game_context=adapter.context(config))
    assert result['trace_sample'], 'Native trajectories must not become empty coaching evidence'
    sampling = result['trace_sampling']
    assert sampling['last_source_step_covered'], 'Native terminal outcome must remain in a complete event'
    assert not sampling['serialized_sample_truncated'] and not sampling['empty_event_evidence']
    assert chars(result) <= 8000 and sampling['serialized_chars'] == chars(result)
    assert all(episode['trace'][i]['policy_role'] == 'candidate' for i in sampling['retained_indices'])
    for row in result['trace_sample']:
        assert 'before' in row and 'action' in row and 'public_result' in row
        assert all('action' not in follow for follow in row['public_followups'])
    packed = fit_pack([result, deepcopy(result)])
    assert all(item['trace_sampling']['last_source_step_covered'] for item in packed)
    assert chars(packed) <= 32000
    assert episode == original
