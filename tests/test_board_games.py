"""MIT board rules and information boundaries; no real model or network calls."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from game_harness.games.army import ArmyAdapter, ArmySession
from game_harness.games.gomoku import GomokuAdapter, GomokuSession
from game_harness.providers import digest
from game_harness.runtime import run_episode


pytestmark = pytest.mark.skipif(shutil.which('node') is None, reason='Board rules require Node.js')
GAMES = Path(__file__).parents[1] / 'src/game_harness/games'
BRIDGE = GAMES / 'board_bridge.cjs'


def node_json(code):
    prefix = "const {army,gomoku,projectArmy,createController}=require(process.argv[1]);\n"
    result = subprocess.run(['node', '-e', prefix + code, str(BRIDGE)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


SPARSE = '''
function sparse(pieces, turn=0) {
  const state=army.create({},'SYNTHETIC-PRIVATE-SEED');
  state.phase='playing'; state.ready=[true,true]; state.turn=turn;
  state.board=Array(60).fill(null);
  for(const [index,seat,kind] of [[1,0,'flag'],[58,1,'flag'],...pieces]) {
    state.board[index]={id:`piece-${seat}-${index}`,seat,kind,moved:false};
  }
  state.positions=[`${turn}|${state.board.map(p=>p?`${p.seat}:${p.kind}`:'.').join(',')}`];
  return state;
}
'''


def keys(value):
    if isinstance(value, dict):
        return set(value) | set().union(*(keys(v) for v in value.values()))
    if isinstance(value, list):
        return set().union(*(keys(v) for v in value))
    return set()


def test_vendored_sources_and_license_match_recorded_hashes():
    root = GAMES / 'vendor/arena-board'
    source = json.loads((root / 'SOURCE.json').read_text())
    assert source['license'] == 'MIT'
    assert source['commit'] == '4e2bda1d6c9b2545837761e2c7d19e2ce546aa9d'
    for filename, record in source['copied_files'].items():
        assert hashlib.sha256((root / filename).read_bytes()).hexdigest() == record['sha256']
        assert record['modified'] is False
    assert hashlib.sha256((root / 'LICENSE').read_bytes()).hexdigest() == source['license_sha256']
    assert 'Copyright (c) 2026 Dougo' in (root / 'LICENSE').read_text()
    assert 'Copyright (c) 2026 Dougo' in BRIDGE.read_text()


@pytest.mark.parametrize('size,count', [(9, 81), (15, 225)])
def test_gomoku_complete_empty_intersections_and_occupied_rejection(size, count):
    with GomokuSession(seed=0, size=size) as game:
        initial = game.snapshot()
        assert initial['rows'] == initial['cols'] == size
        assert len(game.candidates()) == count
        assert {c['id'] for c in game.candidates()} == {f'place:{n}' for n in range(count)}
        after = game.step('place:0')
        assert after['step'] == 1 and after['cells'][0]['seat'] == 0
        assert len(game.candidates()) == count - 1
        assert 'place:0' not in {c['id'] for c in game.candidates()}
        for invalid in ('place:0', 'place:-1', 'place:01', f'place:{count}', 'ready'):
            with pytest.raises(ValueError):
                game.step(invalid)
            assert game.snapshot() == after


@pytest.mark.parametrize('black', [[0, 1, 2, 3, 4], [0, 9, 18, 27, 36],
                                   [0, 10, 20, 30, 40], [8, 16, 24, 32, 40]])
def test_gomoku_five_in_all_four_directions_is_terminal_win(black):
    with GomokuSession(seed=0, max_steps=81) as game:
        for index, point in enumerate(black):
            game.step(f'place:{point}')
            if index < 4:
                game.step(f'place:{72 + index * 2}')
        final = game.snapshot()
        assert final['done'] and not final['truncated']
        assert final['result'] == 'win' and final['score'] == 100
        assert final['reason'] == 'FIVE_IN_ROW'
        assert set(final['winning_line']) == set(black)
        assert game.candidates() == []
        with pytest.raises(RuntimeError):
            game.step('place:5')


def test_gomoku_overline_counts_and_candidate_white_records_loss():
    with GomokuSession(seed=1, max_steps=81) as game:
        for index in [0, 80, 1, 78, 2, 76, 4, 74, 5, 72, 3]:
            game.step(f'place:{index}')
        final = game.snapshot()
        assert final['candidate_seat'] == 1 and final['winner'] == 0
        assert final['result'] == 'loss' and final['score'] == 0
        assert len(final['winning_line']) == 6


def test_full_gomoku_board_is_real_draw():
    black, white = [], []
    for index in range(81):
        (black if (index // 9 * 2 + index % 9) % 4 < 2 else white).append(index)
    with GomokuSession(seed=0, max_steps=81) as game:
        for i, point in enumerate(black):
            game.step(f'place:{point}')
            if i < len(white):
                game.step(f'place:{white[i]}')
        final = game.snapshot()
        assert final['step'] == 81 and final['reason'] == 'BOARD_FULL'
        assert final['result'] == 'draw' and final['score'] == 50 and not final['truncated']


@pytest.mark.parametrize('constructor', [ArmySession, GomokuSession])
def test_budget_cutoff_is_neutral_and_never_reported_as_draw(constructor):
    with constructor(seed=0, max_steps=1) as game:
        final = game.step(game.candidates()[0]['id'])
        assert final['done'] and final['truncated']
        assert final['score'] == 50 and final['result'] is None and final['winner'] is None
        assert final['reason'] == 'step_limit' and final['score_kind'] == 'cutoff_neutral'
        assert game.candidates() == []


def test_army_initialization_is_prearranged_playing_with_safe_private_and_public_views():
    with ArmySession(seed=314159, max_steps=10) as game:
        state = game.snapshot()
        assert state['phase'] == 'playing' and state['step'] == 0
        assert state['rows'] == 12 and state['cols'] == 5 and len(state['cells']) == 60
        assert state['turn'] == state['viewer_seat'] == state['observation']['yourSeat'] == 0
        assert state['candidate_seat'] == 1 and state['policy_role'] == 'opponent'
        own = [p for p in state['cells'] if p and p['seat'] == 0]
        enemy = [p for p in state['cells'] if p and p['seat'] == 1]
        assert len(own) == len(enemy) == 25
        assert all(p['kind'] != 'unknown' for p in own)
        assert all(p['kind'] == 'unknown' and p['hidden'] for p in enemy)
        assert all(p is None or p['hidden'] and p['kind'] == 'unknown' for p in state['public_view']['cells'])
        assert 'legalActions' not in keys(state['public_view'])
        assert not keys(state) & {'seed', 'rng', 'intel', 'positions', 'actions', 'board'}
        assert all(action['id'].startswith('move:') for action in game.candidates())
        assert not {'ready'} & {action['id'] for action in game.candidates()}
        assert all(state['cells'][position] is None for position in state['terrain']['camps'])


def test_army_complete_catalog_matches_unmodified_rules_engine():
    expected = node_json('''
let state=army.create({},'24'); state=army.apply(state,0,'ready'); state=army.apply(state,1,'ready');
console.log(JSON.stringify(army.legalActions(state,state.turn).map(a=>({id:a.id,label:a.label}))));
''')
    with ArmySession(seed=24) as game:
        assert game.candidates() == expected
        before = game.snapshot()
        for invalid in ['ready', 'swap:0:1', 'move:0:59', 'move:-1:2']:
            if invalid not in {action['id'] for action in game.candidates()}:
                with pytest.raises(ValueError):
                    game.step(invalid)
                assert game.snapshot() == before


def test_army_seed_changes_arrangement_and_same_seed_actions_replay_exactly():
    with ArmySession(seed=20, max_steps=6) as left, ArmySession(seed=20, max_steps=6) as right, ArmySession(seed=22) as other:
        assert left.snapshot() == right.snapshot()
        assert left.snapshot()['cells'] != other.snapshot()['cells']
        for _ in range(6):
            assert left.candidates() == right.candidates()
            action = left.candidates()[0]['id']
            assert left.step(action) == right.step(action)
        assert left.snapshot()['truncated']


def test_wrong_seat_and_fake_actions_do_not_advance_controller():
    result = node_json('''
const answers=[];
for(const game of ['army','gomoku']) {
 const c=createController(), before=c.request({op:'init',game,seed:0,max_steps:3,gomoku_size:9});
 for(const command of [{op:'step',action:before.candidates[0].id,seat:1},{op:'step',action:'invalid',seat:0}]) {
  let rejected=false;try{c.request(command);}catch{rejected=true;}
  answers.push(rejected && JSON.stringify(c.request({op:'snapshot'}))===JSON.stringify(before));
 }
}
console.log(JSON.stringify(answers));
''')
    assert result == [True, True, True, True]


def test_army_hidden_identity_changes_do_not_change_player_or_public_projection():
    result = node_json(SPARSE + '''
const mine=sparse([[47,0,'platoon'],[52,1,'mine'],[5,0,'engineer'],[50,1,'platoon']]);
const division=sparse([[47,0,'platoon'],[52,1,'division'],[5,0,'engineer'],[50,1,'platoon']]);
console.log(JSON.stringify({a:projectArmy(mine,0),b:projectArmy(division,0)}));
''')
    assert result['a'] == result['b']
    assert 'SYNTHETIC-PRIVATE-SEED' not in json.dumps(result)
    assert result['a']['cells'][52]['kind'] == 'unknown'


def test_army_terminal_projection_does_not_reveal_hidden_ranks():
    result = node_json(SPARSE + '''
const s=sparse([[5,0,'engineer'],[50,1,'platoon']]);s.phase='finished';s.result={winner:0,reason:'FIXTURE'};
console.log(JSON.stringify({direct:army.view(s,0).cells[50],safe:projectArmy(s,0)}));
''')
    assert result['direct']['kind'] == 'platoon'  # Upstream finished-view behavior being isolated.
    assert result['safe']['cells'][50]['kind'] == 'unknown'
    assert result['safe']['public_view']['cells'][50]['kind'] == 'unknown'
    assert result['safe']['public_view']['result']['winner'] == 0


@pytest.mark.parametrize('attacker,defender,survivor', [
    ('engineer', 'mine', 'engineer'), ('general', 'mine', 'mine'), ('bomb', 'mine', None),
    ('company', 'company', None), ('general', 'company', 'general'), ('company', 'general', 'general')])
def test_army_combat_uses_frozen_rules_and_justified_information(attacker, defender, survivor):
    result = node_json(SPARSE + f'''
const s=sparse([[47,0,{json.dumps(attacker)}],[52,1,{json.dumps(defender)}],[5,0,'engineer'],[50,1,'platoon']]);
const n=army.apply(s,0,'move:47:52');
console.log(JSON.stringify({{survivor:n.board[52]?.kind??null,ply:n.ply,turn:n.turn,attacker:projectArmy(n,0),defender:projectArmy(n,1)}}));
''')
    assert result['survivor'] == survivor and result['ply'] == 1 and result['turn'] == 1
    if attacker == 'general' and defender == 'mine':
        knowledge = result['attacker']['cells'][52]
        assert knowledge['kind'] == 'unknown' and '或雷' in knowledge['label']
    if survivor == attacker:
        assert result['defender']['cells'][52]['kind'] == attacker


def test_army_railway_camp_and_no_jump_rules_are_not_filtered_by_strategy():
    result = node_json(SPARSE + '''
const ids=(s)=>army.legalActions(s,s.turn).map(a=>a.id);
console.log(JSON.stringify({
 engineer:ids(sparse([[5,0,'engineer'],[50,1,'platoon']])),
 general:ids(sparse([[5,0,'general'],[50,1,'platoon']])),
 block:ids(sparse([[5,0,'general'],[15,1,'platoon'],[50,1,'platoon']])),
 camps:ids(sparse([[11,0,'company'],[12,0,'engineer'],[13,1,'commander'],[50,1,'platoon']]))
}));
''')
    assert 'move:5:19' in result['engineer'] and 'move:5:19' not in result['general']
    assert 'move:5:15' in result['block'] and 'move:5:20' not in result['block']
    assert 'move:11:17' in result['camps'] and 'move:12:13' not in result['camps']


def test_army_flag_capture_no_moves_and_repetition_terminal_rules():
    result = node_json(SPARSE + '''
const captured=army.apply(sparse([[53,0,'bomb'],[50,1,'platoon']]),0,'move:53:58');
const trapped=army.apply(sparse([[47,0,'engineer'],[52,1,'mine']]),0,'move:47:52');
let repeated=sparse([[5,0,'company'],[50,1,'company']]);
for(let n=0;n<2;n++)for(const move of ['move:5:6','move:50:51','move:6:5','move:51:50'])repeated=army.apply(repeated,repeated.turn,move);
console.log(JSON.stringify({captured:captured.result,trapped:trapped.result,repeated:repeated.result}));
''')
    assert result['captured'] == {'winner': 0, 'reason': 'FLAG_CAPTURED'}
    assert result['trapped'] == {'winner': 0, 'reason': 'NO_LEGAL_MOVES'}
    assert result['repeated'] == {'winner': None, 'reason': 'THREEFOLD_REPETITION'}


class FixtureDecision:
    def __init__(self):
        self.calls = []

    def decide(self, state, instructions, criteria, request_id):
        self.calls.append(deepcopy((state, instructions, criteria)))
        assert not keys(state) & {'seed', 'rng', 'intel', 'positions', 'actions'}
        assert state['viewer_seat'] == state['observation']['yourSeat'] == state['turn']
        return {'choice_id': criteria[0]['id'], 'model': 'synthetic-board-fixture', 'elapsed_ms': 0,
                'request_id': request_id, 'observation_revision': state['step'],
                'candidate_hash': digest(criteria), 'think_mode': 'never', 'think_used': False,
                'reasoning_tokens': 0, 'raw_response': {'fixture': True}}


@pytest.mark.parametrize('adapter', [ArmyAdapter(), GomokuAdapter()])
def test_both_actors_use_shared_decision_runtime_with_full_current_catalog(adapter):
    provider = FixtureDecision()
    result = run_episode(provider, {'id': 'h-fixture', 'candidate': {
        'decision_instructions': 'CANDIDATE_BOARD_FIXTURE', 'strategy_notes': 'fixture'}},
        episode_id='ep-board-fixture', seed=1, adapter=adapter, config={**adapter.defaults, 'max_steps': 4})
    assert result['infra_error'] is None and result['steps'] == 4 and result['score'] == 50
    assert [row['policy_role'] for row in result['trace']] == ['opponent', 'candidate', 'opponent', 'candidate']
    assert len(provider.calls) == 4
    for index, (state, instructions, criteria) in enumerate(provider.calls):
        assert ('CANDIDATE_BOARD_FIXTURE' in instructions) == (index % 2 == 1)
        assert result['trace'][index]['criteria'] == criteria
        assert result['trace'][index]['receipt']['candidate_hash'] == digest(criteria)
        assert state['candidates'] == criteria
        if adapter.id == 'army':
            assert all(piece is None or piece['kind'] == 'unknown' or not piece.get('hidden') for piece in state['cells'])
            assert len(criteria) > 1


def test_adapter_context_declares_source_scenario_and_hidden_feedback_contract():
    army = ArmyAdapter().context({'max_steps': 100})
    gomoku = GomokuAdapter().context({'gomoku_size': 9, 'max_steps': 81})
    for context in (army, gomoku):
        assert context['source']['license'] == 'MIT' and len(context['source']['commit']) == 40
        assert context['evaluation']['seed_modulus'] == 2
        assert context['score_spec']['minimum_mean_gain'] == 2
        assert context['score_spec']['cutoff_is_draw'] is False
        assert context['opponent_policy']['decision_instructions']
    assert army['information_policy'] == ArmyAdapter.information_policy
    assert army['information_policy']['seed_is_private'] is True
    assert army['config']['setup_optimization'] is False
    assert gomoku['config']['start_position'] == 'empty_board'
