"""Offline planner provenance, call limits, and exact body archival."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import initialize_tetris_landing_planner as module


PROMPT = (
    "本次只选择目标，从完整42项目录比较条件落点后果并选择target、keep或clear。"
    "读取tetris_landing_projections，null表示未产生投影，不能当作零空洞。"
    "投影不是原生按键路径可达保证。holes_delta是净变化。目标不执行按键；"
    "每个实际动作仍由Decision另行决策，think_mode=never。"
)
OUTPUT = {"goal_instructions": PROMPT}
PARENT = {"id": "parent", "game_id": "tetris", "candidate": {
    "decision_instructions": "Frozen action instructions", "strategy_notes": "Frozen strategy"},
    "game_context": {"rules": "Fixture rules"}, "config": {"max_steps": 80},
    "rules_version": "fixture-rules", "think_mode": "never", "decision_model": "fixture-decision"}
SETTINGS = SimpleNamespace(decision_model="fixture-decision", coach_model="glm-5.3",
    coach_timeout=2, coach_base_url="https://example.invalid/v1", coach_api_key="fixture-secret-must-not-appear")


class FakeCoach:
    def __init__(self, outputs):
        self.outputs = iter(outputs)
        self.calls = []
        self.messages = []

    def complete(self, messages, purpose):
        self.messages.append(deepcopy(messages))
        output = next(self.outputs)
        if isinstance(output, Exception):
            raise output
        text = json.dumps(output)
        self.calls.append({"purpose": purpose, "requested_model": "glm-5.3", "returned_model": "glm-5.3",
            "response_hash": module.digest(text), "usage": {"prompt_tokens": 10}})
        return text


def fixture_inputs(tmp_path, monkeypatch):
    parent_path = tmp_path / "parent.json"
    parent_path.write_text(json.dumps(PARENT))
    evidence_path = tmp_path / "old-episode.json"
    evidence_path.write_text('{"fixture":true}')
    evidence = {"input_files": [module.file_record(evidence_path)], "excerpt_indices": [0, 1, 2, 3, 4, 5, 8, 9, 10, 11],
        "excerpts": [{"step": 0, "action": "hard-drop:100"}]}
    monkeypatch.setattr(module, "get_game", lambda name: SimpleNamespace(context=lambda config: PARENT["game_context"]))
    monkeypatch.setattr(module, "history_evidence", lambda root, parent: deepcopy(evidence))
    return parent_path, evidence


def run_fixture(tmp_path, monkeypatch, outputs):
    parent_path, evidence = fixture_inputs(tmp_path, monkeypatch)
    coach = FakeCoach(outputs)
    result = module.initialize(tmp_path / "output", parent_path, tmp_path / "historical",
                               settings=SETTINGS, coach=coach)
    return result, coach, evidence


def test_one_valid_response_is_accepted_exactly_without_changing_action_policy(tmp_path, monkeypatch):
    result, coach, evidence = run_fixture(tmp_path, monkeypatch, [OUTPUT])
    assert set(result) == {"schema", "goal_instructions", "provenance"}
    assert result["schema"] == "tetris-landing-planner/v1"
    assert result["goal_instructions"] == PROMPT
    assert len(coach.calls) == 1
    assert result["provenance"]["new_decision_calls"] == 0
    assert result["provenance"]["new_game_results_seen"] is False
    assert result["provenance"]["action_policy_changed"] is False
    assert result["provenance"]["goal_ids_and_order_changed"] is False
    assert result["provenance"]["goal_criterion_descriptions_added"] is True
    assert result["provenance"]["historical_evidence_hash"] == module.digest(evidence)
    assert json.loads((tmp_path / "output/parent.json").read_text()) == PARENT
    assert not (tmp_path / "output/coach-2.input.json").exists()
    assert json.loads((tmp_path / "output/calls.json").read_text())[0]["status"] == "accepted"


def test_schema_retry_is_only_second_call_and_retains_invalid_response(tmp_path, monkeypatch):
    result, coach, _ = run_fixture(tmp_path, monkeypatch, [{"candidate": "not permitted"}, OUTPUT])
    assert len(coach.calls) == 2
    assert result["goal_instructions"] == PROMPT
    assert coach.calls[1]["purpose"] == "tetris_landing_goal_schema_repair"
    second_input = json.loads((tmp_path / "output/coach-2.input.json").read_text())
    assert second_input[-2]["role"] == "assistant"
    assert json.loads(second_input[-2]["content"]) == {"candidate": "not permitted"}
    assert (tmp_path / "output/coach-1.rejected.json").exists()


def test_second_invalid_response_stops_without_third_call_or_planner(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="Expected exactly"):
        run_fixture(tmp_path, monkeypatch, [{}, {}, OUTPUT])
    assert not (tmp_path / "output/coach-3.input.json").exists()
    assert not (tmp_path / "output/planner.json").exists()
    failure = json.loads((tmp_path / "output/failed.json").read_text())
    assert failure == {"type": "ValueError", "attempted_calls": 2, "returned_calls": 2}


def test_api_failure_does_not_trigger_schema_retry_or_serialize_error_secret(tmp_path, monkeypatch):
    with pytest.raises(module.ProviderError):
        run_fixture(tmp_path, monkeypatch, [module.ProviderError(SETTINGS.coach_api_key), OUTPUT])
    assert not (tmp_path / "output/coach-2.input.json").exists()
    assert not (tmp_path / "output/planner.json").exists()
    assert SETTINGS.coach_api_key not in (tmp_path / "output/failed.json").read_text()
    assert json.loads((tmp_path / "output/calls.json").read_text())[0]["status"] == "requested"


@pytest.mark.parametrize("output", [{"goal_instructions": PROMPT, "candidate": {}},
    {"goal_instructions": PROMPT + " "}, {"goal_instructions": "short"},
    {"goal_instructions": None}, {"goal_instructions": "a" * 2401}])
def test_invalid_output_does_not_get_normalized_or_accept_extra_strategy(output):
    with pytest.raises(ValueError):
        module.validate_output(output)


def test_existing_output_refused_before_model_or_settings_read(tmp_path, monkeypatch):
    destination = tmp_path / "existing"
    destination.mkdir()
    marker = destination / "planner.json"
    marker.write_text("immutable")
    with pytest.raises(ValueError, match="fresh output"):
        module.initialize(destination, tmp_path / "absent-parent")
    assert marker.read_text() == "immutable"


def test_prompt_exposes_fixed_sources_full_target_ids_and_honest_projection_contract():
    original = deepcopy(PARENT)
    messages = module.messages_for(PARENT, {"fixed": True})
    payload = json.loads(messages[1]["content"])
    ids = [item["id"] for item in payload["goal_catalog"]]
    assert ids == ["keep", "clear"] + [f"target:{x}:{r}" for x in range(10) for r in range(4)]
    assert payload["immutable_action_policy_reference_only"] == PARENT["candidate"]
    assert payload["interface"]["native_contract"] == module.LANDING_CONTRACT
    assert "not_proven" in payload["interface"]["projection"]
    assert "points_delta" in payload["interface"]["choice_descriptions"]
    assert all(source["commit"] in url for source in payload["research"] for url in source["urls"])
    assert PARENT == original


def test_actual_coach_json_and_full_response_archived_without_authentication(tmp_path, monkeypatch):
    parent_path, _ = fixture_inputs(tmp_path, monkeypatch)
    response_body = {"model": "glm-5.3", "choices": [{"finish_reason": "stop", "message": {
        "content": json.dumps(OUTPUT), "role": "assistant"}}], "usage": {"prompt_tokens": 123, "completion_tokens": 44}}
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=response_body)
    client = httpx.Client(transport=httpx.MockTransport(handle))
    archive_class = module._ArchivedCoachClient
    monkeypatch.setattr(module, "_ArchivedCoachClient",
        lambda destination, timeout: archive_class(destination, timeout, client=client))
    result = module.initialize(tmp_path / "output", parent_path, tmp_path / "history", settings=SETTINGS)
    assert result["goal_instructions"] == PROMPT
    assert len(requests) == 1
    request = json.loads((tmp_path / "output/coach-1.request.json").read_text())
    assert request == json.loads(requests[0].content)
    assert request["thinking"] == {"type": "disabled"}
    assert request["model"] == "glm-5.3"
    assert json.loads((tmp_path / "output/coach-1.response.json").read_text()) == response_body
    archived = "".join(path.read_text() for path in (tmp_path / "output").glob("*.json"))
    assert SETTINGS.coach_api_key not in archived
    assert "Authorization" not in archived


def test_evidence_indices_and_hashes_are_fixed_before_any_score_selection(tmp_path, monkeypatch):
    episode_path = tmp_path / module.EPISODE_RELATIVE
    episode_path.parent.mkdir(parents=True)
    trace = []
    for i in range(12):
        trace.append({"action": "left:100", "before": {"score": 0, "observation": {"step": i}},
            "after": {"score": 0, "observation": {"step": i + 1}},
            "receipt": {"tetris_context": {"memory_after_planning": {"target": {"leftmost_column": 3, "rotation": 0}}}}})
    episode = {"episode_id": "historical-fixture", "termination_reason": "TOP_OUT", "steps": 12,
        "score": 0, "trace": trace}
    episode_path.write_text(json.dumps(episode))
    (tmp_path / "manifest.json").write_text(json.dumps({"schema": "tetris-vision-experiment/v1", "arms": {"vision": PARENT}}))
    calls = []
    def audit(episode, versions):
        calls.append((episode, versions))
        return {"thinking_verified": 12, "context_audit": {"goal_calls": 4, "thinking_off_goal_calls": 4}}
    monkeypatch.setattr(module, "_audit_episode", audit)
    evidence = module.history_evidence(tmp_path, PARENT)
    assert evidence["excerpt_indices"] == [0, 1, 2, 3, 4, 5, 8, 9, 10, 11]
    assert [row["step"] for row in evidence["excerpts"]] == evidence["excerpt_indices"]
    assert len(calls) == 1
    assert evidence["input_files"][1]["sha256"] == hashlib.sha256(episode_path.read_bytes()).hexdigest()
    assert evidence["source_json_sha256"] == module.digest(episode)
    altered = deepcopy(PARENT)
    altered["candidate"]["strategy_notes"] = "different policy"
    with pytest.raises(ValueError, match="exact frozen parent"):
        module.history_evidence(tmp_path, altered)
