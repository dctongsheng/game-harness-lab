"""Decision Bearer-file authentication with synthetic keys and mock HTTP only."""
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path

import httpx
import pytest

from game_harness import providers
from game_harness.providers import DecisionProvider, ProviderError, Settings


FAKE_KEY = "fixture-only-not-a-real-decision-key"
MODEL = "fixture-decision-model"
STATE = {"step": 3, "game_id": "tetris", "visible": "fixture observation"}
CHOICES = [{"id": "left:100", "label": "left input"}, {"id": "hard-drop:100", "label": "drop input"}]


def settings(key_file=""):
    return Settings("http://127.0.0.1:1/v1/systemone", MODEL, 1,
                    "https://coach-fixture.invalid/v1", "fixture-coach", "", 1,
                    decision_api_key_file=str(key_file))


def response():
    return {"model": MODEL, "answers": {"primary_action": {
        "choice": "left:100", "confidence": 0.75,
        "probabilities": {"left:100": 0.75, "hard-drop:100": 0.25},
        "details": {"thought": {"used": False, "tokens": 0}},
    }}, "usage": {"total_tokens": 7}}


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Authentication tests must use mock HTTP, never a real provider")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)


def test_bearer_file_authenticates_health_and_choice_without_payload_or_receipt_leak(tmp_path):
    key_file = tmp_path / "fixture-api.key"
    key_file.write_text("  " + FAKE_KEY + "\n", encoding="utf-8")
    received = []

    def handler(request):
        received.append(request)
        assert request.headers["authorization"] == "Bearer " + FAKE_KEY
        assert FAKE_KEY not in str(request.url)
        if request.method == "GET":
            assert request.url.path == "/health"
            return httpx.Response(200, json={"model": MODEL, "think_mode": "auto"})
        assert request.method == "POST" and request.url.path == "/v1/systemone"
        payload = json.loads(request.content)
        assert payload["think_mode"] == "never" and payload["return_details"] is True
        assert payload["allow_none"] is False
        assert payload["model"] == MODEL
        question = payload["questions"]["primary_action"]
        assert question["type"] == "choice" and question["allow_none"] is False
        assert question["criteria"] == {row["id"]: row["label"] for row in CHOICES}
        assert json.loads(payload["state"]) == STATE
        assert FAKE_KEY not in request.content.decode()
        assert "authorization" not in payload and "api_key" not in payload
        return httpx.Response(200, json=response())

    state, choices = deepcopy(STATE), deepcopy(CHOICES)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = DecisionProvider(settings(key_file), client=client)
        health = provider.health()
        receipt = provider.decide(state, "fixture choice instruction", choices, "opaque-fixture-request")
        assert health["ok"] and health["model"] == MODEL and health["request_think_mode"] == "never"
        assert receipt["choice_id"] == "left:100" and receipt["think_mode"] == "never"
        assert receipt["think_used"] is False and receipt["reasoning_tokens"] == 0
        assert receipt["raw_response"] == response()
        assert FAKE_KEY not in json.dumps({"state": state, "receipt": receipt, "health": health,
                                           "settings": asdict(provider.settings)})
    assert [request.method for request in received] == ["GET", "POST"]
    assert state == STATE and choices == CHOICES


def test_default_without_key_file_sends_no_authorization():
    received = []

    def handler(request):
        received.append(request)
        assert "authorization" not in request.headers
        return httpx.Response(200, json={"model": MODEL} if request.method == "GET" else response())

    # Seven-argument construction remains compatible with existing callers.
    config = Settings("http://127.0.0.1:1/v1/systemone", MODEL, 1,
                      "https://coach-fixture.invalid/v1", "fixture-coach", "", 1)
    assert config.decision_api_key_file == ""
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = DecisionProvider(config, client=client)
        assert provider.health()["ok"]
        provider.decide(deepcopy(STATE), "fixture", deepcopy(CHOICES), "fixture-no-auth")
    assert len(received) == 2


@pytest.mark.parametrize("contents", ["", " \t\n\r ", FAKE_KEY + "\nsecond-line", FAKE_KEY + "\rsecond-line"])
def test_empty_or_embedded_newline_key_fails_before_request_without_exposing_secret(tmp_path, contents):
    key_file = tmp_path / "fixture-secret-file.key"
    key_file.write_text(contents, encoding="utf-8")
    with httpx.Client(transport=httpx.MockTransport(lambda request: pytest.fail("Invalid key sent a request"))) as client:
        with pytest.raises(ProviderError) as failure:
            DecisionProvider(settings(key_file), client=client)
    assert FAKE_KEY not in str(failure.value) and str(key_file) not in str(failure.value)


def test_missing_key_file_fails_before_request_without_disclosing_path(tmp_path):
    key_file = tmp_path / "missing-fixture-api.key"
    with httpx.Client(transport=httpx.MockTransport(lambda request: pytest.fail("Missing key sent a request"))) as client:
        with pytest.raises(ProviderError) as failure:
            DecisionProvider(settings(key_file), client=client)
    assert str(key_file) not in str(failure.value)


def test_unreadable_key_file_error_is_generic(tmp_path, monkeypatch):
    key_file = tmp_path / "fixture-api.key"
    original = Path.read_text

    def unreadable(path, *args, **kwargs):
        if path == key_file:
            raise OSError("fixture permission failure " + FAKE_KEY + " " + str(path))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)
    with httpx.Client(transport=httpx.MockTransport(lambda request: pytest.fail("Unreadable key sent a request"))) as client:
        with pytest.raises(ProviderError) as failure:
            DecisionProvider(settings(key_file), client=client)
    assert FAKE_KEY not in str(failure.value) and str(key_file) not in str(failure.value)


def test_settings_load_reads_path_only_from_environment_without_reading_the_key(tmp_path, monkeypatch):
    key_file = tmp_path / "not-created-fixture.key"
    monkeypatch.setattr(providers, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setenv("DECISION_API_KEY_FILE", str(key_file))
    monkeypatch.setenv("DECISION_THINK_MODE", "never")
    config = Settings.load()
    assert config.decision_api_key_file == str(key_file)
    assert not key_file.exists()
    monkeypatch.delenv("DECISION_API_KEY_FILE")
    assert Settings.load().decision_api_key_file == ""
