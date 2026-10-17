"""Reject ungrounded LLM experience references before any player test."""
from copy import deepcopy
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from initialize_tetris_context_harness import retain_referenced_experience, validate_bootstrap


EVIDENCE = {"complete_episode_summaries": [{"episode_id": "complete-id-0123456789"}]}
OUTPUT = {
    "brief": {key: "fixture" for key in ("background", "goal", "rules", "precautions", "strategy")},
    "experience_summary": [{"claim": "Observed oscillation", "evidence_episode_ids": ["complete-id-0123456789"],
                            "limitations": "One historical episode, not a causal test"}],
    "candidate": {"decision_instructions": "Choose a native action", "strategy_notes": "Fixture"},
}


def test_valid_evidence_link_does_not_modify_generated_strategy():
    value = deepcopy(OUTPUT)
    candidate, brief, summary = validate_bootstrap(value, EVIDENCE)
    assert value == OUTPUT
    assert (candidate, brief, summary) == (OUTPUT["candidate"], OUTPUT["brief"], OUTPUT["experience_summary"])


@pytest.mark.parametrize("ids", [[], ["complete-id-012345"], ["invented-id"], [None], [["complete-id-0123456789"]]])
def test_truncated_or_unknown_references_are_not_silently_repaired(ids):
    value = deepcopy(OUTPUT)
    value["experience_summary"][0]["evidence_episode_ids"] = ids
    with pytest.raises(ValueError, match="source reference"):
        validate_bootstrap(value, EVIDENCE)


@pytest.mark.parametrize("field", ["claim", "limitations"])
def test_claim_and_limits_are_required(field):
    value = deepcopy(OUTPUT)
    value["experience_summary"][0][field] = "  "
    with pytest.raises(ValueError):
        validate_bootstrap(value, EVIDENCE)


def test_brief_cannot_hide_nontext_or_empty_fields():
    value = deepcopy(OUTPUT)
    value["brief"]["rules"] = []
    with pytest.raises(ValueError, match="brief"):
        validate_bootstrap(value, EVIDENCE)


def test_recovery_drops_whole_bad_claim_without_fixing_ids_or_changing_policy():
    value = deepcopy(OUTPUT)
    bad = {**deepcopy(value["experience_summary"][0]), "evidence_episode_ids": ["complete-id-012345"]}
    value["experience_summary"].append(bad)
    recovered, rejected = retain_referenced_experience(value, EVIDENCE)
    assert recovered == OUTPUT
    assert rejected == [{"index": 1, "item": bad, "reason": "schema_or_source_reference_invalid"}]
    assert len(value["experience_summary"]) == 2


def test_recovery_refuses_empty_remaining_evidence():
    value = deepcopy(OUTPUT)
    value["experience_summary"][0]["evidence_episode_ids"] = ["invented"]
    with pytest.raises(ValueError, match="summary"):
        retain_referenced_experience(value, EVIDENCE)
