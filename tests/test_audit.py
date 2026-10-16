"""Saved artifact audits use real rules replays, with synthetic provider evidence."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import shutil
import sqlite3

import pytest

from game_harness.games import get_game
from game_harness.providers import digest
from game_harness.runtime import run_episode
from game_harness.snake import SnakeGame, ACTION_CANDIDATES, RULES_VERSION


_spec = importlib.util.spec_from_file_location('audit_saved_run', Path(__file__).parents[1] / 'scripts/audit_saved_run.py')
audit_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(audit_module)
audit, AuditError = audit_module.audit, audit_module.AuditError
HARNESS = {'id': 'h-fixture', 'candidate': {'decision_instructions': 'fixture player', 'strategy_notes': 'fixture'}}


def receipt(before, action, criteria, episode_id):
    raw = {'model': 'fixture-model', 'answers': {'primary_action': {
        'choice': action, 'confidence': 1.0,
        'probabilities': {item['id']: float(item['id'] == action) for item in criteria},
        'details': {'thought': {'used': False, 'tokens': 0}}}}}
    return {'choice_id': action, 'model': 'fixture-model', 'elapsed_ms': 1.0,
            'raw_response': raw, 'response_hash': digest(raw), 'think_mode': 'never',
            'think_used': False, 'reasoning_tokens': 0,
            'candidate_hash': digest(criteria), 'observation_revision': before['step'],
            'request_id': f"{episode_id}:step:{before['step']}:{digest(before)[:16]}"}


class FixtureProvider:
    def decide(self, state, instructions, criteria, request_id):
        before = {key: value for key, value in state.items()
                  if key not in {'episode_id', 'observation_hash', 'candidates', 'remaining_steps'}}
        result = receipt(before, criteria[0]['id'], criteria, state['episode_id'])
        assert result['request_id'] == request_id
        return result


def modern_episode(game_id='snake', episode_id='modern'):
    adapter = get_game(game_id)
    config = {'board_size': 4, 'max_steps': 2 if game_id == 'snake' else 4}
    context = adapter.context(config)
    result = run_episode(FixtureProvider(), HARNESS, episode_id=episode_id, seed=0,
                         adapter=adapter, config=config)
    assert result['infra_error'] is None
    result['purpose'] = 'search_train'
    version = {**deepcopy(HARNESS), 'game_context': context, 'game_id': game_id,
               'decision_model': 'fixture-model'}
    return result, version


def legacy_episode():
    game = SnakeGame(size=4, seed=0, max_steps=1)
    before, episode_id = game.snapshot(), 'legacy'
    action = 'right'
    response = receipt(before, action, ACTION_CANDIDATES, episode_id)
    after = game.step(action)
    return {'episode_id': episode_id, 'harness_id': 'legacy-v1', 'config': {'board_size': 4, 'max_steps': 1},
            'seed': 0, 'rules_version': RULES_VERSION, 'steps': 1, 'score': game.score,
            'model': 'fixture-model', 'purpose': 'search_train', 'termination_reason': 'step_limit',
            'final_state': after, 'infra_error': None,
            'trace': [{'before': before, 'after': after, 'action': action,
                       'applied_action': after['direction'], 'receipt': response}]}


def save(root, episodes, versions=(), jobs=()):
    (root / 'episodes').mkdir(exist_ok=True)
    db = sqlite3.connect(root / 'registry.sqlite')
    db.execute('CREATE TABLE records (kind TEXT,id TEXT,created TEXT,body TEXT)')
    for kind, records in [('version', versions), ('job', jobs)]:
        for record in records:
            db.execute('INSERT INTO records VALUES (?,?,?,?)', (kind, record['id'], 'fixture', json.dumps(record)))
    db.commit()
    db.close()
    for episode in episodes:
        (root / 'episodes' / f"{episode['episode_id']}.json").write_text(json.dumps(episode))


def test_audit_replays_modern_and_legacy_snake_and_excludes_failed_evidence(tmp_path):
    episode, version = modern_episode()
    stopped = {'episode_id': 'stopped', 'game_id': 'unavailable-game', 'steps': 1,
               'purpose': 'search_validation', 'termination_reason': 'stopped'}
    save(tmp_path, [episode, legacy_episode(), stopped], [version])
    result = audit(tmp_path)
    assert result['episodes'] == 3 and result['audited_completed_episodes'] == 2
    assert result['steps'] == 3
    assert result['compatibility'] == {'legacy_snake_v1': 1, 'adapter': 1}
    assert result['games']['snake']['audited_completed_episodes'] == 2
    assert result['games']['unavailable-game']['excluded_incomplete_episodes'] == 1
    assert result['excluded_incomplete'][0]['valid_for_evaluation'] is False
    assert result['all_audited_actions_report_thinking_off'] is True
    assert result['models'] == ['fixture-model']


@pytest.mark.skipif(shutil.which('node') is None, reason='Xiangqi rules need Node.js')
def test_xiangqi_full_dynamic_catalog_and_both_actor_policies_replay(tmp_path):
    episode, version = modern_episode('xiangqi')
    save(tmp_path, [episode], [version])
    result = audit(tmp_path)
    assert result['games']['xiangqi']['actors'] == {'candidate': 2, 'opponent': 2}
    assert result['games']['xiangqi']['audited_steps'] == 4
    assert result['all_audited_states_replayed'] is True


@pytest.mark.parametrize('tamper', ['catalog', 'role', 'policy', 'context', 'score', 'final_board', 'raw_action'])
def test_tampering_completed_artifacts_cannot_produce_passing_audit(tmp_path, tamper):
    episode, version = modern_episode()
    if tamper == 'catalog':
        episode['trace'][0]['criteria'] = episode['trace'][0]['criteria'][:1]
    elif tamper == 'role':
        episode['trace'][0]['policy_role'] = 'opponent'
    elif tamper == 'policy':
        episode['trace'][0]['policy_hash'] = 'not-the-candidate'
    elif tamper == 'context':
        episode['game_context']['score_spec']['max'] = 999
    elif tamper == 'score':
        episode['score'] = 999
    elif tamper == 'final_board':
        episode['final_state']['snake'][0] = [99, 99]
    else:
        raw = episode['trace'][0]['receipt']['raw_response']
        raw['answers']['primary_action']['choice'] = 'right'
        episode['trace'][0]['receipt']['response_hash'] = digest(raw)
    save(tmp_path, [episode], [version])
    with pytest.raises(AuditError, match='modern'):
        audit(tmp_path)


def test_infra_error_is_never_treated_as_valid_game_score(tmp_path):
    result = {'episode_id': 'failed', 'game_id': 'snake', 'steps': 5, 'score': 1000,
              'purpose': 'search_validation', 'termination_reason': 'infra_error', 'infra_error': {'type': 'network'}}
    save(tmp_path, [result])
    summary = audit(tmp_path)
    assert summary['audited_completed_episodes'] == 0 and summary['steps'] == 0
    assert summary['games']['snake']['mean_score'] is None
    assert summary['all_audited_actions_report_thinking_off'] is False


def test_evaluation_cannot_spawn_recursive_optimization_job(tmp_path):
    episode, version = modern_episode()
    save(tmp_path, [episode], [version], [{'id': 'invalid-job', 'episode_id': episode['episode_id']}])
    with pytest.raises(AuditError, match='formal gameplay'):
        audit(tmp_path)
