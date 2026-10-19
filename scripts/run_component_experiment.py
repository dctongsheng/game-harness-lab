#!/usr/bin/env python3
"""Preregistered component/experience experiment with real GEPA and Decision.

prepare makes no model requests. run-develop freezes all selected policies and a
bootstrap-training-only experience snapshot before either held-out phase runs.
Every evaluation callback actually plays; completed logical operations are reused
only when recovering an interrupted run. No production registry is modified.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import random
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'scripts'))
from run_feedback_experiment import (Experiment as BaseExperiment, ExperimentError, append,
    digest, experiment_path, load, now, public_settings, read_lines, snapshot_sources, write_new)
from run_tetris_context_experiment import prior_tetris_seeds, contrast_stats
from game_harness.components import TEXT_COMPONENTS, TETRIS_COMPONENTS, upgrade_tetris_candidate, validate_candidate
from game_harness.evolution import evolve_harness
from game_harness.experience import ExperienceLibrary, ExperienceScope, freeze_experiences
from game_harness.games import get_game
from game_harness.providers import Settings
from game_harness.runtime import run_episode
from game_harness.vision_provider import VisionDecisionProvider

ARMS = ('text', 'components', 'experience')
PROFILE = 'tetris-components-v1'
GAME_CONFIG = {'max_steps': 80}
DEFAULT_SEED = 2026101909


def code_hashes():
    files = list((ROOT / 'src').rglob('*.py')) + list((ROOT / 'scripts').glob('*.py')) + [ROOT / 'pyproject.toml']
    return {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(files)}


def runtime_dependencies():
    return {name: version(name) for name in ('Pillow', 'httpx', 'gepa')}


def scenario_plan(seed, excluded=()):
    rng, used = random.Random(seed), set(excluded)
    def take():
        number = rng.randrange(1, 2**31 - 1)
        while number in used:
            number = rng.randrange(1, 2**31 - 1)
        used.add(number)
        return number
    result = {name: {'train': [take(), take()], 'validation': [take(), take()]}
              for name in ('bootstrap', 'development')}
    # Balanced positions over the first six formal trials; remaining imbalance
    # is explicitly fixed before seeing any score.
    orders = [('text', 'components', 'experience'), ('components', 'experience', 'text'),
              ('experience', 'text', 'components'), ('experience', 'components', 'text'),
              ('components', 'text', 'experience'), ('text', 'experience', 'components')]
    result['evaluation'] = [{'id': f'evaluation-{i:02d}', 'seed': take(), 'order': list(orders[i % 6])} for i in range(8)]
    promotion_order = ('parent', *ARMS)
    result['promotion'] = [{'id': f'promotion-{i:02d}', 'seed': take(),
                            'order': list(promotion_order[i:] + promotion_order[:i])} for i in range(4)]
    return result


def scenario_seeds(plan, phases=None):
    values = []
    for phase in phases or plan:
        rows = plan[phase]
        values.extend(rows['train'] + rows['validation'] if isinstance(rows, dict) else [row['seed'] for row in rows])
    return values


def prepare(*, identity, parent_path, seed=DEFAULT_SEED, root=None, data_root=None, settings=None):
    settings = settings or Settings.load()
    if type(seed) is not int:
        raise ExperimentError('Schedule seed must be an integer')
    source_path = Path(parent_path)
    source = load(source_path)
    context = get_game('tetris').context(GAME_CONFIG)
    if source.get('game_id') != 'tetris' or source.get('game_context') != context:
        raise ExperimentError('Parent must match current Tetris rules and an 80-action budget')
    if source.get('think_mode', 'never') != 'never' or source.get('decision_model') != settings.decision_model:
        raise ExperimentError('Parent model or thinking mode differs from configured provider')
    if not isinstance(source.get('brief'), dict) or not source['brief']:
        raise ExperimentError('Parent requires an initialized game brief')
    if source.get('tetris_context_config', {}).get('schema') != 'tetris-context-v3':
        raise ExperimentError('Parent must be the frozen V8 planning research Harness')
    candidate = upgrade_tetris_candidate(source['candidate'], source['tetris_context_config']['goal_instructions'])
    parent = deepcopy(source)
    parent.pop('tetris_context_config')
    parent.update(id='component-parent-' + digest(candidate)[:16], candidate=candidate,
                  runtime_profile=PROFILE, input_mode='structured+vision', think_mode='never', game_id='tetris', game_context=context,
                  provenance={'type': 'migrated_v8_common_parent', 'source_id': source.get('id'),
                              'note': 'Five-component migration adds fixed guidance and ordering; not byte-identical to historical V8.'})
    excluded, evidence = prior_tetris_seeds(data_root or ROOT / 'data')
    plan = scenario_plan(seed, excluded)
    hashes = code_hashes()
    manifest = {'schema': 'component-experiment/v1', 'id': identity, 'created_at': now(), 'game_id': 'tetris',
        'config': GAME_CONFIG, 'game_context': context, 'runtime_profile': PROFILE,
        'parent_source': {'file_sha256': hashlib.sha256(source_path.read_bytes()).hexdigest()},
        'parent': parent, 'parent_hash': digest(parent), 'providers': public_settings(settings),
        'runtime_dependencies': runtime_dependencies(), 'code_hashes': hashes,
        'seed': seed, 'scenarios': plan, 'excluded_environment_seeds': sorted(excluded), 'prior_seed_evidence': evidence,
        'arms': {'text': {'mutable_components': list(TEXT_COMPONENTS), 'experience': False},
                 'components': {'mutable_components': list(TETRIS_COMPONENTS), 'experience': False},
                 'experience': {'mutable_components': list(TETRIS_COMPONENTS), 'experience': True}},
        'arm_order': list(ARMS), 'reflection_rounds': 1,
        'statistics': {'primary_metric': 'native line-clear points', 'primary_comparisons': ['components-text', 'experience-components'],
                       'bootstrap_seed': 74109, 'bootstrap_resamples': 10000, 'confidence_level': .95,
                       'bootstrap_unit': 'paired environment seed; exploratory intervals, no multiplicity-adjusted significance claim'},
        'promotion_gate': {'version': 'v1-empirical-paired', 'minimum_pairs': 4,
                           'minimum_mean_gain': context['score_spec']['minimum_mean_gain'],
                           'no_pair_regression': True, 'requires_candidate_change': True, 'statistical_claim': False},
        'budget': {'max_steps': 80, 'max_calls_per_action': 2, 'evolution_jobs': 4,
                   'gepa_metric_calls_per_job_max': 9, 'trigger_episodes': 2,
                   'development_episodes_max': 38, 'evaluation_episodes': 24, 'promotion_episodes': 16,
                   'completed_episodes_max': 78, 'max_episode_attempts_per_trial': 2,
                   'max_coach_attempts_per_proposal': 2, 'max_gepa_attempts_per_proposal': 2,
                   'decision_request_attempts_max': 78 * 80 * 2 * 2, 'coach_logical_calls_max': 5},
        'constraints': {'all_actions_and_goals': 'Decision', 'think_mode': 'never', 'all_arms_vision': True,
                        'native_action_catalog': 'complete unchanged', 'registry_writes': False,
                        'exports': 'approved bundles only, isolated experiment directory; no automatic activation',
                        'experience_sources': 'bootstrap search_train pairs only; never validation, evaluation or promotion',
                        'experience_snapshot': 'frozen before all three study optimizations',
                        'cache': 'recovery only for identical logical operations; no cross-arm candidate cache',
                        'selection': 'GEPA uses its own training and validation only; all selected arms tested without formal winner selection',
                        'failed_attempts': 'stop, retain evidence; one explicitly authorized retry; never score failures as zero'},
        'limitations': ['One optimization per condition and eight formal seeds do not estimate average optimizer efficacy.',
                        'Formal comparisons are among selected versions; superiority over common parent is tested only in four fresh promotion pairs.',
                        'Experience generation adds separate compute and training games, all itemized.',
                        'NO_ENTRY means C received no usable experience and cannot establish the effect of retrieval.',
                        'Provider names are checked on every response; a silently replaced checkpoint under the same name is not independently detectable.',
                        'Same seed does not force the stochastic model to choose identical actions; 80-action results are not long-run mastery.']}
    path = experiment_path(identity, root)
    path.mkdir(parents=True, exist_ok=False)
    snapshot_sources(path / 'source_snapshot', hashes)
    write_new(path / 'manifest.json', manifest)
    write_new(path / 'manifest.sha256.json', {'sha256': digest(manifest)})
    write_new(path / 'parent.json', parent)
    (path / 'README.md').write_text('# ' + identity + '\n\nFrozen five-component common V8-derived parent. '
        'Prepare made no network requests. Run develop, evaluation, then promotion. All goals/native inputs use Decision never with current-board images. '
        'Only isolated approved packages are exported; no production activation.\n', encoding='utf-8')
    return manifest


def freeze_file(path, value):
    path = Path(path)
    if path.exists():
        existing = load(path)
        if existing != value:
            raise ExperimentError('Frozen result differs from audited reconstruction: ' + path.name)
        checksum = path.with_suffix('.sha256.json')
        if not checksum.exists():
            write_new(checksum, {'sha256': digest(value)})
        return checked_file(path)
    write_new(path, value)
    write_new(path.with_suffix('.sha256.json'), {'sha256': digest(value)})
    return value


def frozen_exists(path):
    path = Path(path)
    return path.exists() and path.with_suffix('.sha256.json').exists()


def checked_file(path):
    path = Path(path)
    value = load(path)
    checksum = path.with_suffix('.sha256.json')
    if not checksum.is_file() or load(checksum) != {'sha256': digest(value)}:
        raise ExperimentError('Frozen artifact checksum mismatch: ' + path.name)
    return value


def summarize(rows, comparisons, statistics_config):
    names = list(rows[0]['arms']) if rows else []
    summaries = {}
    for arm in names:
        episodes = [row['arms'][arm] for row in rows]
        summaries[arm] = {'episodes': len(episodes), 'mean_points': statistics.mean(e['points'] for e in episodes),
            'total_points': sum(e['points'] for e in episodes), 'total_lines': sum(e['lines'] for e in episodes),
            'any_line_episodes': sum(e['lines'] > 0 for e in episodes),
            'top_out_episodes': sum(e['termination_reason'] == 'TOP_OUT' for e in episodes),
            'total_steps': sum(e['steps'] for e in episodes), 'goal_calls': sum(e['goal_calls'] for e in episodes)}
    contrasts = {}
    for left, right in comparisons:
        contrasts[left + '-' + right] = contrast_stats(
            [{'seed': row['seed'], 'difference': row['arms'][left]['points'] - row['arms'][right]['points']} for row in rows],
            resamples=statistics_config['bootstrap_resamples'], bootstrap_seed=statistics_config['bootstrap_seed'])
    return {'arms': summaries, 'comparisons': contrasts}


class ComponentExperiment(BaseExperiment):
    def __init__(self, path, *, retry_failed=False, settings=None, decision=None, coach=None, provider_factory=None):
        self.path, self.settings = Path(path), settings or Settings.load()
        self.manifest = load(self.path / 'manifest.json')
        if load(self.path / 'manifest.sha256.json') != {'sha256': digest(self.manifest)}:
            raise ExperimentError('Manifest changed after preregistration')
        if self.manifest.get('schema') != 'component-experiment/v1' or self.manifest['config'] != GAME_CONFIG:
            raise ExperimentError('Unexpected component experiment schema or action budget')
        if self.manifest['code_hashes'] != code_hashes():
            raise ExperimentError('Source changed since prepare; create a new experiment')
        if self.manifest['runtime_dependencies'] != runtime_dependencies() or self.manifest['providers'] != public_settings(self.settings):
            raise ExperimentError('Provider or runtime dependencies changed since prepare')
        self.parent = self.manifest['parent']
        if digest(self.parent) != self.manifest['parent_hash'] or load(self.path / 'parent.json') != self.parent:
            raise ExperimentError('Frozen common parent changed')
        self.adapter = get_game('tetris')
        self.retry_failed = retry_failed
        self._decision, self._coach = decision, coach
        self._owned_decision, self._owned_coach = decision is None, coach is None
        self.provider_factory, self._providers, self._lock = provider_factory, {}, None
        self._decision_attempts = sum(row['kind'] == 'decision_started' for row in read_lines(self.path / 'calls.jsonl'))

    def __exit__(self, *args):
        try:
            for provider in self._providers.values():
                provider.close()
        finally:
            super().__exit__(*args)

    def harness(self, candidate, label):
        value = validate_candidate(candidate)
        if set(value) != set(TETRIS_COMPONENTS):
            raise ExperimentError('Every study policy must contain all five components')
        return {**deepcopy(self.parent), 'id': 'component-' + label + '-' + digest(value)[:16],
                'candidate': value, 'parent_id': self.parent['id']}

    def counted_decision(self, stage, trial, attempt):
        key = stage, trial, attempt
        archive = self.path / 'calls' / stage / trial / attempt
        if key not in self._providers:
            self._providers[key] = (self.provider_factory(stage, archive, self.path / 'images') if self.provider_factory else
                VisionDecisionProvider(self.settings, enabled=True, archive_dir=archive, image_store=self.path / 'images'))
        provider, experiment = self._providers[key], self
        class CountingDecision:
            settings = experiment.settings
            def decide(self, state, instructions, criteria, request_id):
                if experiment._decision_attempts >= experiment.manifest['budget']['decision_request_attempts_max']:
                    raise ExperimentError('Decision request budget exhausted')
                purpose = 'goal' if request_id.endswith(':goal') else 'game_action'
                directory = archive / digest(request_id)[:32]
                write_new(directory / 'request.json', {'request_id': request_id, 'purpose': purpose, 'state': state,
                    'instructions': instructions, 'criteria': criteria, 'think_mode': 'never',
                    'expected_model': experiment.settings.decision_model})
                fields = {'stage': stage, 'trial': trial, 'attempt': attempt, 'request_id': request_id, 'purpose': purpose}
                append(experiment.path / 'calls.jsonl', {'kind': 'decision_started', **fields})
                experiment._decision_attempts += 1
                try:
                    receipt = provider.decide(state, instructions, criteria, request_id)
                    if load(directory / 'receipt.json') != receipt:
                        raise ExperimentError('Provider archive differs from returned receipt')
                except Exception as exc:
                    write_new(directory / 'failed.json', {'error_type': type(exc).__name__})
                    append(experiment.path / 'calls.jsonl', {'kind': 'decision_failed', **fields, 'error_type': type(exc).__name__})
                    raise
                append(experiment.path / 'calls.jsonl', {'kind': 'decision_completed', **fields,
                    'elapsed_ms': receipt.get('elapsed_ms'), 'model': receipt.get('model'),
                    'usage': receipt.get('raw_response', {}).get('usage', {})})
                return receipt
        return CountingDecision()

    def verify_episode(self, episode, harness):
        from audit_saved_run import _audit_episode
        from report_tetris_vision_experiment import verify_transport_archive
        if episode.get('infra_error') or episode.get('termination_reason') in {'stopped', 'infra_error'}:
            raise ExperimentError('Incomplete episode cannot be scored')
        result = _audit_episode(episode, {harness['id']: harness})
        context = result.get('context_audit', {})
        if (result['thinking_verified'] != len(episode['trace']) or context.get('actual_inputs_verified') is not True
                or context.get('memory_replay_verified') is not True
                or context.get('goal_calls') != context.get('thinking_off_goal_calls')):
            raise ExperimentError('Every native action, goal, memory and input must pass thinking-off replay')
        count, images = 0, []
        for row in episode['trace']:
            for call in row['receipt']['tetris_context']['calls']:
                directory = (self.path / 'calls' / episode['experiment_stage'] / episode['logical_trial'] /
                             episode['experiment_attempt'] / digest(call['request_id'])[:32])
                request, receipt = load(directory / 'request.json'), load(directory / 'receipt.json')
                if (request['state'] != call['input_state'] or request['criteria'] != call['catalog']
                        or digest(request['instructions']) != call['instructions_hash']
                        or request['request_id'] != call['request_id'] or request['purpose'] != call['purpose']
                        or request['think_mode'] != 'never' or request['expected_model'] != self.settings.decision_model
                        or receipt != call['receipt'] or call['status'] != 'validated'):
                    raise ExperimentError('Archived transport differs from reconstructed context ledger')
                images.append(verify_transport_archive(self.path, directory, request, receipt, 'vision'))
                count += 1
        if count != len(episode['trace']) + context['goal_calls']:
            raise ExperimentError('Action/goal call count mismatch')
        return {'actions': len(episode['trace']), 'goal_calls': context['goal_calls'], 'verified_calls': count,
                'image_requests': len(images), 'thinking_off_calls': count, 'native_context_memory_and_transport_replayed': True}

    def episode(self, stage, trial, harness, seed, purpose, *, shared_identity=None):
        directory = self.path / 'episodes' / stage / trial
        episode_id = 'exp-' + digest({'experiment': self.manifest['id'], 'identity': shared_identity or stage + '/' + trial})[:24]
        for saved in sorted(directory.glob('attempt-*/episode.json')):
            episode = load(saved)
            if episode.get('infra_error') or episode.get('termination_reason') in {'stopped', 'infra_error'}:
                continue
            if (episode['seed'] != seed or episode['purpose'] != purpose or episode['harness_id'] != harness['id']
                    or episode['episode_id'] != episode_id or episode.get('experiment_stage') != stage
                    or episode.get('logical_trial') != trial or episode.get('experiment_attempt') != saved.parent.name
                    or episode.get('policy_hashes', {}).get('candidate') != digest(harness['candidate'])):
                raise ExperimentError('Saved episode differs from requested immutable logical trial')
            audit = self.verify_episode(episode, harness)
            evidence = {'episode_hash': digest(episode), 'harness_hash': digest(harness), 'audit': audit}
            if (saved.parent / 'complete.json').exists():
                if load(saved.parent / 'complete.json') != evidence:
                    raise ExperimentError('Saved audit changed')
            else:
                write_new(saved.parent / 'complete.json', evidence)
            return episode
        attempt = self.attempt(directory, self.manifest['budget']['max_episode_attempts_per_trial'])
        write_new(attempt / 'harness.json', harness)
        write_new(attempt / 'trial.json', {'stage': stage, 'trial': trial, 'seed': seed, 'purpose': purpose,
                                         'episode_id': episode_id, 'harness_hash': digest(harness)})
        try:
            episode = run_episode(self.counted_decision(stage, trial, attempt.name), harness,
                episode_id=episode_id, seed=seed, adapter=self.adapter, config=self.manifest['config'],
                log_path=attempt / 'trace.jsonl')
            episode.update(purpose=purpose, experiment_id=self.manifest['id'], experiment_stage=stage,
                           logical_trial=trial, experiment_attempt=attempt.name, runtime_profile=PROFILE)
            write_new(attempt / 'episode.json', episode)
            audit = self.verify_episode(episode, harness)
            write_new(attempt / 'complete.json', {'episode_hash': digest(episode), 'harness_hash': digest(harness), 'audit': audit})
            append(self.path / 'events.jsonl', {'kind': 'episode_completed', 'stage': stage, 'trial': trial,
                'score': episode['score'], 'steps': episode['steps'], 'episode_id': episode_id})
            return episode
        except Exception as exc:
            write_new(attempt / 'failed.json', {'error_type': type(exc).__name__, 'at': now()})
            raise

    def run_search(self, label, schedule, trigger, *, mutable, experiences):
        directory = self.path / 'searches' / label
        if frozen_exists(directory / 'result.json'):
            return checked_file(directory / 'result.json')
        outcomes = sorted(directory.glob('gepa/attempt-*/evolution_result.json'))
        successful = next((p for p in reversed(outcomes) if load(p).get('status') == 'completed'), None)
        if successful:
            outcome, attempt = load(successful), successful.parent
            evaluations = (load(attempt / 'runner-evaluations.json') if (attempt / 'runner-evaluations.json').exists()
                           else read_lines(attempt / 'runner-evaluations.jsonl'))
        else:
            attempt = self.attempt(directory / 'gepa', self.manifest['budget']['max_gepa_attempts_per_proposal'])
            evaluations = []
            def evaluate(candidate, seed, purpose):
                index = len(evaluations)
                expected = schedule['train'] if purpose == 'search_train' else schedule['validation']
                if purpose not in {'search_train', 'search_validation'} or seed not in expected:
                    raise ExperimentError('GEPA requested an unregistered seed or purpose')
                harness = self.harness(candidate, label)
                trial = f'eval-{index:02d}'
                episode = self.episode(label, trial, harness, seed, purpose)
                record = {'index': index, 'seed': seed, 'purpose': purpose, 'candidate': deepcopy(candidate),
                          'candidate_hash': digest(candidate), 'episode': str((self.path / 'episodes' / label / trial /
                            episode['experiment_attempt'] / 'episode.json').relative_to(self.path))}
                evaluations.append(record)
                append(attempt / 'runner-evaluations.jsonl', record)
                append(self.path / 'events.jsonl', {'kind': 'gepa_evaluation', 'search': label, **record})
                return episode
            outcome = evolve_harness(candidate=deepcopy(self.parent['candidate']), trigger_episode=trigger,
                train_seeds=schedule['train'], validation_seeds=schedule['validation'], evaluate_episode=evaluate,
                coach=self.proposal_coach(directory), run_dir=attempt, reflection_rounds=1,
                game_context=self.manifest['game_context'], mutable_components=mutable,
                improvement_experiences=experiences,
                on_event=lambda event: append(self.path / 'events.jsonl', {'search': label, **event}))
            write_new(attempt / 'runner-evaluations.json', evaluations)
        candidate = validate_candidate(outcome['candidate'])
        if any(candidate[key] != self.parent['candidate'][key] for key in TETRIS_COMPONENTS if key not in mutable):
            raise ExperimentError('GEPA changed a frozen component')
        result = {'label': label, 'status': 'completed', 'selected': self.harness(candidate, label),
                  'outcome': outcome, 'evaluations': evaluations, 'mutable_components': list(mutable),
                  'experience_hashes': [entry['content_hash'] for entry in experiences]}
        return freeze_file(directory / 'result.json', result)

    def bootstrap(self):
        snapshot_path = self.path / 'experience_snapshot.json'
        if frozen_exists(snapshot_path):
            return checked_file(snapshot_path)
        schedule = self.manifest['scenarios']['bootstrap']
        trigger = self.episode('bootstrap-trigger', 'train-0', self.parent, schedule['train'][0], 'search_train')
        result = self.run_search('bootstrap', schedule, trigger, mutable=TETRIS_COMPONENTS, experiences=[])
        library = ExperienceLibrary(self.path / 'experience_library')
        scope = ExperienceScope('tetris', self.manifest['game_context']['rules_version'], PROFILE, self.settings.decision_model)
        proposed = [p['candidate'] for p in result['outcome']['proposals'] if p.get('status') == 'proposed' and p.get('candidate')]
        pairs, entry = [], None
        if proposed and proposed[0] != self.parent['candidate']:
            parent_hash, proposed_hash = digest(self.parent['candidate']), digest(proposed[0])
            for seed in schedule['train']:
                records = [r for r in result['evaluations'] if r['purpose'] == 'search_train' and r['seed'] == seed]
                old = next((r for r in records if r['candidate_hash'] == parent_hash), None)
                new = next((r for r in records if r['candidate_hash'] == proposed_hash), None)
                if old and new:
                    pairs.append({'parent': load(self.path / old['episode']), 'candidate': load(self.path / new['episode'])})
            if pairs:
                # Recovery reuses only the exact already-recorded bootstrap entry.
                existing = library.retrieve(scope=scope, exclude_seeds=scenario_seeds(self.manifest['scenarios'], ('development', 'evaluation', 'promotion')))
                if existing:
                    entry = existing[0]
                    if entry['parent_hash'] != parent_hash or entry['candidate_hash'] != proposed_hash:
                        raise ExperimentError('Bootstrap experience does not match its one registered proposal')
                else:
                    entry = library.record_from_pairs(scope=scope, parent=self.parent['candidate'], candidate=proposed[0], pairs=pairs,
                        coach=self.proposal_coach(self.path / 'experience_extraction'), game_context=self.manifest['game_context'])
        sequence = library.snapshot_sequence()
        entries = library.retrieve(scope=scope, exclude_seeds=scenario_seeds(self.manifest['scenarios'], ('development', 'evaluation', 'promotion')),
                                   before_sequence=sequence)
        snapshot = {'schema': 'frozen-component-experience/v1', 'status': 'READY' if entries else 'NO_ENTRY',
                    'entries': entries, 'sequence': sequence, 'scope': scope.as_dict(), 'training_pairs_available': len(pairs),
                    'source': 'bootstrap search_train only', 'bootstrap_selected_changed': result['outcome']['changed']}
        return freeze_file(snapshot_path, snapshot)

    def run_develop(self):
        result_path = self.path / 'development_result.json'
        if frozen_exists(result_path):
            return checked_file(result_path)
        self.ensure_health()
        snapshot = self.bootstrap()
        entries = freeze_experiences(snapshot['entries'], scope=snapshot['scope'], before_sequence=snapshot['sequence'],
            excluded_seeds=scenario_seeds(self.manifest['scenarios'], ('development', 'evaluation', 'promotion')))
        schedule = self.manifest['scenarios']['development']
        trigger = self.episode('shared-trigger', 'train-0', self.parent, schedule['train'][0], 'search_train')
        results = {}
        for label in self.manifest['arm_order']:
            spec = self.manifest['arms'][label]
            results[label] = self.run_search(label, schedule, trigger, mutable=tuple(spec['mutable_components']),
                                             experiences=entries if spec['experience'] else [])
        frozen = {'parent': self.parent, **{name: results[name]['selected'] for name in ARMS}}
        freeze_file(self.path / 'frozen_harnesses.json', frozen)
        return freeze_file(result_path, {'status': 'completed', 'searches': results,
            'experience_status': snapshot['status'], 'experience_count': len(entries),
            'frozen_harnesses_hash': digest(frozen), 'published': False, 'registry_writes': False})

    @staticmethod
    def episode_row(episode):
        context = [step['receipt']['tetris_context'] for step in episode['trace']]
        return {'episode_id': episode['episode_id'], 'points': episode['score'],
            'lines': episode['final_state'].get('metrics', {}).get('lines', 0),
            'steps': episode['steps'], 'termination_reason': episode['termination_reason'],
            'goal_calls': sum(call['purpose'] == 'goal' for item in context for call in item['calls'])}

    def _run_phase(self, phase):
        if not (self.path / 'development_result.json').exists():
            raise ExperimentError('Finish run-develop before held-out games')
        development = checked_file(self.path / 'development_result.json')
        frozen = checked_file(self.path / 'frozen_harnesses.json')
        if digest(frozen) != development['frozen_harnesses_hash']:
            raise ExperimentError('Frozen selected versions changed')
        if phase == 'promotion' and not (self.path / 'evaluation_result.json').exists():
            raise ExperimentError('Finish run-evaluation before run-promotion; evaluation cannot select a winner')
        if phase == 'promotion':
            checked_file(self.path / 'evaluation_result.json')
        self.ensure_health()
        rows = []
        for scenario in self.manifest['scenarios'][phase]:
            row = {**deepcopy(scenario), 'arms': {}}
            for arm in scenario['order']:
                episode = self.episode(phase + '-' + arm, scenario['id'], frozen[arm], scenario['seed'], phase,
                                       shared_identity=scenario['id'])
                row['arms'][arm] = self.episode_row(episode)
            rows.append(row)
        comparisons = [('components', 'text'), ('experience', 'components')] if phase == 'evaluation' else [(arm, 'parent') for arm in ARMS]
        result = {'status': 'completed', 'phase': phase, 'per_seed': rows,
                  **summarize(rows, comparisons, self.manifest['statistics']), 'published': False, 'registry_writes': False,
                  'experience_status': development['experience_status'], 'policy_changed_during_phase': False}
        if phase == 'promotion':
            from game_harness.exporting import export_approved
            result['approvals'] = {}
            scope = digest({'game_context': self.manifest['game_context'], 'model': self.settings.decision_model,
                            'runtime_profile': PROFILE, 'vision': True})
            for arm in ARMS:
                pairs = [{'seed': row['seed'], 'baseline': row['arms']['parent']['points'], 'candidate': row['arms'][arm]['points'],
                          'baseline_episode_id': row['arms']['parent']['episode_id'],
                          'candidate_episode_id': row['arms'][arm]['episode_id']} for row in rows]
                deltas = [p['candidate'] - p['baseline'] for p in pairs]
                gate = self.manifest['promotion_gate']
                passed = (frozen[arm]['candidate'] != self.parent['candidate'] and len(pairs) >= gate['minimum_pairs']
                          and min(deltas) >= 0 and statistics.mean(deltas) >= gate['minimum_mean_gain'])
                approved_version = {**deepcopy(frozen[arm]), 'scope': scope}
                job = {'id': self.manifest['id'] + '-' + arm, 'scope': scope, 'candidate_id': approved_version['id'],
                       'parent_id': self.parent['id'], 'status': 'promoted' if passed else 'retained',
                       'gate': gate, 'pairs': pairs, 'baseline_score': statistics.mean(p['baseline'] for p in pairs),
                       'candidate_score': statistics.mean(p['candidate'] for p in pairs), 'mean_gain': statistics.mean(deltas),
                       'decision': 'Independent empirical gate; isolated export only, no activation.',
                       'gepa': development['searches'][arm]['outcome']['gepa']}
                if passed:
                    job['bundle_manifest'] = export_approved(self.path / 'approved_harnesses', approved_version, job,
                        self.manifest['game_context'], self.parent['brief'])
                result['approvals'][arm] = job
        return freeze_file(self.path / (phase + '_result.json'), result)

    def run_evaluation(self):
        return self._run_phase('evaluation')

    def run_promotion(self):
        return self._run_phase('promotion')

    def costs(self):
        events = read_lines(self.path / 'calls.jsonl')
        counts = Counter(event['kind'] for event in events)
        usage = defaultdict(float)
        per_stage = defaultdict(Counter)
        for event in events:
            if event['kind'].startswith('decision_'):
                per_stage[event['stage']][event['kind']] += 1
            raw = event.get('provider_call', {}).get('usage', {}) if event['kind'] == 'coach_completed' else event.get('usage', {})
            for key, value in raw.items():
                if type(value) in (float, int):
                    usage[('coach_' if event['kind'] == 'coach_completed' else 'decision_') + key] += value
        complete = list((self.path / 'episodes').glob('*/*/attempt-*/complete.json'))
        return {'events': dict(counts), 'by_stage': {key: dict(value) for key, value in per_stage.items()},
                'completed_episodes': len(complete), 'episode_attempts': len(list((self.path / 'episodes').glob('*/*/attempt-*/started.json'))),
                'accepted_actions': sum(load(p.parent / 'episode.json')['steps'] for p in complete),
                'reported_usage': dict(usage), 'currency_cost': 'not calculated; no assumed pricing',
                'bootstrap_costs': 'bootstrap-trigger and bootstrap Decision stages plus bootstrap coach and experience_extraction coach calls'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    command = sub.add_parser('prepare')
    command.add_argument('--id', required=True)
    command.add_argument('--parent', type=Path, default=ROOT / 'docs/experiments/tetris-landing-v8/research-harness.json')
    command.add_argument('--seed', type=int, default=DEFAULT_SEED)
    for name in ('run-develop', 'run-evaluation', 'run-promotion', 'report'):
        command = sub.add_parser(name)
        command.add_argument('--id', required=True)
        if name != 'report':
            command.add_argument('--retry-failed', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.command == 'prepare':
            value = prepare(identity=args.id, parent_path=args.parent, seed=args.seed)
            output = {'prepared': True, 'id': args.id, 'budget': value['budget']}
        elif args.command == 'report':
            from report_component_experiment import report
            output = report(experiment_path(args.id))
        else:
            with ComponentExperiment(experiment_path(args.id), retry_failed=args.retry_failed) as experiment:
                result = getattr(experiment, args.command.replace('-', '_'))()
                output = {key: result[key] for key in ('status', 'phase', 'arms', 'comparisons', 'experience_status') if key in result}
                if 'approvals' in result:
                    output['approvals'] = {arm: {'status': job['status'], 'mean_gain': job['mean_gain']} for arm, job in result['approvals'].items()}
                output['costs'] = experiment.costs()
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print('Component experiment stopped (' + type(exc).__name__ + '): ' + (str(exc) if isinstance(exc, ExperimentError)
              else 'inspect saved attempt evidence'), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
