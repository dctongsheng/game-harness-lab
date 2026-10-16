"""Run GEPA itself with deterministic games and a fake coach, without API calls."""

import json

import gepa
import pytest

from game_harness.evolution import (
    EvolutionInfrastructureError,
    EvolutionStopped,
    evolve_harness,
)


BASE = {"decision_instructions": "observe then choose", "strategy_notes": "baseline"}
BETTER = {"decision_instructions": "check future space", "strategy_notes": "improved"}


def episode(seed, score=2, *, episode_id=None):
    return {
        "episode_id": episode_id or f"episode-{seed}", "seed": seed,
        "harness_id": "h0", "score": score, "steps": 3,
        "termination_reason": "wall", "model": "fake-decision",
        "trace": [{"before": {"head": [1, 1], "food": [2, 1]},
                   "after": {"head": [2, 1]}, "action": "right",
                   "receipt": {"request_id": "r1", "action_id": "right", "api_key": "DO-NOT-LOG"}}],
        "infra_error": None,
    }


class Coach:
    def __init__(self, response=BETTER):
        self.response = response
        self.calls = []

    def complete(self, messages, purpose):
        self.calls.append((messages, purpose))
        return json.dumps(self.response) if isinstance(self.response, dict) else self.response


def run(tmp_path, *, coach=None, evaluator=None, **kwargs):
    return evolve_harness(
        candidate=dict(BASE), trigger_episode=episode(1, episode_id="trigger-only-evidence"),
        train_seeds=[2, 3], validation_seeds=[101, 102],
        evaluate_episode=evaluator or (lambda c, seed, purpose: episode(seed, 5 if c == BETTER else 2)),
        coach=coach or Coach(), run_dir=tmp_path / "job", **kwargs,
    )


def test_real_gepa_proposes_evaluates_and_selects_without_recursive_play(tmp_path, monkeypatch):
    """Spy calls the actual optimizer; all evaluation episodes remain search-only."""
    original = gepa.optimize
    invocations = []

    def instrumented(**kwargs):
        invocations.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(gepa, "optimize", instrumented)
    coach, calls, events = Coach(), [], []

    def evaluator(candidate, seed, purpose):
        calls.append((dict(candidate), seed, purpose))
        return episode(seed, 5 if candidate == BETTER else 2)

    result = run(tmp_path, coach=coach, evaluator=evaluator, on_event=events.append)
    assert len(invocations) == 1
    assert invocations[0]["adapter"] is not None
    assert invocations[0]["reflection_lm"] is None
    assert invocations[0]["skip_perfect_score"] is False
    assert "task_lm" not in invocations[0]
    assert result["candidate"] == BETTER and result["changed"]
    assert result["gepa"]["engine"] == "gepa.optimize"
    assert result["gepa"]["candidate_count"] == 2
    assert result["gepa"]["total_metric_calls"] == len(calls)
    assert result["promotion"] == "not_performed"
    assert {purpose for _, _, purpose in calls} == {"search_train", "search_validation"}
    assert all(seed in ([2, 3] if purpose == "search_train" else [101, 102]) for _, seed, purpose in calls)
    assert len(coach.calls) == 1 and coach.calls[0][1] == "reflection"
    feedback = json.loads(coach.calls[0][0][1]["content"])["training_feedback"]
    assert feedback[0]["episode_id"] == "trigger-only-evidence"
    assert {row["seed"] for row in feedback} == {1, 2, 3}
    assert "DO-NOT-LOG" not in json.dumps(coach.calls)
    assert (tmp_path / "job/gepa/gepa_state.bin").is_file()
    assert (tmp_path / "job/evolution_result.json").is_file()
    assert any(event["type"] == "evolution_reflection_completed" for event in events)
    json.dumps(result, allow_nan=False)


def test_validation_can_retain_baseline_despite_training_improvement(tmp_path):
    def evaluator(candidate, seed, purpose):
        score = 5 if candidate == BETTER and purpose == "search_train" else 0 if candidate == BETTER else 2
        return episode(seed, score)

    result = run(tmp_path, evaluator=evaluator)
    assert result["candidate"] == BASE
    assert not result["changed"]
    assert result["gepa"]["candidate_count"] == 2
    assert result["gepa"]["val_aggregate_scores"] == [2.0, 0.0]


def test_every_requested_reflection_is_attempted_even_for_high_scores(tmp_path):
    coach = Coach(BASE)
    result = run(tmp_path, coach=coach, evaluator=lambda c, seed, purpose: episode(seed, 10), reflection_rounds=3)
    assert len(coach.calls) == 3
    assert result["reflection_rounds_attempted"] == 3
    assert not result["changed"]
    assert result["gepa"]["total_metric_calls"] <= result["gepa"]["max_metric_calls"]


@pytest.mark.parametrize("response", ["not json", {**BETTER, "python": "raise SystemExit()"},
                                       {"decision_instructions": "ok", "strategy_notes": ["invalid type"]}])
def test_invalid_proposal_is_rejected_without_execution_or_retry(tmp_path, response):
    coach = Coach(response)
    seen = []

    def evaluator(candidate, seed, purpose):
        seen.append(candidate)
        return episode(seed)

    result = run(tmp_path, coach=coach, evaluator=evaluator)
    assert len(coach.calls) == 1
    assert result["proposals"][0]["status"] == "rejected_schema"
    assert all(candidate == BASE for candidate in seen)
    assert result["candidate"] == BASE and not result["changed"]


@pytest.mark.parametrize("bad_result", [None, {"score": 0, "infra_error": "service unavailable"},
                                         {"score": float("nan")}, {"score": True}, {"score": 1.5}])
def test_infrastructure_result_is_never_converted_to_zero_score(tmp_path, bad_result):
    coach = Coach()
    with pytest.raises(EvolutionInfrastructureError) as caught:
        run(tmp_path, coach=coach, evaluator=lambda c, seed, purpose: bad_result)
    assert not coach.calls
    assert caught.value.partial_result["status"] == "failed"
    assert caught.value.partial_result["evaluations"] == []


def test_coach_failure_is_not_retried_by_gepa_and_does_not_leak_error_body(tmp_path):
    class BrokenCoach(Coach):
        def complete(self, messages, purpose):
            self.calls.append((messages, purpose))
            raise ConnectionError("Authorization: Bearer private-api-key")

    coach = BrokenCoach()
    with pytest.raises(EvolutionInfrastructureError) as caught:
        run(tmp_path, coach=coach)
    assert len(coach.calls) == 1
    assert "private-api-key" not in str(caught.value)
    assert "private-api-key" not in json.dumps(caught.value.partial_result)
    assert caught.value.partial_result["status"] == "failed"


def test_user_cancellation_before_start_calls_neither_game_nor_coach(tmp_path):
    coach, calls = Coach(), []
    with pytest.raises(EvolutionStopped):
        run(tmp_path, coach=coach, evaluator=lambda *args: calls.append(args), should_stop=lambda: True)
    assert calls == [] and coach.calls == []
    assert not (tmp_path / "job").exists()


def test_stopped_evaluation_cannot_be_scored_as_a_completed_game(tmp_path):
    coach = Coach()

    def evaluator(candidate, seed, purpose):
        return {**episode(seed, score=10), "termination_reason": "stopped"}

    with pytest.raises(EvolutionStopped) as caught:
        run(tmp_path, coach=coach, evaluator=evaluator)
    assert coach.calls == []
    assert caught.value.partial_result["evaluations"] == []
    assert caught.value.partial_result["status"] == "stopped"


def test_stopped_trigger_requires_recovery_before_reflection(tmp_path):
    coach = Coach()
    with pytest.raises(EvolutionStopped):
        evolve_harness(candidate=BASE, trigger_episode={**episode(1), "termination_reason": "stopped"},
                       train_seeds=[2], validation_seeds=[101],
                       evaluate_episode=lambda *args: pytest.fail("must recover trigger first"),
                       coach=coach, run_dir=tmp_path / "job")
    assert coach.calls == []
    assert not (tmp_path / "job").exists()


def test_user_cancellation_after_reflection_prevents_child_evaluation(tmp_path):
    cancelled, calls = [False], []

    class CancelCoach(Coach):
        def complete(self, messages, purpose):
            response = super().complete(messages, purpose)
            cancelled[0] = True
            return response

    coach = CancelCoach()

    def evaluator(candidate, seed, purpose):
        calls.append(candidate)
        return episode(seed)

    with pytest.raises(EvolutionStopped) as caught:
        run(tmp_path, coach=coach, evaluator=evaluator, should_stop=lambda: cancelled[0])
    assert len(coach.calls) == 1
    assert all(candidate == BASE for candidate in calls)
    assert caught.value.partial_result["status"] == "stopped"


def test_reused_directory_cannot_silently_resume_with_changed_data(tmp_path):
    run(tmp_path)
    coach = Coach()
    with pytest.raises(ValueError, match="new job directory"):
        run(tmp_path, coach=coach)
    assert coach.calls == []


@pytest.mark.parametrize("train,validation", [([2, 3], [3, 4]), ([2, 2], [101]), ([2], [1])])
def test_data_leakage_is_rejected_before_running(tmp_path, train, validation):
    coach = Coach()
    with pytest.raises(ValueError):
        evolve_harness(candidate=BASE, trigger_episode=episode(1), train_seeds=train,
                       validation_seeds=validation, evaluate_episode=lambda *args: pytest.fail("must not evaluate"),
                       coach=coach, run_dir=tmp_path / "job")
    assert not coach.calls


def test_long_trace_feedback_is_bounded_and_marked_as_sampled(tmp_path):
    trigger = episode(1)
    trigger["trace"] = [dict(trigger["trace"][0], before={"board": "x" * 100_000, "api_key": "NEVER-SEND"}) for _ in range(500)]
    coach = Coach()
    evolve_harness(candidate=BASE, trigger_episode=trigger, train_seeds=[2], validation_seeds=[101],
                   evaluate_episode=lambda c, seed, purpose: episode(seed), coach=coach, run_dir=tmp_path / "job")
    payload = json.loads(coach.calls[0][0][1]["content"])
    feedback = payload["training_feedback"][0]
    assert feedback["trace_sampling"]["total_steps"] == 500
    assert not feedback["trace_sampling"]["all_steps_sampled"]
    assert len(feedback["trace_sampling"]["retained_indices"]) == 10
    assert len(coach.calls[0][0][1]["content"]) < 40_000
    assert "NEVER-SEND" not in json.dumps(coach.calls)


TURN_CONTEXT = {
    "game_id": "turn-fixture", "name": "Turn-based fixture",
    "rules": "The candidate plays red or black according to its seat.",
    "score_spec": {"min": -1.0, "max": 1.0, "direction": "maximize"},
    "opponent_policy": {"decision_instructions": "FIXED_OPPONENT", "strategy_notes": "do not change"},
}


def test_generic_scores_support_signed_fractional_values_and_fixed_game_context(tmp_path):
    coach = Coach()

    def evaluator(candidate, seed, purpose):
        result = episode(seed, score=0.75 if candidate == BETTER else -0.25)
        result['game_id'] = 'turn-fixture'
        result['trace'][0].update(policy_role='opponent', actor='black', criteria=[{'id': 'a0a1', 'label': 'move'}])
        return result

    result = evolve_harness(candidate=BASE, trigger_episode=evaluator(BASE, 1, 'play'),
                            train_seeds=[2], validation_seeds=[101], evaluate_episode=evaluator,
                            coach=coach, game_context=TURN_CONTEXT, run_dir=tmp_path / 'job')
    assert result['candidate'] == BETTER
    assert result['gepa']['best_score'] == 0.75
    assert result['game_context'] == TURN_CONTEXT
    system = coach.calls[0][0][0]['content']
    payload = json.loads(coach.calls[0][0][1]['content'])
    assert payload['immutable_game_context'] == TURN_CONTEXT
    assert '贪吃蛇' not in system and '食物' not in system
    assert payload['training_feedback'][0]['trace_sample'][0]['policy_role'] == 'opponent'
    assert payload['training_feedback'][0]['trace_sample'][0]['actor'] == 'black'
    manifest = json.loads((tmp_path / 'job/evolution_manifest.json').read_text())
    assert manifest['score'] == TURN_CONTEXT['score_spec']


@pytest.mark.parametrize('score', [-1.01, 1.01, float('inf')])
def test_adapter_score_bounds_reject_evaluation_without_reflection(tmp_path, score):
    coach = Coach()
    with pytest.raises(EvolutionInfrastructureError):
        evolve_harness(candidate=BASE, trigger_episode=episode(1, 0),
                       train_seeds=[2], validation_seeds=[101],
                       evaluate_episode=lambda c, seed, purpose: episode(seed, score),
                       coach=coach, game_context=TURN_CONTEXT, run_dir=tmp_path / 'job')
    assert coach.calls == []


def test_other_game_result_is_rejected_even_when_score_is_in_bounds(tmp_path):
    with pytest.raises(EvolutionInfrastructureError, match='different game'):
        evolve_harness(candidate=BASE, trigger_episode={**episode(1, 0), 'game_id': 'another-game'},
                       train_seeds=[2], validation_seeds=[101],
                       evaluate_episode=lambda *args: pytest.fail('must validate trigger first'),
                       coach=Coach(), game_context=TURN_CONTEXT, run_dir=tmp_path / 'job')


@pytest.mark.parametrize('spec', [{'direction': 'minimize'}, {'min': 2, 'max': 1}, {'min': float('nan')}])
def test_unsupported_or_invalid_adapter_scoring_is_rejected_before_optimizer(tmp_path, spec):
    with pytest.raises(ValueError, match='score'):
        evolve_harness(candidate=BASE, trigger_episode=episode(1, 0),
                       train_seeds=[2], validation_seeds=[101],
                       evaluate_episode=lambda *args: pytest.fail('must validate scoring first'),
                       coach=Coach(), game_context={**TURN_CONTEXT, 'score_spec': spec}, run_dir=tmp_path / 'job')
