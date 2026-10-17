"""Complete coaching evidence: factual projections, privacy, and exact budgets."""
from copy import deepcopy
import json

import pytest

from game_harness.evolution import _summary
from game_harness.feedback import COMPLETE_FORMAT, LEGACY_FORMAT, chars, fit_pack, compact_state


def context(game, private=False):
    return {'game_id': game, 'score_spec': {'min': 0, 'max': 10000, 'direction': 'maximize'},
            'information_policy': {'feedback_visibility': 'candidate_before_only', 'seed_is_private': True} if private else {}}


def episode(game, trace, score=0):
    return {'episode_id': 'fixture-source', 'game_id': game, 'harness_id': 'baseline',
            'seed': 1729415, 'score': score, 'steps': len(trace), 'termination_reason': 'fixture_end',
            'model': 'fixture-model', 'infra_error': None, 'trace': trace}


def row(index, before, after, action='wait', role='candidate', criteria=None):
    return {'before': {'step': index, **before}, 'after': {'step': index + 1, **after},
            'action': action, 'policy_role': role, 'actor': 0 if role == 'candidate' else 1,
            'criteria': criteria or [{'id': 'wait', 'label': 'wait'}],
            'receipt': {'raw_response': {'api_key': 'DO_NOT_SEND'}, 'choice_id': action}}


def platform_state(index):
    players = [dict(x=68, y=292, vx=0, vy=0, onGround=True),
               dict(x=240, y=292, vx=3, vy=0, onGround=True),
               dict(x=420.5, y=337.5, vx=3, vy=6.5, onGround=False),
               dict(x=424, y=387, vx=3, vy=9.5, onGround=False)]
    return {'game_id': 'platformer', 'step': index, 'tick': index * 60,
            'score': index * 10, 'done': index == 3, 'reason': 'FELL_IN_PIT' if index == 3 else None,
            'render': {'redundant': 'x' * 30000},
            'observation': {'level': 2, 'width': 1536, 'height': 384,
                'physics': {'maxSpeed': 3, 'gravity': .5, 'jumpVelocity': -10.5},
                'platforms': [{'x': 0, 'y': 320, 'width': 384, 'height': 64},
                              {'x': 448, 'y': 320, 'width': 320, 'height': 64}],
                'player': {**players[index], 'width': 24, 'height': 28}, 'coins': [], 'enemies': [],
                'exit': {'x': 1440, 'y': 256, 'width': 32, 'height': 64}}}


def test_three_step_platform_failure_retains_entire_causal_chain():
    catalog = [{'id': action, 'label': 'native input'} for action in ('right:1000', 'right+jump:100')]
    trace = [row(i, platform_state(i), platform_state(i + 1),
                 'right+jump:100' if i == 2 else 'right:1000', criteria=catalog) for i in range(3)]
    source = episode('platformer', trace, 30)
    original = deepcopy(source)
    result = _summary(source, include_trace=True, game_context=context('platformer'))
    assert result['feedback_format'] == COMPLETE_FORMAT
    assert result['trace_sampling']['retained_indices'] == [0, 1, 2]
    assert result['trace_sampling']['omitted_count'] == 0
    assert result['trace_sampling']['last_source_step_covered']
    final = result['trace_sample'][-1]
    assert final['before']['player']['vx'] == 3 and not final['before']['player']['onGround']
    assert final['after']['reason'] == 'FELL_IN_PIT'
    assert result['observation_context']['physics']['jumpVelocity'] == -10.5
    assert result['observation_context']['legal_actions'] == ['right:1000', 'right+jump:100']
    assert all(r['legal_actions_ref'] == 'observation_context.legal_actions' for r in result['trace_sample'])
    assert chars(result) <= 8000
    assert chars(result) == result['trace_sampling']['serialized_chars']
    assert 'head_excerpt' not in json.dumps(result) and 'DO_NOT_SEND' not in json.dumps(result)
    assert source == original


def army_state(step, public_cells, private_kind, done=False):
    return {'game_id': 'army', 'rows': 1, 'cols': 2, 'step': step, 'turn': step % 2,
            'candidate_seat': 0, 'score': 0 if done else 50, 'done': done,
            'reason': 'NO_LEGAL_MOVES' if done else None,
            'cells': [{'seat': 0, 'kind': private_kind}, {'seat': 1, 'kind': 'unknown', 'hidden': True}],
            'observation': {'piece_identity': private_kind, 'seed': 1729415},
            'public_view': {'rows': 1, 'cols': 2, 'cells': public_cells, 'turn': step % 2}}


def test_army_terminal_opponent_capture_uses_only_public_followup():
    unknown = {'seat': 1, 'kind': 'unknown', 'hidden': True}
    before = army_state(0, [{'seat': 0, 'kind': 'unknown', 'hidden': True}, unknown], 'engineer')
    opponent = army_state(1, [None, unknown], 'NEVER_OPPONENT_IDENTITY')
    final = army_state(2, [None, unknown], 'NEVER_TERMINAL_PRIVATE', True)
    trace = [row(0, before, opponent, 'move:0:1'),
             row(1, opponent, final, 'move:1:0', role='opponent',
                 criteria=[{'id': 'NEVER_OPPONENT_LEGAL_CATALOG', 'label': 'secret'}])]
    source = episode('army', trace)
    result = _summary(source, include_trace=True, game_context=context('army', True))
    encoded = json.dumps(result)
    assert 'engineer' in encoded
    assert 'NEVER_' not in encoded and '1729415' not in encoded
    assert result['trace_sampling']['retained_indices'] == [0]
    assert result['trace_sampling']['public_followup_indices'] == [1]
    assert result['trace_sampling']['last_source_step_covered']
    event = result['trace_sample'][0]
    assert 'after' not in event
    followup = event['public_followups'][0]
    assert followup['action'] == 'move:1:0'
    assert followup['public_result']['done'] and followup['public_result']['reason'] == 'NO_LEGAL_MOVES'
    assert followup['public_result']['board_rows'] == ['. 1:?']
    assert 'legal_actions' not in followup and 'observation' not in followup
    changed = deepcopy(source)
    for frame in (changed['trace'][0]['after'], changed['trace'][1]['before'], changed['trace'][1]['after']):
        frame['observation'] = {'other_hidden_identity': 'COUNTERFACTUAL'}
        frame['cells'] = [{'kind': 'COUNTERFACTUAL'}]
        frame['material'] = 'COUNTERFACTUAL'
        frame['in_check'] = True
    changed['trace'][1]['criteria'] = [{'id': 'OTHER_PRIVATE_CATALOG'}]
    assert _summary(changed, include_trace=True, game_context=context('army', True)) == result


def test_private_missing_public_projection_fails_closed_without_inventing_terminal_effect():
    source = episode('hidden_fixture', [row(0, {'observation': 'known'}, {'observation': 'NEVER_PRIVATE'}),
        row(1, {'observation': 'NEVER_PRIVATE'}, {'observation': 'NEVER_PRIVATE', 'done': True}, role='opponent')])
    result = _summary(source, include_trace=True, game_context=context('hidden_fixture', True))
    assert 'NEVER_PRIVATE' not in json.dumps(result)
    assert result['trace_sample'][0]['public_result']['unavailable']
    assert result['trace_sample'][0]['public_followups'] == []
    assert not result['trace_sampling']['last_source_step_covered']


def test_army_visible_rank_bounds_survive_without_hidden_identity_or_arbitrary_labels():
    visible = [{'seat': 1, 'kind': 'NEVER_HIDDEN_IDENTITY', 'hidden': True, 'label': '>团长'},
               {'seat': 1, 'kind': 'unknown', 'hidden': True, 'label': '>排长或雷'},
               {'seat': 1, 'kind': 'unknown', 'hidden': True, 'label': 'NEVER_ARBITRARY_LABEL'}]
    before = army_state(0, visible, 'engineer')
    before.update(cols=3, cells=visible)
    after = army_state(1, visible, 'NEVER_OPPONENT_VIEW', True)
    after['public_view']['cols'] = 3
    result = _summary(episode('army', [row(0, before, after)]),
                      include_trace=True, game_context=context('army', True))
    event = result['trace_sample'][0]
    expected = [{'index': 0, 'rank_above': 'regiment', 'or_mine': False},
                {'index': 1, 'rank_above': 'platoon', 'or_mine': True}]
    assert event['before']['visible_bounds'] == expected
    assert event['public_result']['visible_bounds'] == expected
    assert event['before']['board_rows'] == ['1:? 1:? 1:?']
    assert 'NEVER_' not in json.dumps(result)


def test_budget_omits_whole_records_and_reports_exact_ranges():
    trace = [row(i, {'observation': {'whole_value': str(i) + ':' + 'x' * 500}},
                 {'observation': {'whole_value': str(i + 1) + ':' + 'y' * 500}, 'done': i == 29}) for i in range(30)]
    source = episode('generic_fixture', trace)
    result = _summary(source, include_trace=True, game_context=context('generic_fixture'))
    sampling = result['trace_sampling']
    omitted = {i for a, b in sampling['omitted_ranges'] for i in range(a, b + 1)}
    assert omitted == set(range(30)) - set(sampling['retained_indices'])
    assert sampling['omitted_count'] == len(omitted)
    assert sampling['last_source_step_covered'] and 29 in sampling['retained_indices']
    assert chars(result) <= 8000 and chars(result) == sampling['serialized_chars']
    for event in result['trace_sample']:
        i = event['trace_index']
        assert event['before']['observation']['whole_value'] == str(i) + ':' + 'x' * 500
        assert event['after']['observation']['whole_value'] == str(i + 1) + ':' + 'y' * 500
    assert json.loads(json.dumps(result)) == result


def test_global_pack_caps_serialized_feedback_and_counts_original_omissions():
    trace = [row(i, {'observation': 'x' * 450}, {'observation': 'y' * 450, 'done': i == 19}) for i in range(20)]
    summary = _summary(episode('fixture', trace), include_trace=True, game_context=context('fixture'))
    summaries = [dict(deepcopy(summary), episode_id='episode-' + str(i), origin='training') for i in range(12)]
    original = deepcopy(summaries)
    pack = fit_pack(summaries)
    assert len(pack) == 9 and chars(pack) <= 32000
    for item in pack:
        s = item['trace_sampling']
        assert s['omitted_count'] + len(s['retained_indices']) == 20
        assert chars(item) <= s['character_budget'] and chars(item) == s['serialized_chars']
        assert s['last_source_step_covered']
    assert summaries == original


def test_pack_reallocates_unused_shares_to_whole_terminal_events():
    # One event fits the per-episode cap but cannot fit a ninth of 32k.
    trace = [row(0, {'observation': 'x' * 1500}, {'observation': 'y' * 1500, 'done': True})]
    summary = _summary(episode('fixture', trace), include_trace=True, game_context=context('fixture'))
    assert 3553 < chars(summary) < 8000
    sources = [dict(deepcopy(summary), episode_id=str(i)) for i in range(9)]
    pack = fit_pack(sources)
    assert chars(pack) <= 32000
    assert any(item['trace_sample'] for item in pack)
    assert any(item['trace_sampling']['empty_event_evidence'] for item in pack)
    for item in pack:
        sampling = item['trace_sampling']
        assert chars(item) == sampling['serialized_chars']
        assert chars(item) <= sampling['character_budget'] <= 8000
        if item['trace_sample']:
            assert sampling['last_source_step_covered']
            assert not sampling['empty_event_evidence']
            assert 'causal_evidence_limit' not in sampling
        else:
            assert sampling['omitted_count'] == 1
            assert 'do not establish causes' in sampling['causal_evidence_limit']
    # Current three-record A/B budgets and event membership do not change.
    three = fit_pack(sources[:3])
    assert [item['trace_sample'] for item in three] == [item['trace_sample'] for item in sources[:3]]
    assert all(item['trace_sampling']['character_budget'] == 8000 for item in three)
    assert all(item['trace_sampling']['omitted_count'] == 0 for item in three)


def test_gomoku_midgame_missed_win_is_preserved_even_when_final_score_is_win():
    empty = [None] * 81
    threat = deepcopy(empty)
    for index in (10, 11, 12, 13):
        threat[index] = {'seat': 0, 'kind': 'stone'}
    catalog = [{'id': 'place:' + str(i), 'label': str(i)} for i in range(81)]
    trace = []
    for i in range(30):
        before = {'cells': threat if i == 7 else empty, 'cols': 9, 'rows': 9, 'turn': 0}
        after = {'cells': empty, 'cols': 9, 'rows': 9, 'turn': 1, 'done': i == 29, 'score': 100 if i == 29 else 50}
        trace.append(row(i, before, after, 'place:70', criteria=catalog))
    result = _summary(episode('gomoku', trace, 100), include_trace=True, game_context=context('gomoku'))
    assert 7 in result['trace_sampling']['retained_indices']
    event = next(event for event in result['trace_sample'] if event['trace_index'] == 7)
    assert 'missed_immediate_win' in event['selection_reasons']
    assert len(event['before']['board_rows']) == 9
    assert result['observation_context']['legal_actions'] == [a['id'] for a in catalog]
    assert result['trace_sampling']['last_source_step_covered']


def test_gomoku_feedback_matches_real_runtime_board_characters_without_claiming_schema():
    from game_harness.games import get_game
    session = get_game('gomoku').create(0, {'gomoku_size': 9, 'max_steps': 81})
    try:
        session.step('place:0')
        session.step('place:10')
        state = session.snapshot()
    finally:
        session.close()
    projected = compact_state(state, 'gomoku')
    flattened = ''.join(projected['board_rows']).replace(' ', '')
    assert flattened == 'X' + '.' * 9 + 'O' + '.' * 70
    assert state['cells'][0]['seat'] == 0 and state['cells'][10]['seat'] == 1
    assert projected['board_rows'] == state['observation']['board']
    summary = _summary(episode('gomoku', [row(0, state, state)]),
                       include_trace=True, game_context=context('gomoku'))
    assert 'X black, O white' in summary['observation_context']['board_encoding']
    assert 'not the player input schema' in summary['feedback_view_note']


def test_tetris_keeps_all_board_rows_including_bottom_and_hidden_spawn_rows():
    board = [{'y': i, 'cells': 'I.........' if i == 19 else '..........'} for i in range(-2, 20)]
    state = {'game_id': 'tetris', 'observation': {'board': {'rows': board}, 'pieceId': 1,
             'active': {'type': 'T', 'x': 3, 'y': -2, 'rotation': 0, 'cells': [{'x': 4, 'y': -2}]},
             'width': 10, 'height': 20, 'hiddenRows': 2}}
    result = _summary(episode('tetris', [row(0, state, state)]), include_trace=True, game_context=context('tetris'))
    before = result['trace_sample'][0]['before']
    assert before['board_first_y'] == -2 and len(before['board_rows']) == 22
    assert before['board_rows'][-1] == 'I.........'


def test_repeated_hard_drop_on_different_tetris_pieces_is_not_a_loop():
    states = [{'observation': {'pieceId': i, 'active': {'type': 'T', 'x': 3, 'y': -2},
                              'board': {'rows': [{'y': 19, 'cells': 'I' * i + '.' * (10 - i)}]}}}
              for i in range(3)]
    trace = [row(i, states[i], states[i + 1], 'hard-drop:100') for i in range(2)]
    result = _summary(episode('tetris', trace), include_trace=True, game_context=context('tetris'))
    tags = result['trace_sample'][1]['selection_reasons']
    assert 'repeated_action' in tags
    assert 'first_loop' not in tags and 'first_recurrence' not in tags


def test_unknown_oversized_event_is_wholly_omitted_and_terminal_coverage_is_false():
    trace = [row(0, {'observation': 'x' * 20000}, {'observation': 'y' * 20000, 'done': True})]
    result = _summary(episode('unknown_fixture', trace), include_trace=True, game_context=context('unknown_fixture'))
    assert result['trace_sample'] == []
    assert result['trace_sampling']['omitted_ranges'] == [[0, 0]]
    assert result['trace_sampling']['omitted_count'] == 1
    assert result['trace_sampling']['empty_event_evidence']
    assert not result['trace_sampling']['last_source_step_covered']
    assert chars(result) <= 8000


def test_legacy_mode_retains_original_first_two_last_eight_and_string_excerpt():
    trace = [row(i, platform_state(0), platform_state(1), 'right:1000') for i in range(30)]
    result = _summary(episode('platformer', trace), include_trace=True,
                      game_context=context('platformer'), feedback_format=LEGACY_FORMAT)
    assert result['trace_sampling']['retained_indices'] == [0, 1, *range(22, 30)]
    assert result['trace_sampling']['serialized_sample_truncated']
    assert set(result['trace_sample']) == {'head_excerpt', 'tail_excerpt', 'truncated'}
    assert len(result['trace_sample']['head_excerpt']) == len(result['trace_sample']['tail_excerpt']) == 4000
