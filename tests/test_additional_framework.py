"""Six-game orchestration and Army API privacy with local fixtures only."""
from copy import deepcopy
import json
import shutil

import httpx
import pytest
from fastapi.testclient import TestClient

from game_harness.app import create_app
from game_harness.games import game_catalog, get_game
from game_harness.games.army import ArmySession
from game_harness.providers import Settings, digest
from game_harness.service import DEFAULTS, Lab


BASE = {'decision_instructions': 'Fixture baseline policy', 'strategy_notes': 'Fixture baseline notes'}
CHILD = {'decision_instructions': 'Fixture improved policy', 'strategy_notes': 'Fixture improved notes'}
BRIEF = {key: 'Fixture ' + key for key in ('background', 'goal', 'rules', 'precautions', 'strategy')}


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    def reject(*args, **kwargs):
        pytest.fail('Additional framework tests must not make real HTTP requests')
    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', reject)


@pytest.fixture
def settings():
    return Settings('http://127.0.0.1:1/v1/systemone', 'fixture-decision', 1,
                    'https://fixture.invalid/v1', 'fixture-coach', 'fixture-private-key', 1)


@pytest.fixture
def lab(tmp_path, settings):
    instance = Lab(settings, tmp_path)
    yield instance
    if instance.worker:
        instance.worker.join(timeout=2)
    instance.store.db.close()


def fake_worker(lab, monkeypatch):
    def worker(config, scope):
        with lab.lock:
            lab.busy = False
            lab.status = 'fixture_complete'
    monkeypatch.setattr(lab, '_run', worker)


def start(lab, options):
    lab.start(options)
    lab.worker.join(timeout=2)
    assert not lab.worker.is_alive()


def seed_active(lab, game_id, **config):
    adapter = get_game(game_id)
    lab.config = {**DEFAULTS, **adapter.defaults, **config, 'game_id': game_id}
    lab.scope = 'fixture-' + game_id
    version = lab.version(BASE, lab.scope, {'type': 'bootstrap'}, brief=deepcopy(BRIEF),
                          game_context=adapter.context(lab.config))
    lab.store.activate(lab.scope, version['id'], None)
    return version


def save_episode(lab, version, identity, seed, purpose, score):
    result = {'episode_id': identity, 'seed': seed, 'harness_id': version['id'],
              'game_id': lab.config['game_id'], 'scope': version['scope'], 'config': deepcopy(lab.config),
              'purpose': purpose, 'score': score, 'steps': 8, 'termination_reason': 'step_limit',
              'model': 'fixture-decision', 'trace': [], 'latencies_ms': [], 'infra_error': None}
    lab.store.save_episode(result)
    return result


def all_keys(value):
    if isinstance(value, dict):
        return set(value) | set().union(*(all_keys(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(all_keys(item) for item in value))
    return set()


def test_catalog_registers_all_seven_games_with_their_own_scoring_contract(tmp_path, settings):
    expected = {'snake', 'xiangqi', 'army', 'gomoku', 'tetris', 'platformer', 'overcooked'}
    assert {game['id'] for game in game_catalog()} == expected
    app = create_app(tmp_path, settings)
    try:
        with TestClient(app) as client:
            response = client.get('/api/games')
            assert response.status_code == 200
            catalog = {game['id']: game for game in response.json()['games']}
        assert set(catalog) == expected
        assert len({game['score_spec']['id'] for game in catalog.values()}) == 7
        assert catalog['army']['information_policy']['seed_is_private'] is True
        assert not catalog['gomoku']['information_policy']
        assert catalog['platformer']['defaults']['promotion_games'] == 6
        assert catalog['gomoku']['defaults']['gomoku_size'] == 9
        assert catalog['overcooked']['defaults'] == {'max_steps': 240, 'overcooked_layout': 'cramped_room'}
        assert catalog['overcooked']['score_spec']['score_kind'] == 'native_sparse_reward'
    finally:
        app.state.lab.store.db.close()


def test_switching_restores_new_game_defaults_and_discards_foreign_custom_settings(lab, monkeypatch):
    fake_worker(lab, monkeypatch)
    for game_id, expected in [('army', {'max_steps': 100}),
                              ('gomoku', {'max_steps': 81, 'gomoku_size': 9}),
                              ('platformer', {'max_steps': 60, 'promotion_games': 6}),
                              ('tetris', {'max_steps': 80})]:
        start(lab, {'game_id': game_id})
        for key, value in expected.items():
            assert lab.config[key] == value
    start(lab, {'game_id': 'gomoku', 'gomoku_size': 15})
    assert lab.config['gomoku_size'] == 15
    start(lab, {'game_id': 'army'})
    assert 'gomoku_size' not in lab.config
    start(lab, {'game_id': 'gomoku'})
    assert lab.config['gomoku_size'] == 9
    with pytest.raises(ValueError, match='未知运行设置'):
        lab.start({'game_id': 'army', 'gomoku_size': 9})


def test_private_seed_is_fresh_on_entry_and_never_inherited_by_public_settings(lab, monkeypatch):
    fake_worker(lab, monkeypatch)
    lab.config['seed'] = 83451
    generated = []

    def private_seed(limit):
        generated.append(limit)
        return 1729415

    monkeypatch.setattr('game_harness.service.secrets.randbelow', private_seed)
    start(lab, {'game_id': 'army'})
    assert generated == [2**31]
    assert lab.config['seed'] == 1729415
    assert lab.store.get('metadata', 'last_run')['config']['seed'] == 1729415
    assert 'seed' not in lab.state()['settings']
    start(lab, {'game_id': 'snake'})
    assert generated == [2**31]
    assert lab.config['seed'] == DEFAULTS['seed']
    assert lab.state()['settings']['seed'] == DEFAULTS['seed']


@pytest.mark.parametrize('source,target,options,expected_seed', [
    ('snake', 'army', {'seed': 76901}, 76901),
    ('army', 'snake', {'seed': 98301}, 98301),
    ('army', 'army', {}, 1729415),
    ('snake', 'snake', {}, 1729415),
    ('snake', 'gomoku', {}, 1729415),
])
def test_seed_privacy_switch_preserves_explicit_and_compatible_reuse(lab, monkeypatch, source, target, options, expected_seed):
    fake_worker(lab, monkeypatch)
    lab.config = {**DEFAULTS, **get_game(source).defaults, 'game_id': source, 'seed': 1729415}

    def no_generation(*args):
        pytest.fail('Explicit seeds, same-game runs, and public switches must preserve existing behavior')

    monkeypatch.setattr('game_harness.service.secrets.randbelow', no_generation)
    start(lab, {'game_id': target, **options})
    assert lab.config['seed'] == expected_seed
    assert lab.store.get('metadata', 'last_run')['config']['seed'] == expected_seed


def test_identical_policy_text_never_shares_scope_or_approval_between_new_games(lab, monkeypatch):
    fake_worker(lab, monkeypatch)
    snapshots = {}
    for game_id in ('army', 'gomoku', 'tetris', 'platformer'):
        start(lab, {'game_id': game_id})
        version = lab.version(BASE, lab.scope, {'type': 'bootstrap'}, brief=BRIEF)
        lab.store.activate(lab.scope, version['id'], None)
        snapshots[game_id] = (lab.scope, version['id'])
    assert len({scope for scope, _ in snapshots.values()}) == 4
    assert len({identity for _, identity in snapshots.values()}) == 4
    with pytest.raises(ValueError, match='当前游戏'):
        lab.rollback(snapshots['army'][1])
    for game_id, (scope, version_id) in snapshots.items():
        start(lab, {'game_id': game_id})
        assert lab.scope == scope
        assert lab.store.active(scope)['id'] == version_id
        assert lab.state()['active_harness']['game_id'] == game_id


@pytest.mark.parametrize('pair_count,expected_status', [(4, 'retained'), (6, 'promoted')])
def test_platformer_requires_six_nonregressing_pairs_not_four(lab, monkeypatch, pair_count, expected_status):
    baseline = seed_active(lab, 'platformer', max_steps=8, train_games=1, validation_games=1,
                           promotion_games=pair_count)
    trigger = save_episode(lab, baseline, 'ep-platformer-source', 6, 'play', 100)
    job = lab.new_job(trigger, lab.config, lab.scope)

    def evolution(**kwargs):
        assert kwargs['game_context']['evaluation']['minimum_pairs'] == 6
        return {'candidate': deepcopy(CHILD), 'proposals': [], 'gepa': {'engine': 'fixture'}}

    def evaluation(provider, harness, config, seed, purpose, job_id):
        score = 100 if purpose == 'promotion_baseline' else 1200
        return save_episode(lab, harness, f'ep-{purpose}-{seed}', seed, purpose, score)

    monkeypatch.setattr('game_harness.evolution.evolve_harness', evolution)
    monkeypatch.setattr(lab, 'play', evaluation)
    lab.optimize(job, object(), object())
    saved = lab.store.get('job', job['id'])
    assert saved['status'] == expected_status
    assert saved['gate']['minimum_pairs'] == 6
    assert saved['gate']['minimum_mean_gain'] == 100
    assert saved['mean_gain'] == 1100 and len(saved['pairs']) == pair_count
    if pair_count == 4:
        assert lab.store.active(lab.scope)['id'] == baseline['id']
        assert lab.store.list('export') == []
    else:
        assert lab.store.active(lab.scope)['id'] == saved['candidate_id']
        assert lab.store.get('export', saved['candidate_id'])['game_id'] == 'platformer'
        assert sorted(pair['seed'] % 3 for pair in saved['pairs']) == [0, 0, 1, 1, 2, 2]


@pytest.mark.skipif(shutil.which('node') is None, reason='Army projection fixture requires Node.js')
def test_army_state_and_replay_apis_hide_seeds_private_views_and_receipts_without_mutating_local_evidence(tmp_path, settings):
    app = create_app(tmp_path, settings)
    lab = app.state.lab
    original_seed = 1729415
    baseline = seed_active(lab, 'army', max_steps=2, seed=original_seed)
    records = []
    with ArmySession(seed=original_seed, max_steps=2) as session:
        for step_index in range(2):
            before = session.snapshot()
            criteria = session.candidates()
            chosen = criteria[0]['id']
            after = session.step(chosen)
            records.append({'before': before, 'after': after, 'action': chosen,
                            'policy_role': before['policy_role'], 'actor': before['actor'], 'criteria': criteria,
                            'receipt': {'choice_id': chosen, 'candidate_hash': digest(criteria),
                                        'raw_response': {'PRIVATE_RESPONSE_DO_NOT_RENDER': True,
                                                         'probabilities': {item['id']: 0 for item in criteria}}}})
        final = session.snapshot()
    episode = {'episode_id': 'ep-army-private', 'seed': original_seed, 'harness_id': baseline['id'],
               'game_id': 'army', 'scope': lab.scope, 'config': deepcopy(lab.config),
               'game_context': get_game('army').context(lab.config), 'purpose': 'play',
               'score': final['score'], 'steps': final['step'], 'termination_reason': 'step_limit',
               'model': 'fixture-decision', 'trace': records, 'latencies_ms': [1, 2], 'infra_error': None,
               'final_state': final}
    lab.store.save_episode(episode)
    job = lab.new_job(episode, lab.config, lab.scope)
    lab.current_episode = {'episode_id': episode['episode_id'], 'game_id': 'army', 'snapshot': deepcopy(final)}
    lab.log('Private game fixture', 'fixture', detail={'seed': original_seed, 'before': records[0]['before'],
                                                     'criteria': records[0]['criteria'], 'raw_response': {'PRIVATE_RESPONSE_DO_NOT_RENDER': True}})
    path = tmp_path / 'episodes/ep-army-private.json'
    original_bytes = path.read_bytes()
    original_job = deepcopy(lab.store.get('job', job['id']))
    original_live = deepcopy(lab.current_episode)
    forbidden = {'seed', 'train_seeds', 'validation_seeds', 'promotion_seeds', 'evaluation_seed',
                 'criteria', 'raw_response', 'raw_request', 'probabilities', 'legalActions'}
    try:
        with TestClient(app) as client:
            state_response = client.get('/api/state')
            replay_response = client.get('/api/episodes/ep-army-private')
            assert state_response.status_code == replay_response.status_code == 200
            public_state, replay = state_response.json(), replay_response.json()
            assert not all_keys(public_state) & forbidden
            assert not all_keys(replay) & forbidden
            assert 'PRIVATE_RESPONSE_DO_NOT_RENDER' not in json.dumps([public_state, replay])
            current = public_state['current_episode']['snapshot']
            assert current['visibility'] == 'public_common_knowledge'
            assert 'observation' not in current and 'viewer_seat' not in current
            assert current['cells'] == final['public_view']['cells']
            assert current['render'] == final['public_view']
            for index, row in enumerate(replay['trace']):
                for field in ('before', 'after'):
                    frame = row[field]
                    assert 'observation' not in frame and 'viewer_seat' not in frame
                    assert frame['cells'] == records[index][field]['public_view']['cells']
                    assert all(piece is None or piece.get('kind') == 'unknown' or not piece.get('hidden')
                               for piece in frame['cells'])
            # A different currently selected game must not un-redact historical Army records.
            lab.config = {**DEFAULTS, 'game_id': 'snake', 'seed': 98301}
            other_selected = client.get('/api/state').json()
            historic = next(row for row in other_selected['episodes'] if row['episode_id'] == episode['episode_id'])
            assert not all_keys(historic) & forbidden
            assert other_selected['settings']['seed'] == 98301
        assert path.read_bytes() == original_bytes
        assert lab.store.load_episode(episode['episode_id']) == episode
        assert lab.store.get('job', job['id']) == original_job
        assert lab.current_episode == original_live
        assert lab.store.load_episode(episode['episode_id'])['trace'][0]['receipt']['raw_response']['PRIVATE_RESPONSE_DO_NOT_RENDER']
        assert lab.store.load_episode(episode['episode_id'])['seed'] == original_seed
        assert any(piece and piece['kind'] != 'unknown' for piece in episode['trace'][0]['before']['cells'])
    finally:
        lab.store.db.close()
