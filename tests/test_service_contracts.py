"""Service contracts with synthetic HTTP responses and isolated temporary stores.

These tests make no model/API calls and do not exercise or launch a browser.
"""

from copy import deepcopy
import importlib
import json
import threading

import httpx
import pytest
from fastapi.testclient import TestClient

from game_harness.app import create_app
from game_harness.providers import CoachProvider, DecisionProvider, ProviderError, Settings
from game_harness.runtime import run_episode
from game_harness.service import DEFAULTS, Lab
from game_harness.snake import ACTION_CANDIDATES, GAME_RULES, candidate_hash


SECRET = "fixture-key-never-expose-7d421"
BASE = {"decision_instructions": "Observe and choose", "strategy_notes": "Fixture baseline"}
CHILD = {"decision_instructions": "Compare space", "strategy_notes": "Fixture candidate"}


@pytest.fixture(autouse=True)
def forbid_real_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Tests must use MockTransport; a real HTTP request was attempted")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)


@pytest.fixture
def settings():
    return Settings("http://127.0.0.1:1/v1/systemone", "decision-fixture", 1,
                    "https://coach.invalid/v1", "coach-fixture", SECRET, 1)


@pytest.fixture
def lab(tmp_path, settings):
    instance = Lab(settings, tmp_path)
    yield instance
    if instance.worker is not None:
        instance.stop_event.set()
        instance.worker.join(timeout=2)
    instance.store.db.close()


def response_body(model="decision-fixture"):
    return {"model": model, "answers": {"primary_action": {
        "choice": "right", "confidence": .7,
        "probabilities": {"up": .1, "right": .7, "down": .1, "left": .1},
        "details": {"thought": {"used": False, "tokens": 0}},
    }}}


def make_decision(settings, body, capture=None, status=200):
    def handler(request):
        if capture is not None:
            capture.append(request)
        return httpx.Response(status, json=body)

    return DecisionProvider(settings, httpx.Client(transport=httpx.MockTransport(handler)))


def seed_active(lab, scope="scope-a"):
    lab.scope = scope
    version = lab.version(BASE, scope, {"type": "bootstrap"})
    lab.store.activate(scope, version["id"], None)
    return version


def test_bootstrap_persists_decision_initialization_contract(lab, monkeypatch):
    """The reviewed player contract survives startup in the saved version."""
    provenance = {"schema_version": "decision-initialization-v1", "contract_hash": "fixture"}

    class Player:
        def __init__(self, *args, **kwargs):
            pass

        def health(self):
            return {"ok": True}

        def close(self):
            pass

    class Coach(Player):
        initialization_provenance = provenance

        def initialize(self, *args, **kwargs):
            return {"background": "fixture"}, BASE

    monkeypatch.setattr("game_harness.service.DecisionProvider", Player)
    monkeypatch.setattr("game_harness.service.CoachProvider", Coach)
    config = {**DEFAULTS, "game_id": "snake", "episodes": 0}
    lab._run(config, "contract-scope")
    version = lab.store.active("contract-scope")
    assert version["provenance"]["initialization"] == provenance
    provenance["contract_hash"] = "changed-after-initialization"
    assert lab.store.get("version", version["id"])["provenance"]["initialization"]["contract_hash"] == "fixture"


def episode(version, *, identity="ep-fixture", purpose="play", infra_error=None):
    return {"episode_id": identity, "seed": 101, "harness_id": version["id"],
            "scope": version["scope"], "config": dict(DEFAULTS), "purpose": purpose,
            "score": 1, "steps": 4, "termination_reason": "infra_error" if infra_error else "wall_collision",
            "model": "decision-fixture", "latencies_ms": [2, 4], "trace": [],
            "infra_error": infra_error}


def test_decision_wire_protocol_has_all_actions_fixed_model_and_no_thinking(settings):
    requests = []
    provider = make_decision(settings, response_body(), requests)
    state = {"step": 9, "snake": [[1, 1]], "food": [2, 1]}
    try:
        receipt = provider.decide(state, "fixed instructions", ACTION_CANDIDATES, "request-9")
    finally:
        provider.close()
    payload = json.loads(requests[0].content)
    assert requests[0].url.path == "/v1/systemone"
    assert payload["model"] == settings.decision_model
    assert payload["think_mode"] == "never" and payload["return_details"] is True
    assert payload["allow_none"] is False
    assert json.loads(payload["state"]) == state
    question = payload["questions"]["primary_action"]
    assert question["type"] == "choice"
    assert question["allow_none"] is False
    assert list(question["criteria"]) == ["up", "right", "down", "left"]
    assert question["instructions"] == "fixed instructions"
    assert receipt["request_id"] == "request-9" and receipt["observation_revision"] == 9
    assert receipt["candidate_hash"] == candidate_hash(ACTION_CANDIDATES)
    assert receipt["thinking_verification"] == "verified"
    assert SECRET not in json.dumps(receipt)
    assert "authorization" not in requests[0].headers


def test_decision_null_choice_with_valid_42_way_distribution_is_not_locally_argmaxed(settings):
    criteria = [{"id": f"move-{i}", "label": f"Legal fixture move {i}"} for i in range(42)]
    probabilities = {item["id"]: .25 / 41 for item in criteria}
    probabilities["move-17"] = .75
    body = response_body()
    body["answers"]["primary_action"].update(
        choice=None, confidence=.75, probabilities=probabilities)
    requests = []
    provider = make_decision(settings, body, requests)
    try:
        with pytest.raises(ProviderError, match="没有执行动作"):
            provider.decide({"step": 3}, "Select one legal move", criteria, "request-null")
    finally:
        provider.close()
    assert len(requests) == 1  # No retry or replacement decision was attempted.
    payload = json.loads(requests[0].content)
    assert payload["allow_none"] is False and payload["think_mode"] == "never"
    assert payload["questions"]["primary_action"]["allow_none"] is False
    assert set(payload["questions"]["primary_action"]["criteria"]) == set(probabilities)


def test_null_decision_stops_game_before_any_move_despite_clear_probability_winner(settings):
    body = response_body()
    body["answers"]["primary_action"]["choice"] = None
    requests = []
    provider = make_decision(settings, body, requests)
    try:
        result = run_episode(provider, {"id": "h-null", "candidate": BASE},
                             episode_id="ep-null", seed=0, max_steps=8)
    finally:
        provider.close()
    assert len(requests) == 1
    assert result["termination_reason"] == "infra_error"
    assert result["infra_error"]["type"] == "ProviderError"
    assert result["steps"] == result["final_state"]["step"] == 0
    assert result["trace"] == []
    assert result["thinking_verification"] == "not_reported"


@pytest.mark.parametrize("mutate", [
    lambda body: body.update(model="unexpected-model"),
    lambda body: body["answers"]["primary_action"].update(choice="teleport"),
    lambda body: body["answers"]["primary_action"]["probabilities"].pop("left"),
    lambda body: body["answers"]["primary_action"].update(confidence=2),
    lambda body: body["answers"]["primary_action"]["details"].update(thought={"used": True, "tokens": 0}),
    lambda body: body["answers"]["primary_action"]["details"].update(thought={"used": False, "tokens": 12}),
])
def test_bad_decision_response_fails_closed(settings, mutate):
    body = response_body()
    mutate(body)
    provider = make_decision(settings, body)
    try:
        with pytest.raises(ProviderError):
            provider.decide({"step": 0}, "instructions", ACTION_CANDIDATES, "r")
    finally:
        provider.close()


def test_missing_thought_telemetry_does_not_claim_zero_thinking(settings):
    body = response_body()
    body["answers"]["primary_action"].pop("details")
    provider = make_decision(settings, body)
    try:
        receipt = provider.decide({"step": 0}, "instructions", ACTION_CANDIDATES, "r")
    finally:
        provider.close()
    assert receipt["thinking_verification"] == "not_reported"
    assert receipt["think_used"] is None and receipt["reasoning_tokens"] is None


def test_health_rejects_a_different_model(settings):
    provider = make_decision(settings, {"model": "wrong-model", "status": "healthy"})
    try:
        with pytest.raises(ProviderError, match="模型"):
            provider.health()
    finally:
        provider.close()


def test_settings_refuses_enabling_thinking_without_reading_real_dotenv(monkeypatch):
    monkeypatch.setattr("game_harness.providers.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("DECISION_THINK_MODE", "auto")
    with pytest.raises(ProviderError, match="never"):
        Settings.load()


def test_coach_key_is_header_only_and_not_in_call_records(settings):
    requests, calls = [], []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"model": "coach-fixture", "usage": {"total_tokens": 8},
            "choices": [{"finish_reason": "stop", "message": {"content": "fixture answer"}}]})

    coach = CoachProvider(settings, on_call=calls.append,
                          client=httpx.Client(transport=httpx.MockTransport(handler)))
    try:
        assert coach.complete([{"role": "user", "content": "test"}]) == "fixture answer"
    finally:
        coach.close()
    assert requests[0].headers["authorization"] == "Bearer " + SECRET
    payload = json.loads(requests[0].content)
    assert payload["model"] == "coach-fixture" and payload["thinking"] == {"type": "disabled"}
    assert SECRET not in json.dumps(payload)
    assert SECRET not in json.dumps(calls)
    assert SECRET not in json.dumps(coach.calls)


def test_api_error_body_cannot_leak_key_into_provider_error(settings):
    def handler(request):
        return httpx.Response(401, json={"error": "Authorization: Bearer " + SECRET})

    for provider in (
        DecisionProvider(settings, httpx.Client(transport=httpx.MockTransport(handler))),
        CoachProvider(settings, client=httpx.Client(transport=httpx.MockTransport(handler))),
    ):
        try:
            with pytest.raises(ProviderError) as caught:
                if isinstance(provider, DecisionProvider):
                    provider.decide({"step": 0}, "test", ACTION_CANDIDATES, "r")
                else:
                    provider.complete([{"role": "user", "content": "test"}])
            assert "401" in str(caught.value)
            assert SECRET not in str(caught.value)
        finally:
            provider.close()


def test_restart_repairs_orphan_game_job_once_and_keeps_seed_splits(tmp_path, settings):
    first = Lab(settings, tmp_path)
    version = seed_active(first)
    original = episode(version)
    first.store.save_episode(original)
    first.store.save_episode(episode(version, identity="ep-eval", purpose="search_train"))
    first.store.db.close()

    recovered = Lab(settings, tmp_path)
    jobs = recovered.store.list("job")
    assert len(jobs) == 1 and jobs[0]["episode_id"] == "ep-fixture"
    job = jobs[0]
    assert job["train_seeds"][0] == original["seed"]
    train, validation, promotion = map(set, (job["train_seeds"], job["validation_seeds"], job["promotion_seeds"]))
    assert not train & validation and not train & promotion and not validation & promotion
    recovered.store.db.close()

    restarted_again = Lab(settings, tmp_path)
    assert len(restarted_again.store.list("job")) == 1
    assert restarted_again.store.get("job", job["id"])["train_seeds"] == job["train_seeds"]
    restarted_again.store.db.close()


def test_restart_marks_inflight_job_interrupted_without_losing_manifest(tmp_path, settings):
    first = Lab(settings, tmp_path)
    version = seed_active(first)
    result = episode(version)
    first.store.save_episode(result)
    job = first.new_job(result, dict(DEFAULTS), version["scope"])
    job.update(status="evaluating", attempts=2)
    first.store.put("job", job)
    first.store.db.close()
    restored = Lab(settings, tmp_path)
    saved = restored.store.get("job", job["id"])
    assert saved["status"] == "interrupted" and saved["attempts"] == 2
    assert saved["promotion_seeds"] == job["promotion_seeds"]
    restored.store.db.close()


def test_infra_failure_trigger_is_replayed_before_evolution(lab, monkeypatch):
    version = seed_active(lab)
    failed = episode(version, infra_error={"message": "fixture outage"})
    lab.store.save_episode(failed)
    job = lab.new_job(failed, dict(DEFAULTS), version["scope"])
    calls = []

    def recovered_play(provider, harness, config, seed, purpose, job_id):
        assert purpose == "recovery" and seed == failed["seed"]
        calls.append((purpose, seed))
        recovered = episode(harness, identity="ep-recovered", purpose="recovery")
        lab.store.save_episode(recovered)
        return recovered

    def evolution(**kwargs):
        assert kwargs["trigger_episode"]["episode_id"] == "ep-recovered"
        assert kwargs["trigger_episode"]["infra_error"] is None
        calls.append(("evolve", kwargs["trigger_episode"]["seed"]))
        return {"candidate": deepcopy(BASE), "proposals": [], "gepa": {"fixture": True}}

    monkeypatch.setattr(lab, "play", recovered_play)
    monkeypatch.setattr("game_harness.evolution.evolve_harness", evolution)
    lab.optimize(job, object(), object())
    saved = lab.store.get("job", job["id"])
    assert calls == [("recovery", 101), ("evolve", 101)]
    assert saved["trigger_episode_id"] == "ep-recovered" and saved["status"] == "retained"
    assert lab.store.load_episode(failed["episode_id"])["infra_error"] is not None


def test_unapproved_candidate_cannot_be_activated_by_rollback(lab):
    baseline = seed_active(lab)
    child = lab.version(CHILD, lab.scope, {"type": "gepa_candidate"}, baseline["id"])
    with pytest.raises(ValueError, match="未通过发布"):
        lab.rollback(child["id"])
    assert lab.store.active(lab.scope)["id"] == baseline["id"]
    listed = {v["id"]: v for v in lab.state()["versions"]}
    assert listed[child["id"]]["can_rollback"] is False
    assert listed[baseline["id"]]["can_rollback"] is True


def test_resume_after_rollback_replays_trigger_with_current_baseline(lab, monkeypatch):
    bootstrap = seed_active(lab)
    previous = lab.version(CHILD, lab.scope, {"type": "gepa"}, bootstrap["id"])
    lab.store.activate(lab.scope, previous["id"], bootstrap["id"], publication={
        "id": "job-earlier-approval", "created_at": "2026-10-17T00:00:00Z", "status": "promoted",
        "scope": lab.scope, "candidate_id": previous["id"]})
    original = episode(previous, identity="ep-before-rollback")
    lab.store.save_episode(original)
    job = lab.new_job(original, dict(DEFAULTS), lab.scope)
    job.update(status="interrupted")
    lab.store.put("job", job)
    lab.rollback(bootstrap["id"])
    calls = []

    def recovered_play(provider, harness, config, seed, purpose, job_id):
        assert harness["id"] == bootstrap["id"]
        assert seed == original["seed"] and purpose == "recovery" and job_id == job["id"]
        calls.append("recovery")
        result = episode(harness, identity="ep-after-rollback-recovery", purpose=purpose)
        result["job_id"] = job_id
        lab.store.save_episode(result)
        return result

    def evolution(**kwargs):
        assert kwargs["candidate"] == bootstrap["candidate"]
        assert kwargs["trigger_episode"]["harness_id"] == bootstrap["id"]
        assert kwargs["trigger_episode"]["episode_id"] == "ep-after-rollback-recovery"
        calls.append("evolve")
        return {"candidate": deepcopy(BASE), "proposals": [], "gepa": {"fixture": True}}

    monkeypatch.setattr(lab, "play", recovered_play)
    monkeypatch.setattr("game_harness.evolution.evolve_harness", evolution)
    lab.optimize(job, object(), object())
    saved = lab.store.get("job", job["id"])
    assert calls == ["recovery", "evolve"]
    assert saved["status"] == "retained" and saved["parent_id"] == bootstrap["id"]
    assert saved["episode_id"] == original["episode_id"]
    assert saved["trigger_episode_id"] == "ep-after-rollback-recovery"
    assert saved["train_seeds"] == job["train_seeds"]
    assert lab.store.load_episode(original["episode_id"]) == original
    assert lab.store.load_episode(saved["trigger_episode_id"])["job_id"] == job["id"]
    assert lab.store.get("job", "job-" + saved["trigger_episode_id"]) is None
    event = next(row for row in lab.store.list("event") if row.get("stage") == "recovery")
    assert event["previous_harness_id"] == previous["id"]
    assert event["baseline_harness_id"] == bootstrap["id"]


def test_activation_rejects_stale_parent_and_cross_scope(lab):
    baseline = seed_active(lab)
    child = lab.version(CHILD, lab.scope, {"type": "gepa_candidate"}, baseline["id"])
    foreign = lab.version(CHILD, "other-scope", {"type": "bootstrap"})
    with pytest.raises(RuntimeError, match="过期"):
        lab.store.activate(lab.scope, child["id"], "wrong-parent")
    with pytest.raises(RuntimeError, match="当前游戏"):
        lab.store.activate(lab.scope, foreign["id"], baseline["id"])
    assert lab.store.active(lab.scope)["id"] == baseline["id"]


def test_publication_transaction_rolls_back_pointer_and_evidence_on_json_error(lab):
    baseline = seed_active(lab)
    child = lab.version(CHILD, lab.scope, {"type": "gepa_candidate"}, baseline["id"])
    original = lab.store.put("job", {"id": "job-publication", "status": "evaluating", "scope": lab.scope})
    publication = {**original, "status": "promoted", "candidate_id": child["id"], "bad": object()}
    with pytest.raises(TypeError):
        lab.store.activate(lab.scope, child["id"], baseline["id"], publication=publication)
    assert lab.store.active(lab.scope)["id"] == baseline["id"]
    assert lab.store.get("job", original["id"]) == original
    publication.pop("bad")
    lab.store.activate(lab.scope, child["id"], baseline["id"], publication=publication)
    assert lab.store.active(lab.scope)["id"] == child["id"]
    assert lab.store.get("job", original["id"])["status"] == "promoted"
    assert lab.rollback(baseline["id"]) == {"ok": True}
    assert lab.rollback(child["id"]) == {"ok": True}


def test_episode_path_traversal_is_rejected_without_writing_outside_store(lab):
    version = seed_active(lab)
    with pytest.raises(ValueError, match="episode id"):
        lab.store.save_episode(episode(version, identity="../escape"))
    assert lab.store.load_episode("../escape") is None


def test_importing_app_module_does_not_load_configuration_or_open_store(monkeypatch):
    module = importlib.import_module("game_harness.app")
    monkeypatch.setattr(Settings, "load", classmethod(lambda cls: pytest.fail("import must not initialize a lab")))
    importlib.reload(module)


def test_api_start_pause_resume_stop_and_state_never_leak_key(tmp_path, settings, monkeypatch):
    app = create_app(tmp_path, settings)
    lab = app.state.lab
    entered = threading.Event()

    def controlled_worker(config, scope):
        entered.set()
        try:
            assert lab.stop_event.wait(3), "API test did not stop its worker"
        finally:
            with lab.lock:
                lab.busy = False
                lab.paused = False

    monkeypatch.setattr(lab, "_run", controlled_worker)
    try:
        with TestClient(app, base_url="http://127.0.0.1") as client:
            assert client.post("/api/pause").status_code == 400
            assert client.post("/api/run", json={"board_size": True}).status_code == 400
            assert client.post("/api/run", json={"unknown": 1}).status_code == 400
            assert client.post("/api/run", json={"episodes": 1}, headers={"Origin": "http://127.0.0.1"}).status_code == 200
            assert entered.wait(1)
            assert client.post("/api/run", json={}).status_code == 400
            assert client.post("/api/pause").status_code == 200
            assert client.get("/api/state").json()["paused"] is True
            assert client.post("/api/resume").status_code == 200
            state_response = client.get("/api/state")
            assert state_response.json()["paused"] is False
            assert SECRET not in state_response.text
            assert state_response.headers["cache-control"] == "no-store"
            assert client.post("/api/stop").status_code == 200
            lab.worker.join(timeout=1)
            assert not lab.worker.is_alive() and not lab.busy
    finally:
        lab.stop_event.set()
        if lab.worker is not None:
            lab.worker.join(timeout=2)
        lab.store.db.close()


@pytest.mark.parametrize("origin", ["https://evil.invalid", "http://127.0.0.1:9999", "null"])
def test_cross_site_mutations_are_rejected_before_reaching_lab(tmp_path, settings, origin):
    app = create_app(tmp_path, settings)
    try:
        with TestClient(app, base_url="http://127.0.0.1") as client:
            response = client.post("/api/stop", headers={"Origin": origin})
            assert response.status_code == 403
            assert not app.state.lab.stop_event.is_set()
            assert app.state.lab.store.list("event") == []
    finally:
        app.state.lab.store.db.close()


def test_nonlocal_host_is_rejected_and_missing_episode_returns_404(tmp_path, settings):
    app = create_app(tmp_path, settings)
    try:
        with TestClient(app, base_url="http://127.0.0.1") as client:
            assert client.get("/api/state", headers={"Host": "evil.invalid"}).status_code == 403
            assert client.get("/api/episodes/does-not-exist").status_code == 404
    finally:
        app.state.lab.store.db.close()


def test_initialization_repairs_schema_once_then_game_calls_only_decision(settings):
    calls = {"coach": [], "decision": []}
    invalid = {"brief": {"background": "incomplete"}, "candidate": {
        "decision_instructions": "choose an action", "strategy_notes": ["wrong type"]}}
    valid = {"brief": {key: "fixture " + key for key in (
        "background", "goal", "rules", "precautions", "strategy")}, "candidate": BASE}

    def coach_http(request):
        calls["coach"].append(json.loads(request.content))
        content = invalid if len(calls["coach"]) == 1 else valid
        return httpx.Response(200, json={"model": settings.coach_model,
            "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(content)}}]})

    def decision_http(request):
        calls["decision"].append(json.loads(request.content))
        return httpx.Response(200, json=response_body())

    coach = CoachProvider(settings, client=httpx.Client(transport=httpx.MockTransport(coach_http)))
    decision = DecisionProvider(settings, client=httpx.Client(transport=httpx.MockTransport(decision_http)))
    try:
        brief, candidate = coach.initialize(GAME_RULES, size=8, max_steps=1)
        assert brief == valid["brief"] and candidate == BASE
        assert len(calls["coach"]) == 2 and calls["decision"] == []
        repair_messages = calls["coach"][1]["messages"]
        assert len(repair_messages) == 3
        assert json.loads(repair_messages[1]["content"]) == invalid
        assert repair_messages[2]["role"] == "user" and "结构校验失败" in repair_messages[2]["content"]
        assert all(call["response_format"] == {"type": "json_object"} for call in calls["coach"])
        assert all(call["thinking"] == {"type": "disabled"} for call in calls["coach"])

        result = run_episode(decision, {"id": "h-repaired", "candidate": candidate},
                             episode_id="ep-repaired", seed=0, size=8, max_steps=1)
        assert result["infra_error"] is None and result["steps"] == 1
        assert len(calls["coach"]) == 2  # No coach fallback or consultation during gameplay.
        assert len(calls["decision"]) == 1
        assert calls["decision"][0]["think_mode"] == "never"
        assert result["trace"][0]["receipt"]["think_mode"] == "never"
        assert result["thinking_verification"] == "reported_zero"
    finally:
        coach.close()
        decision.close()


def test_initialization_has_at_most_one_format_repair(settings):
    attempts = []

    def invalid_response(request):
        attempts.append(request)
        return httpx.Response(200, json={"model": settings.coach_model,
            "choices": [{"finish_reason": "stop", "message": {"content": '{"candidate": {}}'}}]})

    coach = CoachProvider(settings, client=httpx.Client(transport=httpx.MockTransport(invalid_response)))
    try:
        with pytest.raises(ProviderError):
            coach.initialize(GAME_RULES, size=8, max_steps=1)
        assert len(attempts) == 2
    finally:
        coach.close()


@pytest.mark.parametrize("transport_failure", [False, True])
def test_initialization_does_not_retry_transport_or_http_failure(settings, transport_failure):
    attempts = []

    def failure(request):
        attempts.append(request)
        if transport_failure:
            raise httpx.ConnectError("fixture connection failure", request=request)
        return httpx.Response(503, json={"error": "fixture outage"})

    coach = CoachProvider(settings, client=httpx.Client(transport=httpx.MockTransport(failure)))
    try:
        with pytest.raises(ProviderError):
            coach.initialize(GAME_RULES, size=8, max_steps=1)
        assert len(attempts) == 1
    finally:
        coach.close()


def test_restart_indexes_episode_file_then_enqueues_its_missing_job(tmp_path, settings):
    first = Lab(settings, tmp_path)
    version = seed_active(first)
    completed = episode(version, identity="ep-file-only")
    path = tmp_path / "episodes" / "ep-file-only.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(completed, ensure_ascii=False)
    path.write_text(serialized, encoding="utf-8")
    assert first.store.get("episode", completed["episode_id"]) is None
    assert first.store.list("job") == []
    first.store.db.close()

    restored = Lab(settings, tmp_path)
    try:
        summary = restored.store.get("episode", completed["episode_id"])
        job = restored.store.get("job", "job-" + completed["episode_id"])
        assert summary["score"] == completed["score"]
        assert summary["harness_id"] == version["id"]
        assert summary["latency_mean_ms"] == 3
        assert job["status"] == "pending" and job["episode_id"] == completed["episode_id"]
        assert job["train_seeds"][0] == completed["seed"]
        assert path.read_text(encoding="utf-8") == serialized
        assert restored.store.recover_episode_indexes() == 0
        assert len(restored.store.list("job")) == 1
    finally:
        restored.store.db.close()


def test_completion_log_failure_does_not_downgrade_committed_promotion(lab, monkeypatch):
    baseline = seed_active(lab)
    completed = episode(baseline)
    lab.store.save_episode(completed)
    job = lab.new_job(completed, dict(DEFAULTS), lab.scope)

    def evolution(**kwargs):
        return {"candidate": deepcopy(CHILD), "proposals": [], "gepa": {"fixture": True}}

    def promotion_game(provider, harness, config, seed, purpose, job_id):
        assert purpose in {"promotion_baseline", "promotion_candidate"}
        result = episode(harness, identity=f"ep-{purpose}-{seed}", purpose=purpose)
        result.update(seed=seed, score=2 if purpose == "promotion_candidate" else 1)
        lab.store.save_episode(result)
        return result

    original_log = lab.log

    def broken_completion_log(message, stage=None, **extra):
        if stage == "optimization_complete":
            raise OSError("fixture event log failure after publication")
        return original_log(message, stage, **extra)

    monkeypatch.setattr("game_harness.evolution.evolve_harness", evolution)
    monkeypatch.setattr(lab, "play", promotion_game)
    monkeypatch.setattr(lab, "log", broken_completion_log)
    with pytest.raises(OSError, match="after publication"):
        lab.optimize(job, object(), object())
    published = lab.store.get("job", job["id"])
    active = lab.store.active(lab.scope)
    assert published["status"] == "promoted" and published["error"] is None
    assert published["finished_at"] and len(published["pairs"]) == 4
    assert published["candidate_id"] == active["id"] and active["id"] != baseline["id"]
    assert active["candidate"] == CHILD
    monkeypatch.setattr(lab, "log", original_log)
    assert lab.rollback(baseline["id"]) == {"ok": True}
    assert lab.rollback(active["id"]) == {"ok": True}
