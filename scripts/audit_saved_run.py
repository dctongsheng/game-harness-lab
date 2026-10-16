#!/usr/bin/env python3
"""Replay completed saved episodes through their rules adapter, without API calls.

Stopped and infrastructure-failed episodes are listed as incomplete evidence and
never counted as valid evaluations. Historical V1 Snake snapshots use the original
engine because they predate adapter metadata and dynamic action catalogs.
"""
from collections import Counter
import json
import math
from pathlib import Path
import sqlite3
import statistics
import sys

from game_harness.games import get_game
from game_harness.providers import digest
from game_harness.snake import SnakeGame, ACTION_CANDIDATES, RULES_VERSION


class AuditError(ValueError):
    """A completed artifact contradicts its rules replay or model evidence."""


def require(condition, message):
    if not condition:
        raise AuditError(message)


def _response(receipt, action, criteria, episode, step_index, before):
    require(receipt['observation_revision'] == step_index, 'receipt revision mismatch')
    require(receipt['candidate_hash'] == digest(criteria), 'receipt catalog hash mismatch')
    require(receipt['choice_id'] == action, 'receipt action mismatch')
    require(receipt['request_id'] == f"{episode['episode_id']}:step:{step_index}:{digest(before)[:16]}",
            'receipt request is not bound to this episode observation')
    raw = receipt['raw_response']
    require(receipt['response_hash'] == digest(raw), 'raw response hash mismatch')
    answer = raw['answers']['primary_action']
    require(answer['choice'] == action, 'raw response action mismatch')
    require(raw['model'] == receipt['model'] == episode['model'], 'model identity mismatch')
    require(receipt['think_mode'] == 'never', 'action request did not disable thinking')
    probs, confidence = answer['probabilities'], answer['confidence']
    require(set(probs) == {item['id'] for item in criteria}, 'raw probabilities differ from full action catalog')
    require(all(type(v) in (int, float) and math.isfinite(v) and 0 <= v <= 1
                for v in [confidence, *probs.values()]), 'invalid raw decision probabilities')
    require(abs(sum(probs.values()) - 1) <= 0.02, 'raw decision probabilities do not sum to one')
    thought = answer.get('details', {}).get('thought', {})
    used, tokens = thought.get('used'), thought.get('tokens')
    require(used is None or used is False, 'raw response reports thinking enabled')
    require(tokens is None or type(tokens) is int and tokens == 0, 'raw response reports reasoning tokens')
    require(receipt.get('think_used') == used and receipt.get('reasoning_tokens') == tokens,
            'thinking telemetry differs from raw response')
    latency = receipt['elapsed_ms']
    require(type(latency) in (int, float) and math.isfinite(latency) and latency >= 0,
            'invalid decision latency')
    return used is False and tokens == 0


def _audit_episode(episode, versions):
    modern = 'game_id' in episode
    game_id = episode.get('game_id', 'snake')
    config = episode['config']
    session = None
    receipts = []
    thinking_verified = 0
    try:
        if modern:
            adapter = get_game(game_id)
            context = adapter.context(config)
            require(episode['rules_version'] == adapter.rules_version, 'rules version differs from installed adapter')
            require(episode['game_context'] == context, 'game context differs from fixed adapter rules/scoring/opponent')
            version = versions.get(episode['harness_id'])
            require(version is not None, 'candidate version is missing from registry')
            require(version['game_context'] == context and version['game_id'] == game_id,
                    'candidate belongs to a different game context')
            require(version['decision_model'] == episode['model'], 'candidate model differs from episode model')
            policy_hashes = {'candidate': digest(version['candidate'])}
            if adapter.opponent_policy is not None:
                policy_hashes['opponent'] = digest(adapter.opponent_policy)
            require(episode['policy_hashes'] == policy_hashes, 'frozen policy hash mismatch')
            session = adapter.create(episode['seed'], config)
        else:
            require(not any('criteria' in step or 'policy_role' in step for step in episode['trace']),
                    'partially missing adapter metadata cannot be treated as legacy Snake')
            require(episode['rules_version'] == RULES_VERSION, 'legacy Snake rules version mismatch')
            session = SnakeGame(size=config['board_size'], seed=episode['seed'], max_steps=config['max_steps'])
        for index, step in enumerate(episode['trace']):
            before = session.snapshot()
            require(step['before'] == before, 'recorded before-state differs from rules replay')
            require(before['step'] == index and not before['done'], 'step outside active game')
            criteria = session.candidates() if modern else ACTION_CANDIDATES
            require(step['action'] in {item['id'] for item in criteria}, 'action is outside the complete rules catalog')
            if modern:
                role = before.get('policy_role', 'candidate')
                require(step['criteria'] == criteria, 'recorded catalog pruned or changed legal actions')
                require(step['candidate_hash'] == digest(criteria), 'step catalog hash mismatch')
                require(step['policy_role'] == role, 'candidate/opponent role mismatch')
                require(step['actor'] == before.get('actor', before.get('turn', role)), 'actor/side mismatch')
                require(step['policy_hash'] == policy_hashes[role], 'wrong frozen policy for acting side')
            receipt = step['receipt']
            thinking_verified += _response(receipt, step['action'], criteria, episode, index, before)
            after = session.step(step['action'])
            require(after == step['after'], 'recorded after-state differs from rules replay')
            if modern:
                require(step['applied_action'] == after.get('applied_action', step['action']), 'applied action mismatch')
            else:
                require(step['applied_action'] == after['direction'], 'legacy applied direction mismatch')
            receipts.append(receipt)
        final = session.snapshot()
        require(final['done'], 'completed record does not end in a terminal or cutoff state')
        require(episode['steps'] == len(episode['trace']) == final['step'], 'episode step count mismatch')
        require(episode['score'] == final['score'], 'score differs from immutable rules replay')
        require(episode['final_state'] == final, 'final board/state mismatch')
        require(episode['termination_reason'] == final['reason'], 'terminal reason mismatch')
        return {'receipts': receipts, 'thinking_verified': thinking_verified,
                'compatibility': 'adapter' if modern else 'legacy_snake_v1'}
    finally:
        if session is not None and hasattr(session, 'close'):
            session.close()


def audit(root):
    root = Path(root)
    require((root / 'registry.sqlite').is_file(), 'No registry.sqlite')
    db = sqlite3.connect((root / 'registry.sqlite').resolve().as_uri() + '?mode=ro', uri=True)
    try:
        jobs = [json.loads(row[0]) for row in db.execute("SELECT body FROM records WHERE kind='job' ORDER BY created")]
        versions = {row[0]: json.loads(row[1]) for row in db.execute("SELECT id,body FROM records WHERE kind='version'")}
    finally:
        db.close()
    episodes = []
    for path in sorted((root / 'episodes').glob('*.json')):
        episode = json.loads(path.read_text(encoding='utf-8'))
        require(episode.get('episode_id') == path.stem, 'Episode file name does not match its ID')
        episodes.append(episode)
    require(episodes, 'No saved episodes')
    receipts, completed, excluded, compatibility, groups = [], [], [], Counter(), {}
    verified = 0
    formal_ids = {episode['episode_id'] for episode in episodes if episode.get('purpose') == 'play'}
    require(all(job.get('episode_id') in formal_ids for job in jobs),
            'Optimization job is not tied to a saved formal gameplay episode')
    for episode in episodes:
        game_id = episode.get('game_id', 'snake')
        group = groups.setdefault(game_id, {'episodes': 0, 'audited_completed_episodes': 0,
                                           'excluded_incomplete_episodes': 0, 'audited_steps': 0,
                                           'actors': Counter(), 'purposes': Counter(), 'scores': []})
        group['episodes'] += 1
        group['purposes'][episode.get('purpose', 'unknown')] += 1
        if episode.get('infra_error') or episode.get('termination_reason') in {'stopped', 'infra_error'}:
            excluded.append({'episode_id': episode['episode_id'], 'game_id': game_id,
                             'termination_reason': episode.get('termination_reason'), 'steps': episode.get('steps'),
                             'valid_for_evaluation': False, 'audit_status': 'excluded_incomplete'})
            group['excluded_incomplete_episodes'] += 1
            continue
        try:
            audited = _audit_episode(episode, versions)
            if episode.get('purpose') == 'play':
                require(sum(job['episode_id'] == episode['episode_id'] for job in jobs) == 1,
                        'Completed formal gameplay must have exactly one optimization job')
        except (AuditError, KeyError, TypeError, ValueError) as exc:
            raise AuditError(f"{episode['episode_id']} ({game_id}): {exc}") from exc
        completed.append(episode)
        receipts.extend(audited['receipts'])
        verified += audited['thinking_verified']
        compatibility[audited['compatibility']] += 1
        group['audited_completed_episodes'] += 1
        group['audited_steps'] += len(audited['receipts'])
        group['scores'].append(episode['score'])
        group['actors'].update(step.get('policy_role', 'candidate') for step in episode['trace'])
    for group in groups.values():
        scores = group.pop('scores')
        group['mean_score'] = round(statistics.mean(scores), 4) if scores else None
        group['actors'], group['purposes'] = dict(group['actors']), dict(group['purposes'])
    formal = [episode for episode in completed if episode.get('purpose') == 'play']
    latencies = sorted(receipt['elapsed_ms'] for receipt in receipts)
    return {
        'source': 'Saved artifacts replayed locally; no model requests made by this audit',
        'audit_scope': 'Completed episodes only; stopped/infra_error records are excluded from valid evaluation evidence',
        'episodes': len(episodes), 'audited_completed_episodes': len(completed),
        'steps': len(receipts), 'excluded_incomplete': excluded, 'games': groups,
        'compatibility': dict(compatibility),
        'purposes': dict(Counter(episode.get('purpose', 'unknown') for episode in episodes)),
        'models': sorted({receipt['model'] for receipt in receipts}),
        'all_audited_actions_match_raw_response': True, 'all_audited_states_replayed': True,
        'all_audited_requests_disable_thinking': True,
        'all_audited_actions_report_thinking_off': bool(receipts) and verified == len(receipts),
        'actions_with_thinking_off_telemetry': verified,
        'decision_latency_mean_ms': round(statistics.mean(latencies), 2) if latencies else None,
        'decision_latency_p95_ms': latencies[min(len(latencies)-1, int(len(latencies)*.95))] if latencies else None,
        'formal_episodes': [{key: episode.get(key, 'snake' if key == 'game_id' else None)
                            for key in ('episode_id', 'game_id', 'seed', 'score', 'steps', 'termination_reason', 'harness_id')}
                           for episode in formal],
        'jobs': [{key: job.get(key) for key in ('id', 'game_id', 'status', 'parent_id', 'candidate_id',
                                               'baseline_score', 'candidate_score', 'mean_gain', 'gepa', 'pairs')}
                 for job in jobs],
    }


if __name__ == '__main__':
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / 'data'
    print(json.dumps(audit(root), ensure_ascii=False, indent=2))
