"""Run GEPA itself with deterministic games and a fake coach, without API calls."""

import json

import gepa
import pytest

from game_harness.evolution import (
    EvolutionInfrastructureError,
    EvolutionStopped,
    evolve_harness,
)
from game_harness.providers import ProviderError


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
    assert str(caught.value) == "Reflection failed (ConnectionError)"
    assert "private-api-key" not in (tmp_path / "job/evolution_result.json").read_text()


def test_controlled_provider_diagnostic_survives_reflection_failure(tmp_path):
    class BrokenCoach(Coach):
        def complete(self, messages, purpose):
            self.calls.append((messages, purpose))
            raise ProviderError("教练 API HTTP 429")

    coach = BrokenCoach()
    with pytest.raises(EvolutionInfrastructureError) as caught:
        run(tmp_path, coach=coach)
    expected = "Reflection failed (ProviderError): 教练 API HTTP 429"
    assert len(coach.calls) == 1
    assert str(caught.value) == expected
    assert caught.value.partial_result["error"] == expected
    assert caught.value.partial_result["status"] == "failed"
    saved = json.loads((tmp_path / "job/evolution_result.json").read_text())
    assert saved["error"] == expected and saved["promotion"] == "not_performed"


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
    assert feedback["trace_sampling"]["retained_indices"] == [row["trace_index"] for row in feedback["trace_sample"]]
    assert feedback["trace_sampling"]["omitted_count"] + len(feedback["trace_sample"]) == 500
    assert feedback["trace_sampling"]["last_source_step_covered"]
    assert len(json.dumps(payload["training_feedback"], ensure_ascii=False)) <= 32000
    assert len(coach.calls[0][0][1]["content"]) < 40_000
    assert "NEVER-SEND" not in json.dumps(coach.calls)


def test_feedback_formats_keep_teacher_prompt_and_search_budget_identical(tmp_path):
    runs = []
    for format_name in ('complete_events_v1', 'legacy_excerpt_v1'):
        coach = Coach()
        result = run(tmp_path / format_name, coach=coach, feedback_format=format_name)
        payload = json.loads(coach.calls[0][0][1]['content'])
        manifest = json.loads((tmp_path / format_name / 'job/evolution_manifest.json').read_text())
        assert payload['feedback_format'] == result['feedback_format'] == manifest['feedback_format'] == format_name
        assert payload['immutable_runtime_contract'] == manifest['immutable_runtime_contract']
        assert payload['runtime_contract_hash'] == manifest['runtime_contract_hash'] == result['runtime_contract_hash']
        import hashlib
        serialized = json.dumps(payload['immutable_runtime_contract'], sort_keys=True,
                                ensure_ascii=False, allow_nan=False).encode()
        assert hashlib.sha256(serialized).hexdigest() == payload['runtime_contract_hash']
        assert len(coach.calls) == 1
        runs.append((coach.calls[0][0][0], result['gepa'], payload))
    assert runs[0][0] == runs[1][0]
    for key in ('max_metric_calls', 'total_metric_calls', 'candidate_count'):
        assert runs[0][1][key] == runs[1][1][key]
    for key in ('parent_candidate', 'immutable_game_context', 'immutable_runtime_contract',
                'runtime_contract_hash', 'feedback_episodes_available', 'feedback_scope'):
        assert runs[0][2][key] == runs[1][2][key]


def test_unknown_feedback_format_fails_before_calls_or_writes(tmp_path):
    coach = Coach()
    with pytest.raises(ValueError, match='feedback_format'):
        run(tmp_path, coach=coach, feedback_format='unregistered')
    assert not coach.calls and not (tmp_path / 'job').exists()


TURN_CONTEXT = {
    "game_id": "turn-fixture", "name": "Turn-based fixture",
    "rules": "The candidate plays red or black according to its seat.",
    "score_spec": {"min": -1.0, "max": 1.0, "direction": "maximize"},
    "opponent_policy": {"decision_instructions": "FIXED_OPPONENT", "strategy_notes": "do not change"},
}


def test_generic_scores_support_signed_fractional_values_and_fixed_game_context(tmp_path, monkeypatch):
    coach = Coach()
    monkeypatch.setattr('game_harness.initialization_contract.get_game',
                        lambda *_: pytest.fail('Generic evolution must not require a registered adapter/session'))

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
    assert payload['immutable_runtime_contract']['game_id'] == 'turn-fixture'
    assert payload['immutable_runtime_contract']['player']['model'] is None
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


def full_tetris_candidate():
    from game_harness.components import upgrade_tetris_candidate
    return upgrade_tetris_candidate(BASE, "Choose a feasible column and rotation using conditional projections")


def component_run(tmp_path, *, mutable=None, response=None, experiences=()):
    from types import SimpleNamespace
    base = full_tetris_candidate()
    response = response or {**base, "goal_instructions": "Compare holes and line-clear outcomes before selecting a target"}
    coach = Coach(response)
    coach.settings = SimpleNamespace(decision_model="fake-decision")
    context = {"game_id": "tetris", "rules_version": "tetris-v1", "score_spec": {"min": 0, "direction": "maximize"}}

    def evaluator(c, seed, purpose):
        return {**episode(seed, score=5 if c != base else 2), "game_id": "tetris"}

    result = evolve_harness(candidate=base, trigger_episode=evaluator(base, 1, "play"),
                            train_seeds=[2], validation_seeds=[101], coach=coach,
                            evaluate_episode=evaluator, game_context=context, run_dir=tmp_path / "job",
                            mutable_components=mutable, improvement_experiences=experiences)
    return result, coach


def test_full_profile_gepa_changes_goal_and_preserves_all_components(tmp_path):
    result, coach = component_run(tmp_path)
    assert result["changed"] and len(result["candidate"]) == 5
    assert result["proposals"][0]["changed_components"] == ["goal_instructions"]
    payload = json.loads(coach.calls[0][0][1]["content"])
    contract = payload["immutable_runtime_contract"]
    assert contract["components"]["context_schema"] == "tetris-context-v4"
    assert "tetris_landing_projections" in contract["runtime_fields"]
    assert "stateless" not in contract["player"]["memory"]
    assert "不假设height_profile" not in contract["game_guidance"]["design_focus"]
    assert set(payload["mutable_components"]) == set(result["candidate"])
    assert result["promotion"] == "not_performed"


def test_two_text_arm_retains_frozen_goals_memory_and_observations(tmp_path):
    base = full_tetris_candidate()
    result, coach = component_run(tmp_path, mutable=["decision_instructions", "strategy_notes"],
                                  response={**base, "strategy_notes": "Compare current consequences"})
    assert result["changed"] and len(result["candidate"]) == 5
    for name in ("goal_instructions", "memory_policy", "observation_policy"):
        assert result["candidate"][name] == base[name]
    payload = json.loads(coach.calls[0][0][1]["content"])
    assert set(payload["frozen_components"]) == {"goal_instructions", "memory_policy", "observation_policy"}
    manifest = json.loads((tmp_path / "job/evolution_manifest.json").read_text())
    assert manifest["mutable_components"] == ["decision_instructions", "strategy_notes"]
    assert len(manifest["mutable_components_hash"]) == 64


@pytest.mark.parametrize("kind", ["mutate_frozen", "drop_components", "bad_json_policy", "remove_observation"])
def test_component_schema_escape_is_rejected_without_child_execution(tmp_path, kind):
    response = full_tetris_candidate()
    mutable = None
    if kind == "mutate_frozen":
        mutable = ["decision_instructions", "strategy_notes"]
        response["goal_instructions"] = "Illegally modified"
    elif kind == "drop_components":
        response = BASE
    elif kind == "bad_json_policy":
        response["memory_policy"] = '{"instructions":"x","recent_actions":1000,"replan_every_actions":4}'
    else:
        response["observation_policy"] = '{"instructions":"x","section_order":["observation"]}'
    result, coach = component_run(tmp_path, response=response, mutable=mutable)
    assert not result["changed"] and result["candidate"] == full_tetris_candidate()
    assert result["proposals"][0]["status"] == "rejected_schema"
    assert len(coach.calls) == 1


def test_frozen_experiences_enter_coach_with_hashes_and_never_validation_evidence(tmp_path):
    from game_harness.experience import ExperienceLibrary, ExperienceScope, digest
    from types import SimpleNamespace
    scope = ExperienceScope("snake", "snake-v1", "text-v1", "fake-decision")
    def e(c, score, label):
        return {**episode(50, score, episode_id=label), **scope.as_dict(), "purpose": "experience_probe",
                "final_state": {"score": score}, "policy_hashes": {"candidate": digest(c)},
                "trace": [{"receipt": {"model": scope.model, "think_mode": "never"}}]}
    entry = ExperienceLibrary(tmp_path / "experience").record_from_pairs(
        scope=scope, parent=BASE, candidate=BETTER,
        pairs=[{"parent": e(BASE, 2, "older-parent"), "candidate": e(BETTER, 3, "older-child")}],
        cue="Open space shrinks", lesson="Consider future space", limitations=["One training pair only"])
    coach = Coach()
    coach.settings = SimpleNamespace(decision_model="fake-decision")
    context = {"game_id": "snake", "rules_version": "snake-v1", "score_spec": {"min": 0}}
    result = run(tmp_path, coach=coach, improvement_experiences=[entry], game_context=context)
    payload = json.loads(coach.calls[0][0][1]["content"])
    assert payload["improvement_experiences"] == [entry]
    assert result["improvement_experience_hashes"] == [entry["content_hash"]]
    assert result["proposals"][0]["improvement_experience_snapshot_hash"] == result["improvement_experience_snapshot_hash"]
    entry["lesson"] = "tampered"
    assert payload["improvement_experiences"][0]["lesson"] != entry["lesson"]
