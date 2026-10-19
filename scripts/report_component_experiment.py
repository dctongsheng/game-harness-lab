#!/usr/bin/env python3
"""Read-only audit of preregistered component/experience experiments."""
from __future__ import annotations

from collections import Counter, defaultdict
import fcntl
import hashlib
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'scripts'))
from run_component_experiment import (ARMS, ComponentExperiment, ExperimentError, checked_file,
    digest, load, read_lines, scenario_seeds, summarize)
from game_harness.experience import freeze_experiences
from report_tetris_vision_experiment import verify_transport_archive


def require(condition, message):
    if not condition:
        raise ExperimentError(message)


def report(path, *, settings=None, experiment_factory=None):
    path = Path(path)
    with (path / '.run.lock').open('rb') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ExperimentError('Experiment is running; report after the phase completes') from None
        runner = (experiment_factory(path) if experiment_factory else ComponentExperiment(path, settings=settings))
        try:
            return audit(runner)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def audit(runner):
    path, manifest = runner.path, runner.manifest
    for name, checksum in manifest['code_hashes'].items():
        source = path / 'source_snapshot' / name
        require(source.is_file() and hashlib.sha256(source.read_bytes()).hexdigest() == checksum, 'Source snapshot mismatch')
    all_seeds = scenario_seeds(manifest['scenarios'])
    require(len(all_seeds) == len(set(all_seeds)) and not set(all_seeds) & set(manifest['excluded_environment_seeds']),
            'Registered seed partitions overlap')
    completed, incomplete, verified_requests = {}, [], set()
    verified_calls = 0
    for stage in sorted((path / 'episodes').glob('*')):
        for trial in sorted(stage.glob('*')):
            successes = []
            for attempt in sorted(trial.glob('attempt-*')):
                meta = load(attempt / 'trial.json')
                harness = load(attempt / 'harness.json')
                require(meta['stage'] == stage.name and meta['trial'] == trial.name
                        and meta['harness_hash'] == digest(harness), 'Attempt metadata or policy changed')
                if stage.name in {'bootstrap-trigger', 'shared-trigger'}:
                    schedule = manifest['scenarios']['bootstrap' if stage.name == 'bootstrap-trigger' else 'development']
                    require(trial.name == 'train-0' and meta['seed'] == schedule['train'][0]
                            and meta['purpose'] == 'search_train' and harness == manifest['parent'], 'Unregistered trigger trial')
                elif stage.name in {'bootstrap', *ARMS}:
                    schedule = manifest['scenarios']['bootstrap' if stage.name == 'bootstrap' else 'development']
                    allowed = schedule['train'] if meta['purpose'] == 'search_train' else schedule['validation']
                    require(meta['purpose'] in {'search_train', 'search_validation'} and meta['seed'] in allowed
                            and trial.name.startswith('eval-') and trial.name[5:].isdigit()
                            and int(trial.name[5:]) < manifest['budget']['gepa_metric_calls_per_job_max'], 'Unregistered search trial')
                else:
                    phase, sep, arm = stage.name.partition('-')
                    schedule = manifest['scenarios'].get(phase, [])
                    require(phase in {'evaluation', 'promotion'} and any(row['id'] == trial.name and arm in row['order']
                            and row['seed'] == meta['seed'] for row in schedule) and meta['purpose'] == phase,
                            'Attempt is outside the registered held-out schedule')
                if not (attempt / 'complete.json').exists():
                    incomplete.append({'stage': stage.name, 'trial': trial.name, 'attempt': attempt.name,
                                       'status': 'failed' if (attempt / 'failed.json').exists() else 'unfinished'})
                    continue
                episode, evidence = load(attempt / 'episode.json'), load(attempt / 'complete.json')
                require(episode['seed'] == meta['seed'] and episode['purpose'] == meta['purpose']
                        and episode['episode_id'] == meta['episode_id'] and episode['harness_id'] == harness['id']
                        and episode['logical_trial'] == trial.name and episode['experiment_stage'] == stage.name
                        and episode['experiment_attempt'] == attempt.name, 'Completed episode trial identity changed')
                verified = runner.verify_episode(episode, harness)
                require(evidence == {'episode_hash': digest(episode), 'harness_hash': digest(harness), 'audit': verified},
                        'Saved completion audit differs from replay')
                successes.append(episode)
                verified_calls += verified['verified_calls']
                for step in episode['trace']:
                    for call in step['receipt']['tetris_context']['calls']:
                        key = stage.name, trial.name, attempt.name, call['request_id']
                        require(key not in verified_requests, 'Request reused inside a trial')
                        verified_requests.add(key)
            require(len(successes) <= 1, 'Multiple successful attempts of one logical trial')
            if successes:
                completed[(stage.name, trial.name)] = successes[0]
            require(len(list(trial.glob('attempt-*'))) <= manifest['budget']['max_episode_attempts_per_trial'], 'Attempt budget exceeded')
    development, snapshot, frozen = None, None, None
    if (path / 'experience_snapshot.json').exists():
        snapshot = checked_file(path / 'experience_snapshot.json')
        entries = freeze_experiences(snapshot['entries'], scope=snapshot['scope'], before_sequence=snapshot['sequence'],
            excluded_seeds=scenario_seeds(manifest['scenarios'], ('development', 'evaluation', 'promotion')))
        require(snapshot['status'] == ('READY' if entries else 'NO_ENTRY'), 'Experience status does not match usable entries')
        bootstrap_train = set(manifest['scenarios']['bootstrap']['train'])
        for entry in entries:
            for pair in entry['evidence']:
                require(pair['seed'] in bootstrap_train, 'Experience used a non-bootstrap-training seed')
                for side in ('parent', 'candidate'):
                    record = pair[side]
                    matches = [episode for (stage, _), episode in completed.items() if stage == 'bootstrap'
                               and episode['episode_id'] == record['episode_id']]
                    require(len(matches) == 1 and digest(matches[0]) == record['episode_hash']
                            and matches[0]['purpose'] == 'search_train'
                            and matches[0]['score'] == record['score']
                            and matches[0]['policy_hashes']['candidate'] == record['candidate_hash'],
                            'Experience does not bind its archived training episode')
    if (path / 'development_result.json').exists():
        development = checked_file(path / 'development_result.json')
        frozen = checked_file(path / 'frozen_harnesses.json')
        require(digest(frozen) == development['frozen_harnesses_hash'], 'Selected policies changed after development')
        require(snapshot is not None and development['experience_status'] == snapshot['status'], 'Missing or changed experience snapshot')
        for arm in ARMS:
            search = checked_file(path / 'searches' / arm / 'result.json')
            require(search == development['searches'][arm] and search['selected'] == frozen[arm], 'Frozen search selection differs')
            expected_experience = [entry['content_hash'] for entry in snapshot['entries']] if arm == 'experience' else []
            require(search['experience_hashes'] == expected_experience, 'Experience leaked into a different arm or changed')
            mutable = manifest['arms'][arm]['mutable_components']
            require(search['mutable_components'] == mutable, 'Mutable component treatment changed')
            require(all(frozen[arm]['candidate'][name] == manifest['parent']['candidate'][name]
                        for name in manifest['parent']['candidate'] if name not in mutable), 'A frozen component changed')
            for record in search['evaluations']:
                episode = load(path / record['episode'])
                schedule = manifest['scenarios']['development']
                seeds = schedule['train'] if record['purpose'] == 'search_train' else schedule['validation']
                require(record['seed'] in seeds and episode['purpose'] == record['purpose']
                        and episode['seed'] == record['seed'] and digest(record['candidate']) == record['candidate_hash']
                        and episode['policy_hashes']['candidate'] == record['candidate_hash'], 'Search evaluation escaped its registered training/validation partition')
    phases = {}
    for phase in ('evaluation', 'promotion'):
        rows = []
        for scenario in manifest['scenarios'][phase]:
            values = {}
            for arm in scenario['order']:
                episode = completed.get((phase + '-' + arm, scenario['id']))
                if episode:
                    require(frozen is not None and episode['seed'] == scenario['seed'] and episode['purpose'] == phase
                            and episode['harness_id'] == frozen[arm]['id']
                            and episode['policy_hashes']['candidate'] == digest(frozen[arm]['candidate']), 'Held-out game does not match its frozen treatment')
                    values[arm] = runner.episode_row(episode)
            if len(values) == len(scenario['order']):
                rows.append({**scenario, 'arms': values})
        result_file = path / (phase + '_result.json')
        value = checked_file(result_file) if result_file.exists() else None
        comparisons = [('components', 'text'), ('experience', 'components')] if phase == 'evaluation' else [(arm, 'parent') for arm in ARMS]
        if value:
            require(len(rows) == len(manifest['scenarios'][phase]) and value['per_seed'] == rows, 'Frozen held-out result differs from complete registered games')
            expected = summarize(rows, comparisons, manifest['statistics'])
            require(value['arms'] == expected['arms'] and value['comparisons'] == expected['comparisons'], 'Frozen paired statistics differ from raw native scores')
            require(value['registry_writes'] is False and value['policy_changed_during_phase'] is False, 'Held-out protocol changed')
            if phase == 'promotion':
                from game_harness.exporting import read_bundle
                for arm in ARMS:
                    job = value['approvals'][arm]
                    pairs = [{'seed': row['seed'], 'baseline': row['arms']['parent']['points'], 'candidate': row['arms'][arm]['points'],
                              'baseline_episode_id': row['arms']['parent']['episode_id'], 'candidate_episode_id': row['arms'][arm]['episode_id']} for row in rows]
                    gate = manifest['promotion_gate']
                    deltas = [pair['candidate'] - pair['baseline'] for pair in pairs]
                    passed = (frozen[arm]['candidate'] != manifest['parent']['candidate'] and len(pairs) >= gate['minimum_pairs']
                              and min(deltas) >= 0 and sum(deltas) / len(deltas) >= gate['minimum_mean_gain'])
                    require(job['pairs'] == pairs and job['gate'] == gate and (job['status'] == 'promoted') == passed,
                            'Approval does not follow the independent empirical gate')
                    if passed:
                        bundle = read_bundle(path / 'approved_harnesses', 'tetris', job['candidate_id'])
                        require(bundle['manifest'] == job['bundle_manifest'] and bundle['strategy'] == frozen[arm]['candidate'], 'Approved export does not match verified candidate')
        phases[phase] = {'status': 'completed' if value else 'partial' if rows else 'not_completed',
                         'completed_pairs': len(rows), 'registered_pairs': len(manifest['scenarios'][phase]),
                         'result': value}
    event_map = defaultdict(Counter)
    for event in read_lines(path / 'calls.jsonl'):
        if event['kind'].startswith('decision_'):
            key = event['stage'], event['trial'], event['attempt'], event['request_id']
            event_map[key][event['kind']] += 1
    archived, images, receipts = set(), set(), 0
    for request_path in sorted((path / 'calls').glob('*/*/*/*/request.json')):
        stage, trial, attempt, identity, _ = request_path.relative_to(path / 'calls').parts
        request = load(request_path)
        key = stage, trial, attempt, request['request_id']
        require(identity == digest(request['request_id'])[:32] and key not in archived, 'Duplicate or misfiled request archive')
        archived.add(key)
        directory = request_path.parent
        receipt = load(directory / 'receipt.json') if (directory / 'receipt.json').exists() else None
        failed = (directory / 'failed.json').exists()
        if receipt:
            # A receipt archived by transport can still be rejected by the runner;
            # such attempts remain failed and never contribute a game score.
            images.add(verify_transport_archive(path, directory, request, receipt, 'vision'))
            receipts += 1
        elif (directory / 'payload.json').exists():
            verify_transport_archive(path, directory, request, None, 'vision')
        events = event_map[key]
        require(events['decision_started'] == 1 and events['decision_failed'] == int(failed)
                and events['decision_completed'] == int(receipt is not None and not failed), 'Request archive/event journal disagree')
    require(set(event_map) == archived and verified_requests <= archived, 'Call ledger does not match archived requests')
    require(len(archived) <= manifest['budget']['decision_request_attempts_max'], 'Decision budget exceeded')
    return {'schema': 'component-experiment-audit/v1', 'experiment_id': manifest['id'], 'manifest_hash': digest(manifest),
            'status': 'completed' if all(p['status'] == 'completed' for p in phases.values()) else 'incomplete',
            'model': manifest['providers']['decision_model'], 'think_mode': 'never', 'all_arms_vision': True,
            'runtime_profile': manifest['runtime_profile'], 'experience_status': snapshot['status'] if snapshot else 'not_built',
            'experience_entries': len(snapshot['entries']) if snapshot else 0, 'phases': phases,
            'audit': {'completed_episodes_replayed': len(completed), 'verified_action_and_goal_calls': verified_calls,
                      'archived_requests': len(archived), 'archived_receipts': receipts, 'unique_images': len(images),
                      'training_only_experience_verified': snapshot is not None, 'full_transport_and_native_replay': True},
            'incomplete_attempts': incomplete, 'costs': runner.costs(), 'limitations': manifest['limitations']}
