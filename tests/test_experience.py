"""Evidence isolation and immutable provenance for training-only improvement notes."""
from copy import deepcopy
import json

import pytest

from game_harness.experience import ExperienceLibrary, ExperienceScope, digest, freeze_experiences

BASE = {"decision_instructions": "observe", "strategy_notes": "baseline"}
BETTER = {"decision_instructions": "observe", "strategy_notes": "check consequences"}
SCOPE = ExperienceScope("snake", "snake-v1", "text-v1", "decision-fixture")


def episode(candidate, seed, score, *, purpose="search_train", label="parent"):
    return {"episode_id": f"{label}-{seed}", "purpose": purpose, **SCOPE.as_dict(), "seed": seed,
            "score": score, "steps": 1, "final_state": {"score": score, "done": True},
            "termination_reason": "wall", "infra_error": None,
            "policy_hashes": {"candidate": digest(candidate)}, "config": {"max_steps": 8},
            "game_context": {"game_id": "snake", "rules_version": "snake-v1"},
            "trace": [{"before": {"head": [0, 0]}, "action": "right", "after": {"head": [1, 0]},
                       "receipt": {"think_mode": "never", "model": SCOPE.model}}]}


def pairs(seed=1, gain=2):
    return [{"parent": episode(BASE, seed, 1), "candidate": episode(BETTER, seed, 1 + gain, label="child")}]


def record(lib, *, sample=None, **kwargs):
    return lib.record_from_pairs(scope=SCOPE, parent=BASE, candidate=BETTER,
                                 pairs=sample or pairs(), cue="Repeated local mistakes", lesson="Compare consequences first",
                                 limitations=["One paired training sample, no causal guarantee"], **kwargs)


def test_library_binds_real_scores_and_filters_scope_seeds_and_future_entries(tmp_path):
    lib = ExperienceLibrary(tmp_path)
    assert lib.snapshot_sequence() == 0
    first = record(lib)
    cutoff = lib.snapshot_sequence()
    second = record(lib, sample=pairs(seed=2, gain=-1))
    assert first["outcome"] == "supported" and first["mean_delta"] == 2
    assert second["outcome"] == "regressed" and second["mean_delta"] == -1
    assert first["changed_components"] == ["strategy_notes"] and not first["confounded"]
    assert first["evidence"][0]["parent"]["candidate_hash"] == digest(BASE)
    assert first["evidence"][0]["candidate"]["score"] == 3
    assert lib.retrieve(scope=SCOPE, before_sequence=cutoff) == [first]
    assert lib.retrieve(scope=SCOPE, exclude_seeds=[2]) == [first]
    assert lib.retrieve(scope=ExperienceScope("snake", "snake-v2", "text-v1", SCOPE.model)) == []
    assert lib.retrieve(scope=ExperienceScope("snake", "snake-v1", "text-v1", "other-model")) == []
    assert lib.retrieve(scope=SCOPE, before_sequence=0) == []
    assert lib.retrieve(scope=SCOPE, limit=0) == []
    assert lib.retrieve(scope=SCOPE, max_chars=10) == []
    assert lib.snapshot_sequence() == 2


@pytest.mark.parametrize("side,purpose", [("parent", "search_validation"), ("candidate", "promotion"),
                                         ("candidate", "evaluation"), ("parent", "play")])
def test_never_extract_validation_promotion_test_or_unlabelled_play(tmp_path, side, purpose):
    sample = pairs()
    sample[0][side]["purpose"] = purpose
    with pytest.raises(ValueError, match="training/probe"):
        record(ExperienceLibrary(tmp_path), sample=sample)
    assert not (tmp_path / "manifest.json").exists()


@pytest.mark.parametrize("mutation,match", [
    (lambda e: e.update(seed=99), "same seed"),
    (lambda e: e.update(config={"max_steps": 99}), "same seed"),
    (lambda e: e.update(model="other"), "scope"),
    (lambda e: e.update(runtime_profile="unknown"), "scope"),
    (lambda e: e.update(policy_hashes={"candidate": "0" * 64}), "policy hash"),
    (lambda e: e.update(final_state={"score": 999}), "final state score"),
    (lambda e: e.update(infra_error={"type": "disconnect"}), "Incomplete"),
    (lambda e: e.update(termination_reason="stopped"), "Incomplete"),
    (lambda e: e["trace"][0]["receipt"].update(think_mode="auto"), "thinking disabled"),
    (lambda e: e["trace"][0]["receipt"].update(think_used=True), "thinking-enabled"),
])
def test_corrupt_or_noncomparable_runtime_evidence_is_rejected(tmp_path, mutation, match):
    sample = pairs()
    mutation(sample[0]["candidate"])
    with pytest.raises(ValueError, match=match):
        record(ExperienceLibrary(tmp_path), sample=sample)


def test_manifest_and_entry_tampering_fail_closed(tmp_path):
    lib = ExperienceLibrary(tmp_path)
    record(lib)
    path = tmp_path / "experience-000001.json"
    original = path.read_text()
    entry = json.loads(original)
    entry["lesson"] = "Tampered lesson"
    path.write_text(json.dumps(entry))
    with pytest.raises(ValueError, match="manifest content hash"):
        lib.retrieve(scope=SCOPE)
    path.write_text(original)
    manifest_path = tmp_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["entries"][0]["file"] = "../../validation-results.json"
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="manifest content hash"):
        lib.retrieve(scope=SCOPE)


def test_frozen_snapshot_is_detached_bounded_and_revalidates_scores(tmp_path):
    entry = record(ExperienceLibrary(tmp_path))
    frozen = freeze_experiences([entry], scope=SCOPE)
    entry["lesson"] = "Mutated"
    assert frozen[0]["lesson"] != entry["lesson"]
    with pytest.raises(ValueError, match="hash"):
        freeze_experiences([entry])
    with pytest.raises(ValueError, match="overlaps"):
        freeze_experiences(frozen, excluded_seeds=[1])
    with pytest.raises(ValueError, match="newer"):
        freeze_experiences(frozen, before_sequence=0)
    with pytest.raises(ValueError, match="Duplicate"):
        freeze_experiences(frozen * 2)
    with pytest.raises(ValueError, match="three"):
        freeze_experiences(frozen * 4)
    with pytest.raises(ValueError, match="budget"):
        freeze_experiences(frozen, max_chars=20)
    forged = deepcopy(frozen[0])
    forged["evidence"][0]["delta"] = 900
    forged["content_hash"] = digest({k: v for k, v in forged.items() if k != "content_hash"})
    with pytest.raises(ValueError, match="computed"):
        freeze_experiences([forged])


class Coach:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def complete(self, messages, purpose):
        self.calls.append((messages, purpose))
        return json.dumps(self.response)


def test_coach_only_writes_prose_and_may_choose_no_entry(tmp_path):
    lib = ExperienceLibrary(tmp_path)
    coach = Coach({"status": "NO_ENTRY"})
    assert record(lib, coach=coach) is None
    assert coach.calls[0][1] == "experience_extraction"
    assert lib.snapshot_sequence() == 0
    coach.response = {"cue": "Check a repeated failure", "lesson": "Consider an alternative", "limitations": ["Small sample"]}
    entry = record(lib, coach=coach)
    assert entry["lesson"] == "Consider an alternative"
    assert entry["mean_delta"] == 2
    coach.response["mean_delta"] = 999
    with pytest.raises(ValueError, match="only provide prose"):
        record(lib, coach=coach, sample=pairs(seed=2))
    assert lib.snapshot_sequence() == 1


def test_unchanged_policy_creates_no_fake_improvement_entry(tmp_path):
    coach = Coach({"status": "NO_ENTRY"})
    assert ExperienceLibrary(tmp_path).record_from_pairs(scope=SCOPE, parent=BASE, candidate=BASE,
                                                        pairs=[], coach=coach) is None
    assert not coach.calls


def test_native_terminal_rules_still_apply_to_experience(tmp_path):
    context = {"evaluation": {"require_native_terminal": True}, "score_spec": {"win": 100, "loss": 0, "draw": 50}}
    with pytest.raises(ValueError, match="原生"):
        record(ExperienceLibrary(tmp_path), game_context=context)


def wide_tetris_pairs():
    """Native-shaped 22-row observations, independent of local experiment data."""
    from game_harness.components import upgrade_tetris_candidate
    scope = ExperienceScope("tetris", "fixture-tetris-v1", "tetris-components-v1", SCOPE.model)
    parent = upgrade_tetris_candidate(BASE, "Compare complete conditional landing outcomes")
    candidate = {**parent, "goal_instructions": "Check holes before committing to a column"}
    observation = {
        "width": 10, "height": 20, "hiddenRows": 2, "tickRate": 60,
        "coordinates": "x increases right; y=-2..19 increases downward; board contains locked cells only.",
        "board": {"rows": [{"y": y, "cells": ".........." if y < 15 else "TTT.TTTTTT"}
                            for y in range(-2, 20)]},
        "active": {"type": "O", "rotation": 0, "x": 3, "y": -2,
                   "cells": [{"x": 4, "y": -2}, {"x": 5, "y": -2}, {"x": 4, "y": -1}, {"x": 5, "y": -1}]},
        "next": ["J", "S", "L", "I", "T"], "hold": None, "holdAvailable": True, "pieceId": 1,
        "gravityEveryTicks": 48, "fallTicks": 0, "effectiveFallEveryTicks": 48,
        "grounded": False, "lockTicks": 0, "lockLimitTicks": 30, "lockResets": 0,
        "maxLockResets": 15, "horizontalDirection": 0, "horizontalHeldTicks": 0, "ticksRemaining": 7200,
    }
    actions = ["left:100", "left:300", "left:1000", "right:100", "right:300", "right:1000",
               "rotate-cw:100", "rotate-ccw:100", "hard-drop:100", "hold:100",
               "soft-drop:100", "soft-drop:300", "soft-drop:1000", "noop:100"]
    context = {"game_id": scope.game_id, "rules_version": scope.rules_version}
    samples = []
    for seed in (10, 11):
        pair = {}
        for side, program, score in (("parent", parent, 900), ("candidate", candidate, 200)):
            trace = []
            for index in range(6):
                before = {"step": index, "tick": 6 * index, "score": 0, "done": False, "reason": None,
                          "observation": deepcopy(observation), "metrics": {"lines": 0, "pieces": 1, "ticks": 6 * index}}
                after = deepcopy(before)
                after.update(step=index + 1, tick=6 * (index + 1))
                after["observation"]["fallTicks"] = 6 * (index + 1)
                trace.append({"before": before, "after": after, "action": actions[index],
                              "criteria": [{"id": action, "label": action} for action in actions],
                              "policy_role": "candidate",
                              "receipt": {"model": scope.model, "think_mode": "never"}})
            pair[side] = {"episode_id": f"wide-{side}-{seed}", **scope.as_dict(), "purpose": "search_train",
                          "seed": seed, "score": score, "steps": len(trace), "trace": trace,
                          "termination_reason": "STEP_LIMIT", "infra_error": None,
                          "config": {"max_steps": 80}, "game_context": context,
                          "final_state": {"done": True, "score": score}, "policy_hashes": {"candidate": digest(program)}}
        samples.append(pair)
    return scope, parent, candidate, samples


def test_tetris_extraction_prompt_keeps_actual_actions_in_every_paired_feedback(tmp_path):
    from game_harness.feedback import complete_episode
    from game_harness.experience import MAX_EXTRACTION_EPISODE_CHARS
    scope, parent, candidate, samples = wide_tetris_pairs()
    # Reproduce the previous bug: metadata fits, but no whole native action does.
    for pair in samples:
        for ep in pair.values():
            old = complete_episode(ep, {"episode_id": ep["episode_id"], "score": ep["score"]}, ep["game_context"], 3000)
            assert old["trace_sample"] == []
    coach = Coach({"cue": "Lateral reversals", "lesson": "Check previous movement before repeating a reversal",
                   "limitations": ["Synthetic test evidence; no capability claim"]})
    entry = ExperienceLibrary(tmp_path).record_from_pairs(scope=scope, parent=parent, candidate=candidate,
                                                           pairs=samples, coach=coach)
    assert entry["outcome"] == "regressed"
    assert len(coach.calls) == 1
    payload = json.loads(coach.calls[0][0][1]["content"])
    sent = payload["training_feedback"]
    assert len(sent) == 4 and [row["side"] for row in sent] == ["parent", "candidate", "parent", "candidate"]
    assert MAX_EXTRACTION_EPISODE_CHARS == 12000
    for row in sent:
        feedback = row["feedback"]
        assert feedback["trace_sample"] and not feedback["trace_sampling"]["empty_event_evidence"]
        assert len(json.dumps(feedback, ensure_ascii=False)) <= 12000
        source = next(ep for pair in samples for ep in pair.values() if ep["episode_id"] == feedback["episode_id"])
        for event in feedback["trace_sample"]:
            assert event["action"] == source["trace"][event["trace_index"]]["action"]
            assert event["before"]["board_first_y"] == -2
            assert len(event["before"]["board_rows"]) == 22
            assert len(event["after"]["board_rows"]) == 22
    # A larger extraction context never silently relaxes retrieval limits.
    assert len(freeze_experiences([entry])) == 1
    with pytest.raises(ValueError, match="three"):
        freeze_experiences([entry] * 4)


@pytest.mark.parametrize("missing_side", ["parent", "candidate"])
def test_any_pair_side_without_whole_action_feedback_is_no_entry_before_coach(tmp_path, missing_side):
    scope, parent, candidate, samples = wide_tetris_pairs()
    # One side has valid receipts/scores but an event too wide for the 12k cap.
    for row in samples[1][missing_side]["trace"]:
        for frame in ("before", "after"):
            row[frame]["observation"]["active"]["cells"] = [{"x": 4, "y": 0}] * 2000
    coach = Coach({"cue": "Would be invented", "lesson": "Do not call with score-only evidence", "limitations": []})
    library = ExperienceLibrary(tmp_path)
    assert library.record_from_pairs(scope=scope, parent=parent, candidate=candidate,
                                     pairs=samples, coach=coach) is None
    assert coach.calls == []
    assert library.snapshot_sequence() == 0


def test_lowered_extraction_budget_fails_closed_instead_of_score_only_lesson(tmp_path, monkeypatch):
    scope, parent, candidate, samples = wide_tetris_pairs()
    monkeypatch.setattr("game_harness.experience.MAX_EXTRACTION_EPISODE_CHARS", 3000)
    coach = Coach({"cue": "Wrong inference", "lesson": "Scores alone are insufficient", "limitations": []})
    library = ExperienceLibrary(tmp_path)
    assert library.record_from_pairs(scope=scope, parent=parent, candidate=candidate,
                                     pairs=samples, coach=coach) is None
    assert not coach.calls and library.snapshot_sequence() == 0
