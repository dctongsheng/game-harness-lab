"""Multi-game integration contracts, using isolated fixture stores only.

No real model, GitHub, or Node call is needed here. Rules themselves are tested
in test_xiangqi.py; GEPA itself is tested separately in test_evolution.py.
"""
from copy import deepcopy

import httpx
import pytest
from fastapi.testclient import TestClient

from game_harness.app import create_app
from game_harness.games import get_game, register_game
from game_harness.providers import Settings, digest
from game_harness.runtime import run_episode
from game_harness.service import DEFAULTS, Lab
from game_harness.store import utcnow


BASE = {"decision_instructions": "Fixture baseline selects the action.", "strategy_notes": "Fixture baseline notes."}
CHILD = {"decision_instructions": "Fixture changed strategy selects the action.", "strategy_notes": "Fixture changed notes."}
BRIEF = {key: "Fixture " + key for key in ("background", "goal", "rules", "precautions", "strategy")}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def reject(*args, **kwargs):
        pytest.fail("Framework tests must not make network requests")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", reject)


@pytest.fixture
def settings():
    return Settings("http://127.0.0.1:1/v1/systemone", "fixture-decision", 1,
                    "https://fixture.invalid/v1", "fixture-coach", "fixture-secret-no-network", 1)


@pytest.fixture
def lab(tmp_path, settings):
    value = Lab(settings, tmp_path)
    yield value
    if value.worker:
        value.worker.join(timeout=2)
    value.store.db.close()


def no_model_worker(lab, monkeypatch):
    captured = []

    def worker(config, scope):
        captured.append((deepcopy(config), scope))
        with lab.lock:
            lab.busy = False
            lab.status = "fixture_complete"

    monkeypatch.setattr(lab, "_run", worker)
    return captured


def start_and_join(lab, config):
    lab.start(config)
    lab.worker.join(timeout=2)
    assert not lab.worker.is_alive()


def active_version(lab, game="snake", scope=None, config=None):
    lab.config = {**DEFAULTS, **get_game(game).defaults, **(config or {}), "game_id": game}
    lab.scope = scope or "fixture-" + game
    version = lab.version(BASE, lab.scope, {"type": "bootstrap"}, brief=deepcopy(BRIEF),
                          game_context=get_game(game).context(lab.config))
    lab.store.activate(lab.scope, version["id"], None)
    return version


def saved_episode(lab, version, identity, *, seed=12, purpose="play", score=1, config=None):
    config = deepcopy(config or lab.config)
    result = {"episode_id": identity, "seed": seed, "harness_id": version["id"],
              "scope": version["scope"], "config": config, "game_id": config["game_id"],
              "purpose": purpose, "score": score, "steps": config["max_steps"],
              "termination_reason": "step_limit", "infra_error": None,
              "model": "fixture-decision", "trace": [], "latencies_ms": []}
    lab.store.save_episode(result)
    return result


def test_game_switching_restores_adapter_defaults_and_scopes_are_isolated(lab, monkeypatch):
    calls = no_model_worker(lab, monkeypatch)
    start_and_join(lab, {"game_id": "snake"})
    snake_scope = lab.scope
    snake = lab.version(BASE, snake_scope, {"type": "bootstrap"})
    lab.store.activate(snake_scope, snake["id"], None)
    assert lab.config["max_steps"] == 120

    start_and_join(lab, {"game_id": "xiangqi"})
    xiangqi_scope = lab.scope
    chess = lab.version(BASE, xiangqi_scope, {"type": "bootstrap"})
    lab.store.activate(xiangqi_scope, chess["id"], None)
    assert lab.config["max_steps"] == 80
    assert snake_scope != xiangqi_scope and snake["id"] != chess["id"]
    assert lab.state()["active_harness"]["game_id"] == "xiangqi"
    with pytest.raises(ValueError, match="当前游戏"):
        lab.rollback(snake["id"])

    start_and_join(lab, {"game_id": "snake"})
    assert lab.config["max_steps"] == 120 and lab.scope == snake_scope
    assert lab.state()["active_harness"]["id"] == snake["id"]
    assert lab.store.active(xiangqi_scope)["id"] == chess["id"]
    assert [item[0]["game_id"] for item in calls] == ["snake", "xiangqi", "snake"]


def test_scope_ignores_irrelevant_snake_board_size_for_chess_but_tracks_budget(lab, monkeypatch):
    no_model_worker(lab, monkeypatch)
    start_and_join(lab, {"game_id": "xiangqi", "board_size": 8, "max_steps": 80})
    first_scope = lab.scope
    start_and_join(lab, {"game_id": "xiangqi", "board_size": 12, "train_games": 8})
    assert lab.scope == first_scope
    start_and_join(lab, {"max_steps": 100})
    assert lab.scope != first_scope


class FixtureSession:
    def __init__(self):
        self.steps = 0

    def snapshot(self):
        return {"game_id": "fixture_registered", "step": self.steps, "score": self.steps,
                "done": self.steps == 1, "reason": "fixture_complete" if self.steps else None,
                "policy_role": "candidate"}

    def candidates(self):
        return [] if self.steps else [{"id": "advance", "label": "One fixture step"}]

    def step(self, action):
        assert action == "advance" and self.steps == 0
        self.steps += 1
        return self.snapshot()

    def close(self):
        pass


class FixtureAdapter:
    id = "fixture_registered"
    name = "Registered fixture game"
    rules_version = "fixture-v1"
    rules = "Choose advance once. One completed step scores one point."
    score_spec = {"min": 0, "max": 1, "direction": "maximize", "minimum_mean_gain": 1}
    defaults = {"max_steps": 12, "custom_depth": 3}
    opponent_policy = None

    def validate_config(self, config):
        if type(config["custom_depth"]) is not int or not 1 <= config["custom_depth"] <= 9:
            raise ValueError("custom_depth must be 1..9")

    def scope_config(self, config):
        return {"custom_depth": config["custom_depth"], "max_steps": config["max_steps"]}

    def context(self, config):
        return {"game_id": self.id, "name": self.name, "rules_version": self.rules_version,
                "rules": self.rules, "score_spec": self.score_spec,
                "config": self.scope_config(config), "opponent_policy": None}

    def create(self, seed, config):
        self.validate_config(config)
        return FixtureSession()


class FixtureDecision:
    def decide(self, state, instructions, criteria, request_id):
        assert "Choose advance once" in instructions
        return {"request_id": request_id, "observation_revision": state["step"],
                "candidate_hash": digest(criteria), "choice_id": "advance",
                "model": "fixture-decision", "elapsed_ms": 1,
                "raw_response": {}, "think_mode": "never", "think_used": False,
                "reasoning_tokens": 0}


def test_registered_adapter_custom_settings_work_through_api_and_shared_runtime(tmp_path, settings, monkeypatch):
    from game_harness import games
    monkeypatch.setattr(games, "_REGISTRY", dict(games._REGISTRY))
    adapter = FixtureAdapter()
    register_game(adapter)
    app = create_app(tmp_path, settings)
    lab = app.state.lab
    no_model_worker(lab, monkeypatch)
    try:
        with TestClient(app) as client:
            catalog = client.get("/api/games").json()["games"]
            assert any(row["id"] == adapter.id for row in catalog)
            response = client.post("/api/run", json={"game_id": adapter.id, "custom_depth": 7})
            assert response.status_code == 200
            lab.worker.join(timeout=2)
            assert lab.config["custom_depth"] == 7 and lab.config["max_steps"] == 12
            original_scope = lab.scope
            assert client.post("/api/run", json={"custom_depth": 0}).status_code == 400
            assert lab.scope == original_scope
            assert client.post("/api/run", json={"game_id": "snake"}).status_code == 200
            lab.worker.join(timeout=2)
            assert "custom_depth" not in lab.config
            assert client.post("/api/run", json={"game_id": "snake", "custom_depth": 7}).status_code == 400

        result = run_episode(FixtureDecision(), {"id": "h-fixture", "candidate": BASE},
                             episode_id="ep-registered", seed=1, adapter=adapter,
                             config={"custom_depth": 7, "max_steps": 12})
        assert result["game_id"] == adapter.id and result["score"] == 1
        assert result["steps"] == 1 and result["infra_error"] is None
        assert result["thinking_verification"] == "reported_zero"
    finally:
        lab.store.db.close()


@pytest.mark.parametrize("seed", [10, 11])
def test_xiangqi_job_splits_are_disjoint_color_stratified_and_idempotent(lab, seed):
    config = {"max_steps": 80, "train_games": 4, "validation_games": 4, "promotion_games": 4}
    version = active_version(lab, "xiangqi", config=config)
    trigger = saved_episode(lab, version, "ep-chess-trigger", seed=seed)
    job = lab.new_job(trigger, lab.config, lab.scope)
    train, validation, promotion = (job[key] for key in ("train_seeds", "validation_seeds", "promotion_seeds"))
    assert train[0] == seed
    for values in (train, validation, promotion):
        assert len(values) == len(set(values)) == 4
        assert sorted(value % 2 for value in values) == [0, 0, 1, 1]
    assert not set(train) & set(validation)
    assert not set(train) & set(promotion)
    assert not set(validation) & set(promotion)
    assert lab.new_job(trigger, lab.config, lab.scope) == job


def test_promotion_automatically_exports_complete_bundle_readable_by_api(tmp_path, settings, monkeypatch):
    app = create_app(tmp_path, settings)
    lab = app.state.lab
    baseline = active_version(lab, config={"max_steps": 8, "train_games": 1, "validation_games": 1})
    trigger = saved_episode(lab, baseline, "ep-trigger")
    job = lab.new_job(trigger, lab.config, lab.scope)

    def evolve(**kwargs):
        assert kwargs["game_context"]["game_id"] == "snake"
        return {"candidate": deepcopy(CHILD), "proposals": [],
                "gepa": {"engine": "fixture", "version": "fixture", "candidate_count": 2}}

    def play(provider, harness, config, seed, purpose, job_id):
        score = 1 if purpose == "promotion_baseline" else 2
        return saved_episode(lab, harness, f"ep-{purpose}-{seed}", seed=seed,
                             purpose=purpose, score=score, config=config)

    monkeypatch.setattr("game_harness.evolution.evolve_harness", evolve)
    monkeypatch.setattr(lab, "play", play)
    try:
        lab.optimize(job, object(), object())
        saved = lab.store.get("job", job["id"])
        assert saved["status"] == "promoted"
        version_id = saved["candidate_id"]
        assert lab.store.active(lab.scope)["id"] == version_id
        export = lab.store.get("export", version_id)
        assert export["game_id"] == "snake" and export["status"] == "exported"
        with TestClient(app) as client:
            response = client.get(f"/api/harnesses/snake/{version_id}")
            assert response.status_code == 200
            bundle = response.json()
            assert bundle["strategy"] == CHILD
            assert bundle["game"]["brief"] == BRIEF
            assert bundle["evaluation"]["status"] == "promoted"
            assert len(bundle["evaluation"]["pairs"]) == 4
            assert bundle["manifest"]["parent_id"] == baseline["id"]
            assert client.get(f"/api/harnesses/xiangqi/{version_id}").status_code == 404
            assert client.get(f"/api/harnesses/snake/{baseline['id']}").status_code == 404
    finally:
        lab.store.db.close()


def legacy_publication(lab, *, status="promoted"):
    baseline = active_version(lab, scope="legacy-snake-scope")
    candidate = lab.version(CHILD, lab.scope, {"type": "gepa"}, baseline["id"])
    # Old files did not carry game_id/game_context; inheritance must survive.
    for version in (baseline, candidate):
        version.pop("game_id")
        version.pop("game_context")
        if version is candidate:
            version.pop("brief")
        lab.store.put("version", version)
    config = {key: value for key, value in lab.config.items() if key != "game_id"}
    job = {"id": "job-legacy", "status": status, "scope": lab.scope,
           "parent_id": baseline["id"], "candidate_id": candidate["id"],
           "config": config, "created_at": utcnow(), "finished_at": utcnow(),
           "baseline_score": 1, "candidate_score": 2, "mean_gain": 1,
           "pairs": [{"seed": n, "baseline": 1, "candidate": 2,
                      "baseline_episode_id": f"ep-old-{n}", "candidate_episode_id": f"ep-new-{n}"}
                     for n in range(4)]}
    lab.store.put("job", job)
    if status == "promoted":
        lab.store.activate(lab.scope, candidate["id"], baseline["id"], publication=job)
    return candidate, job


def test_restart_backfills_legacy_approved_export_and_is_idempotent(tmp_path, settings):
    first = Lab(settings, tmp_path)
    candidate, _ = legacy_publication(first)
    assert first.store.list("export") == []
    first.store.db.close()

    restored = Lab(settings, tmp_path)
    record = restored.store.get("export", candidate["id"])
    assert record["game_id"] == "snake" and record["status"] == "exported"
    manifest_path = restored.export_dir / "snake" / candidate["id"] / "manifest.json"
    original_bytes = manifest_path.read_bytes()
    restored.reconcile_exports()
    assert manifest_path.read_bytes() == original_bytes
    assert len(restored.store.list("export")) == 1
    restored.store.db.close()

    app = create_app(tmp_path, settings)
    try:
        with TestClient(app) as client:
            bundle = client.get(f"/api/harnesses/snake/{candidate['id']}").json()
            assert bundle["game"]["brief"] == BRIEF
            assert bundle["game"]["context"]["game_id"] == "snake"
            assert bundle["manifest"]["scope"] == "legacy-snake-scope"
    finally:
        app.state.lab.store.db.close()


@pytest.mark.parametrize("status", ["retained", "failed", "interrupted", "pending"])
def test_restart_and_explicit_export_never_publish_unapproved_candidates(tmp_path, settings, status):
    first = Lab(settings, tmp_path)
    candidate, job = legacy_publication(first, status=status)
    with pytest.raises(ValueError, match="approved"):
        first.export_version(job)
    first.store.db.close()
    restored = Lab(settings, tmp_path)
    try:
        assert restored.store.list("export") == []
        assert not (restored.export_dir / "snake" / candidate["id"]).exists()
    finally:
        restored.store.db.close()
