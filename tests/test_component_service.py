"""Profile isolation, real initialization payloads, and independent export gates."""
from copy import deepcopy
import json

import httpx
import pytest

from game_harness.components import context_config, upgrade_tetris_candidate
from game_harness.exporting import export_approved, read_bundle
from game_harness.games import get_game
from game_harness.providers import CoachProvider, ProviderError, Settings
from game_harness.service import Lab


BASE = {"decision_instructions": "Choose a native key.", "strategy_notes": "Use current facts."}
FULL = upgrade_tetris_candidate(BASE, "Compare all conditional outcomes before choosing a target.")
BRIEF = {key: "fixture" for key in ("background", "goal", "rules", "precautions", "strategy")}


def settings():
    return Settings("http://127.0.0.1:1/v1/systemone", "fixture-decision", 1,
                    "https://fixture.invalid/v1", "fixture-coach", "fixture-key", 1)


def test_component_initialization_has_actual_fields_and_strict_schema():
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        content = json.dumps({"brief": BRIEF, "candidate": FULL})
        return httpx.Response(200, json={"model": "fixture-coach", "choices": [
            {"message": {"content": content}, "finish_reason": "stop"}]})

    coach = CoachProvider(settings(), client=httpx.Client(transport=httpx.MockTransport(respond)))
    try:
        _, candidate = coach.initialize(game_context=get_game("tetris").context({"max_steps": 80}),
                                        component_profile="tetris-components-v1")
        assert candidate == FULL
        payload = requests[0]["messages"][0]["content"]
        assert "tetris_landing_projections" in payload and "memory_policy" in payload
        assert coach.initialization_contract["player"]["search_helpers"]
        assert "stateless：" not in coach.initialization_contract["player"]["memory"]
        assert coach.initialization_provenance["component_profile"] == "tetris-components-v1"
        with pytest.raises(ProviderError, match="仅支持"):
            coach.initialize(game_context=get_game("snake").context({"max_steps": 80, "board_size": 8}),
                             component_profile="tetris-components-v1")
        assert len(requests) == 1
    finally:
        coach.close()


def test_component_scopes_do_not_replace_legacy_or_other_games(tmp_path, monkeypatch):
    lab = Lab(settings(), tmp_path)

    def worker(*args):
        lab.busy = False

    monkeypatch.setattr(lab, "_run", worker)

    def start(options):
        lab.start(options)
        lab.worker.join(2)
        return lab.scope

    try:
        legacy = start({"game_id": "tetris"})
        full = start({"harness_profile": "tetris-components-v1"})
        experience = start({"use_experience": True})
        assert len({legacy, full, experience}) == 3
        start({"game_id": "snake"})
        assert lab.config["harness_profile"] == "text-v1" and lab.config["use_experience"] is False
        assert start({"game_id": "tetris"}) == legacy
        with pytest.raises(ValueError, match="仅支持"):
            start({"game_id": "snake", "harness_profile": "tetris-components-v1"})
    finally:
        lab.store.db.close()


def test_five_component_export_keeps_independent_gate_and_runtime_identity(tmp_path):
    context = get_game("tetris").context({"max_steps": 80})
    version = {"id": "fixture-full", "scope": "full-scope", "candidate": FULL,
               "decision_model": "fixture-decision", "parent_id": "fixture-parent",
               "input_mode": "structured+vision"}
    job = {"id": "fixture-job", "candidate_id": version["id"], "scope": version["scope"],
           "status": "retained", "gate": {"minimum_pairs": 4, "minimum_mean_gain": 100},
           "pairs": [{"seed": i, "baseline": 100, "candidate": 200} for i in range(4)]}
    with pytest.raises(ValueError, match="approved"):
        export_approved(tmp_path, version, job, context, BRIEF)
    job["status"] = "promoted"
    bad = deepcopy(job)
    bad["pairs"][0]["candidate"] = 0
    with pytest.raises(ValueError, match="non-regressing"):
        export_approved(tmp_path, version, bad, context, BRIEF)
    manifest = export_approved(tmp_path, version, job, context, BRIEF)
    assert manifest["runtime_context"] == context_config(FULL)
    assert manifest["input_mode"] == "structured+vision"
    assert read_bundle(tmp_path, "tetris", version["id"])["strategy"] == FULL
    with pytest.raises(ValueError, match="input mode"):
        export_approved(tmp_path, {**version, "input_mode": "structured"}, job, context, BRIEF)
    path = tmp_path / "tetris" / version["id"] / "manifest.json"
    manifest["runtime_context"]["replan_every_actions"] = 8
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="runtime contract"):
        read_bundle(tmp_path, "tetris", version["id"])


def test_experience_collection_cannot_learn_promotion_results(tmp_path):
    """Only actual training contrasts reach the library helper, never outcome scores."""
    from game_harness.evolution import _digest
    child = {**FULL, "goal_instructions": "Revised goal policy."}
    seen = []

    class Library:
        def record_from_pairs(self, **kwargs):
            seen.append(kwargs)
            return {"id": "fixture-entry"}

    lab = Lab(settings(), tmp_path)
    try:
        old = {"seed": 10, "purpose": "search_train", "score": 100}
        new = {"seed": 10, "purpose": "search_train", "score": 0}
        outcome = {"proposals": [{"candidate": child, "parent_hash": _digest(FULL)}],
                   "promotion_scores": "DO_NOT_PASS_TO_LIBRARY"}
        lab._record_training_experience(Library(), "fixture", FULL, outcome,
                                        [(FULL, old), (child, new)], None, {}, {"id": "job-fixture"})
        assert len(seen) == 1 and seen[0]["pairs"] == [{"parent": old, "candidate": new}]
        assert "DO_NOT_PASS" not in json.dumps(seen[0])
    finally:
        lab.store.db.close()


def test_corrupt_experience_library_leaves_retryable_failed_job(tmp_path, monkeypatch):
    from game_harness.experience import ExperienceLibrary
    lab = Lab(settings(), tmp_path)
    try:
        config = {**lab.config, "game_id": "tetris", "harness_profile": "tetris-components-v1", "use_experience": True}
        baseline = {"id": "parent", "candidate": FULL}
        monkeypatch.setattr(lab.store, "active", lambda scope: baseline)
        monkeypatch.setattr(lab.store, "load_episode", lambda identity: {"episode_id": identity, "seed": 9})
        def corrupt(self):
            raise ValueError("Experience content hash mismatch")
        monkeypatch.setattr(ExperienceLibrary, "snapshot_sequence", corrupt)
        job = {"id": "fixture-corrupt", "scope": "fixture", "episode_id": "trigger", "config": config,
               "train_seeds": [9], "validation_seeds": [10], "promotion_seeds": [11, 12, 13, 14]}
        with pytest.raises(ValueError, match="hash mismatch"):
            lab.optimize(job, None, None)
        assert lab.store.get("job", job["id"])["status"] == "failed"
    finally:
        lab.store.db.close()
