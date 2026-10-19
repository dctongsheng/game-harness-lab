"""Protocol boundaries; HTTP/model outputs are fixtures, not capability evidence."""
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import sys

import httpx
import pytest

pytest.importorskip('PIL')
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import run_component_experiment as runner
import report_component_experiment as reporter
from game_harness.components import TETRIS_COMPONENTS, TEXT_COMPONENTS
from game_harness.games import get_game
from game_harness.providers import Settings
from game_harness.vision_provider import VisionDecisionProvider

SETTINGS = Settings('http://127.0.0.1:1/v1/systemone', 'fixture-decision', 1,
                    'https://fixture.invalid/v1', 'fixture-coach', 'never-publish-this-fixture-key', 1)


class Health:
    def health(self):
        return {'ok': True, 'model': SETTINGS.decision_model, 'request_think_mode': 'never'}


class Providers:
    def __init__(self, fail_at=None):
        self.requests, self.fail_at = [], fail_at
    def __call__(self, stage, archive_dir, images):
        def response(request):
            value = json.loads(request.content)
            self.requests.append((stage, value))
            if len(self.requests) == self.fail_at:
                raise httpx.ConnectError('fixture connection interrupted')
            criteria = value['questions']['primary_action']['criteria']
            chosen = 'target:0:0' if 'target:0:0' in criteria else 'hard-drop:100'
            return httpx.Response(200, json={'model': SETTINGS.decision_model,
                'answers': {'primary_action': {'choice': chosen, 'confidence': 1,
                    'probabilities': {key: float(key == chosen) for key in criteria},
                    'details': {'thought': {'used': False, 'tokens': 0}}}}, 'usage': {'total_tokens': 10}})
        return VisionDecisionProvider(SETTINGS, enabled=True, archive_dir=archive_dir,
            image_store=images, client=httpx.Client(transport=httpx.MockTransport(response)))


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    hashes = {'pyproject.toml': hashlib.sha256((ROOT / 'pyproject.toml').read_bytes()).hexdigest()}
    monkeypatch.setattr(runner, 'code_hashes', lambda: hashes)
    monkeypatch.setattr(runner, 'prior_tetris_seeds', lambda root: ({1, 2, 3}, []))
    source = {'game_id': 'tetris', 'game_context': get_game('tetris').context({'max_steps': 80}),
        'brief': {'goal': 'Native points only'}, 'decision_model': SETTINGS.decision_model, 'think_mode': 'never',
        'candidate': {'decision_instructions': 'Choose native inputs', 'strategy_notes': 'Fixture'},
        'tetris_context_config': {'schema': 'tetris-context-v3', 'goal_instructions': 'Fixture full goal prompt'}}
    source_path = tmp_path / 'v8.json'; source_path.write_text(json.dumps(source))
    manifest = runner.prepare(identity='component-test', parent_path=source_path, root=tmp_path / 'experiments',
                              data_root=tmp_path, settings=SETTINGS)
    return tmp_path / 'experiments/component-test', manifest


def experiment(path, **kwargs):
    return runner.ComponentExperiment(path, settings=SETTINGS, decision=Health(), **kwargs)


def test_fresh_partition_and_fixed_orders():
    plan = runner.scenario_plan(9)
    all_seeds = runner.scenario_seeds(plan)
    assert len(all_seeds) == 20 and len(set(all_seeds)) == 20
    assert not set(runner.scenario_seeds(runner.scenario_plan(9, all_seeds))) & set(all_seeds)
    assert plan == runner.scenario_plan(9)
    assert len(plan['evaluation']) == 8 and len(plan['promotion']) == 4
    for position in range(4):
        assert Counter(row['order'][position] for row in plan['promotion']) == dict.fromkeys(('parent', *runner.ARMS), 1)


def test_manifest_bounds_and_identical_initial_components(prepared):
    path, m = prepared
    assert set(m['parent']['candidate']) == set(TETRIS_COMPONENTS)
    assert 'tetris_context_config' not in m['parent']
    assert m['arms']['text']['mutable_components'] == list(TEXT_COMPONENTS)
    assert m['arms']['components']['mutable_components'] == list(TETRIS_COMPONENTS)
    assert m['arms']['experience']['experience'] is True
    assert m['constraints']['all_arms_vision'] is True
    assert m['budget']['completed_episodes_max'] == 78
    assert m['promotion_gate']['minimum_mean_gain'] == 100
    assert 'never-publish-this-fixture-key' not in (path / 'manifest.json').read_text()
    assert 'bootstrap' in m['constraints']['experience_sources']


def test_manifest_or_code_drift_fails_before_models(prepared, monkeypatch):
    path, m = prepared
    monkeypatch.setattr(runner, 'code_hashes', lambda: {})
    with pytest.raises(runner.ExperimentError, match='Source changed'):
        experiment(path)
    monkeypatch.setattr(runner, 'code_hashes', lambda: m['code_hashes'])
    m['reflection_rounds'] = 2
    (path / 'manifest.json').write_text(json.dumps(m))
    with pytest.raises(runner.ExperimentError, match='Manifest changed'):
        experiment(path)


def test_missing_development_blocks_both_heldout_phases(prepared):
    path, _ = prepared
    with experiment(path) as run:
        with pytest.raises(runner.ExperimentError, match='run-develop'):
            run.run_evaluation()
        with pytest.raises(runner.ExperimentError, match='run-develop'):
            run.run_promotion()


@pytest.mark.skipif(not shutil.which('node'), reason='Native Tetris needs Node')
def test_native_episode_images_receipts_and_recovery_are_audited(prepared):
    path, m = prepared
    providers = Providers()
    with experiment(path, provider_factory=providers) as run:
        episode = run.episode('bootstrap', 'eval-00', run.parent, m['scenarios']['bootstrap']['train'][0], 'search_train')
        count = len(providers.requests)
        assert run.episode('bootstrap', 'eval-00', run.parent, episode['seed'], 'search_train') == episode
        assert len(providers.requests) == count
        assert episode['runtime_profile'] == 'tetris-components-v1'
        assert run.verify_episode(episode, run.parent)['verified_calls'] == count
        assert all(payload['think_mode'] == 'never' and len(payload['images']) == 1 for _, payload in providers.requests)
        assert {payload['model'] for _, payload in providers.requests} == {SETTINGS.decision_model}
    result = reporter.report(path, experiment_factory=experiment)
    assert result['status'] == 'incomplete'
    assert result['audit']['completed_episodes_replayed'] == 1
    assert result['audit']['verified_action_and_goal_calls'] == count
    request = next((path / 'calls').glob('*/*/*/*/request.json'))
    value = runner.load(request); value['instructions'] += ' tamper'
    request.write_text(json.dumps(value))
    with pytest.raises(runner.ExperimentError, match='transport'):
        reporter.report(path, experiment_factory=experiment)


@pytest.mark.skipif(not shutil.which('node'), reason='Native Tetris needs Node')
def test_failed_attempt_never_scored_and_explicit_retry_retains_old_evidence(prepared):
    path, m = prepared
    seed = m['scenarios']['bootstrap']['train'][0]
    with experiment(path, provider_factory=Providers(fail_at=2)) as run:
        with pytest.raises(runner.ExperimentError, match='Incomplete'):
            run.episode('bootstrap', 'eval-00', run.parent, seed, 'search_train')
    with experiment(path, provider_factory=Providers()) as run:
        with pytest.raises(runner.ExperimentError, match='retry-failed'):
            run.episode('bootstrap', 'eval-00', run.parent, seed, 'search_train')
    with experiment(path, retry_failed=True, provider_factory=Providers()) as run:
        completed = run.episode('bootstrap', 'eval-00', run.parent, seed, 'search_train')
        assert completed['experiment_attempt'] == 'attempt-02'
    result = reporter.report(path, experiment_factory=experiment)
    assert result['audit']['completed_episodes_replayed'] == 1
    assert len(result['incomplete_attempts']) == 1
    assert (path / 'episodes/bootstrap/eval-00/attempt-01/failed.json').exists()


def test_phase_statistics_use_pairs_and_never_select_formal_winner(prepared, monkeypatch):
    path, m = prepared
    with experiment(path) as run:
        frozen = {'parent': run.parent, **{arm: run.harness(run.parent['candidate'], arm) for arm in runner.ARMS}}
        runner.freeze_file(path / 'frozen_harnesses.json', frozen)
        runner.freeze_file(path / 'development_result.json', {'status': 'completed', 'frozen_harnesses_hash': runner.digest(frozen),
            'experience_status': 'NO_ENTRY', 'searches': {arm: {'outcome': {'gepa': {}}} for arm in runner.ARMS}})
        seen = []
        def fake_episode(stage, trial, harness, seed, purpose, **kw):
            seen.append((stage, trial, seed))
            # All below parent => no approval. These are explicit fixture games.
            points = 200 if stage.endswith('-parent') else 100 if stage.endswith('-text') else 0
            return {'episode_id': trial, 'score': points, 'steps': 0, 'trace': [],
                    'final_state': {'metrics': {'lines': points // 100}}, 'termination_reason': 'step_limit'}
        monkeypatch.setattr(run, 'episode', fake_episode)
        evaluation = run.run_evaluation()
        assert len(seen) == 24
        assert evaluation['comparisons']['components-text']['mean_gain'] == -100
        promotion = run.run_promotion()
        assert len(seen) == 40
        assert all(job['status'] == 'retained' for job in promotion['approvals'].values())
        assert not (path / 'approved_harnesses').exists()


def test_freeze_recovers_only_matching_reconstruction(tmp_path):
    path = tmp_path / 'result.json'
    runner.write_new(path, {'audited': True})
    assert runner.freeze_file(path, {'audited': True}) == {'audited': True}
    assert runner.checked_file(path) == {'audited': True}
    with pytest.raises(runner.ExperimentError, match='differs'):
        runner.freeze_file(path, {'audited': False})


def test_real_gepa_calls_are_not_cross_arm_cached_and_no_entry_is_explicit(prepared, monkeypatch):
    path, m = prepared
    class Coach:
        settings = SETTINGS
        calls = []
        def complete(self, messages, purpose='reflection'):
            assert purpose == 'reflection'
            return json.dumps(json.loads(messages[1]['content'])['parent_candidate'])
    seen = []
    with experiment(path, coach=Coach()) as run:
        def fixture_episode(stage, trial, harness, seed, purpose, **kwargs):
            seen.append((stage, trial, seed, purpose))
            return {'episode_id': 'fixture-' + stage + '-' + trial, 'harness_id': harness['id'], 'seed': seed,
                'purpose': purpose, 'game_id': 'tetris', 'game_context': m['game_context'],
                'score': 0, 'steps': 0, 'termination_reason': 'step_limit', 'model': SETTINGS.decision_model,
                'trace': [], 'final_state': {'score': 0, 'metrics': {'lines': 0}},
                'policy_hashes': {'candidate': runner.digest(harness['candidate'])}, 'runtime_profile': runner.PROFILE,
                'experiment_attempt': 'attempt-01'}
        monkeypatch.setattr(run, 'episode', fixture_episode)
        result = run.run_develop()
        assert result['status'] == 'completed' and result['experience_status'] == 'NO_ENTRY'
        assert result['experience_count'] == 0
        assert set(result['searches']) == set(runner.ARMS)
        assert {stage for stage, _, _, _ in seen} >= {'bootstrap', *runner.ARMS}
        assert all(search['outcome']['gepa']['engine'] == 'gepa.optimize' for search in result['searches'].values())
        assert all(search['experience_hashes'] == [] for search in result['searches'].values())
        count = len(seen)
        assert run.run_develop() == result and len(seen) == count
