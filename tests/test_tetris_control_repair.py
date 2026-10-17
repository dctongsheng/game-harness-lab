"""LLM policy provenance stays intact; unknown evidence cannot silently pass."""
from copy import deepcopy
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import repair_tetris_control_harness as module


OUTPUT = {"candidate": {"decision_instructions": "Decision selects a native action", "strategy_notes": "Check the actual displacement"},
          "changes": [{"change": "Read the held timer", "evidence": ["engine_control_facts"],
                       "limitation": "Collision changes displacement"}], "limitations": "Not a capability result"}
PARENT = {key: {"fixture": key} for key in module.IMMUTABLE}
PARENT.update({"id": "parent", "candidate": {"decision_instructions": "Old", "strategy_notes": "Old"},
               "decision_model": "fixture-model", "think_mode": "never", "provenance": {"old": True}})


def test_accepted_candidate_preserves_llm_strings_and_all_immutable_metadata():
    accepted = module.validate_output(OUTPUT, {})
    result = module.build_repaired(PARENT, accepted, {"new": True})
    assert result["candidate"] == OUTPUT["candidate"]
    assert all(result[key] == PARENT[key] for key in module.IMMUTABLE)
    result["brief"]["fixture"] = "changed-copy"
    assert PARENT["brief"]["fixture"] == "brief"


@pytest.mark.parametrize("refs", [[], ["invented"], [["engine_control_facts"]]])
def test_unknown_or_malformed_evidence_alias_rejected(refs):
    output = deepcopy(OUTPUT)
    output["changes"][0]["evidence"] = refs
    with pytest.raises(ValueError, match="evidence aliases"):
        module.validate_output(output, {})


def test_candidate_whitespace_is_rejected_not_rewritten():
    output = deepcopy(OUTPUT)
    output["candidate"]["strategy_notes"] += " "
    with pytest.raises(ValueError, match="normalization"):
        module.validate_output(output, {})


class FakeCoach:
    def __init__(self, outputs):
        self.outputs = iter(outputs)
        self.calls = []

    def complete(self, messages, purpose):
        text = json.dumps(next(self.outputs))
        self.calls.append({"purpose": purpose, "response_hash": module.digest(text), "usage": {}})
        return text


def run_fixture(tmp_path, monkeypatch, outputs, **kwargs):
    parent_file = tmp_path / "parent.json"
    parent_file.write_text(json.dumps(PARENT))
    monkeypatch.setattr(module, "get_game", lambda name: SimpleNamespace(context=lambda config: PARENT["game_context"]))
    monkeypatch.setattr(module, "history_evidence", lambda *args: {})
    coach = FakeCoach(outputs)
    result = module.repair(tmp_path / "output", parent_file, tmp_path / "history",
                           settings=SimpleNamespace(decision_model="fixture-model", coach_model="fixture-coach"), coach=coach,
                           **kwargs)
    return result, coach


def test_one_valid_coach_response_stops_without_extra_call(tmp_path, monkeypatch):
    result, coach = run_fixture(tmp_path, monkeypatch, [OUTPUT])
    assert len(coach.calls) == 1
    assert result["candidate"] == OUTPUT["candidate"]
    assert result["provenance"]["new_decision_calls"] == 0
    assert (tmp_path / "output/coach-1.output.json").is_file()
    assert not (tmp_path / "output/coach-2.input.json").exists()


def test_schema_repair_keeps_rejected_response_and_accepts_exact_second_values(tmp_path, monkeypatch):
    result, coach = run_fixture(tmp_path, monkeypatch, [{}, OUTPUT])
    assert len(coach.calls) == 2
    assert result["candidate"] == OUTPUT["candidate"]
    assert (tmp_path / "output/coach-1.rejected.json").is_file()
    assert len(result["provenance"]["coach_calls"]) == 2


def test_two_invalid_responses_fail_without_third_or_candidate(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="Expected exactly"):
        run_fixture(tmp_path, monkeypatch, [{}, {}])
    assert (tmp_path / "output/coach-2.output.json").is_file()
    assert not (tmp_path / "output/coach-3.input.json").exists()
    assert not (tmp_path / "output/repaired.json").exists()
    assert json.loads((tmp_path / "output/failed.json").read_text())["completed_calls"] == 2


def semantic_fixture(tmp_path):
    prior = module.build_repaired(PARENT, OUTPUT, {
        "parent_hash": module.digest(PARENT), "historical_evidence_hash": module.digest({}),
        "new_game_results_seen": False, "coach_calls": [{"call": "first"}, {"call": "second"}]})
    review = {"schema": "tetris-control-semantic-review-v1", "candidate_hash": module.digest(prior["candidate"]),
              "status": "rejected_before_any_v6_game", "new_game_results_seen": False,
              "findings": [{"issue": "Contradictory timing claim"}]}
    prior_path, review_path = tmp_path / "prior.json", tmp_path / "review.json"
    prior_path.write_text(json.dumps(prior))
    review_path.write_text(json.dumps(review))
    return prior_path, review_path


def test_semantic_stage_links_prior_calls_and_has_only_one_new_call(tmp_path, monkeypatch):
    prior_path, review_path = semantic_fixture(tmp_path)
    result, coach = run_fixture(tmp_path, monkeypatch, [OUTPUT], prior_repair=prior_path, semantic_review=review_path)
    assert len(coach.calls) == 1
    assert coach.calls[0]["purpose"] == "tetris_control_semantic_repair"
    assert len(result["provenance"]["coach_calls"]) == 3
    assert result["provenance"]["stage_coach_calls"] == 1
    manifest = json.loads((tmp_path / "output/manifest.json").read_text())
    assert manifest["max_coach_calls"] == 1
    assert manifest["semantic_repair_from"]["review_hash"] == module.digest(json.loads(review_path.read_text()))


def test_semantic_stage_never_uses_its_own_schema_retry(tmp_path, monkeypatch):
    prior_path, review_path = semantic_fixture(tmp_path)
    with pytest.raises(ValueError, match="Expected exactly"):
        run_fixture(tmp_path, monkeypatch, [{}, OUTPUT], prior_repair=prior_path, semantic_review=review_path)
    assert not (tmp_path / "output/coach-2.input.json").exists()
    assert not (tmp_path / "output/repaired.json").exists()
    assert json.loads((tmp_path / "output/failed.json").read_text())["completed_calls"] == 1


def test_semantic_review_is_bound_to_exact_rejected_candidate(tmp_path):
    prior_path, review_path = semantic_fixture(tmp_path)
    review = json.loads(review_path.read_text())
    review["candidate_hash"] = "changed"
    review_path.write_text(json.dumps(review))
    with pytest.raises(ValueError, match="bind the rejected candidate"):
        module.semantic_input(prior_path, review_path, PARENT, {})
