#!/usr/bin/env python3
"""Audit saved model receipts and replay recorded actions without model/API calls."""
from collections import Counter
import json
from pathlib import Path
import sqlite3
import statistics
import sys

from game_harness.providers import digest
from game_harness.snake import SnakeGame, ACTION_CANDIDATES


def audit(root):
    db = sqlite3.connect(root / 'registry.sqlite')
    jobs = [json.loads(row[0]) for row in db.execute("SELECT body FROM records WHERE kind='job' ORDER BY created")]
    episodes = [json.loads(path.read_text()) for path in sorted((root / 'episodes').glob('*.json'))]
    assert episodes, 'No saved episodes'
    receipts = []
    for episode in episodes:
        game = SnakeGame(size=episode['config']['board_size'], seed=episode['seed'], max_steps=episode['config']['max_steps'])
        for index, step in enumerate(episode['trace']):
            receipt = step['receipt']
            assert step['before'] == game.snapshot(), 'Recorded before-state mismatch'
            assert receipt['observation_revision'] == index
            assert receipt['candidate_hash'] == digest(ACTION_CANDIDATES)
            assert receipt['choice_id'] == step['action']
            assert receipt['response_hash'] == digest(receipt['raw_response'])
            assert receipt['raw_response']['answers']['primary_action']['choice'] == step['action']
            assert receipt['raw_response']['model'] == receipt['model'] == episode['model']
            assert receipt['think_mode'] == 'never'
            assert receipt['think_used'] is False and receipt['reasoning_tokens'] == 0
            thought = receipt['raw_response']['answers']['primary_action']['details']['thought']
            assert thought['used'] is False and thought['tokens'] == 0
            assert game.step(step['action']) == step['after'], 'Recorded action replay mismatch'
            receipts.append(receipt)
        assert episode['steps'] == len(episode['trace']) == game.step_count
        assert episode['score'] == game.score and episode['final_state'] == game.snapshot()
        if episode['termination_reason'] not in {'stopped', 'infra_error'}:
            assert episode['termination_reason'] == game.reason
        if episode['purpose'] == 'play':
            assert sum(job['episode_id'] == episode['episode_id'] for job in jobs) == 1
    formal = [episode for episode in episodes if episode['purpose'] == 'play']
    assert len(jobs) == len(formal), 'Evaluation episode spawned a nested optimization job'
    latencies = sorted(r['elapsed_ms'] for r in receipts)
    return {
        'source': 'Saved real run artifacts; no model requests made by this audit',
        'episodes': len(episodes), 'steps': len(receipts),
        'purposes': dict(Counter(episode['purpose'] for episode in episodes)),
        'models': sorted({r['model'] for r in receipts}),
        'all_actions_match_raw_response': True, 'all_recorded_states_replayed': True,
        'all_executed_actions_report_thinking_off': True,
        'decision_latency_mean_ms': round(statistics.mean(latencies), 2) if latencies else None,
        'decision_latency_p95_ms': latencies[min(len(latencies)-1,int(len(latencies)*.95))] if latencies else None,
        'formal_episodes': [{key:e[key] for key in ('episode_id','seed','score','steps','termination_reason','harness_id')} for e in formal],
        'jobs': [{key:j.get(key) for key in ('id','status','parent_id','candidate_id','baseline_score','candidate_score','mean_gain','gepa','pairs')} for j in jobs],
    }


if __name__ == '__main__':
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / 'data'
    print(json.dumps(audit(root), ensure_ascii=False, indent=2))
