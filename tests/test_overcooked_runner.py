"""Real HTTP serialization through MockTransport; no live model calls."""
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from game_harness.providers import ProviderError, Settings


_spec = importlib.util.spec_from_file_location("run_overcooked", Path(__file__).parents[1] / "scripts/run_overcooked.py")
runner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runner)
MODEL = "fixture-decision-model"
KEY = "synthetic-overcooked-key-not-a-real-credential"
CATALOG = [{"id": action, "label": action} for action in ("north", "south", "east", "west", "stay", "interact")]
STATE = {"game_id": "overcooked", "step": 1, "native_tick": 0, "actor": "chef_1",
         "visible": {"players": [{"position": [1, 2]}, {"position": [3, 1]}]}}


def response(thought=None):
    return {"model": MODEL, "answers": {"primary_action": {
        "choice": "stay", "confidence": 1.0,
        "probabilities": {row["id"]: float(row["id"] == "stay") for row in CATALOG},
        "details": {"thought": {"used": False, "tokens": 0} if thought is None else thought},
    }}, "usage": {"total_tokens": 7}}


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Overcooked runner tests must not call a real model service")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)


def make_provider(tmp_path, monkeypatch, handler, *, budget=2):
    keyfile = tmp_path / "synthetic.key"
    keyfile.write_text(KEY)
    config = Settings("http://fixture.invalid/v1/systemone", MODEL, 1,
                      "https://coach-fixture.invalid/v1", "fixture-coach", "", 1,
                      decision_api_key_file=str(keyfile))
    original_client = httpx.Client

    def mock_client(**kwargs):
        return original_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(runner.httpx, "Client", mock_client)
    return runner.RecordingDecision(config, tmp_path, budget)


def test_recording_uses_actual_http_body_with_all_actions_and_no_credentials(tmp_path, monkeypatch):
    observed = []

    def handler(request):
        assert request.headers["authorization"] == "Bearer " + KEY
        observed.append(json.loads(request.content))
        return httpx.Response(200, json=response())

    provider = make_provider(tmp_path, monkeypatch, handler)
    try:
        receipt = provider.decide(deepcopy(STATE), "fixture frozen instructions", deepcopy(CATALOG), "request-1")
        assert receipt["request_payload"] == observed[0]
        payload = receipt["request_payload"]
        assert json.loads(payload["state"]) == STATE
        assert payload["model"] == MODEL and payload["think_mode"] == "never"
        assert payload["allow_none"] is False and payload["return_details"] is True
        assert payload["questions"]["primary_action"] == {
            "type": "choice", "instructions": "fixture frozen instructions",
            "criteria": {row["id"]: row["label"] for row in CATALOG}, "allow_none": False}
        assert receipt["request_evidence"] == {"state": STATE, "instructions": "fixture frozen instructions",
            "criteria": CATALOG, "request_id": "request-1", "think_mode": "never", "allow_none": False}
        assert receipt["think_used"] is False and receipt["reasoning_tokens"] == 0
        assert KEY not in json.dumps(receipt)
        log = (tmp_path / "calls.jsonl").read_text()
        assert KEY not in log and "authorization" not in log.lower() and "fixture.invalid" not in log
        records = [json.loads(line) for line in log.splitlines()]
        assert [row["kind"] for row in records] == ["decision_started", "decision_completed"]
        assert records[1]["usage"] == {"total_tokens": 7}
    finally:
        provider.close()


def test_each_request_capture_is_fresh_and_copied(tmp_path, monkeypatch):
    received = []

    def handler(request):
        received.append(json.loads(request.content))
        return httpx.Response(200, json=response())

    provider = make_provider(tmp_path, monkeypatch, handler)
    state, catalog = deepcopy(STATE), deepcopy(CATALOG)
    try:
        first = provider.decide(state, "first", catalog, "first")
        state["step"] = 2
        catalog[0]["label"] = "changed second request label"
        second = provider.decide(state, "second", catalog, "second")
        assert len(provider.payloads) == 1
        assert json.loads(first["request_payload"]["state"])["step"] == 1
        assert first["request_evidence"]["state"]["step"] == 1
        assert first["request_evidence"]["criteria"][0]["label"] == "north"
        assert first["request_payload"] == received[0]
        assert second["request_payload"] == received[1]
        assert provider.attempts == 2
    finally:
        provider.close()


def test_budget_prevents_next_http_request(tmp_path, monkeypatch):
    received = []

    def handler(request):
        received.append(request)
        return httpx.Response(200, json=response())

    provider = make_provider(tmp_path, monkeypatch, handler, budget=1)
    try:
        provider.decide(STATE, "fixture", CATALOG, "first")
        with pytest.raises(ProviderError, match="budget exhausted"):
            provider.decide(STATE, "fixture", CATALOG, "second")
        assert len(received) == provider.attempts == 1
        assert len((tmp_path / "calls.jsonl").read_text().splitlines()) == 2
    finally:
        provider.close()


def test_failed_http_attempt_also_consumes_budget_and_has_no_completion(tmp_path, monkeypatch):
    received = []

    def handler(request):
        received.append(request)
        return httpx.Response(503, text="fixture unavailable")

    provider = make_provider(tmp_path, monkeypatch, handler, budget=1)
    try:
        with pytest.raises(ProviderError, match="HTTP 503"):
            provider.decide(STATE, "fixture", CATALOG, "failed")
        with pytest.raises(ProviderError, match="budget exhausted"):
            provider.decide(STATE, "fixture", CATALOG, "retry")
        assert len(received) == provider.attempts == 1
        lines = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
        assert [row["kind"] for row in lines] == ["decision_started"]
    finally:
        provider.close()


@pytest.mark.parametrize("thought", [{}, {"used": False}, {"tokens": 0}, {"used": None, "tokens": None},
                                     {"used": True, "tokens": 0}, {"used": False, "tokens": 1},
                                     {"used": 0, "tokens": 0}, {"used": False, "tokens": False}])
def test_unverified_or_enabled_thinking_rejected_before_return(tmp_path, monkeypatch, thought):
    provider = make_provider(tmp_path, monkeypatch,
                             lambda request: httpx.Response(200, json=response(thought)))
    try:
        with pytest.raises(ProviderError):
            provider.decide(STATE, "fixture", CATALOG, "unverified")
        rows = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
        assert [row["kind"] for row in rows] == ["decision_started"]
    finally:
        provider.close()


def test_missing_http_hook_capture_rejected(tmp_path, monkeypatch):
    provider = make_provider(tmp_path, monkeypatch, lambda request: httpx.Response(200, json=response()))
    provider.client.event_hooks["request"].clear()
    try:
        with pytest.raises(ProviderError, match="captured HTTP request"):
            provider.decide(STATE, "fixture", CATALOG, "no-capture")
    finally:
        provider.close()


@pytest.mark.parametrize("failure", ["infra_error", "stopped"])
def test_evidence_lab_never_returns_incomplete_score(tmp_path, monkeypatch, failure):
    result = {"episode_id": "failed", "score": 0, "steps": 0,
              "infra_error": {"type": "fixture"} if failure == "infra_error" else None,
              "termination_reason": failure}
    monkeypatch.setattr(runner.Lab, "play", lambda *args, **kwargs: deepcopy(result))
    lab = object.__new__(runner.EvidenceLab)
    lab.store = SimpleNamespace(root=tmp_path)
    with pytest.raises(ProviderError, match="Incomplete episode"):
        lab.play(None, {}, {}, 42, "search_train")
    assert not (tmp_path / "audits").exists()


def test_evidence_lab_audits_before_score_returns(tmp_path, monkeypatch):
    import audit_overcooked_run
    events = []
    result = {"episode_id": "completed", "score": 20, "steps": 4,
              "infra_error": None, "termination_reason": "step_limit"}

    def saved_play(*args, **kwargs):
        events.append("saved")
        return deepcopy(result)

    def audit(episode, harness):
        assert events == ["saved"] and episode == result and harness == {"id": "h-fixture"}
        events.append("audited")
        return {"fixture_audit": "passed"}

    monkeypatch.setattr(runner.Lab, "play", saved_play)
    monkeypatch.setattr(audit_overcooked_run, "audit_episode", audit)
    lab = object.__new__(runner.EvidenceLab)
    lab.store = SimpleNamespace(root=tmp_path)
    observed = lab.play(None, {"id": "h-fixture"}, {}, 42, "search_train")
    events.append("returned")
    assert observed["score"] == 20 and events == ["saved", "audited", "returned"]
    assert json.loads((tmp_path / "audits/completed.json").read_text()) == {"fixture_audit": "passed"}


def test_audit_rejection_prevents_completed_score_from_returning(tmp_path, monkeypatch):
    import audit_overcooked_run
    result = {"episode_id": "completed", "score": 999, "steps": 4,
              "infra_error": None, "termination_reason": "step_limit"}
    monkeypatch.setattr(runner.Lab, "play", lambda *args, **kwargs: result)

    def rejected(*args):
        raise audit_overcooked_run.AuditError("fixture invalid evidence")

    monkeypatch.setattr(audit_overcooked_run, "audit_episode", rejected)
    lab = object.__new__(runner.EvidenceLab)
    lab.store = SimpleNamespace(root=tmp_path)
    with pytest.raises(audit_overcooked_run.AuditError, match="invalid evidence"):
        lab.play(None, {}, {}, 42, "search_train")
    assert not (tmp_path / "audits").exists()


def test_protocol_freezes_sources_and_budget_before_any_provider_call(tmp_path, monkeypatch):
    class StopAfterProtocol(Exception):
        pass

    config = Settings("http://fixture.invalid/v1/systemone", MODEL, 1,
                      "https://coach-fixture.invalid/v1", "fixture-coach", "", 1)
    monkeypatch.setattr(runner.Settings, "load", lambda: config)
    closed = []
    adapter = SimpleNamespace(defaults={"max_steps": 240, "overcooked_layout": "cramped_room"},
        validate_config=lambda config: None, context=lambda config: {"game_id": "overcooked"},
        create=lambda seed, config: SimpleNamespace(close=lambda: closed.append(True)))
    monkeypatch.setattr(runner, "get_game", lambda _: adapter)

    def stop(*args, **kwargs):
        assert (tmp_path / "new-run/protocol.json").is_file()
        raise StopAfterProtocol()

    monkeypatch.setattr(runner, "EvidenceLab", stop)
    with pytest.raises(StopAfterProtocol):
        runner.run(tmp_path / "new-run", "cramped_room", 240)
    protocol = json.loads((tmp_path / "new-run/protocol.json").read_text())
    assert closed == [True]
    assert protocol["budget"] == {"max_episodes": 14, "decision_request_attempts_max": 3360,
                                  "coach_attempts_max": 4, "gepa_reflections": 1}
    assert protocol["schedule"] == {"train_seeds": [42], "validation_seeds": [4242],
                                    "promotion_seeds": [1042, 2042, 3042, 4042]}
    required = {"src/game_harness/runtime.py", "src/game_harness/providers.py",
                "src/game_harness/evolution.py", "src/game_harness/feedback.py",
                "src/game_harness/games/overcooked.py", "src/game_harness/games/overcooked_worker.py",
                "scripts/run_overcooked.py", "scripts/audit_overcooked_run.py", "requirements-overcooked.txt"}
    assert required.issubset(protocol["source_hashes"])
    for path, content_hash in protocol["source_hashes"].items():
        assert not Path(path).is_absolute()
        assert content_hash == hashlib.sha256((runner.ROOT / path).read_bytes()).hexdigest()
    assert "fixture.invalid" not in json.dumps(protocol)
    assert not (tmp_path / "new-run/calls.jsonl").exists()


def test_run_refuses_existing_evidence_directory_before_loading_settings(tmp_path, monkeypatch):
    marker = tmp_path / "protocol.json"
    marker.write_text("preserve me")
    monkeypatch.setattr(runner.Settings, "load", lambda: pytest.fail("must reject before loading settings"))
    with pytest.raises(ValueError, match="already exists"):
        runner.run(tmp_path, "cramped_room", 240)
    assert marker.read_text() == "preserve me"
